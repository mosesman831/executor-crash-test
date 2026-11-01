import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from crashtest.agents_api import AgentsAPIError, AgentsClient
from crashtest.sse import iter_sse, parse_event

BASE = 'https://api.openai.com/v1'


async def lines(text):
    for line in text.splitlines():
        yield line


@pytest.mark.asyncio
async def test_sse_comments_and_multiline():
    wire = ': keep-alive\n\nevent: message\ndata: {"type":"agent.session.idle","event_id":"e1"}\n\ndata: {"type":\ndata: "agent.session.idle","event_id":"e2"}\n\ndata: [DONE]\n\n'
    events = [parse_event(message) async for message in iter_sse(lines(wire))]
    assert [e['event_id'] for e in events if e] == ['e1', 'e2']
    assert events[-1] is None


@pytest.mark.asyncio
async def test_sse_bad_json():
    messages = [message async for message in iter_sse(lines('data: {oops\n\n'))]
    assert parse_event(messages[0])['type'] == 'crashtest.sse.bad_json'


@pytest.mark.asyncio
@respx.mock
async def test_send_message_path_header_and_retries_same_key(monkeypatch):
    async def no_sleep(_):
        pass
    monkeypatch.setattr('crashtest.agents_api.asyncio.sleep', no_sleep)
    route = respx.post(f'{BASE}/agents/sessions/sess_x/events').mock(side_effect=[httpx.Response(503), httpx.Response(202)])
    client = AgentsClient(SecretStr('secret'))
    try:
        assert await client.send_message('sess_x', 'hello', 'key-1') == 202
        assert route.call_count == 2
        for call in route.calls:
            request = call.request
            assert request.headers['OpenAI-Beta'] == 'agents=v1'
            assert request.headers['Idempotency-Key'] == 'key-1'
            assert json.loads(request.content)['events'][0]['type'] == 'agent.session.input.message'
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_create_session_not_retried():
    route = respx.post(f'{BASE}/agents/sessions').respond(500, json={'error': {'code': 'server_error'}})
    client = AgentsClient(SecretStr('secret'))
    try:
        with pytest.raises(AgentsAPIError):
            await client.create_session(model='model', instructions='instructions', workspace_directory='/tmp/ws', metadata={})
        assert route.call_count == 1
        assert 'input' not in json.loads(route.calls[0].request.content)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_list_items_pagination():
    route = respx.get(f'{BASE}/agents/sessions/sess_x/items').mock(side_effect=[
        httpx.Response(200, json={'data': [{'id': 'a'}], 'has_more': True, 'last_id': 'a'}),
        httpx.Response(200, json={'data': [{'id': 'b'}], 'has_more': False}),
    ])
    client = AgentsClient(SecretStr('secret'))
    try:
        assert [item['id'] for item in await client.list_items('sess_x')] == ['a', 'b']
        assert route.calls[1].request.url.params['after'] == 'a'
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_stream_400_retries_without_query():
    route = respx.get(f'{BASE}/agents/sessions/sess_x/events').mock(side_effect=[
        httpx.Response(400, json={'error': {'code': 'bad_query'}}),
        httpx.Response(200, text='data: {"type":"agent.session.idle"}\n\n'),
    ])
    client = AgentsClient(SecretStr('secret'))
    try:
        assert [event async for event in client.stream_events('sess_x')] == [{'type': 'agent.session.idle'}]
        assert route.call_count == 2
        assert route.calls[0].request.url.params['stream'] == 'true'
        assert 'stream' not in route.calls[1].request.url.params
        assert all(call.request.headers['accept'] == 'text/event-stream' for call in route.calls)
        assert client.stream_query_used is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_stream_fallback_error_body():
    route = respx.get(f'{BASE}/agents/sessions/sess_x/events').mock(side_effect=[
        httpx.Response(400), httpx.Response(400, json={'error': {'code': 'bad_request'}}),
    ])
    client = AgentsClient(SecretStr('secret'))
    try:
        with pytest.raises(AgentsAPIError) as caught:
            _ = [event async for event in client.stream_events('sess_x')]
        assert caught.value.code == 'bad_request'
        assert route.call_count == 2
        assert client.stream_query_used is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_decision_has_no_beta_header():
    route = respx.post(f'{BASE}/decisions').respond(200, json={'answers': []})
    client = AgentsClient(SecretStr('secret'))
    payload = {'model': 'gpt-6-luna', 'input': 'evidence', 'questions': []}
    try:
        assert await client.create_decision(payload) == {'answers': []}
        request = route.calls[0].request
        assert request.headers['authorization'] == 'Bearer secret'
        assert request.headers['content-type'] == 'application/json'
        assert 'openai-beta' not in request.headers
        assert json.loads(request.content) == payload
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('last_id', [None, '', '   ', 1])
@respx.mock
async def test_pagination_rejects_missing_cursor(last_id):
    respx.get(f'{BASE}/agents/sessions/sess_x/items').respond(200, json={'data': [{'id': 'a'}], 'has_more': True, 'last_id': last_id})
    client = AgentsClient(SecretStr('secret'))
    try:
        with pytest.raises(ValueError, match='advancing cursor'):
            await client.list_items('sess_x')
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_pagination_rejects_repeated_cursor():
    route = respx.get(f'{BASE}/agents/sessions/sess_x/items').mock(side_effect=[
        httpx.Response(200, json={'data': [{'id': 'a'}], 'has_more': True, 'last_id': 'a'}),
        httpx.Response(200, json={'data': [{'id': 'a'}], 'has_more': True, 'last_id': 'a'}),
    ])
    client = AgentsClient(SecretStr('secret'))
    try:
        with pytest.raises(ValueError, match='advancing cursor'):
            await client.list_items('sess_x')
        assert route.call_count == 2
    finally:
        await client.aclose()
