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


class RecoveryClient(Client):
    def __init__(self):
        super().__init__()
        self.session_started = asyncio.Event()
        self.allow_session = asyncio.Event()
        self.items_started = asyncio.Event()
        self.allow_items = asyncio.Event()
        self.event_yielded = asyncio.Event()
        self.list_calls = 0

    async def stream_events(self, session_id, *, ready):
        ready.set()
        while True:
            event = await self.events.get()
            self.event_yielded.set()
            yield event

    async def retrieve_session(self, session_id):
        self.session_started.set()
        await self.allow_session.wait()
        return {'status': 'active', 'required_actions': [{'type': 'form', 'id': 'form_1'}]}

    async def list_items(self, session_id):
        self.list_calls += 1
        self.items_started.set()
        await self.allow_items.wait()
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
async def test_recovery_buffers_updates_and_keeps_state_current_after_resume(tmp_path):
    client = RecoveryClient()
    recorder = EventRecorder(client, 'sess', tmp_path / 'events.jsonl', Clock())
    task = asyncio.create_task(recorder.recover_from_items())

    await client.session_started.wait()
    await client.events.put({
        'type': 'agent.session.turn.item.updated',
        'item_id': 'live',
        'item_type': 'command_execution',
        'status': 'in_progress',
        'command': 'buffered update one',
    })
    await client.event_yielded.wait()
    client.event_yielded.clear()
    # An update targeting a final saved item must be discarded.
    await client.events.put({
        'type': 'agent.session.turn.item.updated',
        'item': {'id': 'final', 'status': 'in_progress'},
    })
    await client.event_yielded.wait()

    client.allow_session.set()
    await client.items_started.wait()
    client.event_yielded.clear()
    await client.events.put({
        'type': 'agent.session.turn.item.updated',
        'item_id': 'live',
        'status': 'in_progress',
        'output': 'buffered update two',
    })
    await client.event_yielded.wait()
    client.allow_items.set()

    recovered = await task
    assert recovered is recorder.recovered_state
    assert recovered['final']['status'] == 'completed'
    assert recovered['live']['command'] == 'buffered update one'
    assert recovered['live']['output'] == 'buffered update two'
    assert recorder.recovery_discarded_final_updates == 1
    reopened = [event for event in recorder.events if event.type == 'crashtest.stream.reopened'][-1]
    assert reopened.raw['buffered_applied'] == 2
    assert reopened.raw['buffered_final_discarded'] == 1
    assert recorder.recovery_required_actions == [{'type': 'form', 'id': 'form_1'}]

    # Later live full-item events update the same state returned to the runner.
    client.event_yielded.clear()
    await client.events.put({
        'type': 'agent.session.turn.item.done',
        'item': {'id': 'live', 'type': 'command_execution', 'status': 'completed', 'exit_code': 0},
    })
    await client.event_yielded.wait()
    assert recorder.recovered_state['live']['status'] == 'completed'
    assert recorder.recovered_state['live']['exit_code'] == 0
    await recorder.stop()


@pytest.mark.asyncio
async def test_failed_session_event_stops_recovery_without_listing_items(tmp_path):
    client = RecoveryClient()
    client.events.put_nowait({'type': 'agent.session.failed', 'session_id': 'sess'})
    client.allow_session.set()
    recorder = EventRecorder(client, 'sess', tmp_path / 'events.jsonl', Clock())
    assert await recorder.recover_from_items() == {}
    assert recorder.session_failed is True
    assert client.list_calls == 0
    assert recorder._task is not None and recorder._task.done()
    assert recorder.events[-1].type == 'crashtest.stream.recovery_stopped'
    with pytest.raises(RuntimeError, match='session failed'):
        await recorder.start()


@pytest.mark.asyncio
async def test_recovery_retains_pre_drop_observed_items_missing_from_saved_snapshot(tmp_path):
    client = Client()
    client.open.set()
    recorder = EventRecorder(client, 'sess', tmp_path / 'events.jsonl', Clock())
    await recorder.start()
    await client.events.put({
        'type': 'agent.session.turn.item.done',
        'item': {
            'id': 'observed_before_drop',
            'type': 'command_execution',
            'status': 'completed',
            'exit_code': 0,
            'command': 'already observed before disconnection',
        },
    })
    observed = await recorder.wait_for(lambda event: event.item_id == 'observed_before_drop', 1)
    assert observed is not None

    await recorder.drop()
    recovered = await recorder.recover_from_items()
    assert 'observed_before_drop' in recovered
    assert recovered['observed_before_drop']['status'] == 'completed'
    # The recovered snapshot contains saved items plus local event evidence;
    # it does not fabricate a reasoning item that was never observed.
    assert set(recovered) == {'final', 'live', 'observed_before_drop'}
    await recorder.stop()


@pytest.mark.asyncio
async def test_failed_retrieved_session_stops_without_listing_items(tmp_path):
    class FailedClient(RecoveryClient):
        async def retrieve_session(self, session_id):
            self.session_started.set()
            await self.allow_session.wait()
            return {'status': 'failed', 'required_actions': []}

    client = FailedClient()
    client.allow_session.set()
    recorder = EventRecorder(client, 'sess', tmp_path / 'events.jsonl', Clock())
    assert await recorder.recover_from_items() == {}
    assert recorder.session_failed is True
    assert client.list_calls == 0


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
