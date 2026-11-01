from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from crashtest.models import CommandOutcome, Metrics, RecordedEvent, Survival


NOT_DONE = {"failed", "incomplete", "in_progress"}
CONNECTED = "agent.session.environment.connected"
DISCONNECTED = "agent.session.environment.disconnected"


def compute_survival(
    before_manifest: Iterable[dict[str, Any]],
    after_manifest: Iterable[dict[str, Any]],
    expected_paths: Iterable[str],
) -> Survival:
    """Compare expected pre-fault files using their SHA-256 values.

    `before_manifest` must be captured immediately before the fault. A path
    missing from that snapshot is `never_created`; it is not counted as a
    surviving file merely because it appears in the later snapshot.
    """
    expected = list(dict.fromkeys(expected_paths))
    before = _manifest_by_path(before_manifest)
    after = _manifest_by_path(after_manifest)
    present_after: list[str] = []
    lost: list[str] = []
    changed: list[str] = []
    never_created: list[str] = []
    unknown: list[str] = []

    for path in expected:
        original = before.get(path)
        if original is None:
            never_created.append(path)
            continue
        original_hash = original.get("sha256")
        if not isinstance(original_hash, str) or not original_hash:
            unknown.append(path)
            continue
        current = after.get(path)
        if current is None:
            lost.append(path)
            continue
        current_hash = current.get("sha256")
        if not isinstance(current_hash, str) or not current_hash:
            unknown.append(path)
        elif current_hash == original_hash:
            present_after.append(path)
        else:
            changed.append(path)

    return Survival(
        expected=expected,
        present_after=present_after,
        lost=lost,
        changed=changed,
        never_created=never_created,
        unknown=unknown,
        evidence_complete=bool(expected) and not unknown and not never_created,
    )


def compute_metrics(
    events: list[RecordedEvent],
    items: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    session: dict[str, Any],
    fault_record: dict[str, Any],
    before_manifest: Iterable[dict[str, Any]],
    after_manifest: Iterable[dict[str, Any]],
    *,
    expected_paths: Iterable[str],
    long_command_marker: str,
) -> Metrics:
    """Compute report metrics without inferring success from missing data.

    A reconnect timeout is right-censored: `reconnect_ms` remains `None` and
    `reconnect_censor_ms` gives the observed lower bound when an observation
    end timestamp is available. Missing turn or command evidence is retained
    as missing and reflected in `evidence_complete`.
    """
    t_inject_ns = _timestamp(fault_record.get("t_inject_ns"))
    triggered = fault_record.get("triggered") is not False
    reconnect_events = [
        event
        for event in events
        if not event.synthetic and event.type == CONNECTED
    ]
    disconnect_events = [
        event
        for event in events
        if not event.synthetic and event.type == DISCONNECTED
    ]
    disconnects_after = [
        event for event in disconnect_events
        if t_inject_ns is not None and event.recv_ns > t_inject_ns
    ]
    connects_after = [
        event for event in reconnect_events
        if t_inject_ns is not None and event.recv_ns > t_inject_ns
    ]

    reconnect_ms: float | None = None
    reconnect_basis = "none"
    reconnect_censored = bool(triggered and t_inject_ns is not None and not connects_after)
    reconnect_censor_ms: float | None = None
    observation_end_ns = _timestamp(
        fault_record.get("t_observe_end_ns")
        if fault_record.get("t_observe_end_ns") is not None
        else fault_record.get("t_recover_done_ns")
    )
    if connects_after:
        connected = min(connects_after, key=lambda event: event.recv_ns)
        prior_disconnects = [
            event for event in disconnects_after if event.recv_ns < connected.recv_ns
        ]
        if prior_disconnects:
            anchor = max(prior_disconnects, key=lambda event: event.recv_ns).recv_ns
            reconnect_basis = "disconnected_event"
        else:
            anchor = t_inject_ns
            reconnect_basis = "kill_timestamp"
        if anchor is not None:
            reconnect_ms = max(0, connected.recv_ns - anchor) / 1_000_000
    elif reconnect_censored and observation_end_ns is not None and observation_end_ns >= t_inject_ns:
        reconnect_censor_ms = (observation_end_ns - t_inject_ns) / 1_000_000

    disconnect_detect_ms: float | None = None
    if t_inject_ns is not None and disconnects_after:
        first_disconnect = min(disconnects_after, key=lambda event: event.recv_ns)
        disconnect_detect_ms = max(0, first_disconnect.recv_ns - t_inject_ns) / 1_000_000

    root_turn = next((turn for turn in turns if not turn.get("subagent_id")), None)
    turn_status = root_turn.get("status") if root_turn else None
    turn_error = root_turn.get("error") if root_turn and isinstance(root_turn.get("error"), dict) else {}
    turn_error_code = turn_error.get("code")
    if not isinstance(turn_error_code, str):
        turn_error_code = None

    seen_item_ids = {
        event.item_id
        for event in events
        if event.item_id and not event.synthetic
    }
    commands: list[CommandOutcome] = []
    for item in items:
        if item.get("type") != "command_execution":
            continue
        command = item.get("command") if isinstance(item.get("command"), str) else ""
        status = item.get("status") if isinstance(item.get("status"), str) else None
        exit_code = item.get("exit_code") if isinstance(item.get("exit_code"), int) else None
        item_id = item.get("id") if isinstance(item.get("id"), str) else ""
        duration = item.get("duration_ms")
        duration_ms = int(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None
        commands.append(
            CommandOutcome(
                item_id=item_id,
                command=command,
                status=status,
                exit_code=exit_code,
                duration_ms=duration_ms,
                seen_live=item_id in seen_item_ids,
                fault_window=bool(long_command_marker and long_command_marker in command),
                exit_code_missing=status == "completed" and exit_code is None,
                evidence_complete=bool(item_id and status is not None and (status != "completed" or exit_code is not None)),
            )
        )

    fault_commands = [command for command in commands if command.fault_window]
    fault_command_evidence_complete = bool(fault_commands) and all(
        command.evidence_complete for command in fault_commands
    )
    if turn_status != "completed":
        contradiction: bool | None = False if turn_status in {"failed", "cancelled"} else None
    elif not fault_commands:
        contradiction = None
    elif any(_command_has_failure_evidence(command) for command in fault_commands):
        contradiction = True
    elif fault_command_evidence_complete:
        contradiction = False
    else:
        # A completed command with a missing exit code is not proof of success
        # and is not enough evidence to call the turn contradictory.
        contradiction = None

    saved_ids = list(dict.fromkeys(
        item["id"] for item in items if isinstance(item.get("id"), str) and item["id"]
    ))
    missed = [item_id for item_id in saved_ids if item_id not in seen_item_ids]
    required_actions = [
        {"type": action.get("type"), "source": "session.required_actions"}
        for action in session.get("required_actions") or []
        if isinstance(action, dict)
    ]
    for event in events:
        if event.type in {"agent.session.action_required", "agent.session.requires_action"}:
            required_actions.append({"type": "requires_action", "source": "stream", "recv_ns": event.recv_ns})

    retried = False
    if t_inject_ns is not None:
        for event in events:
            if event.synthetic or event.recv_ns <= t_inject_ns:
                continue
            item = event.raw.get("item") if isinstance(event.raw.get("item"), dict) else {}
            command_text = item.get("command") if isinstance(item.get("command"), str) else ""
            if event.item_type == "command_execution" and long_command_marker in command_text:
                retried = True
                break

    survival = compute_survival(before_manifest, after_manifest, expected_paths)
    reasons: list[str] = []
    if turn_status is None:
        reasons.append("root_turn_missing")
    elif turn_status not in {"completed", "failed", "cancelled"}:
        reasons.append("root_turn_not_terminal")
    if not fault_commands:
        reasons.append("fault_window_command_missing")
    elif not fault_command_evidence_complete:
        reasons.append("fault_window_command_evidence_incomplete")
    if not survival.evidence_complete:
        reasons.append("file_survival_evidence_incomplete")
    if triggered and t_inject_ns is None:
        reasons.append("fault_injection_timestamp_missing")

    session_status = session.get("status")
    if not isinstance(session_status, str):
        session_status = None
    return Metrics(
        reconnect_ms=reconnect_ms,
        reconnect_basis=reconnect_basis,
        reconnect_censored=reconnect_censored,
        reconnect_censor_ms=reconnect_censor_ms,
        disconnect_event_seen=bool(disconnects_after),
        disconnect_detect_ms=disconnect_detect_ms,
        turn_status=turn_status if isinstance(turn_status, str) else None,
        turn_error_code=turn_error_code,
        commands=commands,
        contradiction=contradiction,
        command_evidence_complete=fault_command_evidence_complete,
        missed_items=missed,
        missed_items_count=len(missed),
        saved_items_not_seen_live=len(missed),
        intermediate_events_missed=None,
        intermediate_events_reason="not_reconstructable_from_saved_items",
        live_item_count=len(seen_item_ids),
        saved_item_count=len(saved_ids),
        survival=survival,
        required_actions=required_actions,
        session_final_status=session_status,
        agent_retried_command=retried,
        evidence_complete=not reasons,
        inconclusive_reasons=reasons,
    )


def _manifest_by_path(entries: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for entry in entries:
        path = entry.get("path")
        if isinstance(path, str):
            found[path] = entry
    return found


def _timestamp(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _command_has_failure_evidence(command: CommandOutcome) -> bool:
    return command.status in NOT_DONE or (
        command.exit_code is not None and command.exit_code != 0
    )
