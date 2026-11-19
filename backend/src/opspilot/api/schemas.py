import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from opspilot.models import DocumentStatus, Role, SourceType

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    created_at: datetime


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    role: Role
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class ApiKeyOut(BaseModel):
    """What anyone can see about a key. Never includes the key or its hash."""

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    role: Role
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    # Shown exactly once, in the response that creates the key
    key: str


class WorkspaceCreated(BaseModel):
    workspace: WorkspaceOut
    api_key: ApiKeyCreated


class Me(BaseModel):
    workspace: WorkspaceOut
    api_key: ApiKeyOut


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    filename: str
    source_type: SourceType
    media_type: str
    size_bytes: int
    status: DocumentStatus
    error: str | None
    chunk_count: int
    created_at: datetime
    updated_at: datetime
    processed_at: datetime | None


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    # Pass as ?cursor= to get the next page. null on the last page.
    next_cursor: str | None


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    ordinal: int
    heading: str
    content: str
    token_count: int
    char_start: int
    char_end: int
