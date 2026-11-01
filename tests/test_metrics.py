from crashtest.metrics import compute_metrics, compute_survival
from crashtest.models import RecordedEvent


MARKER = "seq 1 45"


def event(
    event_type,
    recv_ns,
    *,
    item_id=None,
    item_type=None,
    raw=None,
    synthetic=False,
):
    return RecordedEvent(
        seq=recv_ns,
        conn=1,
        recv_ns=recv_ns,
        recv_at=recv_ns / 1_000_000_000,
        type=event_type,
        session_id="sess_x",
        turn_id="turn_x",
        item_id=item_id,
        item_type=item_type,
        raw=raw or {"type": event_type},
        synthetic=synthetic,
    )


def baseline_manifest(sha="same"):
    return [{"path": "step1.txt", "size": 5, "sha256": sha, "mtime": 1.0}]


def test_metrics_contradiction_and_disconnect_reconnect_basis():
    metrics = compute_metrics(
        events=[
            event("agent.session.environment.disconnected", 7_000_000_000),
            event("agent.session.environment.connected", 11_000_000_000),
            event(
                "agent.session.turn.item.done",
                12_000_000_000,
                item_id="cmd_1",
                item_type="command_execution",
            ),
            event(
                "agent.session.turn.completed",
                13_000_000_000,
                raw={"type": "agent.session.turn.completed", "turn": {"subagent_id": None}},
            ),
            event(
                "agent.session.turn.completed",
                12_500_000_000,
                raw={"type": "agent.session.turn.completed", "turn": {"subagent_id": "sub_1"}},
            ),
        ],
        items=[
            {
                "id": "cmd_1",
                "type": "command_execution",
                "command": f"sh -c '{MARKER}'",
                "status": "incomplete",
                "exit_code": None,
            },
            {"id": "msg_1", "type": "message", "status": "completed"},
        ],
        turns=[
            {"subagent_id": "sub_1", "status": "completed"},
            {"subagent_id": None, "status": "completed"},
        ],
        session={"status": "idle", "required_actions": [{"type": "environment_connection"}]},
        fault_record={"triggered": True, "t_inject_ns": 6_000_000_000, "t_observe_end_ns": 15_000_000_000},
        before_manifest=baseline_manifest(),
        after_manifest=baseline_manifest(),
        expected_paths=["step1.txt"],
        long_command_marker=MARKER,
    )
    assert metrics.contradiction is True
    assert metrics.turn_status == "completed"
    assert metrics.reconnect_basis == "disconnected_event"
    assert metrics.reconnect_ms == 4_000
    assert metrics.disconnect_detect_ms == 1_000
    assert metrics.survival.present_after == ["step1.txt"]
    assert metrics.saved_items_not_seen_live == 1
    assert metrics.intermediate_events_missed is None
    assert metrics.required_actions == [
        {"type": "environment_connection", "source": "session.required_actions"}
    ]


def test_metrics_kill_timestamp_basis():
    metrics = compute_metrics(
        events=[event("agent.session.environment.connected", 11_000_000_000)],
        items=[
            {
                "id": "cmd_1",
                "type": "command_execution",
                "command": MARKER,
                "status": "completed",
                "exit_code": 0,
            }
        ],
        turns=[{"subagent_id": None, "status": "completed"}],
        session={"status": "idle"},
        fault_record={"triggered": True, "t_inject_ns": 6_000_000_000},
        before_manifest=baseline_manifest(),
        after_manifest=baseline_manifest(),
        expected_paths=["step1.txt"],
        long_command_marker=MARKER,
    )
    assert metrics.reconnect_ms == 5_000
    assert metrics.reconnect_basis == "kill_timestamp"
    assert metrics.contradiction is False
    assert metrics.command_evidence_complete is True


def test_timeout_is_censored_not_zero():
    metrics = compute_metrics(
        events=[],
        items=[],
        turns=[],
        session={"status": "idle"},
        fault_record={
            "triggered": True,
            "t_inject_ns": 2_000_000_000,
            "t_observe_end_ns": 32_000_000_000,
        },
        before_manifest=baseline_manifest(),
        after_manifest=baseline_manifest(),
        expected_paths=["step1.txt"],
        long_command_marker=MARKER,
    )
    assert metrics.reconnect_ms is None
    assert metrics.reconnect_basis == "none"
    assert metrics.reconnect_censored is True
    assert metrics.reconnect_censor_ms == 30_000
    assert metrics.contradiction is None
    assert metrics.evidence_complete is False
    assert "root_turn_missing" in metrics.inconclusive_reasons
    assert "fault_window_command_missing" in metrics.inconclusive_reasons


def test_nonterminal_root_turn_is_inconclusive():
    for status in ("waiting", "in_progress"):
        metrics = compute_metrics(
            events=[],
            items=[
                {
                    "id": "cmd_1",
                    "type": "command_execution",
                    "command": MARKER,
                    "status": "incomplete",
                    "exit_code": None,
                }
            ],
            turns=[{"subagent_id": None, "status": status}],
            session={"status": "idle"},
            fault_record={"triggered": True, "t_inject_ns": 1_000_000_000},
            before_manifest=baseline_manifest(),
            after_manifest=baseline_manifest(),
            expected_paths=["step1.txt"],
            long_command_marker=MARKER,
        )
        assert metrics.turn_status == status
        assert metrics.evidence_complete is False
        assert "root_turn_not_terminal" in metrics.inconclusive_reasons


def test_missing_exit_code_does_not_create_false_contradiction():
    metrics = compute_metrics(
        events=[],
        items=[
            {
                "id": "cmd_1",
                "type": "command_execution",
                "command": MARKER,
                "status": "completed",
                "exit_code": None,
            }
        ],
        turns=[{"subagent_id": None, "status": "completed"}],
        session={"status": "idle"},
        fault_record={"triggered": True, "t_inject_ns": 1_000_000_000},
        before_manifest=baseline_manifest(),
        after_manifest=baseline_manifest(),
        expected_paths=["step1.txt"],
        long_command_marker=MARKER,
    )
    assert metrics.contradiction is None
    assert metrics.commands[0].exit_code_missing is True
    assert metrics.command_evidence_complete is False
    assert metrics.evidence_complete is False


def test_survival_sha_classification_is_strict():
    before = [
        {"path": "same", "sha256": "a"},
        {"path": "changed", "sha256": "b"},
        {"path": "lost", "sha256": "c"},
        {"path": "unknown", "size": 2},
    ]
    after = [
        {"path": "same", "sha256": "a"},
        {"path": "changed", "sha256": "different"},
        {"path": "unknown", "sha256": "d"},
    ]
    survival = compute_survival(before, after, ["same", "changed", "lost", "absent", "unknown"])
    assert survival.present_after == ["same"]
    assert survival.changed == ["changed"]
    assert survival.lost == ["lost"]
    assert survival.never_created == ["absent"]
    assert survival.unknown == ["unknown"]
    assert survival.evidence_complete is False


def test_never_created_file_makes_survival_evidence_incomplete():
    survival = compute_survival([], [], ["step1.txt"])
    assert survival.never_created == ["step1.txt"]
    assert survival.evidence_complete is False
