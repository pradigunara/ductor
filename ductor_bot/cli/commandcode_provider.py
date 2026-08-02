"""Async wrapper around the Command Code CLI (``cmd`` / ``commandcode``).

Command Code headless surface (verified against v1.7.0):

* ``-p <prompt>`` one-shot query; when no query arg is given, stdin is
  auto-detected (piped input).
* ``--output-format json`` NDJSON stream: ``{"type":"event",...}`` frames
  followed by one final ``{"type":"result","subtype":...,"sessionId":...}``
  line.
* ``--continue`` resumes the most recent headless session in the working
  directory; ``--resume <sessionId>`` resumes a specific session (full id).
* ``--model`` / ``--effort`` / ``--max-turns``; ``--yolo`` bypasses all
  permission prompts, ``--auto-accept`` auto-approves tools.
* ``--list-models`` lists the available model catalog.

Reasoning effort is model-dependent: Command Code rejects unsupported levels
with ``Unknown effort "x". Supported: ...`` on stderr. The wrapper resolves
the requested effort to the closest supported level (preferring the higher
one on a tie, e.g. ``xhigh`` -> ``max`` on DeepSeek V4 Flash) and retries
once, caching the resolution per ``(model, requested)`` so default runs do
not fail-then-retry on every turn.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.base import BaseCLI, CLIConfig, add_cli_opt, docker_wrap, format_cli_cmd
from ductor_bot.cli.commandcode_events import (
    parse_commandcode_json,
    parse_commandcode_stream_line,
)
from ductor_bot.cli.executor import (
    SubprocessResult,
    SubprocessSpec,
    _default_post_handler,
    run_oneshot_subprocess,
    run_streaming_subprocess,
)
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    StreamEvent,
)
from ductor_bot.cli.types import CLIResponse

if TYPE_CHECKING:
    from ductor_bot.cli.timeout_controller import TimeoutController

logger = logging.getLogger(__name__)

# Command Code argv safety: prompts beyond this go via piped stdin instead of
# ``-p`` so we never hit the OS argv limit (mirrors Grok's --prompt-file).
_PROMPT_ARGV_SOFT_LIMIT = 24_000

# CLI names checked in order; both resolve to the same product.
_BINARY_NAMES = ("cmd", "commandcode")

# Canonical effort ladder for closest-match resolution. Ordering is used only
# for clamping; unknown levels sort after ``max`` (they are never requested by
# ductor, which validates against a fixed set first).
_EFFORT_ORDER: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# stderr / error-text shape when the CLI rejects an effort level:
#   Unknown effort "medium". Supported: high, max.
_EFFORT_REJECT_RE = re.compile(
    r'Unknown effort "([^"]+)".*?Supported:\s*([a-z,\s]+)',
    re.IGNORECASE,
)

# In-process cache: (model, requested_effort) -> resolved_effort. Keyed by
# model because supported levels are per-model; persists across turns so a
# default run never pays the reject-and-retry cost twice.
_EFFORT_CACHE: dict[tuple[str, str], str] = {}

# Default vision-bridge settings passed via --config unless the user overrides
# them in cli_parameters. image-vision lets a text-only model read attached
# images by describing them with the vision model; feature-model:vision picks
# that vision model. gpt-5.6-luna is OpenAI's cost-optimized model.
_DEFAULT_CONFIG_FLAGS: tuple[str, ...] = (
    "--config",
    "image-vision=enabled",
    "--config",
    "feature-model:vision=gpt-5.6-luna",
)


class CommandCodeCLI(BaseCLI):
    """Async wrapper around the Command Code CLI."""

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli = "cmd" if config.docker_container else self._find_cli()
        logger.info("Command Code CLI wrapper: cwd=%s model=%s", self._working_dir, config.model)

    @staticmethod
    def _find_cli() -> str:
        from ductor_bot.cli.commandcode_discovery import find_commandcode_cli

        path = find_commandcode_cli()
        if path:
            return path
        msg = (
            "Command Code CLI not found on PATH. "
            "Install via: https://commandcode.ai/docs/getting-started"
        )
        raise FileNotFoundError(msg)

    def _build_command(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        effort: str | None = None,
    ) -> tuple[list[str], str | None]:
        cfg = self._config
        cmd = [self._cli, "-p", "--output-format", "json"]

        # Permission handling: bypassPermissions maps to --yolo (full bypass),
        # anything else with approval semantics maps to --auto-accept.
        if cfg.permission_mode == "bypassPermissions":
            cmd.append("--yolo")
        elif cfg.permission_mode == "auto-accept":
            cmd.append("--auto-accept")

        add_cli_opt(cmd, "--model", cfg.model)
        if effort and effort != "default":
            add_cli_opt(cmd, "--effort", effort)
        add_cli_opt(cmd, "--max-turns", str(cfg.max_turns) if cfg.max_turns is not None else None)

        if resume_session:
            cmd += ["--resume", resume_session]
        elif continue_session:
            cmd.append("--continue")

        # System prompt override: Command Code has no --system-prompt flag; the
        # append_system_prompt is prepended to the prompt so ductor's workspace
        # context still reaches the model.
        full_prompt = prompt
        if cfg.append_system_prompt:
            full_prompt = f"{cfg.append_system_prompt}\n\n{prompt}"

        # Vision bridge defaults (--config image-vision + vision model). These
        # come before cli_parameters so explicit user flags override them
        # (argparse-style last-flag-wins).
        cmd.extend(_DEFAULT_CONFIG_FLAGS)

        if cfg.cli_parameters:
            cmd.extend(cfg.cli_parameters)

        # Long prompts are piped via stdin (the CLI auto-detects piped input
        # when no query argument is given). Short prompts go directly as -p.
        if len(full_prompt) > _PROMPT_ARGV_SOFT_LIMIT:
            return cmd, full_prompt
        return [*cmd, full_prompt], None

    def _resolved_effort(self, requested: str | None) -> str | None:
        """Resolve *requested* to a CLI-accepted effort for the active model.

        Uses the per-(model, requested) cache when available. Without a cache
        hit the requested value is returned unchanged; the reject-and-clamp
        retry happens in :meth:`send` / :meth:`send_streaming`.
        """
        if not requested or requested == "default":
            return None
        model = self._config.model or "<default>"
        return _EFFORT_CACHE.get((model, requested), requested)

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> CLIResponse:
        """Send a prompt and return the final result."""
        effort = self._resolved_effort(self._config.reasoning_effort)
        cmd, stdin_text = self._build_command(
            prompt, resume_session, continue_session, effort=effort
        )
        exec_cmd, use_cwd = docker_wrap(cmd, self._config, interactive=stdin_text is not None)
        _log_cmd(exec_cmd)
        response = await run_oneshot_subprocess(
            config=self._config,
            spec=SubprocessSpec(
                exec_cmd,
                use_cwd,
                prompt,
                timeout_seconds,
                timeout_controller,
                stdin_text=stdin_text,
            ),
            parse_output=_parse_response,
            provider_label="Command Code",
        )
        return await self._handle_effort_reject(
            response,
            prompt,
            resume_session,
            continue_session,
            timeout_seconds,
            timeout_controller,
            effort=effort,
        )

    async def _handle_effort_reject(  # noqa: PLR0913, PLR0917
        self,
        response: CLIResponse,
        prompt: str,
        resume_session: str | None,
        continue_session: bool,
        timeout_seconds: float | None,
        timeout_controller: TimeoutController | None,
        *,
        effort: str | None,
    ) -> CLIResponse:
        """Retry once with a clamped effort when the CLI rejected the level."""
        if effort is None or not response.is_error:
            return response
        supported = _supported_efforts_from_reject(response)
        if not supported:
            return response
        resolved = _closest_effort(effort, supported)
        model = self._config.model or "<default>"
        _EFFORT_CACHE[(model, effort)] = resolved
        if resolved == effort:
            return response

        logger.info(
            "Command Code effort %r unsupported for model %s; using closest %r",
            effort,
            model,
            resolved,
        )
        cmd, stdin_text = self._build_command(
            prompt, resume_session, continue_session, effort=resolved
        )
        exec_cmd, use_cwd = docker_wrap(cmd, self._config, interactive=stdin_text is not None)
        _log_cmd(exec_cmd)
        return await run_oneshot_subprocess(
            config=self._config,
            spec=SubprocessSpec(
                exec_cmd,
                use_cwd,
                prompt,
                timeout_seconds,
                timeout_controller,
                stdin_text=stdin_text,
            ),
            parse_output=_parse_response,
            provider_label="Command Code",
        )

    async def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Send a prompt and yield stream events as they arrive.

        Effort rejection retries once with the clamped level (same as
        :meth:`send`); the retry re-streams from scratch because the CLI
        rejects the whole run before producing any output. Non-effort
        failures are emitted once and stop.
        """
        effort = self._resolved_effort(self._config.reasoning_effort)
        for attempt in range(2 if effort else 1):
            events: list[StreamEvent] = []
            async for event in self._stream_once(
                prompt,
                resume_session,
                continue_session,
                timeout_seconds,
                timeout_controller,
                effort=effort,
            ):
                events.append(event)
                # On the first attempt, defer effort-reject errors (the retry
                # will suppress them); everything else streams through.
                if attempt == 0 and _is_effort_reject(event):
                    continue
                yield event

            if attempt < 1 and effort is not None:
                resolved = _retry_effort_from_stream(events, effort)
                if resolved is None:
                    return  # genuine failure: already emitted, do not retry
                model = self._config.model or "<default>"
                _EFFORT_CACHE[(model, effort)] = resolved
                logger.info(
                    "Command Code effort %r unsupported for model %s; using closest %r",
                    effort,
                    model,
                    resolved,
                )
                effort = resolved

    async def _stream_once(  # noqa: PLR0913
        self,
        prompt: str,
        resume_session: str | None,
        continue_session: bool,
        timeout_seconds: float | None,
        timeout_controller: TimeoutController | None,
        *,
        effort: str | None,
    ) -> AsyncGenerator[StreamEvent, None]:
        cmd, stdin_text = self._build_command(
            prompt, resume_session, continue_session, effort=effort
        )
        exec_cmd, use_cwd = docker_wrap(cmd, self._config, interactive=stdin_text is not None)
        _log_cmd(exec_cmd, streaming=True)

        accumulated: list[str] = []
        saw_result = False

        def _mark_result(event: StreamEvent) -> StreamEvent:
            """Track result frames and fill missing text from accumulated deltas."""
            nonlocal saw_result
            if isinstance(event, ResultEvent):
                saw_result = True
                # End frames sometimes omit the full text; fill from deltas.
                if not event.result and accumulated:
                    return event.model_copy(update={"result": "".join(accumulated)})
            return event

        async def _no_duplicate_error(result: SubprocessResult) -> AsyncGenerator[StreamEvent, None]:
            """Suppress the post-stream error ResultEvent.

            The CLI emits its own result frame on stdout for genuine failures;
            without this, the executor's post-stream error would duplicate it.
            """
            if not saw_result:
                async for event in _default_post_handler(result):
                    yield event

        async for event in run_streaming_subprocess(
            config=self._config,
            spec=SubprocessSpec(
                exec_cmd,
                use_cwd,
                prompt,
                timeout_seconds,
                timeout_controller,
                stdin_text=stdin_text,
            ),
            line_handler=_commandcode_line_handler,
            post_handler=_no_duplicate_error,
            provider_label="Command Code",
        ):
            if isinstance(event, AssistantTextDelta) and event.text:
                accumulated.append(event.text)
            yield _mark_result(event)
        if not saw_result and accumulated:
            yield ResultEvent(type="result", result="".join(accumulated), is_error=False)


async def _commandcode_line_handler(line: str) -> AsyncGenerator[StreamEvent, None]:
    """Parse a single Command Code NDJSON line into stream events."""
    for event in parse_commandcode_stream_line(line):
        yield event


def _supported_efforts_from_reject(response: CLIResponse) -> tuple[str, ...]:
    """Parse the ``Supported: ...`` list from a CLI effort rejection.

    Returns an empty tuple when the error does not look like an effort
    rejection, so callers leave the response untouched.
    """
    text = f"{response.result or ''}\n{response.stderr or ''}"
    match = _EFFORT_REJECT_RE.search(text)
    if not match:
        return ()
    supported = [
        level.strip().lower()
        for level in match.group(2).split(",")
        if level.strip()
    ]
    return tuple(supported)


def _is_effort_reject(event: StreamEvent) -> bool:
    """True when *event* is an error result caused by an unsupported effort."""
    if not isinstance(event, ResultEvent) or not event.is_error:
        return False
    return bool(_supported_efforts_from_reject(CLIResponse(result=event.result, is_error=True)))


def _retry_effort_from_stream(
    events: list[StreamEvent],
    effort: str,
) -> str | None:
    """Return the clamped effort when *events* indicate an effort rejection.

    The streaming path surfaces the CLI rejection as an error ``ResultEvent``
    whose text matches ``Unknown effort ... Supported: ...``. Returns ``None``
    when the run did not fail on effort, so callers keep the emitted error.
    """
    for event in events:
        if not isinstance(event, ResultEvent) or not event.is_error:
            continue
        supported = _supported_efforts_from_reject(
            CLIResponse(
                result=event.result,
                is_error=True,
                stderr="",
            )
        )
        if supported:
            resolved = _closest_effort(effort, supported)
            return resolved if resolved != effort else None
    return None


def _closest_effort(requested: str, supported: tuple[str, ...]) -> str:
    """Return the supported effort closest to *requested*.

    Unsupported levels are clamped to the nearest supported neighbour on the
    canonical ladder, preferring the higher one on a tie (``xhigh`` -> ``max``
    when only ``high``/``max`` are supported). Unknown requested levels map to
    the highest supported value.
    """
    if requested in supported:
        return requested
    if not supported:
        return requested

    rank = {level: index for index, level in enumerate(_EFFORT_ORDER)}
    requested_rank = rank.get(requested, len(_EFFORT_ORDER))
    best = supported[0]
    best_gap = float("inf")
    best_is_higher = False
    for candidate in supported:
        candidate_rank = rank.get(candidate, len(_EFFORT_ORDER))
        if candidate_rank == requested_rank:
            return candidate
        if requested_rank < candidate_rank:
            gap = candidate_rank - requested_rank
            higher = True
        else:
            gap = requested_rank - candidate_rank
            higher = False
        if gap < best_gap or (gap == best_gap and higher and not best_is_higher):
            best = candidate
            best_gap = gap
            best_is_higher = higher
    return best


def _log_cmd(cmd: list[str], *, streaming: bool = False) -> None:
    """Log the Command Code CLI command with truncated long values (no redaction)."""
    kind = "stream cmd" if streaming else "cmd"
    logger.info("CommandCode %s: %s", kind, format_cli_cmd(cmd, redact=False, opt_prefix="-"))


def _parse_response(stdout: bytes, stderr: bytes, returncode: int | None) -> CLIResponse:
    """Parse Command Code oneshot JSON into a CLIResponse."""
    stderr_text = stderr.decode(errors="replace")[:2000] if stderr else ""
    if stderr_text:
        logger.warning("Command Code stderr: %s", stderr_text[:500])

    raw = stdout.decode(errors="replace").strip()
    if not raw:
        logger.error("Command Code returned empty output (exit=%s)", returncode)
        return CLIResponse(
            result=stderr_text.strip(),
            is_error=True,
            returncode=returncode,
            stderr=stderr_text,
        )

    text, session_id, usage, num_turns, is_error = _parse_best_result(raw)
    if returncode not in (None, 0, 8):  # 8 = max-turns reached (partial result)
        is_error = True

    response = CLIResponse(
        session_id=session_id,
        result=text,
        is_error=is_error,
        returncode=returncode,
        stderr=stderr_text,
        num_turns=num_turns,
        usage=usage,
    )

    if response.is_error:
        logger.error("Command Code error: %s", (response.result or stderr_text)[:200])
    else:
        logger.info(
            "Command Code done session=%s turns=%s tokens=%d",
            (response.session_id or "?")[:8],
            response.num_turns,
            response.total_tokens,
        )
    return response


def _parse_best_result(
    raw: str,
) -> tuple[str, str | None, dict[str, object], int | None, bool]:
    """Parse raw stdout, preferring the last valid JSON result line.

    The CLI may print event frames and the final result line; when stdout is
    not NDJSON-shaped (text-mode leak, wrapped output), fall back to the last
    valid JSON line, then to plain text.
    """
    import json

    for line in reversed(raw.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            text, sid, usage, turns, is_err = parse_commandcode_json(candidate)
            return text, sid, usage, turns, is_err
    # No NDJSON result line — treat the whole stdout as plain text.
    return raw, None, {}, None, False
