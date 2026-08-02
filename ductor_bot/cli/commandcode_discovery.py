"""Model discovery for the Command Code CLI (``cmd --list-models``)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from shutil import which

from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS
from ductor_bot.infra.process_tree import force_kill_process_tree

logger = logging.getLogger(__name__)

_DISCOVERY_TIMEOUT = 15.0

_BINARY_NAMES = ("cmd", "commandcode")

# ``cmd --list-models`` output: two-column listing with category headers
# ("Open Source", "Anthropic", ...) and rows like::
#
#   deepseek/deepseek-v4-flash  fast hybrid-attention reasoning (default)
#   claude-sonnet-5             best combo of speed & intelligence (recommended)
#
# Model tokens are the first whitespace-separated token of a row; they contain
# ``/`` (provider prefix) and may be the default/recommended entry.
# Model rows are separated by 2+ spaces; category headers are not.
_MODEL_ROW = re.compile(r"^(\S+)\s{2,}")
_DEFAULT_MARK = re.compile(r"\(default\)", re.IGNORECASE)


async def discover_commandcode_models() -> tuple[str, ...]:
    """Return model IDs reported by ``cmd --list-models``.

    Returns an empty tuple when the CLI is missing, unauthenticated, times out,
    or errors — callers then fall back to the cached or hardcoded list.
    """
    binary = _find_binary()
    if not binary:
        logger.debug("Command Code not available for model discovery")
        return ()

    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "--list-models",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATION_FLAGS,
        )
    except (OSError, ValueError):
        logger.debug("Command Code --list-models spawn failed", exc_info=True)
        return ()

    try:
        async with asyncio.timeout(_DISCOVERY_TIMEOUT):
            stdout_bytes, stderr_bytes = await proc.communicate()
    except TimeoutError:
        logger.warning("Command Code --list-models discovery timed out")
        force_kill_process_tree(proc.pid)
        await proc.communicate()
        return ()

    output = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""
    combined = f"{output}\n{stderr}".lower()
    if any(
        token in combined
        for token in ("not logged in", "not authenticated", "login required", "unauthorized")
    ):
        logger.debug("Command Code --list-models: not authenticated")
        return ()

    if proc.returncode not in (0, None):
        logger.debug("Command Code --list-models exited with code %s", proc.returncode)
        return ()

    return _parse_models(output)


def _parse_models(output: str) -> tuple[str, ...]:  # noqa: C901
    """Parse ``cmd --list-models`` stdout into an ordered tuple of model IDs.

    Category header lines and usage banners are skipped; the default model
    (marked ``(default)``) is moved to the front so ductor's default resolves
    to the CLI's actual default.
    """
    models: list[str] = []
    seen: set[str] = set()
    default_model: str | None = None

    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Usage/help banner means the command was rejected — treat as failure.
        if line.startswith(("Usage:", "Flags:")):
            return ()
        # Trailing footer ("Docs: ...", "Pass the full id ...") appears after
        # the model rows; stop there rather than misparse it as a model.
        if line.startswith(("Docs:", "Pass the full id")):
            break
        # Category headers ("Open Source") have no 2+ space gap, so
        # _MODEL_ROW skips them naturally.
        match = _MODEL_ROW.match(line)
        if not match:
            continue
        model_id = match.group(1).strip()
        if not model_id or model_id in seen:
            continue
        # Safety net: reject tokens that are obviously not model ids
        # (e.g. a header word that happened to be followed by 2+ spaces).
        if ":" in model_id or model_id.isupper():
            continue
        seen.add(model_id)
        if _DEFAULT_MARK.search(line):
            default_model = model_id
        models.append(model_id)

    if not models:
        return ()

    if default_model and models[0] != default_model:
        models.remove(default_model)
        models.insert(0, default_model)
    return tuple(models)


def _find_binary() -> str | None:
    return find_commandcode_cli()


def find_commandcode_cli() -> str | None:  # noqa: C901
    """Locate the Command Code CLI (``cmd`` / ``commandcode``).

    Checks PATH first, then probes common install locations that a systemd
    service's minimal PATH may omit (bun global installs, user-local bin,
    NVM node bins). Mirrors the Gemini CLI fallback pattern
    (``gemini_utils._find_gemini_fallback``).
    """
    for name in _BINARY_NAMES:
        path = which(name)
        if path:
            return path

    home = Path.home()
    candidates: list[Path] = []
    bun_install = os.environ.get("BUN_INSTALL")
    if bun_install:
        candidates.append(Path(bun_install) / "bin")
    candidates.extend(
        [
            home / ".bun" / "bin",
            home / ".local" / "bin",
            home / ".npm-global" / "bin",
            home / "AppData" / "Roaming" / "npm",  # Windows bun/npm global
        ]
    )
    for bin_dir in candidates:
        for name in _BINARY_NAMES:
            candidate = bin_dir / name
            if candidate.is_file():
                return str(candidate)

    # NVM layout: ~/.nvm/versions/node/<ver>/bin
    versions_dir = home / ".nvm" / "versions" / "node"
    if versions_dir.is_dir():
        for version_dir in sorted(versions_dir.iterdir(), reverse=True):
            bin_dir = version_dir / "bin"
            for name in _BINARY_NAMES:
                candidate = bin_dir / name
                if candidate.is_file():
                    return str(candidate)
    return None
