import uuid
from datetime import datetime

from opspilot.models import ApiKey, Role
from opspilot.security.api_keys import display_prefix, generate_api_key, hash_api_key


def new_api_key(
    workspace_id: uuid.UUID, name: str, role: Role, expires_at: datetime | None = None
) -> tuple[ApiKey, str]:
    """Returns the row to store and the plain key, which is shown once and never stored."""
    key = generate_api_key()
    row = ApiKey(
        workspace_id=workspace_id,
        name=name,
        role=role.value,
        prefix=display_prefix(key),
        key_hash=hash_api_key(key),
        expires_at=expires_at,
    )
    return row, key
