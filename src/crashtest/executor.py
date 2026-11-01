from __future__ import annotations

import asyncio
import os
import pwd
import signal
import shutil
import subprocess
import tempfile
from enum import StrEnum
from pathlib import Path

from pydantic import SecretStr

from crashtest.redact import redact


class ExecutorState(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    KILLED = "killed"
    EXITED = "exited"


class ExecutorSupervisor:
    """Own one exec-server child and keep its application credentials separate."""

    def __init__(
        self,
        codex_bin: str,
        executor_key: SecretStr,
        log_path: Path,
        *,
        application_key: SecretStr | None = None,
        dedicated_user: str | None = None,
        child_path: str | None = None,
        exec_server_help_path: Path = Path("docs-exec-server-help.txt"),
    ) -> None:
        candidate = Path(codex_bin).expanduser()
        if candidate.is_absolute() or candidate.parent != Path("."):
            self.codex_bin = str(candidate.resolve())
        else:
            self.codex_bin = shutil.which(codex_bin) or codex_bin
        self._key = executor_key
        self._application_key = application_key
        self.log_path = Path(log_path)
        self.state = ExecutorState.NOT_STARTED
        self.restarts = 0
        self._proc: asyncio.subprocess.Process | None = None
        self._args: list[str] = []
        self._cwd: Path | None = None
        self._pump: asyncio.Task | None = None
        self._remote_url: str | None = None
        self._environment_id: str | None = None
        self._dedicated_user = dedicated_user
        if dedicated_user not in {None, "crashexec"}:
            raise ValueError("dedicated_user must be unset or 'crashexec'")
        self._exec_server_help_path = Path(exec_server_help_path)
        self._child_path = child_path if child_path is not None else os.environ.get("PATH", os.defpath)
        self._home: Path | None = None
        self._secrets = [executor_key.get_secret_value()]
        if application_key is not None:
            self._secrets.append(application_key.get_secret_value())

    def build_child_env(self, key_override: str | None = None) -> dict[str, str]:
        """Build a deliberately tiny environment and an isolated HOME.

        In particular, do not inherit the controller's HOME: Codex can read
        credentials and configuration from its home directory. The per-run
        directory is empty and mode 0700, while CODEX_API_KEY is the only key
        passed to the child.
        """
        home = self._ensure_home()
        key = key_override if key_override is not None else self._key.get_secret_value()
        return {"CODEX_API_KEY": key, "PATH": self._child_path, "HOME": str(home)}

    async def start(self, *, remote_url: str, environment_id: str, cwd: Path) -> int:
        if self.state == ExecutorState.RUNNING:
            raise RuntimeError("executor already running for this environment")
        self._remote_url = remote_url
        self._environment_id = environment_id
        self._cwd = Path(cwd)
        self._args = [
            self.codex_bin,
            "exec-server",
            "--remote",
            remote_url,
            "--environment-id",
            environment_id,
        ]
        if self._help_supports_exit_on_stdin_close():
            self._args.append("--exit-on-stdin-close")
        return await self._spawn()

    async def _spawn(self) -> int:
        if self._cwd is None or self._remote_url is None or self._environment_id is None:
            raise RuntimeError("executor has not been configured")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._args,
                cwd=str(self._cwd),
                env=self.build_child_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            self.state = ExecutorState.EXITED
            detail = redact(
                str(exc), self._secrets, sensitive_urls=[self._remote_url]
            )
            raise RuntimeError(f"could not start executor: {detail}") from None
        self.state = ExecutorState.RUNNING
        self._pump = asyncio.create_task(self._read_log())
        return self._proc.pid

    async def kill(self, sig: int = signal.SIGKILL, scope: str = "process_group") -> int:
        if self._proc is None or self._proc.pid is None:
            raise RuntimeError("no executor process")
        if scope not in {"process_group", "process"}:
            raise ValueError("scope must be 'process_group' or 'process'")
        killed_at = _monotonic_ns()
        try:
            if scope == "process_group":
                os.killpg(self._proc.pid, sig)
            else:
                self._proc.send_signal(sig)
        except ProcessLookupError:
            pass
        self.state = ExecutorState.KILLED
        if scope == "process_group" and self._dedicated_user is not None:
            self._kill_dedicated_user_processes()
        # Reap the old process before reporting the kill, so restart cannot
        # overlap the previous exec-server instance for this environment.
        await self._proc.wait()
        return killed_at

    async def restart(self, *, cwd: Path | None = None) -> int:
        if self._proc is not None and self._proc.returncode is None:
            raise RuntimeError("cannot restart while the previous executor is still running")
        if cwd is not None:
            self._cwd = Path(cwd)
        self.restarts += 1
        return await self._spawn()

    async def stop(self) -> None:
        if self._proc is None or self.state != ExecutorState.RUNNING:
            return
        # The installed exec-server supports this flag. Closing the parent
        # pipe asks it to exit cleanly before escalating to signals.
        if self._proc.stdin is not None:
            self._proc.stdin.close()
            try:
                await self._proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            await asyncio.wait_for(self._proc.wait(), 5.0)
        except TimeoutError:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), 2.0)
            except TimeoutError:
                try:
                    os.killpg(self._proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self._proc.wait()
                if self._dedicated_user is not None:
                    self._kill_dedicated_user_processes()
        self.state = ExecutorState.EXITED
        if self._pump is not None:
            await self._pump

    def _ensure_home(self) -> Path:
        if self._home is None:
            home = Path(tempfile.mkdtemp(prefix="crashtest-executor-home-"))
            os.chmod(home, 0o700)
            self._home = home
        return self._home

    def _help_supports_exit_on_stdin_close(self) -> bool:
        try:
            help_text = self._exec_server_help_path.read_text(encoding="utf-8")
        except OSError:
            return False
        return any(
            line.strip().startswith("--exit-on-stdin-close")
            for line in help_text.splitlines()
        )

    def _kill_dedicated_user_processes(self) -> None:
        """Use only a configured, non-root, non-controller uid and scoped pkill."""
        assert self._dedicated_user is not None
        try:
            account = pwd.getpwnam(self._dedicated_user)
        except KeyError as exc:
            raise RuntimeError("configured dedicated executor user does not exist") from exc
        if account.pw_uid in {0, os.getuid()}:
            raise RuntimeError("dedicated executor user must differ from root and controller")

        # Keep the kill command argv exact and uid-scoped. The application and
        # executor keys are not placed in this subprocess environment or argv.
        command = ["sudo", "-n", "pkill", "-KILL", "-u", self._dedicated_user]
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)
        if result.returncode not in {0, 1}:
            raise RuntimeError("could not kill configured dedicated executor processes")
        verify = subprocess.run(
            ["pgrep", "-u", self._dedicated_user],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if verify.returncode == 0:
            raise RuntimeError("dedicated executor processes remain after scoped kill")
        if verify.returncode != 1:
            raise RuntimeError("could not verify dedicated executor process cleanup")

    async def _read_log(self) -> None:
        assert self._proc and self._proc.stdout
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    return
                safe_line = redact(
                    line.decode("utf-8", "replace"),
                    self._secrets,
                    sensitive_urls=[self._remote_url] if self._remote_url else [],
                )
                handle.write(safe_line)
                handle.flush()


def _monotonic_ns() -> int:
    # Import at call time so tests can replace time.monotonic_ns cleanly.
    import time

    return time.monotonic_ns()
