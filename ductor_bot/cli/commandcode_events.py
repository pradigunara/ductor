"""NDJSON parser for the Command Code CLI (``cmd`` / ``commandcode``).

Headless mode (``cmd -p --output-format json``) emits newline-delimited
JSON in two shapes:

* ``{"type": "event", "event": {...}}`` — one per ``AgentEvent`` as the run
  progresses (``text_delta``, ``thinking_delta``, ``tool_running``, ...).
* ``{"type": "result", "subtype": "success"|"error"|"max_turns", ...}`` —
  exactly one final line carrying ``sessionId``, ``stopReason``, ``usage``,
  ``durationMs``, and ``finalText``.

See https://commandcode.ai/docs/headless for the full spec.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    StreamEvent,
    SystemInitEvent,
    SystemStatusEvent,
    ThinkingEvent,
    ToolUseEvent,
)

logger = logging.getLogger(__name__)

# Result subtypes treated as successful completion.
_OK_RESULT_SUBTYPES = frozenset({"success", "max_turns"})


def parse_commandcode_json(raw: str) -> tuple[str, str | None, dict[str, Any], int | None, bool]:
    """Parse the final headless result line (oneshot ``--output-format json``).

    Returns ``(text, session_id, usage, num_turns, is_error)``.
    """
    stripped = raw.strip()
    if not stripped:
        return "", None, {}, None, True

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("Command Code: unparseable JSON envelope, treating as plain text")
        return stripped, None, {}, None, False

    if not isinstance(data, dict):
        return str(data), None, {}, None, False

    # A run that failed before any session resolved still emits a result line.
    subtype = str(data.get("subtype") or "").lower()
    is_error = subtype not in _OK_RESULT_SUBTYPES
    text = _as_str(
        data.get("finalText")
        or data.get("text")
        or data.get("result")
        or data.get("error")
        or ""
    )
    session_id = _as_str(data.get("sessionId") or "") or None
    usage = data["usage"] if isinstance(data.get("usage"), dict) else {}
    num_turns = data.get("turnCount")
    if not isinstance(num_turns, int):
        num_turns = data.get("turns")
    if not isinstance(num_turns, int):
        num_turns = None
    return text, session_id, usage, num_turns, is_error


def parse_commandcode_stream_line(line: str) -> list[StreamEvent]:  # noqa: PLR0911
    """Parse a single Command Code NDJSON line into stream events."""
    stripped = line.strip()
    if not stripped:
        return []

    try:
        payload: Any = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("Command Code: unparseable stream line: %.200s", stripped)
        return []

    if not isinstance(payload, dict):
        return []
    data: dict[str, Any] = payload

    if data.get("type") == "event":
        event = data.get("event")
        if isinstance(event, dict):
            return _parse_event_frame(event)
        return []

    if data.get("type") == "result":
        return _parse_result_frame(data)

    return []


def _parse_event_frame(event: dict[str, Any]) -> list[StreamEvent]:
    """Map an ``AgentEvent`` frame to ductor stream events."""
    event_type = str(event.get("type") or "")
    handler = _EVENT_FRAME_HANDLERS.get(event_type)
    if handler is not None:
        return handler(event)
    if event_type in ("turn_start", "message_start", "model_request_start", "model_trace"):
        # Progress frames without user-visible content.
        return []
    # Unknown frames are forward-compatible: ignore rather than fail.
    logger.debug("Command Code: ignoring event type=%s", event_type)
    return []


def _frame_run_start(event: dict[str, Any]) -> list[StreamEvent]:
    return [
        SystemInitEvent(
            type="system",
            subtype="init",
            session_id=_as_str(event.get("sessionId")) or None,
        )
    ]


def _frame_text_delta(event: dict[str, Any]) -> list[StreamEvent]:
    delta = _as_str(event.get("delta") or "")
    return [AssistantTextDelta(type="assistant", text=delta)] if delta else []


def _frame_thinking(event: dict[str, Any]) -> list[StreamEvent]:
    delta = _as_str(event.get("delta") or event.get("text") or "")
    return [ThinkingEvent(type="assistant", text=delta)] if delta else []


def _frame_tool(event: dict[str, Any]) -> list[StreamEvent]:
    return [
        ToolUseEvent(
            type="assistant",
            tool_name=_as_str(event.get("toolName") or "tool"),
            tool_id=_as_str(event.get("toolCallId")) or None,
            parameters=event.get("input") if isinstance(event.get("input"), dict) else None,
        )
    ]


def _frame_run_end(_event: dict[str, Any]) -> list[StreamEvent]:
    # ``run_end`` is a run summary, not a terminal result: it carries no
    # ``subtype`` and its sessionId is nested under ``nextState``. The
    # authoritative ``{"type":"result",...}`` line always follows it, so
    # ignore this frame to avoid a spurious error result.
    return []


def _frame_model_request_end(event: dict[str, Any]) -> list[StreamEvent]:
    stop_reason = _as_str(event.get("stopReason") or "")
    if stop_reason and any(tok in stop_reason.lower() for tok in ("error", "fail", "cancel")):
        return [SystemStatusEvent(type="system", subtype="status", status="error")]
    return []


_EVENT_FRAME_HANDLERS: dict[str, Callable[[dict[str, Any]], list[StreamEvent]]] = {
    "run_start": _frame_run_start,
    "text_delta": _frame_text_delta,
    "thinking_start": _frame_thinking,
    "thinking_delta": _frame_thinking,
    "thinking_end": _frame_thinking,
    "tool_queued": _frame_tool,
    "tool_running": _frame_tool,
    "tool_completed": _frame_tool,
    "run_end": _frame_run_end,
    "model_request_end": _frame_model_request_end,
}


def _parse_result_frame(data: dict[str, Any]) -> list[StreamEvent]:
    """Build stream events from a final result frame."""
    subtype = str(data.get("subtype") or "").lower()
    is_error = subtype not in _OK_RESULT_SUBTYPES
    text = _as_str(
        data.get("finalText")
        or data.get("text")
        or data.get("result")
        or data.get("error")
        or ""
    )
    session_id = _as_str(data.get("sessionId") or "") or None
    usage = data["usage"] if isinstance(data.get("usage"), dict) else {}

    num_turns: int | None = None
    raw_turns = data.get("turnCount")
    if isinstance(raw_turns, int):
        num_turns = raw_turns

    duration_ms: float | None = None
    raw_duration = data.get("durationMs")
    if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool):
        duration_ms = float(raw_duration)

    stop_reason = _as_str(data.get("stopReason") or "")
    events: list[StreamEvent] = []
    if subtype == "max_turns" or stop_reason == "max_turns":
        events.append(
            SystemStatusEvent(type="system", subtype="status", status="max_turns_reached")
        )
    elif is_error:
        events.append(SystemStatusEvent(type="system", subtype="status", status="error"))
    events.append(
        ResultEvent(
            type="result",
            subtype=subtype,
            session_id=session_id,
            result=text,
            is_error=is_error,
            duration_ms=duration_ms,
            usage=usage,
            num_turns=num_turns,
        )
    )
    return events


def _as_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)
