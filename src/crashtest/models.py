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


class FileEntry(BaseModel):
    path: str
    size: int
    sha256: str
    mtime: float


class Survival(BaseModel):
    expected: list[str]
    present_after: list[str]
    lost: list[str]
    changed: list[str]
    never_created: list[str]
    unknown: list[str] = Field(default_factory=list)
    evidence_complete: bool = True


class CommandOutcome(BaseModel):
    item_id: str
    command: str
    status: str | None
    exit_code: int | None
    duration_ms: int | None
    seen_live: bool
    fault_window: bool
    exit_code_missing: bool = False
    evidence_complete: bool = False


class Metrics(BaseModel):
    reconnect_ms: float | None
    reconnect_basis: Literal["disconnected_event", "kill_timestamp", "none"]
    reconnect_censored: bool = False
    reconnect_censor_ms: float | None = None
    disconnect_event_seen: bool
    disconnect_detect_ms: float | None
    turn_status: str | None
    turn_error_code: str | None
    commands: list[CommandOutcome]
    contradiction: bool | None
    command_evidence_complete: bool = False
    missed_items: list[str]
    missed_items_count: int
    saved_items_not_seen_live: int
    intermediate_events_missed: int | None = None
    intermediate_events_reason: str = "not_reconstructable_from_saved_items"
    live_item_count: int
    saved_item_count: int
    survival: Survival
    required_actions: list[dict[str, Any]]
    session_final_status: str | None
    agent_retried_command: bool
    evidence_complete: bool = False
    inconclusive_reasons: list[str] = Field(default_factory=list)

