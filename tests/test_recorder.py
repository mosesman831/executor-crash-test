import asyncio
import json

import pytest

from crashtest.clock import Clock
from crashtest.recorder import EventRecorder, is_root_terminal


class Client:
    def __init__(self):
        self.open = asyncio.Event()
        self.events = asyncio.Queue()

    async def stream_events(self, session_id, *, ready):
        await self.open.wait()
        ready.set()
        while True:
            yield await self.events.get()

    async def retrieve_session(self, session_id):
        return {'status': 'active', 'required_actions': [{'type': 'form'}]}

    async def list_items(self, session_id):
        return [{'id': 'final', 'status': 'completed'}, {'id': 'live', 'status': 'in_progress'}]


@pytest.mark.asyncio
async def test_start_waits_for_open_and_persists_redacted(tmp_path):
    client = Client()
    url = 'https://example.test/path?token=secret-value'
    recorder = EventRecorder(client, 'sess', tmp_path / 'events.jsonl', Clock(), secrets=['secret-value'], sensitive_urls=[url])
    task = asyncio.create_task(recorder.start())
    await asyncio.sleep(0)
    assert not task.done()
    client.open.set()
    await task
    await client.events.put({'type': 'agent.session.idle', 'url': url, 'data': 'secret-value'})
    event = await recorder.wait_for(lambda e: e.type == 'agent.session.idle', 1)
    assert event is not None
    assert 'secret-value' not in json.dumps(event.raw)
    assert 'secret-value' not in recorder.out.read_text()
    assert len(recorder.out.read_text().splitlines()) == 1
    await recorder.stop()


@pytest.mark.asyncio
async def test_recovery_restores_final_and_in_progress(tmp_path):
    client = Client()
    client.open.set()
    recorder = EventRecorder(client, 'sess', tmp_path / 'events.jsonl', Clock())
    recorder._apply_buffered(state := {'final': {'id': 'final', 'status': 'completed'}}, {'item': {'id': 'final', 'status': 'in_progress'}})
    assert state['final']['status'] == 'completed'
    recorder._apply_buffered(state, {'item': {'id': 'live', 'status': 'completed'}})
    assert state['live']['status'] == 'completed'
    recovered = await recorder.recover_from_items()
    assert set(recovered) == {'final', 'live'}
    assert recorder.events[-1].raw['required_actions'] == [{'type': 'form'}]
    await recorder.stop()


@pytest.mark.asyncio
async def test_root_terminal_ignores_subagent(tmp_path):
    recorder = EventRecorder(Client(), 'sess', tmp_path / 'events.jsonl', Clock())
    sub = recorder.record_synthetic('agent.session.turn.completed', {'turn': {'subagent_id': 'sub'}})
    root = recorder.record_synthetic('agent.session.turn.completed', {'turn': {'subagent_id': None}})
    assert not is_root_terminal(sub)
    assert is_root_terminal(root)


@pytest.mark.parametrize("existing", [False, True])
def test_event_log_is_private(tmp_path, existing):
    path = tmp_path / 'events.jsonl'
    if existing:
        path.write_text('')
        path.chmod(0o644)
    recorder = EventRecorder(Client(), 'sess', path, Clock())
    recorder.record_synthetic('crashtest.test', {})
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())['type'] == 'crashtest.test'
