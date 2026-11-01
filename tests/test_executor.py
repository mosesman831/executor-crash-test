import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from crashtest.executor import ExecutorState, ExecutorSupervisor
from crashtest.redact import redact


def test_redact_removes_exact_secrets_and_url_query_data():
    text = (
        "token=app-secret url=wss://example.test/register?key=remote-secret&x=1 "
        "https://example.test/path#fragment"
    )
    safe = redact(text, ["app-secret", "remote-secret"])
    assert "app-secret" not in safe
    assert "remote-secret" not in safe
    assert "?key=" not in safe
    assert "&x=1" not in safe
    assert "#fragment" not in safe
    assert "wss://example.test/register" in safe


def test_redact_sensitive_non_http_url():
    url = "wss://environment.test/connect?token=private"
    assert redact(f"remote={url}", [], sensitive_urls=[url]) == "remote=wss://environment.test/connect"


def test_child_environment_is_exact_and_does_not_use_parent_home(tmp_path, monkeypatch):
    parent_home = tmp_path / "parent-home"
    (parent_home / ".codex").mkdir(parents=True)
    (parent_home / ".codex" / "auth.json").write_text(
        '{"OPENAI_API_KEY":"application-secret"}', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(parent_home))
    monkeypatch.setenv("OPENAI_API_KEY", "application-secret")
    monkeypatch.setenv("OPENAI_EXECUTOR_API_KEY", "executor-secret")
    monkeypatch.setenv("CODEX_API_KEY", "attacker-controlled")
    monkeypatch.setenv("LANG", "en_GB.UTF-8")

    supervisor = ExecutorSupervisor(
        "/bin/true",
        SecretStr("executor-secret"),
        tmp_path / "run" / "executor.log",
        application_key=SecretStr("application-secret"),
    )
    child = supervisor.build_child_env()
    assert set(child) == {"CODEX_API_KEY", "PATH", "HOME"}
    assert child["CODEX_API_KEY"] == "executor-secret"
    assert child["PATH"] == os.environ["PATH"]
    isolated_home = Path(child["HOME"])
    assert isolated_home != parent_home
    assert isolated_home.stat().st_mode & 0o777 == 0o700
    assert not (isolated_home / ".codex" / "auth.json").exists()
    assert "application-secret" not in repr(child)


def test_codex_command_is_resolved_from_path_and_only_crashexec_is_accepted(tmp_path, monkeypatch):
    codex_path = tmp_path / "bin" / "codex"
    monkeypatch.setattr("crashtest.executor.shutil.which", lambda name: str(codex_path) if name == "codex" else None)
    supervisor = ExecutorSupervisor("codex", SecretStr("key"), tmp_path / "log")
    assert supervisor.codex_bin == str(codex_path)
    with pytest.raises(ValueError, match="crashexec"):
        ExecutorSupervisor("codex", SecretStr("key"), tmp_path / "log", dedicated_user="ubuntu")


@pytest.mark.asyncio
async def test_start_uses_isolated_env_and_supported_exec_server_flags(tmp_path, monkeypatch):
    help_path = tmp_path / "docs-exec-server-help.txt"
    help_path.write_text("  --exit-on-stdin-close\n", encoding="utf-8")
    supervisor = ExecutorSupervisor(
        "/bin/true",
        SecretStr("executor-secret"),
        tmp_path / "run" / "executor.log",
        application_key=SecretStr("application-secret"),
        exec_server_help_path=help_path,
    )
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=4321, stdout=asyncio.StreamReader(), returncode=None)

    async def no_log():
        return None

    monkeypatch.setattr("crashtest.executor.asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(supervisor, "_read_log", no_log)
    pid = await supervisor.start(
        remote_url="wss://example.test/register?token=remote-secret",
        environment_id="env_test",
        cwd=tmp_path,
    )
    assert pid == 4321
    assert supervisor.state == ExecutorState.RUNNING
    assert captured["args"][1:2] == ("exec-server",)
    assert "--remote" in captured["args"]
    assert "--environment-id" in captured["args"]
    assert "--exit-on-stdin-close" in captured["args"]
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.PIPE
    child = captured["kwargs"]["env"]
    assert set(child) == {"CODEX_API_KEY", "PATH", "HOME"}
    assert child["CODEX_API_KEY"] == "executor-secret"
    assert "application-secret" not in repr(child)
    assert "remote-secret" not in repr(child)
    assert child["HOME"] != os.environ.get("HOME")
    assert supervisor._pump is not None
    await supervisor._pump


@pytest.mark.asyncio
async def test_start_omits_unverified_stdin_close_flag(tmp_path, monkeypatch):
    supervisor = ExecutorSupervisor(
        "/bin/true",
        SecretStr("executor-secret"),
        tmp_path / "run" / "executor.log",
        exec_server_help_path=tmp_path / "missing-help.txt",
    )
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=4322, stdout=asyncio.StreamReader(), returncode=None)

    async def no_log():
        return None

    monkeypatch.setattr("crashtest.executor.asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(supervisor, "_read_log", no_log)
    await supervisor.start(remote_url="wss://example.test/register", environment_id="env", cwd=tmp_path)
    assert "--exit-on-stdin-close" not in captured["args"]
    await supervisor._pump


@pytest.mark.asyncio
async def test_start_failure_does_not_leak_remote_query_or_keys(tmp_path, monkeypatch):
    supervisor = ExecutorSupervisor(
        "/bin/true",
        SecretStr("executor-secret"),
        tmp_path / "run" / "executor.log",
        application_key=SecretStr("application-secret"),
    )

    async def fail(*_args, **_kwargs):
        raise OSError("failed wss://example.test/register?token=remote-secret application-secret executor-secret")

    monkeypatch.setattr("crashtest.executor.asyncio.create_subprocess_exec", fail)
    with pytest.raises(RuntimeError) as caught:
        await supervisor.start(
            remote_url="wss://example.test/register?token=remote-secret",
            environment_id="env_test",
            cwd=tmp_path,
        )
    message = str(caught.value)
    assert "remote-secret" not in message
    assert "application-secret" not in message
    assert "executor-secret" not in message
    assert "?token=" not in message


@pytest.mark.asyncio
async def test_log_is_private_and_redacts_keys_and_url_queries(tmp_path):
    remote_url = "wss://example.test/register?token=remote-secret"
    supervisor = ExecutorSupervisor(
        "/bin/true",
        SecretStr("executor-secret"),
        tmp_path / "run" / "executor.log",
        application_key=SecretStr("application-secret"),
    )

    class Output:
        def __init__(self):
            self.lines = [
                b"application-secret executor-secret "
                b"wss://example.test/register?token=remote-secret&other=1\n",
                b"",
            ]

        async def readline(self):
            return self.lines.pop(0)

    supervisor._remote_url = remote_url
    supervisor._proc = SimpleNamespace(stdout=Output())
    await supervisor._read_log()
    log = supervisor.log_path.read_text(encoding="utf-8")
    assert "application-secret" not in log
    assert "executor-secret" not in log
    assert "remote-secret" not in log
    assert "?token=" not in log
    assert "&other=1" not in log
    assert supervisor.log_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_kill_uses_process_group_and_monotonic_timestamp_without_user_pkills(monkeypatch):
    supervisor = ExecutorSupervisor("/bin/true", SecretStr("key"), Path("/tmp/log"))

    class Process:
        pid = 123
        returncode = -9

        async def wait(self):
            return self.returncode

        def send_signal(self, _sig):
            return None

    supervisor._proc = Process()
    supervisor.state = ExecutorState.RUNNING
    seen = []
    monkeypatch.setattr("crashtest.executor.os.killpg", lambda pid, sig: seen.append((pid, sig)))
    monkeypatch.setattr("crashtest.executor._monotonic_ns", lambda: 987654321)
    assert await supervisor.kill() == 987654321
    assert seen == [(123, 9)]
    assert supervisor.state == ExecutorState.KILLED


def test_redact_unlisted_key_pattern():
    prefix = "s" + "k-"
    assert redact(prefix + "a" * 16 + " " + prefix + "short") == "[REDACTED] " + prefix + "short"
