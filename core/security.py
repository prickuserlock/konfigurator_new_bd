import os
import re
import hashlib
import hmac

# === Пароли (PBKDF2) ===
_PBKDF2_ITERATIONS = int(os.getenv("PBKDF2_ITERATIONS", "200000"))


def hash_password(p: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", p.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(p: str, stored: str) -> bool:
    # legacy: старый sha256(пароль) из прошлой версии
    if stored and re.fullmatch(r"[0-9a-f]{64}", stored):
        legacy = hashlib.sha256(p.encode("utf-8")).hexdigest()
        return hmac.compare_digest(legacy, stored)

    try:
        algo, it_s, salt_hex, hash_hex = (stored or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        it = int(it_s)
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", p.encode("utf-8"), salt, it)
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False
