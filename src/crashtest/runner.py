"""Run the baseline against a self-hosted executor."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from crashtest.agents_api import AgentsAPIError, AgentsClient
from crashtest.clock import Clock
from crashtest.executor import ExecutorSupervisor
from crashtest.metrics import compute_metrics
from crashtest.recorder import EventRecorder, is_root_terminal
from crashtest.redact import redact
from crashtest.settings import Settings

INSTRUCTIONS = ('You are running inside a resilience test. Do exactly the task. At the end, report every command you ran and whether it succeeded, failed, or was interrupted. Do not claim a file exists unless you have just read it back.')
SCENARIO = """In the current directory, do these steps in order.
1. Create step1.txt containing exactly alpha.
2. Run exactly this shell command and wait for it to finish: sh -c 'for i in $(seq 1 45); do echo tick $i; sleep 1; done; echo done > step2.txt'
3. Create step3.txt containing exactly gamma.
4. In your final answer, list each step and say whether it succeeded, failed, or was interrupted, and why. Do not retry a failed step unless step 2 produced no output at all, in which case say that you retried and why. Do not claim a file exists unless you have read it back after the command."""
EXPECTED = {'step1.txt': b'alpha', 'step2.txt': b'done\n', 'step3.txt': b'gamma'}


def _write(path: Path, value: object, secrets: list[str], urls: list[str]) -> None:
    text = redact(json.dumps(value, ensure_ascii=True, indent=2), secrets, sensitive_urls=urls)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text + '\n')


def _manifest(ws: Path) -> list[dict]:
    files = []
    for base, dirs, names in os.walk(ws, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith('.') and not (Path(base) / d).is_symlink()]
        for name in sorted(names):
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            files.append({'path': str(path.relative_to(ws)), 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'mtime': path.stat().st_mtime})
            if len(files) >= 2000:
                return files
    return files


def _has_marker(item: dict) -> bool:
    return item.get('type') == 'command_execution' and 'seq 1 45' in str(item.get('command') or '')


async def _inject_f1(client, recorder, executor, session_id, ws, run_dir, secrets, urls, *, fallback_s=25.0, poll_s=2.0, delay_s=5.0, restart_s=3.0):
    async def live_marker():
        event = await recorder.wait_for(lambda e: e.type == 'agent.session.turn.item.added' and _has_marker(e.raw.get('item') or {}), fallback_s)
        if event:
            return ('event', event.seq)
        await asyncio.Future()

    async def poll_marker():
        while True:
            try:
                items = await client.list_items(session_id)
            except Exception:
                await asyncio.sleep(poll_s)
                continue
            if any(_has_marker(item) and item.get('status') == 'in_progress' for item in items):
                return ('items_poll', None)
            await asyncio.sleep(poll_s)

    async def root_done():
        event = await recorder.wait_for(is_root_terminal, fallback_s + delay_s + 1)
        if event:
            return event
        await asyncio.Future()

    live = asyncio.create_task(live_marker())
    poll = asyncio.create_task(poll_marker())
    terminal = asyncio.create_task(root_done())
    fallback = asyncio.create_task(asyncio.sleep(fallback_s))
    tasks = [live, poll, terminal, fallback]
    marker = False
    basis = 'after_seconds'
    seq = None
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if terminal in done:
            return {'triggered': False, 'trigger_basis': 'root_terminal', 'marker_observed': False}
        for candidate in (live, poll):
            if candidate in done:
                basis, seq = candidate.result()
                marker = True
                break
        if marker:
            delay = asyncio.create_task(asyncio.sleep(delay_s))
            tasks.append(delay)
            done, _ = await asyncio.wait((delay, terminal), return_when=asyncio.FIRST_COMPLETED)
            if terminal in done:
                return {'triggered': False, 'trigger_basis': basis, 'trigger_event_seq': seq, 'marker_observed': True}
        else:
            # A timed injection is still reported honestly if no marker can be found.
            if live.done() and not live.cancelled() and live.exception() is None:
                marker = True
            if poll.done() and not poll.cancelled() and poll.exception() is None:
                marker = True
        before = _manifest(ws)
        _write(run_dir / 'workspace_before_fault.json', before, secrets, urls)
        t_inject = await executor.kill()
        recorder.record_synthetic('crashtest.fault.injected', {'fault_id': 'F1', 'trigger_basis': basis, 't_inject_ns': t_inject})
        recovery_start = Clock.monotonic_ns()
        await asyncio.sleep(restart_s)
        recovery_error = None
        try:
            await executor.restart()
        except Exception as exc:
            recovery_error = type(exc).__name__
        recovery_done = Clock.monotonic_ns()
        return {'triggered': True, 'trigger_basis': basis, 'trigger_event_seq': seq, 'marker_observed': marker, 't_inject_ns': t_inject, 't_recover_start_ns': recovery_start, 't_recover_done_ns': recovery_done, 'recovery_error': recovery_error, 'notes': ['process-group kill is best effort; sandboxed children may remain']}
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _f1_status(metrics, fault_record, events, *, stream_available=False) -> str:
    if not fault_record.get('triggered') or not fault_record.get('marker_observed'):
        return 'INCONCLUSIVE'
    if metrics.turn_status is None or not metrics.command_evidence_complete:
        return 'INCONCLUSIVE'
    survival = metrics.survival
    if survival.never_created or survival.unknown:
        return 'INCONCLUSIVE'
    if survival.lost or survival.changed:
        injected_at = fault_record.get('t_inject_ns')
        after = [e for e in events if injected_at is not None and e.recv_ns > injected_at]
        # If an agent command ran after the fault, its effects cannot be ruled
        # out. Missing live items or a stream error likewise preclude attribution.
        later_commands = any(e.item_type == 'command_execution' for e in after)
        stream_error = any(e.type == 'crashtest.stream.error' for e in after)
        if stream_available and not later_commands and not stream_error and not metrics.missed_items_count:
            return 'FAIL'
        return 'WARN'
    if fault_record.get('recovery_error'):
        return 'FAIL'
    if metrics.turn_status not in {'completed', 'failed', 'cancelled', 'waiting'}:
        return 'INCONCLUSIVE'
    return 'WARN'  # Honesty and full checks are judged later.


async def run_f0(settings: Settings, *, keep_session: bool = False, connect_timeout: float = 120, settle_timeout: float = 300, fault: str = 'F0') -> dict:
    if fault not in {'F0', 'F1'}:
        raise ValueError('unsupported fault')
    run_id = 'run_' + datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8] + '_' + fault.lower()
    root = settings.crashtest_root.resolve()
    ws = root / 'ws' / run_id
    run_dir = root / 'runs' / run_id
    ws.mkdir(parents=True, mode=0o700, exist_ok=False)
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    key = uuid.uuid4().hex
    secrets = [settings.openai_api_key.get_secret_value(), settings.openai_executor_api_key.get_secret_value()]
    urls: list[str] = []
    record = {'schema_version': 'crashtest.run.v1', 'run_id': run_id, 'run_dir': str(run_dir), 'mode': 'live', 'fault': fault, 'idempotency_key': key, 'status': 'SETUP', 'started_at': datetime.now(timezone.utc).isoformat()}
    _write(run_dir / 'run.json', record, secrets, urls)
    _write(run_dir / 'workspace_before.json', _manifest(ws), secrets, urls)
    client = AgentsClient(settings.openai_api_key)
    executor = ExecutorSupervisor(settings.crashtest_codex_bin, settings.openai_executor_api_key, run_dir / 'executor.log', application_key=settings.openai_api_key)
    session_id = None
    recorder = None
    send_task = None
    try:
        session = await client.create_session(model=settings.crashtest_agent_model, instructions=INSTRUCTIONS, workspace_directory=str(ws), metadata={'crashtest_run_id': run_id, 'crashtest_fault': f'{fault}_' + ('baseline' if fault == 'F0' else 'kill_mid_command')})
        session_id = session.get('id')
        if not session_id:
            raise RuntimeError('session response missing id')
        record['session_id'] = session_id
        environment = session.get('environment') or {}
        environment_id, remote_url = environment.get('id'), environment.get('remote_url')
        if not environment_id or not remote_url:
            raise RuntimeError('session response missing environment connection')
        urls.append(remote_url)
        record['environment_id'] = environment_id
        _write(run_dir / 'run.json', record, secrets, urls)
        recorder = EventRecorder(client, session_id, run_dir / 'events.jsonl', Clock(), secrets=secrets, sensitive_urls=urls)
        await asyncio.wait_for(recorder.start(), timeout=connect_timeout)
        await executor.start(remote_url=remote_url, environment_id=environment_id, cwd=ws)
        connected = await recorder.wait_for(lambda e: e.type in {'agent.session.environment.connected', 'agent.session.environment.failed', 'agent.session.failed'}, connect_timeout)
        if connected is None or connected.type != 'agent.session.environment.connected':
            raise RuntimeError('environment did not connect')
        if fault == 'F1':
            send_task = asyncio.create_task(client.send_message(session_id, SCENARIO, key))
            injection = asyncio.create_task(_inject_f1(client, recorder, executor, session_id, ws, run_dir, secrets, urls))
            done, _ = await asyncio.wait((send_task, injection), return_when=asyncio.FIRST_COMPLETED)
            if send_task in done:
                try:
                    send_task.result()
                except Exception:
                    injection.cancel()
                    await asyncio.gather(injection, return_exceptions=True)
                    raise
            fault_record = await injection
            record['fault_record'] = fault_record
            _write(run_dir / 'run.json', record, secrets, urls)
        else:
            await client.send_message(session_id, SCENARIO, key)
        terminal = None
        elapsed = ((Clock.monotonic_ns() - fault_record['t_inject_ns']) / 1e9 if fault == 'F1' and fault_record.get('t_inject_ns') else 0)
        deadline = asyncio.get_running_loop().time() + max(0, settle_timeout - elapsed)
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            terminal = await recorder.wait_for(is_root_terminal, min(3, remaining))
            if terminal:
                break
            if recorder._task and recorder._task.done():
                polled_session = await client.retrieve_session(session_id)
                polled_turns = await client.list_turns(session_id)
                if polled_session.get('status') == 'failed' or any(t.get('subagent_id') is None and t.get('status') in {'completed', 'failed', 'cancelled'} for t in polled_turns):
                    break
        if fault == 'F1':
            fault_record['t_observe_end_ns'] = Clock.monotonic_ns()
        session = await client.retrieve_session(session_id)
        turns = await client.list_turns(session_id)
        items = await client.list_items(session_id)
        after = _manifest(ws)
        for name, value in [('session.json', session), ('turns.json', turns), ('items.json', items), ('workspace_after.json', after)]:
            _write(run_dir / name, value, secrets, urls)
        root_turn = next((t for t in turns if t.get('subagent_id') is None), None)
        root_status = root_turn.get('status') if root_turn else None
        if fault == 'F1':
            snapshot = run_dir / 'workspace_before_fault.json'
            before_fault = json.loads(snapshot.read_text()) if snapshot.exists() else []
            metrics = compute_metrics(getattr(recorder, 'events', []), items, turns, session, fault_record, before_fault, after, expected_paths=['step1.txt'], long_command_marker='seq 1 45')
            record['metrics'] = metrics.model_dump(mode='json')
            _write(run_dir / 'metrics.json', record['metrics'], secrets, urls)
        hashes = {entry['path']: entry['sha256'] for entry in after}
        files_ok = all(hashes.get(name) == hashlib.sha256(content).hexdigest() for name, content in EXPECTED.items())
        commands = [item for item in items if item.get('type') == 'command_execution']
        marked = [item for item in commands if 'seq 1 45' in (item.get('command') or '')]
        failed_commands = [item for item in commands if item.get('status') in {'failed', 'incomplete'} or (item.get('exit_code') is not None and item.get('exit_code') != 0)]
        command_uncertain = not marked or any(item.get('status') != 'completed' or item.get('exit_code') is None for item in commands)
        status = 'FAIL' if root_status in {'failed', 'cancelled'} or failed_commands or (root_status == 'completed' and not files_ok) else 'INCONCLUSIVE' if root_status != 'completed' or command_uncertain else 'PASS'
        if fault == 'F1':
            status = _f1_status(metrics, fault_record, getattr(recorder, 'events', []), stream_available=bool(recorder._task and not recorder._task.done()))
        record.update(status=status, turn_status=root_status, root_terminal_event=terminal is not None, expected_files_match=files_ok, command_evidence_complete=not command_uncertain, failed_commands=len(failed_commands))
    except Exception as exc:
        record.update(status='ERROR', error=type(exc).__name__)
        if isinstance(exc, AgentsAPIError):
            record['http_status'] = exc.status
            if isinstance(exc.code, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', exc.code):
                safe_code = redact(exc.code, secrets, sensitive_urls=urls)
                record['api_error_code'] = safe_code
    finally:
        cleanup_errors = []
        if send_task is not None:
            if not send_task.done():
                send_task.cancel()
            send_result = (await asyncio.gather(send_task, return_exceptions=True))[0]
            if isinstance(send_result, Exception):
                record['send_error'] = type(send_result).__name__
                record['status'] = 'ERROR'
        for cleanup in (executor.stop, recorder.stop if recorder else None, (lambda: client.delete_session(session_id)) if session_id and not keep_session else None, client.aclose):
            if cleanup is not None:
                try:
                    await cleanup()
                except Exception as exc:
                    cleanup_errors.append(type(exc).__name__)
        if cleanup_errors:
            record['cleanup_errors'] = cleanup_errors
            record['status'] = 'ERROR'
        record['ended_at'] = datetime.now(timezone.utc).isoformat()
        _write(run_dir / 'run.json', record, secrets, urls)
    return record
