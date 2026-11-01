from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class RecordedEvent(BaseModel):
    seq: int
    conn: int
    recv_ns: int
    recv_at: float
    type: str
    event_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    item_type: str | None = None
    raw: dict[str, Any]
    synthetic: bool = False

