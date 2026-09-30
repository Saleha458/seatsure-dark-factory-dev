"""Portable salted scrypt credentials with bounded memory use."""
import hashlib
import hmac
import secrets
from threading import BoundedSemaphore

HASH_WORKERS = BoundedSemaphore(2)
PARAMETERS = {"n": 16384, "r": 8, "p": 1, "dklen": 32}


def derive(password, salt):
    with HASH_WORKERS:
        # JSON can represent isolated surrogate code points. Encode them consistently
        # rather than crashing for an otherwise accepted password string.
        return hashlib.scrypt(password.encode("utf-8", errors="surrogatepass"),
                              salt=bytes.fromhex(salt), **PARAMETERS).hex()


def hash_password(password):
    salt = secrets.token_hex(16)
    return {"algorithm": "scrypt", **PARAMETERS, "salt": salt, "digest": derive(password, salt)}


def verify_password(password, credential):
    return hmac.compare_digest(derive(password, credential["salt"]), credential["digest"])
