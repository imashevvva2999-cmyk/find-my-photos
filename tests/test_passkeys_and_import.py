"""Organiser sign-in without a password (passkeys), the admin area staying closed, and the
photo-file import that is off unless a token is configured.

A small software authenticator (a P-256 key, like Face ID / Touch ID keep in their secure
chip) produces the same answers a browser would, so the real signature checks are tested."""
import base64
import hashlib
import io
import json
import os
import tarfile
from dataclasses import replace

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from conftest import ORIGIN, create_event
from app import importer, main

RP_ID = "testserver"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftAuthenticator:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(32)
        self.count = 0

    @staticmethod
    def _client_data(kind, challenge, origin):
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin}).encode()

    def create(self, options, origin="http://testserver"):
        nums = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        flags = bytes([0x45])  # user present + user verified + credential data included
        auth_data = (hashlib.sha256(options["rp"]["id"].encode()).digest() + flags + (0).to_bytes(4, "big")
                     + bytes(16) + len(self.cred_id).to_bytes(2, "big") + self.cred_id + cose)
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64(self._client_data("webauthn.create", options["challenge"], origin)),
                             "attestationObject": b64(att)}}

    def get(self, options, rp_id=RP_ID, origin="http://testserver"):
        self.count += 1
        auth_data = hashlib.sha256(rp_id.encode()).digest() + bytes([0x05]) + self.count.to_bytes(4, "big")
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        sig = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64(client_data), "authenticatorData": b64(auth_data),
                             "signature": b64(sig)}}


def add_passkey(admin, device, name="MacBook"):
    options = admin.post("/admin/api/passkeys/options").json()
    assert options["authenticatorSelection"]["residentKey"] == "required"
    assert options["authenticatorSelection"]["userVerification"] == "required"
    return admin.post("/admin/api/passkeys", json={"credential": device.create(options), "name": name})


def passkey_login(client, device, **kw):
    options = client.post("/admin/passkey/login/options").json()
    return client.post("/admin/passkey/login/verify", json=device.get(options, **kw))


def test_admin_area_is_closed_without_signing_in(client):
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/admin/login"
    assert client.post("/admin/events", data={"name": "x"}).status_code == 401
    assert client.post("/admin/api/passkeys/options").status_code == 401   # adding a passkey needs a session
    assert client.post("/admin/api/passkeys", json={}).status_code == 401
    page = client.get("/admin/login").text
    assert 'id="passkey-login"' in page and "Войти по паролю" in page


def test_open_admin_setting_no_longer_exists(client):
    assert not hasattr(main.settings, "admin_open")          # the old ADMIN_OPEN switch is gone
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_passkey_sign_in_without_password(admin, app):
    device = SoftAuthenticator()
    assert add_passkey(admin, device).json() == {"ok": True}
    assert "MacBook" in admin.get("/admin").text

    fresh = TestClient(app, headers=ORIGIN)                 # another browser, no session
    r = passkey_login(fresh, device)
    assert r.status_code == 200 and r.json()["next"] == "/admin"
    assert fresh.get("/admin", follow_redirects=False).status_code == 200  # signed in, nothing typed
    event_id, _ = create_event(fresh, "Технокадр")
    assert event_id


def test_passkey_sign_in_is_refused_when_not_genuine(admin, app):
    device = SoftAuthenticator()
    add_passkey(admin, device)
    stranger = TestClient(app, headers=ORIGIN)
    assert passkey_login(stranger, SoftAuthenticator()).status_code == 401          # unknown passkey
    assert passkey_login(stranger, device, rp_id="evil.example").status_code == 401  # made for another site
    assert passkey_login(stranger, device, origin="https://evil.example").status_code == 401
    forged = device.get(stranger.post("/admin/passkey/login/options").json())
    forged["response"]["signature"] = b64(unb64(forged["response"]["signature"])[:-2] + b"\0\0")
    assert stranger.post("/admin/passkey/login/verify", json=forged).status_code == 401
    # an answer is valid once: replaying it (even with its own session cookie) is refused
    replayer = TestClient(app, headers=ORIGIN)
    options = replayer.post("/admin/passkey/login/options").json()
    cookie = dict(replayer.cookies)
    answer = device.get(options)
    assert replayer.post("/admin/passkey/login/verify", json=answer).status_code == 200
    again = TestClient(app, headers=ORIGIN, cookies=cookie)
    assert again.post("/admin/passkey/login/verify", json=answer).status_code == 401
    assert TestClient(app, headers=ORIGIN).get("/admin", follow_redirects=False).status_code == 303


def test_passkey_sign_in_is_rate_limited(app):
    c = TestClient(app, headers=ORIGIN)
    codes = [passkey_login(c, SoftAuthenticator()).status_code for _ in range(main.settings.login_failures_per_15_min + 1)]
    assert codes[-1] == 429 and set(codes[:-1]) == {401}


def test_removed_passkey_no_longer_signs_in(admin, app):
    device = SoftAuthenticator()
    add_passkey(admin, device)
    passkey_id = int(admin.get("/admin").text.split('action="/admin/passkeys/')[1].split("/")[0])
    admin.post(f"/admin/passkeys/{passkey_id}/delete")
    assert passkey_login(TestClient(app, headers=ORIGIN), device).status_code == 401


# ---- photo-file import

def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_import_is_off_without_a_token(client):
    assert client.post(importer.PATH, content=_tar({"events/7/thumbs/1.jpg": b"x"}),
                       headers={"Authorization": "Bearer anything"}).status_code == 404
    assert client.get(importer.PATH, params={"event": 7}).status_code == 404


def test_import_writes_only_event_photo_files(client, monkeypatch):
    token = "t" * 40
    monkeypatch.setattr(importer, "settings",
                        replace(importer.settings, import_token_sha256=hashlib.sha256(token.encode()).hexdigest()))
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post(importer.PATH, content=b"", headers={"Authorization": "Bearer wrong"}).status_code == 404
    archive = _tar({"events/7/thumbs/1.jpg": b"thumb", "events/7/originals/ab12.jpg": b"orig",
                    "../escape.jpg": b"no", "events/7/../../etc.jpg": b"no", "secrets/.env": b"no"})
    r = client.post(importer.PATH, content=archive, headers=auth)
    assert r.status_code == 200 and r.json()["written"] == 2 and len(r.json()["refused"]) == 3, r.json()
    assert client.post(importer.PATH, content=archive, headers=auth).json()["skipped"] == 2   # safe to resend
    assert client.get(importer.PATH, params={"event": 7}, headers=auth).json() == {
        "events/7/thumbs/1.jpg": 5, "events/7/originals/ab12.jpg": 4}
    assert not (importer.settings.data_dir.parent / "escape.jpg").exists()
