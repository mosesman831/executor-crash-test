import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from crashtest import runner
from crashtest.settings import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize(('command_status', 'exit_code', 'expected_status'), [
    ('completed', 0, 'PASS'), ('failed', 1, 'FAIL'), ('completed', None, 'INCONCLUSIVE'),
])
async def test_baseline_order_cleanup_and_redaction(tmp_path, monkeypatch, command_status, exit_code, expected_status):
    calls = []
    url = 'https://example.test/path?token=private'
    class Client:
        def __init__(self, key): pass
        async def create_session(self, **kwargs):
            calls.append('create')
            runs = list((tmp_path / 'runs').glob('*/run.json'))
            assert json.loads(runs[0].read_text())['idempotency_key']
            assert Path(kwargs['workspace_directory']).is_absolute()
            return {'id': 'sess', 'environment': {'id': 'env', 'remote_url': url}}
        async def send_message(self, session_id, text, key): calls.append('send')
        async def retrieve_session(self, session_id): calls.append('retrieve'); return {'status': 'idle', 'remote_url': url}
        async def list_turns(self, session_id): calls.append('turns'); return [{'subagent_id': None, 'status': 'completed'}]
        async def list_items(self, session_id): calls.append('items'); return [{'type': 'command_execution', 'command': 'seq 1 45', 'status': command_status, 'exit_code': exit_code}]
        async def delete_session(self, session_id): calls.append('delete')
        async def aclose(self): calls.append('close')
    class Executor:
        def __init__(self, *args, **kwargs): pass
        async def start(self, **kwargs):
            calls.append('executor')
            for name, content in runner.EXPECTED.items(): (kwargs['cwd'] / name).write_bytes(content)
        async def stop(self): calls.append('stop_executor')
    class Recorder:
        def __init__(self, *args, **kwargs): self._task = None
        async def start(self): calls.append('stream')
        async def wait_for(self, pred, timeout):
            from types import SimpleNamespace
            event = SimpleNamespace(type='agent.session.environment.connected', raw={}) if 'send' not in calls else SimpleNamespace(type='agent.session.turn.completed', raw={'turn': {'subagent_id': None}})
            assert pred(event)
            calls.append('connect' if 'send' not in calls else 'terminal')
            return event
        async def stop(self): calls.append('stop_stream')
    monkeypatch.setattr(runner, 'AgentsClient', Client)
    monkeypatch.setattr(runner, 'ExecutorSupervisor', Executor)
    monkeypatch.setattr(runner, 'EventRecorder', Recorder)
    settings = Settings(openai_api_key=SecretStr('appsecret'), openai_executor_api_key=SecretStr('execsecret'), crashtest_root=tmp_path)
    result = await runner.run_f0(settings)
    assert result['status'] == expected_status
    assert calls == ['create', 'stream', 'executor', 'connect', 'send', 'terminal', 'retrieve', 'turns', 'items', 'stop_executor', 'stop_stream', 'delete', 'close']
    for path in Path(result['run_dir']).iterdir():
        assert 'private' not in path.read_text()
        assert 'appsecret' not in path.read_text()
        assert 'execsecret' not in path.read_text()


@pytest.mark.asyncio
async def test_cleanup_when_stream_fails(tmp_path, monkeypatch):
    calls = []
    class Client:
        def __init__(self, key): pass
        async def create_session(self, **kwargs): return {'id': 'sess', 'environment': {'id': 'env', 'remote_url': 'https://example.test'}}
        async def delete_session(self, session_id): calls.append('delete')
        async def aclose(self): calls.append('close')
    class Executor:
        def __init__(self, *args, **kwargs): pass
        async def stop(self): calls.append('stop_executor')
    class Recorder:
        def __init__(self, *args, **kwargs): pass
        async def start(self): raise RuntimeError('error')
        async def stop(self): calls.append('stop_stream')
    monkeypatch.setattr(runner, 'AgentsClient', Client)
    monkeypatch.setattr(runner, 'ExecutorSupervisor', Executor)
    monkeypatch.setattr(runner, 'EventRecorder', Recorder)
    settings = Settings(openai_api_key=SecretStr('app'), openai_executor_api_key=SecretStr('executor'), crashtest_root=tmp_path)
    result = await runner.run_f0(settings)
    assert result['status'] == 'ERROR'
    assert calls == ['stop_executor', 'stop_stream', 'delete', 'close']


@pytest.mark.asyncio
async def test_stream_handshake_timeout_and_safe_api_error(tmp_path, monkeypatch):
    from crashtest.agents_api import AgentsAPIError
    import asyncio
    calls = []
    class Client:
        def __init__(self, key): pass
        async def create_session(self, **kwargs):
            if mode[0] == 'error':
                raise AgentsAPIError(403, 'permission_denied', {'secret': 'appsecret'})
            return {'id': 'sess', 'environment': {'id': 'env', 'remote_url': 'https://example.test'}}
        async def delete_session(self, session_id): calls.append('delete')
        async def aclose(self): calls.append('close')
    class Executor:
        def __init__(self, *args, **kwargs): pass
        async def stop(self): calls.append('stop')
    class Recorder:
        def __init__(self, *args, **kwargs): pass
        async def start(self): await asyncio.Event().wait()
        async def stop(self): calls.append('recorder_stop')
    monkeypatch.setattr(runner, 'AgentsClient', Client)
    monkeypatch.setattr(runner, 'ExecutorSupervisor', Executor)
    monkeypatch.setattr(runner, 'EventRecorder', Recorder)
    settings = Settings(openai_api_key=SecretStr('appsecret'), openai_executor_api_key=SecretStr('execsecret'), crashtest_root=tmp_path)
    mode = ['timeout']
    timeout = await runner.run_f0(settings, connect_timeout=0.001)
    assert timeout['status'] == 'ERROR' and timeout['error'] == 'TimeoutError'
    assert calls == ['stop', 'recorder_stop', 'delete', 'close']
    mode[0] = 'error'
    error = await runner.run_f0(settings)
    assert error['http_status'] == 403 and error['api_error_code'] == 'permission_denied'
    assert 'appsecret' not in Path(error['run_dir'], 'run.json').read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(('source', 'basis', 'marked'), [('event', 'event', True), ('poll', 'items_poll', True), ('none', 'after_seconds', False), ('terminal', 'root_terminal', False)])
async def test_f1_trigger_and_timed_fallback(tmp_path, source, basis, marked):
    import asyncio
    from types import SimpleNamespace
    calls = []
    class Client:
        async def list_items(self, session_id):
            if source == 'poll':
                return [{'type': 'command_execution', 'command': 'seq 1 45', 'status': 'in_progress'}]
            return []
    class Recorder:
        async def wait_for(self, pred, timeout):
            event = SimpleNamespace(type='agent.session.turn.item.added', raw={'item': {'type': 'command_execution', 'command': 'seq 1 45'}}, seq=7)
            terminal = SimpleNamespace(type='agent.session.turn.completed', raw={'turn': {'subagent_id': None}}, seq=8)
            if source == 'event' and pred(event): return event
            if source == 'terminal' and pred(terminal): return terminal
            await asyncio.Event().wait()
        def record_synthetic(self, *args): calls.append('record')
    class Executor:
        async def kill(self): calls.append('kill'); return 100
        async def restart(self): calls.append('restart')
    result = await runner._inject_f1(Client(), Recorder(), Executor(), 'sess', tmp_path, tmp_path, [], [], fallback_s=0.01, poll_s=0.001, delay_s=0.001, restart_s=0)
    assert result['trigger_basis'] == basis
    assert result['marker_observed'] == marked
    assert result['triggered'] == (source != 'terminal')
    assert calls == ([] if source == 'terminal' else ['kill', 'record', 'restart'])
    if source != 'terminal':
        assert (tmp_path / 'workspace_before_fault.json').exists()


@pytest.mark.asyncio
async def test_f1_fault_runs_while_input_post_is_pending(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    pending = asyncio.Event()
    observed = []
    class Client:
        def __init__(self, key): pass
        async def create_session(self, **kwargs): return {'id': 'sess', 'environment': {'id': 'env', 'remote_url': 'https://example.test'}}
        async def send_message(self, *args):
            observed.append('post_started')
            await pending.wait()
        async def retrieve_session(self, *args): return {'status': 'idle'}
        async def list_turns(self, *args): return [{'status': 'completed', 'subagent_id': None}]
        async def list_items(self, *args): return []
        async def delete_session(self, *args): pass
        async def aclose(self): pass
    class Executor:
        def __init__(self, *args, **kwargs): pass
        async def start(self, **kwargs): pass
        async def stop(self): pass
    class Recorder:
        def __init__(self, *args, **kwargs): self._task = None
        async def start(self): pass
        async def wait_for(self, pred, timeout):
            if not observed:
                return SimpleNamespace(type='agent.session.environment.connected')
            return SimpleNamespace(type='agent.session.turn.completed', raw={'turn': {'subagent_id': None}})
        async def stop(self): pass
    async def inject(*args, **kwargs):
        assert observed == ['post_started'] and not pending.is_set()
        observed.append('inject')
        pending.set()
        return {'triggered': True, 'marker_observed': False, 't_inject_ns': runner.Clock.monotonic_ns()}
    monkeypatch.setattr(runner, 'AgentsClient', Client)
    monkeypatch.setattr(runner, 'ExecutorSupervisor', Executor)
    monkeypatch.setattr(runner, 'EventRecorder', Recorder)
    monkeypatch.setattr(runner, '_inject_f1', inject)
    settings = Settings(openai_api_key=SecretStr('app'), openai_executor_api_key=SecretStr('executor'), crashtest_root=tmp_path)
    result = await runner.run_f0(settings, fault='F1')
    assert observed == ['post_started', 'inject']
    assert result['status'] == 'INCONCLUSIVE'


def test_f1_status_requires_evidence_and_attribution():
    from types import SimpleNamespace as NS
    record = {'triggered': True, 'marker_observed': True, 't_inject_ns': 10}
    survival = NS(never_created=[], unknown=[], lost=[], changed=[])
    metrics = NS(turn_status='completed', command_evidence_complete=True, survival=survival, missed_items_count=0)
    assert runner._f1_status(metrics, record, []) == 'WARN'
    metrics.turn_status = None
    assert runner._f1_status(metrics, record, []) == 'INCONCLUSIVE'
    metrics.turn_status = 'completed'
    metrics.command_evidence_complete = False
    assert runner._f1_status(metrics, record, []) == 'INCONCLUSIVE'
    metrics.command_evidence_complete = True
    survival.lost = ['step1.txt']
    assert runner._f1_status(metrics, record, [], stream_available=True) == 'FAIL'
    event = NS(recv_ns=11, item_type='command_execution', type='agent.session.turn.item.added')
    assert runner._f1_status(metrics, record, [event]) == 'WARN'
    metrics.missed_items_count = 1
    assert runner._f1_status(metrics, record, []) == 'WARN'


@pytest.mark.asyncio
async def test_f1_poll_error_does_not_abort_fallback(tmp_path):
    import asyncio
    class Client:
        async def list_items(self, session_id): raise RuntimeError('transient')
    class Recorder:
        async def wait_for(self, pred, timeout): await asyncio.Event().wait()
        def record_synthetic(self, *args): pass
    class Executor:
        async def kill(self): return 1
        async def restart(self): pass
    result = await runner._inject_f1(Client(), Recorder(), Executor(), 'sess', tmp_path, tmp_path, [], [], fallback_s=0.01, poll_s=0.001, restart_s=0)
    assert result['trigger_basis'] == 'after_seconds'
    assert result['triggered'] and not result['marker_observed']
