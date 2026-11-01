import json
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from crashtest.cli import app
from crashtest.replay import replay

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'f1_demo'


@pytest.mark.asyncio
async def test_replay_timing_and_metrics_without_network(monkeypatch):
    delays = []
    async def sleep(delay): delays.append(delay)
    def reject(*args, **kwargs): raise AssertionError('network is forbidden')
    monkeypatch.setattr('crashtest.replay.asyncio.sleep', sleep)
    monkeypatch.setattr(socket.socket, 'connect', reject)
    monkeypatch.setattr(socket, 'getaddrinfo', reject)
    result = await replay(FIXTURE, speed=2)
    assert delays == [0.5] * 6
    assert result['mode'] == 'replay' and result['source'] == 'synthetic fixture'
    assert result['status'] == 'WARN'
    assert result['metrics']['contradiction'] is True
    assert result['metrics']['intermediate_events_missed'] is None


def test_cli_replay_needs_no_credentials_or_network(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_EXECUTOR_API_KEY', raising=False)
    monkeypatch.setattr('crashtest.cli.load_settings', lambda: (_ for _ in ()).throw(AssertionError('settings accessed')))
    def reject(*args, **kwargs): raise AssertionError('network is forbidden')
    monkeypatch.setattr(socket.socket, 'connect', reject)
    monkeypatch.setattr(socket, 'getaddrinfo', reject)
    result = CliRunner().invoke(app, ['replay', str(FIXTURE), '--speed', '1000000'])
    assert result.exit_code == 0, result.output
    assert 'mode: replay' in result.output and 'synthetic fixture' in result.output


@pytest.mark.asyncio
async def test_missing_evidence_inconclusive(tmp_path):
    (tmp_path / 'events.jsonl').write_text('')
    (tmp_path / 'run.json').write_text(json.dumps({'fault': 'F1'}))
    result = await replay(tmp_path)
    assert result['status'] == 'INCONCLUSIVE'
    assert 'items.json' in result['missing_evidence']
    assert 'confirmed fault trigger' in result['missing_evidence']


@pytest.mark.asyncio
@pytest.mark.parametrize('speed', [0, -1, float('inf'), float('nan')])
async def test_replay_rejects_invalid_speed(speed):
    with pytest.raises(ValueError):
        await replay(FIXTURE, speed=speed)


@pytest.mark.asyncio
async def test_f3_replay_preserves_recorded_reconstruction_mismatch(tmp_path):
    import hashlib
    import shutil
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    run = json.loads((tmp_path / 'run.json').read_text())
    run['fault'] = 'F3'
    (tmp_path / 'run.json').write_text(json.dumps(run))
    items = json.loads((tmp_path / 'items.json').read_text())
    items[0].update(status='completed', exit_code=0)
    (tmp_path / 'items.json').write_text(json.dumps(items))
    files = [{'path': name, 'sha256': hashlib.sha256(content).hexdigest()} for name, content in {'step1.txt': b'alpha', 'step2.txt': b'done\n', 'step3.txt': b'gamma'}.items()]
    (tmp_path / 'workspace_after.json').write_text(json.dumps(files))
    recovery = {'reconstructed_item_ids': ['cmd_1'], 'settle_item_ids': ['cmd_1', 'msg_1'], 'required_actions': []}
    (tmp_path / 'stream_recovery.json').write_text(json.dumps(recovery))
    result = await replay(tmp_path, speed=1_000_000)
    assert result['status'] == 'FAIL'
    assert result['metrics']['stream_recovery'] == recovery
    (tmp_path / 'stream_recovery.json').unlink()
    result = await replay(tmp_path, speed=1_000_000)
    assert result['status'] == 'INCONCLUSIVE'


@pytest.mark.parametrize(('status', 'exit_code'), [('PASS', 0), ('WARN', 0), ('FAIL', 1), ('ERROR', 2), ('INCONCLUSIVE', 3)])
def test_replay_cli_exit_status(monkeypatch, status, exit_code):
    async def fake_replay(*args, **kwargs):
        return {'status': status}
    monkeypatch.setattr('crashtest.cli.replay_run', fake_replay)
    result = CliRunner().invoke(app, ['replay', str(FIXTURE)])
    assert result.exit_code == exit_code


@pytest.mark.asyncio
async def test_f1_replay_preserves_recorded_failure(tmp_path):
    import shutil
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    run = json.loads((tmp_path / 'run.json').read_text())
    run['status'] = 'FAIL'
    (tmp_path / 'run.json').write_text(json.dumps(run))
    result = await replay(tmp_path, speed=1_000_000)
    assert result['status'] == 'FAIL'
    (tmp_path / 'items.json').unlink()
    result = await replay(tmp_path, speed=1_000_000)
    assert result['status'] == 'FAIL'
    assert 'items.json' in result['missing_evidence']
