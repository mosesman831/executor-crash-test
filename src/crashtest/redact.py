from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit


_URL_RE = re.compile(r"(?:https?|wss?)://[^\s\"'<>]+", re.IGNORECASE)


def redact(
    text: str,
    secrets: Iterable[str] = (),
    *,
    sensitive_urls: Iterable[str] = (),
) -> str:
    """Remove credentials and URL query/fragment data from text.

    Query strings are dropped from every URL in persisted output because a
    remote registration URL may carry a credential under an unknown name.
    Exact secrets are also replaced when they occur outside a URL.
    """
    # Strip URL data before exact-secret replacement. Replacing a secret first
    # could alter the middle of a known URL and leave trailing query fields.
    result = _URL_RE.sub(lambda match: _without_url_credentials(match.group(0)), text)

    # Explicit URLs also cover callers that know a URL is sensitive even if it
    # uses a non-HTTP form not matched by the general URL expression.
    for value in sensitive_urls:
        if not value:
            continue
        sanitized = _without_url_credentials(value)
        result = result.replace(value, sanitized)

    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")
    return result


def _without_url_credentials(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if parts.scheme.lower() not in {"http", "https", "ws", "wss"} or not parts.netloc:
        return value
    # Strip userinfo too: the executor's remote URL must not expose either
    # credentials or query tokens in logs or reports.
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
