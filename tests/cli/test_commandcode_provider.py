"""Unit tests for Command Code provider event parsing, command building, and effort clamping."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.commandcode_events import (
    parse_commandcode_json,
    parse_commandcode_stream_line,
)
from ductor_bot.cli.commandcode_provider import (
    CommandCodeCLI,
    _closest_effort,
    _parse_response,
    _supported_efforts_from_reject,
)
from ductor_bot.cli.factory import create_cli
from ductor_bot.cli.param_resolver import TaskExecutionConfig
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    SystemInitEvent,
    SystemStatusEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import ModelRegistry
from ductor_bot.cron.execution import _build_commandcode_cmd, parse_commandcode_result

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "commandcode"


def _fixture(name: str) -> Path:
    return _FIXTURES / name


class TestGoldenFiles:
    """Regression tests against real `cmd` v1.7.0 captured output.

    The fixtures in tests/fixtures/commandcode/ are verbatim captures from the
    real CLI (see docs/commandcode-fork-notes.md §"Captured CLI output").
    """

    def test_golden_success_stream_parses(self) -> None:
        events: list[object] = []
        for line in _fixture("success.ndjson").read_text().splitlines():
            events.extend(parse_commandcode_stream_line(line))
        # Final authoritative result line carries session + finalText.
        results = [e for e in events if isinstance(e, ResultEvent)]
        assert len(results) == 1
        assert results[0].result == "PONG"
        assert results[0].is_error is False
        # run_start produced a SystemInitEvent; its session id must match the
        # result line's (self-consistency, not a pinned UUID so re-capturing
        # the fixture does not force an edit here).
        inits = [e for e in events if isinstance(e, SystemInitEvent)]
        assert len(inits) == 1
        assert inits[0].session_id == results[0].session_id

    def test_golden_success_frame_inventory(self) -> None:
        """Lock the full ordered frame-type mapping from the real capture.

        Guards the run_end-ignored invariant (no ResultEvent from run_end) and
        the frame-type -> stream-event mapping the docs §5 annotate.
        """
        types: list[str] = []
        for line in _fixture("success.ndjson").read_text().splitlines():
            types.extend(type(event).__name__ for event in parse_commandcode_stream_line(line))
        # run_end must be ignored: exactly one ResultEvent (the final line).
        assert types.count("ResultEvent") == 1
        # Thinking deltas dominate a reasoning run.
        assert types.count("ThinkingEvent") >= 10
        # run_start -> SystemInitEvent comes first.
        assert types[0] == "SystemInitEvent"
        # The final event is the authoritative result.
        assert types[-1] == "ResultEvent"

    def test_golden_success_oneshot_parse(self) -> None:
        lines = _fixture("success.ndjson").read_text().splitlines()
        result_line = next(line for line in lines if line.startswith('{"type":"result"'))
        text, sid, usage, turns, is_error = parse_commandcode_json(result_line)
        assert text == "PONG"
        assert is_error is False
        assert isinstance(sid, str)
        assert len(sid) >= 8
        assert usage["outputTokens"] > 0
        # The top-level result line carries no turnCount — that lives in the
        # run_end frame. The parser correctly leaves turns as None here.
        assert turns is None

    def test_golden_effort_reject_empty_stdout(self) -> None:
        """Effort rejection produces EMPTY stdout — only stderr has the error."""
        assert _fixture("effort_reject.ndjson").read_text() == ""
        stderr = _fixture("effort_reject.stderr").read_text()
        assert 'Unknown effort "medium". Supported: high, max.' in stderr
        # The reject regex must match the exact stderr text.
        r = CLIResponse(result=stderr.strip(), is_error=True)
        assert _supported_efforts_from_reject(r) == ("high", "max")

    def test_golden_error_resume_parses_error_field(self) -> None:
        lines = _fixture("error_resume.ndjson").read_text().splitlines()
        result_line = next(line for line in lines if line.startswith('{"type":"result"'))
        text, _sid, _usage, _turns, is_error = parse_commandcode_json(result_line)
        assert is_error is True
        assert "No session" in text

    def test_golden_error_resume_stream_single_result(self) -> None:
        """A genuine failure yields exactly ONE error ResultEvent (no dup)."""
        events: list[object] = []
        for line in _fixture("error_resume.ndjson").read_text().splitlines():
            events.extend(parse_commandcode_stream_line(line))
        results = [e for e in events if isinstance(e, ResultEvent)]
        assert len(results) == 1
        assert results[0].is_error is True
        assert "No session" in results[0].result

    def test_golden_status_authenticated(self) -> None:
        """`cmd status` on an authenticated install (for auth-detect docs)."""
        out = _fixture("status.txt").read_text()
        assert "Authenticated as" in out

    def test_golden_list_models_no_junk(self) -> None:
        from ductor_bot.cli.commandcode_discovery import _parse_models

        models = _parse_models(_fixture("list_models.txt").read_text())
        assert models
        assert "Docs:" not in models
        assert models[0] == "deepseek/deepseek-v4-flash"  # default moved to front
        assert all("/" in m or m in ("claude-sonnet-5", "gpt-5.5") for m in models[:10])


class TestCommandCodeEvents:
    def test_parse_result_happy_path(self) -> None:
        raw = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "sessionId": "sid-1",
                "stopReason": "end_turn",
                "usage": {"inputTokens": 10, "outputTokens": 3},
                "durationMs": 4977,
                "finalText": "hello from command code",
                "turnCount": 1,
            }
        )
        text, session_id, usage, turns, is_error = parse_commandcode_json(raw)
        assert text == "hello from command code"
        assert session_id == "sid-1"
        assert usage["inputTokens"] == 10
        assert turns == 1
        assert is_error is False

    def test_parse_result_error_uses_error_field(self) -> None:
        raw = json.dumps(
            {
                "type": "result",
                "subtype": "error",
                "usage": {},
                "durationMs": 11,
                "finalText": "",
                "error": 'Error: No session "abc" found to resume.',
            }
        )
        text, _session_id, _usage, _turns, is_error = parse_commandcode_json(raw)
        assert is_error is True
        assert "No session" in text

    def test_parse_result_plain_text_fallback(self) -> None:
        text, _session_id, _usage, _turns, is_error = parse_commandcode_json("not json at all")
        assert text == "not json at all"
        assert is_error is False

    def test_parse_stream_run_start_and_text(self) -> None:
        events = parse_commandcode_stream_line(
            '{"type":"event","event":{"type":"run_start","sessionId":"abc-123"}}'
        )
        assert len(events) == 1
        assert isinstance(events[0], SystemInitEvent)
        assert events[0].session_id == "abc-123"

        events = parse_commandcode_stream_line(
            '{"type":"event","event":{"type":"text_delta","delta":"hi "}}'
        )
        assert isinstance(events[0], AssistantTextDelta)
        assert events[0].text == "hi "

    def test_parse_stream_thinking_and_tool(self) -> None:
        thinking = parse_commandcode_stream_line(
            '{"type":"event","event":{"type":"thinking_delta","delta":"plan"}}'
        )
        assert isinstance(thinking[0], ThinkingEvent)
        assert thinking[0].text == "plan"

        tool = parse_commandcode_stream_line(
            '{"type":"event","event":{"type":"tool_running","toolName":"read_file",'
            '"toolCallId":"t1","description":"read"}}'
        )
        assert isinstance(tool[0], ToolUseEvent)
        assert tool[0].tool_name == "read_file"
        assert tool[0].tool_id == "t1"

    def test_parse_stream_final_result_line(self) -> None:
        events = parse_commandcode_stream_line(
            '{"type":"result","subtype":"success","sessionId":"s1","stopReason":"end_turn",'
            '"usage":{"inputTokens":5},"durationMs":100,"finalText":"ok"}'
        )
        assert len(events) == 1
        assert isinstance(events[0], ResultEvent)
        assert events[0].session_id == "s1"
        assert events[0].result == "ok"
        assert events[0].is_error is False

    def test_parse_stream_run_end_is_ignored(self) -> None:
        # run_end is a summary frame (no subtype, sessionId nested under
        # nextState); the authoritative {"type":"result"} line follows it.
        events = parse_commandcode_stream_line(
            '{"type":"event","event":{"type":"run_end","result":{"finalText":"done",'
            '"stopReason":"end_turn","turnCount":1,"usage":{"inputTokens":1},'
            '"nextState":{"sessionId":"abc"}}}}'
        )
        assert events == []

    def test_parse_stream_max_turns_status(self) -> None:
        events = parse_commandcode_stream_line(
            '{"type":"result","subtype":"max_turns","sessionId":"s2","stopReason":"max_turns",'
            '"usage":{},"finalText":"partial"}'
        )
        assert isinstance(events[0], SystemStatusEvent)
        assert events[0].status == "max_turns_reached"
        assert isinstance(events[1], ResultEvent)
        assert events[1].is_error is False  # partial result, not a hard error

    def test_parse_stream_ignores_unknown_frames(self) -> None:
        assert (
            parse_commandcode_stream_line(
                '{"type":"event","event":{"type":"some_future_event","data":{}}}'
            )
            == []
        )


class TestCommandCodeProvider:
    def test_find_cli_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ductor_bot.cli.commandcode_discovery.find_commandcode_cli", lambda: None
        )
        with pytest.raises(FileNotFoundError, match="Command Code CLI not found"):
            CommandCodeCLI(CLIConfig(provider="commandcode"))

    def test_build_command_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ductor_bot.cli.commandcode_discovery.find_commandcode_cli",
            lambda: "/usr/bin/cmd",
        )
        cli = CommandCodeCLI(
            CLIConfig(
                provider="commandcode",
                model="claude-sonnet-5",
                permission_mode="bypassPermissions",
                reasoning_effort="high",
                append_system_prompt="RULES",
                max_turns=7,
            )
        )
        cmd, stdin = cli._build_command("hello world", effort="high")
        assert stdin is None
        assert cmd[0] == "/usr/bin/cmd"
        # `-p` is a boolean flag; the prompt (with prepended rules) is last.
        assert cmd[-1] == "RULES\n\nhello world"
        assert cmd[cmd.index("-p") + 1] == "--output-format"
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "--yolo" in cmd
        assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5"
        assert cmd[cmd.index("--effort") + 1] == "high"
        assert cmd[cmd.index("--max-turns") + 1] == "7"
        # Vision bridge defaults are present by default.
        assert cmd[cmd.index("--config") + 1] == "image-vision=enabled"
        assert "feature-model:vision=gpt-5.6-luna" in cmd

    def test_build_command_vision_defaults_overridable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit cli_parameters override the vision-bridge defaults."""
        monkeypatch.setattr(
            "ductor_bot.cli.commandcode_discovery.find_commandcode_cli",
            lambda: "/usr/bin/cmd",
        )
        cli = CommandCodeCLI(
            CLIConfig(
                provider="commandcode",
                permission_mode="bypassPermissions",
                cli_parameters=["--config", "image-vision=disabled"],
            )
        )
        cmd, _ = cli._build_command("hi")
        # last-flag-wins: the explicit override comes after the 2 defaults
        assert cmd.count("--config") == 3
        assert cmd[-3:-1] == ["--config", "image-vision=disabled"]

    def test_build_command_auto_accept_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ductor_bot.cli.commandcode_discovery.find_commandcode_cli",
            lambda: "/usr/bin/cmd",
        )
        cli = CommandCodeCLI(CLIConfig(provider="commandcode", permission_mode="auto-accept"))
        cmd, _stdin = cli._build_command("hi")
        assert "--auto-accept" in cmd
        assert "--yolo" not in cmd

    def test_build_command_resume_continue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ductor_bot.cli.commandcode_discovery.find_commandcode_cli",
            lambda: "/usr/bin/cmd",
        )
        cli = CommandCodeCLI(CLIConfig(provider="commandcode"))
        cmd, _ = cli._build_command("follow up", resume_session="full-id-123")
        assert cmd[cmd.index("--resume") + 1] == "full-id-123"
        cmd, _ = cli._build_command("follow up", continue_session=True)
        assert "--continue" in cmd
        assert "--resume" not in cmd

    def test_long_prompt_uses_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ductor_bot.cli.commandcode_discovery.find_commandcode_cli",
            lambda: "/usr/bin/cmd",
        )
        cli = CommandCodeCLI(CLIConfig(provider="commandcode"))
        long_prompt = "x" * 30_000
        cmd, stdin = cli._build_command(long_prompt)
        assert stdin == long_prompt
        assert "-p" in cmd  # bare -p; stdin is auto-detected by the CLI
        assert not any(len(arg) > 24_100 for arg in cmd)

    def test_parse_response_happy(self) -> None:
        payload = {
            "type": "result",
            "subtype": "success",
            "sessionId": "s9",
            "stopReason": "end_turn",
            "usage": {"inputTokens": 100, "outputTokens": 50},
            "durationMs": 500,
            "finalText": "ok",
            "turnCount": 1,
        }
        resp = _parse_response(json.dumps(payload).encode(), b"", 0)
        assert resp.result == "ok"
        assert resp.session_id == "s9"
        assert resp.is_error is False
        assert resp.num_turns == 1

    def test_parse_response_max_turns_not_error(self) -> None:
        payload = {
            "type": "result",
            "subtype": "max_turns",
            "sessionId": "s8",
            "usage": {},
            "finalText": "partial",
        }
        resp = _parse_response(json.dumps(payload).encode(), b"", 8)
        assert resp.result == "partial"
        assert resp.is_error is False

    def test_parse_response_error(self) -> None:
        payload = {
            "type": "result",
            "subtype": "error",
            "usage": {},
            "durationMs": 11,
            "finalText": "",
            "error": 'Unknown effort "medium". Supported: high, max.',
        }
        resp = _parse_response(json.dumps(payload).encode(), b"", 1)
        assert resp.is_error is True
        assert "Unknown effort" in resp.result

    def test_parse_response_empty_output(self) -> None:
        resp = _parse_response(b"", b"boom", 1)
        assert resp.is_error is True
        assert resp.result == "boom"


class TestEffortClamping:
    def test_closest_effort_prefers_higher_on_tie(self) -> None:
        assert _closest_effort("xhigh", ("high", "max")) == "max"
        assert _closest_effort("medium", ("high", "max")) == "high"

    def test_closest_effort_known_level(self) -> None:
        assert _closest_effort("high", ("high", "max")) == "high"

    def test_closest_effort_clamps_down(self) -> None:
        assert _closest_effort("xhigh", ("low", "medium", "high")) == "high"

    def test_closest_effort_unknown_requests_max(self) -> None:
        assert _closest_effort("zzz", ("high", "max")) == "max"

    def test_supported_efforts_from_reject(self) -> None:
        r = CLIResponse(result='Unknown effort "medium". Supported: high, max.', is_error=True)
        assert _supported_efforts_from_reject(r) == ("high", "max")

    def test_supported_efforts_from_non_reject(self) -> None:
        r = CLIResponse(result="something else", is_error=True)
        assert _supported_efforts_from_reject(r) == ()


def _cc_task_cfg(**kwargs: Any) -> TaskExecutionConfig:
    base: dict[str, Any] = {
        "provider": "commandcode",
        "model": "claude-sonnet-5",
        "reasoning_effort": "medium",
        "permission_mode": "bypassPermissions",
        "cli_parameters": [],
        "working_dir": "/tmp",
        "file_access": "all",
    }
    base.update(kwargs)
    return TaskExecutionConfig(**base)


class TestCommandCodeCronCmd:
    def test_short_prompt_uses_p(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ductor_bot.cron.execution.find_commandcode_cli", lambda: "/usr/bin/cmd"
        )
        one = _build_commandcode_cmd(_cc_task_cfg(), "hello")
        assert one is not None
        assert one.cmd[-1] == "hello"
        assert "--yolo" in one.cmd

    def test_long_prompt_uses_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ductor_bot.cron.execution.find_commandcode_cli", lambda: "/usr/bin/cmd"
        )
        long_prompt = "y" * 30_000
        one = _build_commandcode_cmd(_cc_task_cfg(), long_prompt)
        assert one is not None
        assert one.stdin_input == long_prompt.encode()

    def test_parse_commandcode_result_uses_last_json_line(self) -> None:
        stdout = (
            b'{"type":"event","event":{"type":"text_delta","delta":"x"}}\n'
            b'{"type":"result","subtype":"success","sessionId":"s1","usage":{},'
            b'"finalText":"final answer"}\n'
        )
        assert parse_commandcode_result(stdout) == "final answer"


class TestFactoryAndRegistry:
    def test_factory_returns_commandcode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ductor_bot.cli.commandcode_discovery.find_commandcode_cli",
            lambda: "/usr/bin/cmd",
        )
        cli = create_cli(CLIConfig(provider="commandcode", model="claude-sonnet-5"))
        assert isinstance(cli, CommandCodeCLI)

    def test_model_registry_routes_commandcode(self) -> None:
        assert ModelRegistry.provider_for("deepseek/deepseek-v4-flash") == "commandcode"
        # gpt-5.5 is claimed by Codex natively; the bare-id inference keeps it.
        assert ModelRegistry.provider_for("gpt-5.5") == "codex"
        # Claude-native prefixes keep their own provider.
        assert ModelRegistry.provider_for("opus") == "claude"
