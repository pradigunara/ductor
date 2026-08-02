# Command Code (commandcode.ai) Provider — Fork Notes

This document is a **rebase and refactor aid** for the Command Code provider
support added to ductor. It is not user documentation (see
`docs/modules/cli.md` for that); it exists so a future agent can:

1. See exactly what was added and why.
2. Understand how the real `cmd` CLI behaves at the pinned version, from
   captured evidence rather than guesses.
3. Rebase cleanly when upstream ductor diverges, and know which hunks are
   likely to conflict.

## 1. What was added

The provider id is `commandcode`, the CLI binary is `cmd` (also ships as
`commandcode`). The surface mirrors the Grok Build integration as closely as
possible, because Grok is the most similar existing provider (JSON NDJSON
headless output, `--continue`/`--resume` sessions, model discovery via a CLI
subcommand).

### New files (self-contained, no upstream dependencies)

| File | Purpose |
|---|---|
| `ductor_bot/cli/commandcode_provider.py` | `CommandCodeCLI` wrapper (`send` / `send_streaming`) + effort-clamp retry logic |
| `ductor_bot/cli/commandcode_events.py` | NDJSON parser for `--output-format json` (event frames + final result line) |
| `ductor_bot/cli/commandcode_discovery.py` | `discover_commandcode_models()` via `cmd --list-models` |
| `ductor_bot/cli/commandcode_cache.py` | `CommandCodeModelCache` (persistent model cache) |
| `ductor_bot/cli/commandcode_cache_observer.py` | `CommandCodeCacheObserver` (hourly refresh) |

### Modified files (small, additive hunks)

| File | What changed |
|---|---|
| `ductor_bot/config.py` | `CLIParametersConfig.commandcode`, `SkillSyncProviders.commandcode`, `COMMANDCODE_MODELS(_ORDERED)`, `COMMANDCODE_SUPPORTED_EFFORTS`, runtime model registry + accessors, `ModelRegistry.provider_for` branch |
| `ductor_bot/cli/auth.py` | `check_commandcode_auth()` + `_commandcode_cli_logged_in()` + `_CHECKERS` entry |
| `ductor_bot/cli/factory.py` | `create_cli` branch for `commandcode` |
| `ductor_bot/cli/init_wizard.py` | probe tuple includes commandcode |
| `ductor_bot/cli/param_resolver.py` | task providers set, model validation, effort validation |
| `ductor_bot/cli/service.py` | `commandcode_cli_parameters` bucket + resolver branch |
| `ductor_bot/cron/execution.py` | `_build_commandcode_cmd` + `parse_commandcode_result` + registry entries |
| `ductor_bot/orchestrator/core.py` | `commandcode_cli_parameters` in initial `CLIServiceConfig` construction |
| `ductor_bot/orchestrator/providers.py` | `ProviderManager`: name, refresh callback, known-model set, default model, `@commandcode` directive, API provider meta |
| `ductor_bot/orchestrator/observers.py` | `CommandCodeCacheObserver` wiring in `init_model_caches` (new callback param is **optional** — existing tests/callers stay valid) + `stop_all` attr |
| `ductor_bot/orchestrator/lifecycle.py` | passes `on_commandcode_refresh` callback |
| `ductor_bot/orchestrator/commands.py` | `/status` effort line includes commandcode |
| `ductor_bot/orchestrator/selectors/model_selector.py` | provider button, model step, effort support/validation |
| `ductor_bot/text/response_format.py` | `/new` provider label |
| `ductor_bot/workspace/rules_selector.py` | commandcode counts as an `AGENTS.md` consumer (like Codex/Grok) |
| `ductor_bot/workspace/skill_sync.py` | `~/.commandcode/skills` in sync dirs + priority |
| `ductor_bot/_home_defaults/workspace/tools/...` | provider choice lists in `cron_add`/`cron_edit`/`webhook_add`/`webhook_edit`/`create_agent` |
| `ductor_bot/i18n/*/{chat,wizard}.toml` | `select_commandcode`, `commandcode`, `commandcode_check_failed` (all 8 locales) |
| `config.example.json` | `cli_parameters.commandcode` bucket |
| `docs/modules/cli.md` | provider docs (see next section) |

## 2. Pinned CLI behavior (verified against `cmd` v1.7.0)

Everything below was captured by running the real CLI on 2026-08-01. If the
CLI changes, re-verify before trusting the wrapper.

### Binary

- `cmd` and `commandcode` both resolve to the same product; the wrapper tries
  `cmd` first, then `commandcode`.
- Auth state lives in `~/.commandcode/auth.json` (keys: `apiKey`, `userId`,
  `userName`, `keyName`, `authenticatedAt`).
- `cmd status` prints (verified interactively; when piped, the first progress
  line is dropped and only the last two lines appear):
  ```
  - Checking authentication status...
  ✔ Authentication verified
  ✔ Authenticated as <username>
    Provider: Command Code
  ```

### Model routing (gateway overlap)

Command Code is a **multi-provider gateway**: its catalog contains ids that
also belong to Claude (`claude-sonnet-5`), Codex (`gpt-5.5`), Gemini
(`google/gemini-3.6-flash`), and Grok (`xai/grok-4.5`). Routing rules in
`ModelRegistry.provider_for`:

- Native providers win for their own prefixes: `claude-*` → claude,
  `gemini-*` → gemini, `grok-*` (bare) → grok.
- Catalog ids not claimed by another provider (`deepseek/*`,
  `moonshotai/*`, `zai-org/*`, ...) route to `commandcode` when in the
  runtime discovery set.
- The **hardcoded fallback** (`COMMANDCODE_MODELS_ORDERED`) intentionally
  contains only `deepseek/deepseek-v4-flash` (the CLI's `(default)` model,
  unclaimed by other providers) so bare-id inference never hijacks a native
  provider when discovery is unavailable. The full catalog is populated at
  startup by `CommandCodeCacheObserver` via `cmd --list-models`.
- Consequence: picking a shared id (e.g. `gpt-5.5`) from the **Command Code
  provider button** in the Telegram wizard re-infers the provider from the id
  and routes to the native provider (Codex). This mirrors the pre-existing
  `ms:m:<id>` wizard design (no provider tag in the callback). The
  `@commandcode <model>` directive path carries the provider explicitly and
  routes correctly.

### Headless mode

- `-p <query>` runs one-shot. With no query argument, **stdin is auto-detected**
  (piped input). Used for long prompts (>24 KB) to avoid argv limits.
- `--output-format json` emits NDJSON: `{"type":"event","event":{...}}` frames
  then exactly one final `{"type":"result", ...}` line.
- The final result line carries: `subtype` (`success`/`error`/`max_turns`),
  `sessionId`, `stopReason`, `usage`, `durationMs`, `finalText`, and `error`
  (present only on `error` subtype).
- **`sessionId` is NOT in `run_end`'s `result`** — it lives on the top-level
  `result` line and the `run_start` event. The wrapper reads it from the
  `result` line.
- Exit codes: `0` ok, `1` general error, `3` not authenticated, `8` max-turns
  reached (partial result still returned). The wrapper treats `8` as a
  non-error (partial) result.

### Event frame types observed (v1.7.0)

```
run_start  {sessionId}
turn_start {turnNumber}
message_start
model_request_start {model}
model_trace {traceId}
text_delta {delta}
thinking_start / thinking_delta {delta} / thinking_end {text}
message_update {content}
message_end {content}
tool_queued {input, toolCallId, toolName}
tool_running {description, toolCallId, toolName}
tool_completed {result, toolCallId, toolName}
model_request_end {model, usage, stopReason, effort}
turn_end {turnNumber, hadToolCalls, usage}
run_end {result}
```

Unknown frame types are ignored (forward-compatible per the CLI docs).

### Sessions

- `--continue` (or `-c`) resumes the most recent headless session **in the
  current working directory**.
- `--resume <id>` needs the **full session id** — a short prefix errors with
  `Error: No session "..." found to resume.` (verified).
- Headless sessions stay hidden from interactive `/resume`.
- On a genuine failure (e.g. bad resume), the CLI still emits a
  `{"type":"result","subtype":"error",...}` line on **stdout** with the error
  in the `error` field (finalText is empty), and echoes the error on stderr
  (prefixed by `Reasoning effort set to ...` when `--effort` was accepted).
  The streaming path suppresses the executor's post-stream error duplicate
  when stdout already carried a result frame.

### Model discovery footer

`cmd --list-models` ends with a footer that must not be parsed as models:

```
Pass the full id, or just the short name after the last "/":
cmd --model moonshotai/kimi-k2.5
cmd --model kimi-k2.5

Docs:  https://commandcode.ai/docs/reference/cli/models
```

`_parse_models` stops at the `Docs:`/`Pass the full id` lines (they appear
after all model rows). The `cmd --model ...` lines have a single space, so
they never match the 2+-space model-row regex.

### Reasoning effort (the tricky part)

- `--effort` levels are **per-model**, not global. The default model
  (`deepseek/deepseek-v4-flash`) accepts only `high` and `max`; requesting
  `low`/`medium`/`xhigh` fails the whole run:
  ```
  Unknown effort "medium". Supported: high, max.   # exit 1, empty stdout
  ```
- The wrapper handles this with a reject-and-clamp retry:
  1. Send with the requested effort (config default `medium` or explicit).
  2. On rejection, parse `Supported: ...` from the error, clamp the requested
     level to the **closest supported** (preferring the higher one on a tie:
     `xhigh` → `max`).
  3. Retry once with the clamped effort; cache `(model, requested) -> resolved`
     in a module-level dict so default runs don't reject-then-retry every turn.
- The streaming path suppresses the first attempt's effort-reject error
  ResultEvent and only emits the retried run's events.
- The cron one-shot path (`_build_commandcode_cmd`) cannot retry, so it reads
  the same `_EFFORT_CACHE`: when `(model, effort)` is already resolved it uses
  the clamped level; when the cache is cold it **omits `--effort`** so the CLI
  uses the model's own default instead of failing the whole job.
- `_closest_effort` ladder: `low < medium < high < xhigh < max`.

### Permission modes

- ductor's `bypassPermissions` → `--yolo` (full bypass).
- ductor's `auto-accept` → `--auto-accept`.
- Headless mode blocks file writes/shell by default; `--yolo` enables them.
  This is why the wrapper defaults to `--yolo` under the default permission
  mode — otherwise the CLI is useless as a coding agent.

### Model discovery

- `cmd --list-models` prints a two-column catalog with category headers
  (`Open Source`, `Anthropic`, ...). Rows: `<id>  <description>`.
- The default model row is marked `(default)`; discovery moves it to the front
  so ductor's `default_model_for_provider("commandcode")` resolves to the
  CLI's actual default.
- Model ids contain `/` (e.g. `deepseek/deepseek-v4-flash`,
  `claude-sonnet-5`, `google/gemini-3.6-flash`).
- `ModelRegistry.provider_for` routes: catalog ids in the runtime/discovery set
  → `commandcode`; known Claude/Gemini/Grok prefixes keep their native
  providers. **Known overlap**: `claude-*` ids are valid Command Code models
  AND Claude Code models — the registry routes them to Claude. Picking a
  Claude id through the Command Code provider button works because the
  selector buttons send `ms:m:<id>` and the session stores the provider
  explicitly; only the *inference* path (bare model id with no provider
  context) prefers native Claude.

## 3. Rebase / refactor guidance

### Likely conflict points

- `ductor_bot/config.py`: every new provider touches the model registry
  (`_runtime_*` lists, `provider_for` chain, `CLAUDE/CODEX/..._MODELS`). If
  upstream adds a provider, merge order matters.
- `ductor_bot/cli/auth.py` `_CHECKERS` dict and `ductor_bot/cli/factory.py`:
  one-line additions; conflicts are trivial but frequent.
- `ductor_bot/orchestrator/observers.py` `init_model_caches`: signature is
  **additive with an optional default** (`on_commandcode_refresh=None`) so
  upstream signature changes to this method are the main risk. If upstream
  renames/restructures the cache-observer wiring, port the `commandcode`
  block to the new shape.
- `ductor_bot/orchestrator/providers.py` `provider_for` / `_known_model_ids` /
  `provider_meta`: same pattern as above.
- `ductor_bot/workspace/skill_sync.py` `_SYNCABLE_PROVIDERS` and priority
  tuple; `ductor_bot/workspace/rules_selector.py` auth flags.
- `ductor_bot/i18n/*`: the i18n checker (`python -m ductor_bot.i18n.check`)
  fails if en and any locale diverge — always add the 3 keys to all 8 locales
  in one change.

### Design decisions that ease rebasing

- **New provider code is 5 self-contained files** with no imports from each
  other beyond the standard `cli/` scaffolding (base, executor, stream_events,
  model_cache). Moving them to a different layout is a mechanical rename.
- **No existing behavior changed**: all edits are additive (new dict entries,
  new optional kwargs, new branches). The only touched existing lines are
  registry/choice lists. `git diff` should show no deletions of upstream code
  except the `_TASK_PROVIDERS`/`_SYNCABLE_PROVIDERS` set literals.
- **The `on_commandcode_refresh` kwarg is optional** so upstream test files
  that call `init_model_caches` keep passing without modification.
- The hot-reload path in `orchestrator/core.py` (`_on_config_hot_reload`)
  deliberately does **not** include `commandcode_cli_parameters` — mirroring
  how `grok_cli_parameters` was (not) handled upstream. If upstream ever adds
  grok there, add commandcode the same way.

### Service (systemd) PATH handling

Systemd user services run with a minimal PATH that omits bun/npm-global/nvm/
volta dirs, so `cmd` (a `#!/usr/bin/env node` wrapper) is invisible to a bare
`which`. Two coordinated pieces handle this:

- `commandcode_discovery.find_commandcode_cli()` probes PATH first, then
  `$BUN_INSTALL/bin`, `~/.bun/bin`, `~/.local/bin`, `~/.npm-global/bin`,
  Windows npm dir, and `~/.nvm/versions/node/*/bin`. Used by the provider
  (`_find_cli`), the auth check, the model cache, and the cron builder.
- `executor._augment_commandcode_path()` prepends the CLI's *symlink parent*
  (e.g. `~/.bun/bin`, NOT the resolved `dist/`) and a real `node` bin dir to
  the subprocess PATH for commandcode runs. `_find_node_bin_dir()` probes
  volta's `tools/image/node/<ver>/bin` (real binaries — the top-level
  `~/.volta/bin` is a shim and is intentionally skipped), nvm, bun,
  npm-global, and user-local.

If a service host has `cmd` installed somewhere unusual, add the dir to both
probe lists. Debugging tip: `systemctl --user show ductor -p Environment` /
`-p ExecStart` reveals the service PATH; the wrapper logs the resolved CLI
path at init.

### Docker sandbox

The sandbox image (`Dockerfile.sandbox`) installs Command Code via the npm
package `command-code` (`npm install -g ... command-code`), which exposes the
`cmd` bin and requires node >= 22 (the image is `node:22-bookworm-slim`).

Auth inside the container works via **host auth-dir mounts**: `DockerManager`
mounts `~/.commandcode` (plus `~/.claude`, `~/.codex`, `~/.gemini`) into
`/home/node/...` read-write, with `HOME=/home/node` set. So the containerized
`cmd` uses the host's `auth.json` — no re-login needed. When adding the
provider to the Docker path, remember BOTH pieces:
1. the image must install the CLI (npm `command-code`), and
2. the host auth dir must be in `auth_dirs` in `infra/docker.py`
   (`_start_container`), or the containerized CLI is unauthenticated.
- In Docker mode the provider uses the bare `"cmd"` name (found on the
  container PATH); `find_commandcode_cli()` and the PATH augmentation only
  apply to host execution (docker exec uses the container's own env).

### Vision bridge defaults

The provider passes two `--config` flags by default so image support works
without per-user setup:

```
--config image-vision=enabled
--config feature-model:vision=gpt-5.6-luna
```

- `image-vision` lets a text-only model read attached images by describing
  them with the vision model.
- `feature-model:vision` picks which model does that describing; the default
  is `gpt-5.6-luna` (OpenAI, cost-optimized).
- The flags are injected **before** `cli_parameters`, so a user's explicit
  `--config image-vision=...` (or a different vision model) in
  `cli_parameters.commandcode` wins (last-flag-wins).
- Both settings were verified accepted by the real v1.7.0 CLI in headless
  mode (`cmd -p --config image-vision=enabled --config
  "feature-model:vision=gpt-5.6-luna" ...`).

### Known divergences / footguns

- The bundled `command-code-knowledge` reference docs (in the Command Code
  install) may lag the actual CLI (the earlier `--verbose` session-id-on-stderr
  behavior documented there was not what v1.7.0 did). When in doubt, run the
  real CLI.
- The `run_end` event frame is a **run summary, not a result**: it has no
  `subtype` and its sessionId is nested under `nextState`. The parser ignores
  it; the authoritative `{"type":"result",...}` line always follows. Do not
  "fix" this back into emitting a ResultEvent from `run_end` — it produces a
  spurious error result.
- `cmd -p` with an effort rejection produces **empty stdout** — the wrapper's
  error path must read stderr, and the streaming path surfaces it as an error
  `ResultEvent` that `send_streaming` suppresses on retry.
- Running `cmd` inside the ductor repo can create a project-local
  `.commandcode/` dir (project settings). The wrapper does not create it, but
  a stray one may appear after manual `cmd` runs in the repo.
- Docker: `docker_wrap` is used, but the sandbox image needs `cmd` installed
  and the user's `~/.commandcode` auth available — verify before relying on
  Docker mode for this provider.

## 4. Quick verification

```bash
# Event parser (unit, no CLI needed)
uv run pytest tests/cli/test_commandcode_provider.py tests/cli/test_commandcode_discovery.py -q

# Live smoke test (requires `cmd` on PATH + auth)
uv run python - <<'PY'
import asyncio
from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.commandcode_provider import CommandCodeCLI

async def main():
    cfg = CLIConfig(provider="commandcode", working_dir="/tmp",
                    permission_mode="bypassPermissions", reasoning_effort="medium")
    cli = CommandCodeCLI(cfg)
    resp = await cli.send("reply with exactly: PONG")
    print(resp.result, resp.is_error, resp.session_id)

asyncio.run(main())
PY

# i18n completeness
uv run python -m ductor_bot.i18n.check --quiet
```

## 5. Captured CLI output (golden fixtures)

Verbatim captures from the real `cmd` v1.7.0 live in
`tests/fixtures/commandcode/` and are asserted by `TestGoldenFiles` in
`tests/cli/test_commandcode_provider.py`. When debugging parser behavior,
read the raw fixture first — it is the ground truth. To re-capture (e.g. after
a CLI upgrade), re-run the exact commands below.

| Fixture | Command used | What it exercises |
|---|---|---|
| `success.ndjson` | `echo "reply with exactly: PONG" \| cmd -p --output-format json` | Happy-path streaming: full event-frame inventory + final `result` line |
| `effort_reject.ndjson` (empty) + `.stderr` | `... --effort medium --output-format json` | Effort rejection: **stdout is empty**, stderr has `Unknown effort ... Supported: ...` |
| `effort_ok.stderr` | `... --effort high --output-format json` | Accepted effort stderr note: `Reasoning effort set to high for DeepSeek V4 Flash.` |
| `error_resume.ndjson` + `.stderr` | `... --resume no-such-session-xyz --output-format json` | Genuine failure: stdout result line `subtype:"error"` with `error` field; stderr repeats it |
| `list_models.txt` | `cmd --list-models` | Full model catalog incl. the `Docs:` footer the parser must stop at |
| `status.txt` | `cmd status` | Authenticated status output for the auth probe |

### Success stream (annotated)

The event frames in order (from `success.ndjson`):

```
{"type":"event","event":{"type":"run_start","sessionId":"..."}}   -> SystemInitEvent
{"type":"event","event":{"type":"turn_start","turnNumber":1}}
{"type":"event","event":{"type":"message_start"}}
{"type":"event","event":{"type":"model_request_start","model":"..."}}
{"type":"event","event":{"type":"model_trace","traceId":"..."}}
{"type":"event","event":{"type":"thinking_start"}}
{"type":"event","event":{"type":"thinking_delta","delta":"..."}}   -> ThinkingEvent (xN)
{"type":"event","event":{"type":"thinking_end","text":"..."}}
{"type":"event","event":{"type":"message_update","content":[...]}}
{"type":"event","event":{"type":"text_delta","delta":"..."}}       -> AssistantTextDelta
{"type":"event","event":{"type":"message_end","content":[...]}}
{"type":"event","event":{"type":"model_request_end","model":"...","usage":{...},"stopReason":"end_turn","effort":...}}
{"type":"event","event":{"type":"turn_end","turnNumber":1,"hadToolCalls":false,"usage":{...}}}
{"type":"event","event":{"type":"run_end","result":{...}}}         -> IGNORED (summary; no subtype)
{"type":"result","subtype":"success","sessionId":"...","stopReason":"end_turn",
 "usage":{...},"durationMs":5059,"finalText":"PONG"}                -> ResultEvent (authoritative)
```

Notes for debugging:

- The top-level `result` line is the **only** authoritative result. It carries
  `subtype`, `sessionId`, `stopReason`, `usage`, `durationMs`, `finalText`.
  It has **no** `turnCount` (that lives in the `run_end` frame).
- `run_end`'s `result` dict nests `sessionId` under `nextState` and has no
  `subtype` — the parser must ignore it or it produces a spurious error.
- Tool runs add `tool_queued` / `tool_running` / `tool_completed` frames (see
  the earlier "tool" capture; not in `success.ndjson` because PONG made no
  tool calls). `tool_running` carries `{description, toolCallId, toolName}`.

### Error cases

Effort rejection (`effort_reject.*`):
```
# stdout (0 bytes — empty!)
# stderr:
Unknown effort "medium". Supported: high, max.
```
The wrapper regex `_EFFORT_REJECT_RE` matches this exact text; `exit 1` with
empty stdout is why `_parse_response` falls back to stderr text.

Genuine failure (`error_resume.*`):
```
# stdout:
{"type":"result","subtype":"error","usage":{...},"durationMs":10,"finalText":"",
 "error":"Error: No session \"no-such-session-xyz\" found to resume."}
# stderr:
Reasoning effort set to high for DeepSeek V4 Flash.
Error: No session "no-such-session-xyz" found to resume.
```
`finalText` is empty on errors — the message lives in the `error` field. The
stderr carries the same text prefixed by the `Reasoning effort set to ...`
note when an effort was applied. This is why the streaming path suppresses the
executor's post-stream error duplicate (stdout already had the result frame).

### Model list footer (`list_models.txt`)

The catalog ends with:
```
Pass the full id, or just the short name after the last "/":
cmd --model moonshotai/kimi-k2.5
cmd --model kimi-k2.5

Docs:  https://commandcode.ai/docs/reference/cli/models
```
`_parse_models` stops at `Docs:` / `Pass the full id`; the two
`cmd --model ...` lines have a single space and never match the 2+-space
model-row regex. If a CLI upgrade reorders the footer, this is the first place
to check.

