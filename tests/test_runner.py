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
