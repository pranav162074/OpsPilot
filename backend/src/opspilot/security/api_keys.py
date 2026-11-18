import hashlib
import secrets

KEY_PREFIX = "ops_"
DISPLAY_PREFIX_LENGTH = 12


def generate_api_key() -> str:
    # 32 random bytes = 256 bits: impossible to guess. The "ops_" start makes leaked keys
    # easy to spot, for example by GitHub's secret scanning.
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    # A fast hash is right here, unlike for passwords: the key is 256 random bits, so there is
    # nothing to brute-force, and every request has to look its key up quickly.
    return hashlib.sha256(key.encode()).hexdigest()


def display_prefix(key: str) -> str:
    return key[:DISPLAY_PREFIX_LENGTH]


def looks_like_api_key(value: str) -> bool:
    return value.startswith(KEY_PREFIX) and 20 <= len(value) <= 100
