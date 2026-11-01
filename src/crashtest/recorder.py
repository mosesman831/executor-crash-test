from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path

from crashtest.agents_api import AgentsClient
from crashtest.clock import Clock
from crashtest.models import RecordedEvent
from crashtest.redact import redact

FINAL_STATUS = {"completed", "failed", "incomplete", "cancelled"}
TERMINAL_TURN = {
    "agent.session.turn.completed",
    "agent.session.turn.failed",
    "agent.session.turn.cancelled",
}


class EventRecorder:
    def __init__(self, client: AgentsClient, session_id: str, out: Path, clock: Clock, *, secrets: list[str] | None = None, sensitive_urls: list[str] | None = None) -> None:
        self.client = client
        self.session_id = session_id
        self.out = out
        self.clock = clock
        self.secrets = secrets or []
        self.sensitive_urls = sensitive_urls or []
        self.events: list[RecordedEvent] = []
        self._queues: list[asyncio.Queue[RecordedEvent]] = []
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.conn = 0
        self._buffer: list[dict] = []
        self._buffering = False

    def subscribe(self) -> asyncio.Queue[RecordedEvent]:
        queue: asyncio.Queue[RecordedEvent] = asyncio.Queue()
        self._queues.append(queue)
        return queue

    async def start(self) -> None:
        self.conn += 1
        self._stop.clear()
        ready = asyncio.Event()
        self._task = asyncio.create_task(self._read(self.conn, ready))
        ready_wait = asyncio.create_task(ready.wait())
        try:
            done, _ = await asyncio.wait((ready_wait, self._task), return_when=asyncio.FIRST_COMPLETED)
            if self._task in done and not ready.is_set():
                await self._task
                raise RuntimeError("event stream closed before opening")
        finally:
            ready_wait.cancel()
            if not ready.is_set() and self._task and not self._task.done():
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def drop(self) -> int:
        """F3: hard-close the reader. Returns monotonic ns."""
        t = self.clock.monotonic_ns()
        await self.stop()
        self.record_synthetic("crashtest.stream.dropped", {"conn": self.conn, "t_ns": t})
        return t

    def record_synthetic(self, event_type: str, data: dict) -> RecordedEvent:
        recorded = RecordedEvent(
            seq=len(self.events) + 1,
            conn=self.conn,
            recv_ns=self.clock.monotonic_ns(),
            recv_at=self.clock.wall(),
            type=event_type,
            event_id=None,
            session_id=self.session_id,
            turn_id=None,
            item_id=None,
            item_type=None,
            raw=data,
            synthetic=True,
        )
        return self._persist(recorded)

    async def wait_for(
        self,
        pred: Callable[[RecordedEvent], bool],
        timeout: float,
    ) -> RecordedEvent | None:
        queue = self.subscribe()
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            for existing in self.events:
                if pred(existing):
                    return existing
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    event = await asyncio.wait_for(queue.get(), remaining)
                except TimeoutError:
                    return None
                if pred(event):
                    return event
        finally:
            self._queues.remove(queue)

    @property
    def seen_item_ids(self) -> set[str]:
        found: set[str] = set()
        for event in self.events:
            if event.item_id and not event.synthetic:
                found.add(event.item_id)
        return found

    async def recover_from_items(self) -> dict[str, dict]:
        """Documented recovery. Do not resend the task.

        1. Open a new stream and buffer.
        2. Retrieve session and items while the stream stays connected.
        3. Restore local state keyed by item id.
        4. Apply buffered updates whose items are not already final.
        5. Resume live handling.
        """
        self._buffer = []
        self._buffering = True
        await self.start()
        session = await self.client.retrieve_session(self.session_id)
        if session.get("status") == "failed":
            self.record_synthetic(
                "crashtest.stream.recovery_stopped",
                {"reason": "session_failed"},
            )
            self._buffering = False
            return {}
        items = await self.client.list_items(self.session_id)
        state = {item["id"]: item for item in items if item.get("id")}
        buffered = list(self._buffer)
        self._buffer = []
        self._buffering = False
        for event in buffered:
            self._apply_buffered(state, event)
        self.record_synthetic(
            "crashtest.stream.reopened",
            {
                "conn": self.conn,
                "restored_items": len(state),
                "buffered_applied": len(buffered),
                "required_actions": session.get("required_actions") or [],
            },
        )
        return state

    def _apply_buffered(self, state: dict[str, dict], event: dict) -> None:
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        item_id = (item or {}).get("id") or event.get("item_id")
        if not item_id:
            return
        current = state.get(item_id)
        if current and _is_final(current):
            return
        if item:
            state[item_id] = item
        elif item_id in state and event.get("type", "").endswith(".delta"):
            # Deltas are not a full item. Keep the saved item.
            return

    async def _read(self, conn: int, ready: asyncio.Event) -> None:
        try:
            async for event in self.client.stream_events(self.session_id, ready=ready):
                if self._stop.is_set():
                    return
                recorded = self._persist(self._wrap(event, conn))
                if self._buffering:
                    self._buffer.append(recorded.raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.record_synthetic(
                "crashtest.stream.error",
                {"conn": conn, "error": type(exc).__name__},
            )
            if not ready.is_set():
                raise

    def _wrap(self, event: dict, conn: int) -> RecordedEvent:
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_id = item.get("id") or event.get("item_id")
        return RecordedEvent(
            seq=len(self.events) + 1,
            conn=conn,
            recv_ns=self.clock.monotonic_ns(),
            recv_at=self.clock.wall(),
            type=str(event.get("type") or "unknown"),
            event_id=event.get("event_id"),
            session_id=event.get("session_id") or self.session_id,
            turn_id=event.get("turn_id"),
            item_id=item_id,
            item_type=item.get("type"),
            raw=event,
            synthetic=False,
        )

    def _persist(self, recorded: RecordedEvent) -> RecordedEvent:
        recorded = RecordedEvent.model_validate_json(redact(recorded.model_dump_json(), self.secrets, sensitive_urls=self.sensitive_urls))
        self.events.append(recorded)
        self.out.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(recorded.model_dump_json())
            handle.write("\n")
            handle.flush()
        for queue in self._queues:
            queue.put_nowait(recorded)
        return recorded


def _is_final(item: dict) -> bool:
    status = item.get("status")
    return isinstance(status, str) and status in FINAL_STATUS


def is_root_terminal(event: RecordedEvent) -> bool:
    if event.type not in TERMINAL_TURN:
        return False
    turn = event.raw.get("turn") if isinstance(event.raw.get("turn"), dict) else {}
    return turn.get("subagent_id") in (None, "")
