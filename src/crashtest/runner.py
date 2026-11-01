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


async def run_f0(settings: Settings, *, keep_session: bool = False, connect_timeout: float = 120, settle_timeout: float = 300) -> dict:
    run_id = 'run_' + datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8] + '_f0'
    root = settings.crashtest_root.resolve()
    ws = root / 'ws' / run_id
    run_dir = root / 'runs' / run_id
    ws.mkdir(parents=True, mode=0o700, exist_ok=False)
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    key = uuid.uuid4().hex
    secrets = [settings.openai_api_key.get_secret_value(), settings.openai_executor_api_key.get_secret_value()]
    urls: list[str] = []
    record = {'schema_version': 'crashtest.run.v1', 'run_id': run_id, 'run_dir': str(run_dir), 'mode': 'live', 'fault': 'F0', 'idempotency_key': key, 'status': 'SETUP', 'started_at': datetime.now(timezone.utc).isoformat()}
    _write(run_dir / 'run.json', record, secrets, urls)
    _write(run_dir / 'workspace_before.json', _manifest(ws), secrets, urls)
    client = AgentsClient(settings.openai_api_key)
    executor = ExecutorSupervisor(settings.crashtest_codex_bin, settings.openai_executor_api_key, run_dir / 'executor.log', application_key=settings.openai_api_key)
    session_id = None
    recorder = None
    try:
        session = await client.create_session(model=settings.crashtest_agent_model, instructions=INSTRUCTIONS, workspace_directory=str(ws), metadata={'crashtest_run_id': run_id, 'crashtest_fault': 'F0_baseline'})
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
        await client.send_message(session_id, SCENARIO, key)
        terminal = None
        deadline = asyncio.get_running_loop().time() + settle_timeout
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
        session = await client.retrieve_session(session_id)
        turns = await client.list_turns(session_id)
        items = await client.list_items(session_id)
        after = _manifest(ws)
        for name, value in [('session.json', session), ('turns.json', turns), ('items.json', items), ('workspace_after.json', after)]:
            _write(run_dir / name, value, secrets, urls)
        root_turn = next((t for t in turns if t.get('subagent_id') is None), None)
        root_status = root_turn.get('status') if root_turn else None
        hashes = {entry['path']: entry['sha256'] for entry in after}
        files_ok = all(hashes.get(name) == hashlib.sha256(content).hexdigest() for name, content in EXPECTED.items())
        commands = [item for item in items if item.get('type') == 'command_execution']
        marked = [item for item in commands if 'seq 1 45' in (item.get('command') or '')]
        failed_commands = [item for item in commands if item.get('status') in {'failed', 'incomplete'} or (item.get('exit_code') is not None and item.get('exit_code') != 0)]
        command_uncertain = not marked or any(item.get('status') != 'completed' or item.get('exit_code') is None for item in commands)
        status = 'FAIL' if root_status in {'failed', 'cancelled'} or failed_commands or (root_status == 'completed' and not files_ok) else 'INCONCLUSIVE' if root_status != 'completed' or command_uncertain else 'PASS'
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
