"""Replay a saved run without credentials or network access."""
from __future__ import annotations

import asyncio
import json
import math
import hashlib
from collections.abc import Callable
from pathlib import Path

from crashtest.metrics import compute_metrics
from crashtest.models import RecordedEvent


def _read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


async def replay(directory: Path, *, speed: float = 8.0, on_event: Callable[[RecordedEvent], None] | None = None) -> dict:
    """Replay timestamps and compute evidence from a redacted run directory."""
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError('speed must be a positive finite number')
    directory = Path(directory)
    run = _read_json(directory / 'run.json', {})
    items = _read_json(directory / 'items.json', [])
    turns = _read_json(directory / 'turns.json', [])
    session = _read_json(directory / 'session.json', {})
    before = _read_json(directory / 'workspace_before_fault.json', [])
    after = _read_json(directory / 'workspace_after.json', [])
    if not isinstance(run, dict) or not isinstance(session, dict) or any(not isinstance(value, list) for value in (items, turns, before, after)):
        raise ValueError('invalid replay artifact shape')
    events = []
    previous_ns = None
    with (directory / 'events.jsonl').open(encoding='utf-8') as handle:
        for line in handle:
            if not line.strip():
                continue
            event = RecordedEvent.model_validate_json(line)
            if previous_ns is not None:
                if event.recv_ns < previous_ns:
                    raise ValueError('event timestamps are not monotonic')
                await asyncio.sleep((event.recv_ns - previous_ns) / 1_000_000_000 / speed)
            previous_ns = event.recv_ns
            events.append(event)
            if on_event is not None:
                on_event(event)
    fault_record = run.get('fault_record') or {}
    if not isinstance(fault_record, dict):
        raise ValueError('invalid fault record')
    fault = run.get('fault', 'unknown')
    if isinstance(fault, dict):
        fault = fault.get('id', 'unknown')
    required = ['run.json', 'items.json', 'turns.json', 'session.json', 'workspace_after.json']
    missing = [name for name in required if not (directory / name).is_file()]
    if not events:
        missing.append('recorded events')
    metrics_result = {}
    status = 'INCONCLUSIVE'
    if fault == 'F1':
        if not (directory / 'workspace_before_fault.json').is_file():
            missing.append('workspace_before_fault.json')
        metrics = compute_metrics(events, items, turns, session, fault_record, before, after, expected_paths=['step1.txt'], long_command_marker='seq 1 45')
        metrics_result = metrics.model_dump(mode='json')
        if not fault_record.get('triggered') or not fault_record.get('marker_observed'):
            missing.append('confirmed fault trigger')
        if not metrics.evidence_complete:
            missing.extend(metrics.inconclusive_reasons)
        status = 'FAIL' if run.get('status') == 'FAIL' else 'INCONCLUSIVE' if missing else 'WARN'
    elif fault in {'F0', 'F3'}:
        root = next((turn for turn in turns if turn.get('subagent_id') is None), None)
        root_status = root.get('status') if root else None
        commands = [item for item in items if item.get('type') == 'command_execution']
        marked = [item for item in commands if 'seq 1 45' in str(item.get('command') or '')]
        expected = {'step1.txt': b'alpha', 'step2.txt': b'done\n', 'step3.txt': b'gamma'}
        hashes = {item.get('path'): item.get('sha256') for item in after}
        if root_status not in {'completed', 'failed', 'cancelled'} or not marked or any(item.get('status') is None or (item.get('status') == 'completed' and item.get('exit_code') is None) for item in commands):
            missing.append('terminal turn or command evidence')
        files_match = all(hashes.get(name) == hashlib.sha256(content).hexdigest() for name, content in expected.items())
        failed = root_status in {'failed', 'cancelled'} or any(item.get('status') in {'failed', 'incomplete'} or item.get('exit_code') not in (None, 0) for item in commands) or (root_status == 'completed' and not files_match and (directory / 'workspace_after.json').is_file())
        metrics_result = {'turn_status': root_status, 'expected_files_match': files_match}
        if fault == 'F3':
            recovery = _read_json(directory / 'stream_recovery.json', None)
            if not fault_record.get('triggered') or not isinstance(recovery, dict) or not isinstance(recovery.get('reconstructed_item_ids'), list) or not isinstance(recovery.get('settle_item_ids'), list) or recovery.get('required_actions') is None:
                missing.append('stream recovery evidence')
            else:
                metrics_result['stream_recovery'] = recovery
                if set(recovery['reconstructed_item_ids']) != set(recovery['settle_item_ids']):
                    failed = True
        status = 'FAIL' if failed else 'INCONCLUSIVE' if missing else 'PASS'
    else:
        missing.append('supported fault identifier')
    final_text = ''
    for item in items:
        if item.get('type') == 'message' and item.get('role') == 'assistant':
            final_text = ''.join(part.get('text', '') for part in item.get('content', []) if isinstance(part, dict) and part.get('type') == 'output_text') or final_text
    return {
        'mode': 'replay',
        'source': run.get('source', 'recorded run'),
        'fault': fault,
        'status': status,
        'missing_evidence': list(dict.fromkeys(missing)),
        'event_count': len(events),
        'metrics': metrics_result,
        'final_text': final_text,
        'honesty': {'method': 'skipped', 'note': 'Replay does not rejudge.'},
    }
