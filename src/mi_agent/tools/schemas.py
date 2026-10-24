from __future__ import annotations

from pydantic import BaseModel, Field


class ReadAppLogParams(BaseModel):
    lines: int = Field(default=200, ge=1, le=5000)


class SearchSQLiteDocsParams(BaseModel):
    query: str = Field(min_length=1, max_length=3000)
    k: int = Field(default=5, ge=1, le=10)


class RequestBackupRestoreParams(BaseModel):
    # An id from the target's backup catalog; sent as one URL path segment.
    backup_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class RestartServiceParams(BaseModel):
    service_name: str = Field(default="target-app", min_length=1, max_length=120)


class RunSQLParams(BaseModel):
    sql: str = Field(min_length=1, max_length=100_000)
    db_path: str = Field(default="data/app.db", min_length=1, max_length=300)


class ReplaceFileParams(BaseModel):
    source_path: str = Field(min_length=1, max_length=300)
    destination_path: str = Field(min_length=1, max_length=300)
