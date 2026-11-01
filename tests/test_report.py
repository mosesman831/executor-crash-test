import json
import socket
from pathlib import Path

from typer.testing import CliRunner

from crashtest.cli import app
from crashtest.report import write_report


def test_report_escapes_and_redacts(tmp_path):
    record = {
        'mode': 'live', 'fault': 'F1', 'status': 'FAIL',
        'remote_url': 'https://remote.test/path?token=private-value',
        'metrics': {'turn_status': 'completed', 'reconnect_ms': None, 'reconnect_censored': True, 'reconnect_censor_ms': 3000, 'commands': [{'status': 'failed', 'exit_code': None, 'command': '<script>alert(1)</script> private-value'}]},
        'honesty': {'method': 'rules', 'final_text': '<script>alert(1)</script> https://example.test/path?auth=private-value'},
    }
    report = write_report(record, tmp_path, secrets=['private-value'])
    html = (tmp_path / 'report.html').read_text()
    raw = (tmp_path / 'report.json').read_text()
    assert '&lt;script&gt;' in html and '<script>' not in html
    assert 'private-value' not in html and 'private-value' not in raw
    assert '?auth=' not in html and 'remote.test' not in raw
    assert 'not reconnected by 3.0 s (censored)' in html
    assert report['runs'][0]['status'] == 'FAIL'
    assert 'Inter' not in html and 'spinner' not in html and 'cdn' not in html
    assert (tmp_path / 'report.html').stat().st_mode & 0o777 == 0o600


def test_missing_evidence_never_gets_score(tmp_path):
    report = write_report({'mode': 'replay', 'fault': 'F1', 'status': 'INCONCLUSIVE'}, tmp_path)
    assert report['score'] is None
    assert report['summary']['inconclusive'] == 1
    assert 'Essential evidence' in (tmp_path / 'report.html').read_text()


def test_offline_replay_writes_reports(tmp_path, monkeypatch):
    fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'f1_demo'
    def reject(*args, **kwargs): raise AssertionError('network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', reject)
    monkeypatch.setattr(socket, 'getaddrinfo', reject)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_EXECUTOR_API_KEY', raising=False)
    result = CliRunner().invoke(app, ['replay', str(fixture), '--speed', '1000000', '--output', str(tmp_path)])
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / 'report.json').read_text())
    assert report['runs'][0]['mode'] == 'replay'
    assert report['runs'][0]['source'] == 'synthetic fixture'
    assert 'replay' in (tmp_path / 'report.html').read_text()


def test_command_evidence_and_sha_survival(tmp_path):
    record = {
        'mode': 'live', 'fault': 'F0', 'status': 'FAIL',
        'commands': [{'command': 'seq 1 45', 'status': 'failed', 'exit_code': 42}],
        'metrics': {'survival': {'present_after': ['step1.txt'], 'lost': ['lost.txt'], 'changed': ['changed.txt'], 'never_created': ['missing.txt'], 'unknown': []}},
    }
    report = write_report(record, tmp_path)
    html = (tmp_path / 'report.html').read_text()
    assert report['runs'][0]['commands'][0]['exit_code'] == 42
    assert 'failed · exit 42 · seq 1 45' in html
    for value in ('SHA-256', 'step1.txt', 'lost.txt', 'changed.txt', 'missing.txt'):
        assert value in html


def test_replay_report_includes_saved_command(tmp_path):
    fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'f1_demo'
    result = CliRunner().invoke(app, ['replay', str(fixture), '--speed', '1000000', '--output', str(tmp_path)])
    assert result.exit_code == 0
    report = json.loads((tmp_path / 'report.json').read_text())
    assert report['runs'][0]['commands'][0]['status'] == 'incomplete'
    assert 'step1.txt' in report['runs'][0]['survival']['present_after']
