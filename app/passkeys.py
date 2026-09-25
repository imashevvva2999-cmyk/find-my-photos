"""Passwordless organiser sign-in with passkeys (WebAuthn).

- A passkey is added once, from the organiser area (so only someone already signed in can add
  one). After that the organiser signs in with the device's Face ID / Touch ID / Windows Hello
  or a security key: nothing to type, and a passkey cannot be phished or reused on another site.
- The server stores only the public key. Each challenge is random, valid for 2 minutes and
  usable once (kept in the signed session cookie and recorded in the database until used).
- A passkey belongs to one site address (rp_id): it only works on the address it was made on.
- The password login stays available as a fallback (e.g. a new computer without a passkey).
"""
import hashlib
import logging
import time

from webauthn import (base64url_to_bytes, generate_authentication_options, generate_registration_options,
                      options_to_json, verify_authentication_response, verify_registration_response)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

from . import db
from .config import settings

log = logging.getLogger("passkeys")

CHALLENGE_TTL = 120
RP_NAME = "Мои фото — организатор"


class PasskeyError(Exception):
    """Shown to the organiser as is (Russian)."""


def site(request) -> tuple[str, str]:
    """(rp_id, origin) of the address the organiser is using, e.g. findmyphotos-backend.onrender.com."""
    host = request.headers.get("host", "")
    return host.split(":")[0], f"{request.url.scheme}://{host}"


def _user_id() -> bytes:
    # One organiser account: the same user id on every device, so a device keeps one passkey.
    return hashlib.sha256(settings.derived_key("passkey-user").encode()).digest()[:16]


def _remember(session: dict, kind: str, challenge: bytes) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM passkey_challenges WHERE created_at < now() - make_interval(secs => %s)",
                     (CHALLENGE_TTL,))
        conn.execute("INSERT INTO passkey_challenges (challenge, kind) VALUES (%s, %s)", (challenge, kind))
    session["pk_challenge"] = {"kind": kind, "value": bytes_to_base64url(challenge), "at": time.time()}


def _take(session: dict, kind: str) -> bytes:
    """The pending challenge of this kind. It is removed from the session and from the database
    at once, so the same answer can never be accepted twice."""
    data = session.pop("pk_challenge", None)
    expired = PasskeyError("Время ожидания истекло. Попробуйте ещё раз.")
    if not data or data.get("kind") != kind or time.time() - data.get("at", 0) > CHALLENGE_TTL:
        raise expired
    challenge = base64url_to_bytes(data["value"])
    with db.connect() as conn:
        used = conn.execute("""DELETE FROM passkey_challenges WHERE challenge = %s AND kind = %s
                               AND created_at >= now() - make_interval(secs => %s) RETURNING 1""",
                            (challenge, kind, CHALLENGE_TTL)).fetchone()
    if used is None:
        raise expired
    return challenge


def list_passkeys() -> list[dict]:
    with db.connect() as conn:
        return conn.execute("SELECT id, name, rp_id, created_at, last_used_at FROM admin_passkeys ORDER BY id").fetchall()


# ---- adding a passkey (organiser already signed in)

def registration_options(request) -> str:
    rp_id, _ = site(request)
    with db.connect() as conn:
        existing = conn.execute("SELECT credential_id FROM admin_passkeys WHERE rp_id = %s", (rp_id,)).fetchall()
    options = generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME, user_id=_user_id(), user_name="Организатор", user_display_name="Организатор",
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,          # sign in without typing a user name
            user_verification=UserVerificationRequirement.REQUIRED),  # Face ID / fingerprint / device PIN
        exclude_credentials=[PublicKeyCredentialDescriptor(id=bytes(r["credential_id"])) for r in existing],
    )
    _remember(request.session, "register", options.challenge)
    return options_to_json(options)


def register(request, credential: dict, name: str) -> None:
    rp_id, origin = site(request)
    challenge = _take(request.session, "register")
    if not isinstance(credential, dict):
        raise PasskeyError("Не удалось добавить ключ доступа. Попробуйте ещё раз.")
    try:
        verified = verify_registration_response(credential=credential, expected_challenge=challenge,
                                                expected_rp_id=rp_id, expected_origin=origin,
                                                require_user_verification=True)
    except (InvalidRegistrationResponse, ValueError, KeyError, TypeError) as exc:
        log.warning("passkey registration refused: %s", type(exc).__name__)
        raise PasskeyError("Не удалось добавить ключ доступа. Попробуйте ещё раз.") from None
    name = (name or "").strip()[:60] or "Ключ доступа"
    with db.connect() as conn:
        conn.execute("""INSERT INTO admin_passkeys (credential_id, public_key, sign_count, rp_id, name)
                        VALUES (%s, %s, %s, %s, %s) ON CONFLICT (credential_id) DO NOTHING""",
                     (verified.credential_id, verified.credential_public_key, verified.sign_count, rp_id, name))
    log.info("passkey added")


def delete(passkey_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM admin_passkeys WHERE id = %s", (passkey_id,))
    log.info("passkey removed")


# ---- signing in with a passkey

def authentication_options(request) -> str:
    rp_id, _ = site(request)
    options = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
    _remember(request.session, "login", options.challenge)
    return options_to_json(options)


def authenticate(request, credential: dict) -> None:
    """Raises PasskeyError unless the response is a valid signature by a stored passkey."""
    rp_id, origin = site(request)
    challenge = _take(request.session, "login")
    try:
        raw_id = base64url_to_bytes(credential["rawId"])
    except (KeyError, TypeError, ValueError):
        raise PasskeyError("Не удалось войти с ключом доступа.") from None
    with db.connect() as conn:
        stored = conn.execute("SELECT * FROM admin_passkeys WHERE credential_id = %s AND rp_id = %s",
                              (raw_id, rp_id)).fetchone()
    if stored is None:
        log.warning("passkey sign-in refused: unknown passkey")
        raise PasskeyError("Этот ключ доступа не подходит для этого сайта. Войдите по паролю и добавьте новый ключ.")
    try:
        verified = verify_authentication_response(
            credential=credential, expected_challenge=challenge, expected_rp_id=rp_id, expected_origin=origin,
            credential_public_key=bytes(stored["public_key"]), credential_current_sign_count=stored["sign_count"],
            require_user_verification=True)
    except (InvalidAuthenticationResponse, ValueError, KeyError, TypeError) as exc:
        log.warning("passkey sign-in refused: %s", type(exc).__name__)
        raise PasskeyError("Не удалось войти с ключом доступа.") from None
    with db.connect() as conn:
        conn.execute("UPDATE admin_passkeys SET sign_count = %s, last_used_at = now() WHERE id = %s",
                     (verified.new_sign_count, stored["id"]))
