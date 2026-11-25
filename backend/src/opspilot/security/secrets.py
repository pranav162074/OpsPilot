"""Encrypting integration tokens at rest.

A database dump or a read-only SQL bug must not hand out a team's Grafana or GitHub
tokens, so they are stored encrypted with a key that lives only in the server's
environment (INTEGRATIONS_KEY), never in the database.
"""

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr


class SecretsUnavailable(Exception):
    """No INTEGRATIONS_KEY is configured, so tokens can't be stored or read."""


class SecretBox:
    def __init__(self, key: SecretStr | None) -> None:
        self._fernet = Fernet(key.get_secret_value().encode()) if key else None

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plaintext: str) -> str:
        if self._fernet is None:
            raise SecretsUnavailable("Set INTEGRATIONS_KEY to store integration tokens")
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        if self._fernet is None:
            raise SecretsUnavailable("INTEGRATIONS_KEY is not set, so stored tokens can't be read")
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            # The key was changed: the token has to be entered again
            raise SecretsUnavailable("A stored token can't be decrypted with this key") from exc
