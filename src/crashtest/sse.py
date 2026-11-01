from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class SSEMessage:
    event: str | None
    data: str
    id: str | None


async def iter_sse(lines: AsyncIterator[str]) -> AsyncIterator[SSEMessage]:
    event: str | None = None
    data: list[str] = []
    msg_id: str | None = None
    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data:
                yield SSEMessage(event, "\n".join(data), msg_id)
            event, data, msg_id = None, [], None
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
        elif field == "id":
            msg_id = value
    if data:
        yield SSEMessage(event, "\n".join(data), msg_id)


def parse_event(msg: SSEMessage) -> dict | None:
    if not msg.data or msg.data == "[DONE]":
        return None
    try:
        payload = json.loads(msg.data)
    except json.JSONDecodeError:
        return {"type": "crashtest.sse.bad_json", "data": msg.data[:500]}
    if isinstance(payload, dict) and "type" in payload:
        return payload
    return {"type": "crashtest.sse.no_type", "data": payload}
