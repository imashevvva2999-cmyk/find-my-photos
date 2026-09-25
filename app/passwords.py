"""Admin password hashing with scrypt (Python standard library, memory-hard).

Stored format: scrypt$<n>$<r>$<p>$<salt hex>$<hash hex>
"""
import hashlib
import hmac
import secrets

N, R, P = 2**15, 8, 1
_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, maxmem=_MAXMEM, dklen=32)
    return f"scrypt${N}${R}${P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r),
                                p=int(p), maxmem=_MAXMEM, dklen=len(hash_hex) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def password_version(stored: str) -> str:
    """Short fingerprint of the stored hash. Sessions carry it, so changing the password
    logs out every existing session."""
    return hashlib.sha256(stored.encode()).hexdigest()[:16]
