import pytest
from crashtest.hosted import _hosted_crash_evidence


def test_crash_evidence_uses_tool_output_not_user_prompt():
    trace = {'instructions': 'PARENT_NAME=codex SIGNALLING exec-server transport disconnected',
             'attributes': [{'key': 'gen_ai.tool.call.arguments', 'value': {'stringValue': 'PARENT_NAME SIGKILL'}}, {'key': 'openai.managed_agents.error.data', 'value': {'stringValue': '{"output":"PARENT_NAME=codex\\nSIGNALLING\\nexec-server transport disconnected","exit_code":-1}'}}]}
    evidence = _hosted_crash_evidence(trace, True)
    assert evidence['disconnect_observed']
    assert evidence['verified_parent_marker']
    assert evidence['signal_branch_marker']
    assert evidence['transport_disconnected']
    assert evidence['signal_completed'] is None
    empty = _hosted_crash_evidence({'prompt': trace['instructions']}, False)
    assert empty['signal_branch_marker'] is None
    assert empty['transport_disconnected'] is None


@pytest.mark.asyncio
async def test_trace_poll_waits_for_tool_output():
    from crashtest.hosted import _wait_for_crash_trace
    class Client:
        def __init__(self): self.calls = 0
        async def _pages(self, *args):
            self.calls += 1
            if self.calls == 1: return []
            return [{'attributes': [{'key': 'gen_ai.tool.call.arguments', 'value': {'stringValue': 'PARENT_NAME SIGKILL'}}, {'key': 'openai.managed_agents.error.data', 'value': {'stringValue': '{"output":"SIGNALLING"}'}}]}]
    client = Client()
    trace = await _wait_for_crash_trace(client, 'session', attempts=2, delay=0)
    assert client.calls == 2
    assert _hosted_crash_evidence(trace, True)['signal_branch_marker'] is True


@pytest.mark.asyncio
async def test_hosted_crash_replay_is_offline_and_inconclusive(tmp_path, monkeypatch):
    import json
    import socket
    from crashtest.replay import replay

    def deny_network(*args, **kwargs):
        raise AssertionError('replay must not use the network')

    monkeypatch.setattr(socket, 'socket', deny_network)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_EXECUTOR_API_KEY', raising=False)
    (tmp_path / 'run.json').write_text(json.dumps({'environment': 'openai_hosted', 'fault': 'F1', 'status': 'INCONCLUSIVE'}))
    (tmp_path / 'events.jsonl').write_text(json.dumps({'seq': 1, 'conn': 1, 'recv_ns': 1, 'recv_at': 1, 'type': 'agent.session.environment.disconnected', 'raw': {}}) + '\n')
    (tmp_path / 'traces.json').write_text(json.dumps({'attributes': [{'key': 'gen_ai.tool.call.arguments', 'value': {'stringValue': 'PARENT_NAME SIGKILL'}}, {'key': 'openai.managed_agents.error.data', 'value': {'stringValue': json.dumps({'output': 'PARENT_NAME=codex\nSIGNALLING\nexec-server transport disconnected'})}}]}))
    result = await replay(tmp_path, speed=1000)
    assert result['status'] == 'INCONCLUSIVE'
    assert result['environment'] == 'openai_hosted'
    assert any(c['name'] == 'transport_disconnected' and c['result'] == 'PASS' for c in result['checks'])
