"""Tests for dynamic Command Code model discovery and caching."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.cli.commandcode_cache import _FALLBACK_COMMANDCODE_MODELS, CommandCodeModelCache
from ductor_bot.cli.commandcode_discovery import _parse_models, discover_commandcode_models
from ductor_bot.config import (
    ModelRegistry,
    get_commandcode_models_ordered,
    reset_commandcode_models,
    set_commandcode_models,
)

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "commandcode"

_SAMPLE_OUTPUT = """Available models  ·  50 models

Open Source

deepseek/deepseek-v4-flash             fast hybrid-attention reasoning (default)
moonshotai/kimi-k3                     long-horizon coding & knowledge work with 1M context
claude-sonnet-5                        best combo of speed & intelligence (recommended)

Anthropic

gpt-5.5                                latest frontier model for general complex work
"""


@pytest.fixture(autouse=True)
def _reset_commandcode_models() -> Iterator[None]:
    reset_commandcode_models()
    yield
    reset_commandcode_models()


def test_parse_models_default_moves_to_front() -> None:
    assert _parse_models(_SAMPLE_OUTPUT) == (
        "deepseek/deepseek-v4-flash",
        "moonshotai/kimi-k3",
        "claude-sonnet-5",
        "gpt-5.5",
    )


def test_parse_real_list_models_capture() -> None:
    """The real `cmd --list-models` capture parses to a clean catalog."""
    raw = (_FIXTURES / "list_models.txt").read_text()
    models = _parse_models(raw)
    assert len(models) >= 40
    assert models[0] == "deepseek/deepseek-v4-flash"  # (default) moved to front
    assert "Docs:" not in models
    assert "Pass the full id" not in models
    # No header words or footer junk survived parsing.
    assert all(" " not in m for m in models)
    assert all(m for m in models if "/" in m or m.startswith(("claude-", "gpt-5", "fable")))



def test_parse_models_skips_headers_and_usage() -> None:
    assert _parse_models("Usage: cmd --list-models\nAvailable models:\n") == ()
    assert _parse_models("Open Source:\nfoo/bar  desc\n") == ("foo/bar",)


def test_parse_models_skips_duplicates() -> None:
    raw = "x/a  first\nx/b  second\nx/a  dup\n"
    assert _parse_models(raw) == ("x/a", "x/b")


def test_parse_models_rejects_docs_footer_and_usage() -> None:
    """The real CLI prints a trailing footer; it must not leak into models."""
    raw = (
        "Open Source\n"
        "deepseek/deepseek-v4-flash  fast hybrid-attention reasoning (default)\n"
        "claude-sonnet-5             best combo of speed & intelligence\n"
        "\n"
        'Pass the full id, or just the short name after the last "/":\n'
        "cmd --model moonshotai/kimi-k2.5\n"
        "\n"
        "Docs:  https://commandcode.ai/docs/reference/cli/models\n"
    )
    assert _parse_models(raw) == ("deepseek/deepseek-v4-flash", "claude-sonnet-5")


def test_parse_models_rejects_uppercase_header_with_spaces() -> None:
    # A header word followed by 2+ spaces must not be mistaken for a model.
    assert _parse_models("OPEN SOURCE\n") == ()


def _mock_proc(stdout: bytes, returncode: int = 0) -> AsyncMock:
    proc = AsyncMock(spec=asyncio.subprocess.Process)
    proc.returncode = returncode
    proc.pid = 4242
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


async def test_discover_returns_models_on_success() -> None:
    with (
        patch("ductor_bot.cli.commandcode_discovery.which", return_value="/usr/bin/cmd"),
        patch(
            "ductor_bot.cli.commandcode_discovery.asyncio.create_subprocess_exec",
            return_value=_mock_proc(_SAMPLE_OUTPUT.encode()),
        ),
    ):
        models = await discover_commandcode_models()

    assert models[0] == "deepseek/deepseek-v4-flash"


async def test_discover_returns_empty_when_not_logged_in() -> None:
    with (
        patch("ductor_bot.cli.commandcode_discovery.which", return_value="/usr/bin/cmd"),
        patch(
            "ductor_bot.cli.commandcode_discovery.asyncio.create_subprocess_exec",
            return_value=_mock_proc(b"not authenticated\n", returncode=1),
        ),
    ):
        assert await discover_commandcode_models() == ()


async def test_discover_returns_empty_when_binary_missing() -> None:
    with patch("ductor_bot.cli.commandcode_discovery.find_commandcode_cli", return_value=None):
        assert await discover_commandcode_models() == ()


async def test_discover_falls_back_to_commandcode_binary() -> None:
    """When ``cmd`` is absent but ``commandcode`` exists, use it."""
    real = {"cmd": None, "commandcode": "/usr/bin/commandcode"}

    def _which(name: str) -> str | None:
        return real[name]

    with (
        patch("ductor_bot.cli.commandcode_discovery.which", side_effect=_which),
        patch(
            "ductor_bot.cli.commandcode_discovery.asyncio.create_subprocess_exec",
            return_value=_mock_proc(_SAMPLE_OUTPUT.encode()),
        ) as create,
    ):
        models = await discover_commandcode_models()

    assert models
    args = create.call_args.args
    assert args[0] == "/usr/bin/commandcode"


async def test_cache_persists_discovered_models(tmp_path: Path) -> None:
    path = tmp_path / "commandcode_models.json"
    with patch(
        "ductor_bot.cli.commandcode_cache.discover_commandcode_models",
        return_value=("deepseek/deepseek-v4-flash", "claude-sonnet-5"),
    ):
        cache = await CommandCodeModelCache.load_or_refresh(path, force_refresh=True)
    assert cache.models == ("deepseek/deepseek-v4-flash", "claude-sonnet-5")
    assert path.is_file()
    loaded = CommandCodeModelCache.from_json(json.loads(path.read_text()))
    assert loaded.models == cache.models


def test_set_commandcode_models_updates_registry_and_order() -> None:
    set_commandcode_models(("zai-org/glm-5", "deepseek/deepseek-v4-pro"))
    assert get_commandcode_models_ordered() == ("zai-org/glm-5", "deepseek/deepseek-v4-pro")
    assert ModelRegistry.provider_for("zai-org/glm-5") == "commandcode"
    assert ModelRegistry.provider_for("deepseek/deepseek-v4-pro") == "commandcode"


def test_native_provider_prefix_wins_over_commandcode() -> None:
    """Claude/Gemini-native ids keep their provider even when in the
    Command Code runtime set (Command Code is a gateway with overlapping ids)."""
    set_commandcode_models(("claude-sonnet-5", "gemini-3.5-flash", "xai/grok-4.5"))
    assert ModelRegistry.provider_for("claude-sonnet-5") == "claude"
    assert ModelRegistry.provider_for("gemini-3.5-flash") == "gemini"
    # xai/grok-4.5 (prefixed) is NOT a native grok id (native = grok-*),
    # so it routes to the gateway.
    assert ModelRegistry.provider_for("xai/grok-4.5") == "commandcode"


def test_fallback_models_match_hardcoded() -> None:
    assert "deepseek/deepseek-v4-flash" in _FALLBACK_COMMANDCODE_MODELS
