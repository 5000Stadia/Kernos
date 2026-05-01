# Install Guide

Complete setup for running Kernos locally.

## Requirements

- **Python 3.11 or later**
- **Node.js** (optional — required for MCP servers that run via `npx`, including Google Calendar integration)
- **An LLM provider credential.** One of:
  - Anthropic API key
  - OpenAI Codex (ChatGPT OAuth credentials)
  - Ollama (local models — no external credential needed, but requires Ollama running locally)
- **At least one messaging adapter credential.** One of:
  - Discord bot token + Discord user ID for the owner
  - Twilio account credentials + phone numbers
  - Telegram bot token

A minimal setup runs on Anthropic + Discord and needs nothing else. Everything beyond that is optional capability expansion.

## Quick install

```
git clone https://github.com/5000Stadia/Kernos.git
cd Kernos
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edit `.env` to fill in your credentials. See the [Environment Configuration](#environment-configuration) section below for what each variable does.

Start the server:

```
python kernos/server.py
```

On first run, Kernos creates its data directory (default `./data`) and initializes the SQLite databases for instance state, knowledge facts, relational messages, and conversation logs.

## Launcher paths

Kernos ships three distinct launchers. Pick the one that matches how you'll talk to it.

### Discord (default) — `python kernos/server.py`

The full runtime: Discord adapter, scheduler, awareness, optional Twilio SMS adapter (when credentials are present), optional Telegram adapter (when token is present). This is what most operators run.

```
python kernos/server.py
```

**Required env:**
- `DISCORD_BOT_TOKEN` — bot credential
- `DISCORD_OWNER_ID` — the user-id Kernos accepts as owner
- `KERNOS_INSTANCE_ID=discord:<owner-id>` — instance identity

**First-run checklist:**
1. Create a Discord application + bot at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable the **Message Content Intent** under the bot's "Privileged Gateway Intents."
3. Generate an OAuth invite URL with `bot` scope + `Send Messages` / `Read Message History` permissions; invite the bot to a server you control, or DM it directly.
4. Copy the bot token into `.env` as `DISCORD_BOT_TOKEN`.
5. Set `DISCORD_OWNER_ID` to your Discord user id and `KERNOS_INSTANCE_ID` to `discord:<that-id>`.
6. Start the server. The first DM you send begins onboarding.

### Twilio webhook (SMS-first) — `uvicorn kernos.app:app`

Lighter footprint, exposes a Twilio-compatible HTTP webhook. Use when SMS is the primary surface and you don't want the Discord runtime running.

```
uvicorn kernos.app:app --host 0.0.0.0 --port 8000
```

**Required env:**
- `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` + `TWILIO_PHONE_NUMBER` — Twilio credentials
- `OWNER_PHONE_NUMBER` — your phone, in E.164 format
- `KERNOS_INSTANCE_ID=sms:<owner-phone>` — instance identity

**First-run checklist:**
1. Buy a Twilio phone number with SMS capability.
2. Configure the number's "A message comes in" webhook to point at your public URL plus `/twilio/sms` (use ngrok or similar for local development).
3. Drop the credentials and your phone number into `.env`.
4. Run `uvicorn kernos.app:app`. Text the Twilio number — Kernos onboards the same way Discord does.

### CLI chat — `python -m kernos.chat`

Local terminal chat. No adapter, no scheduler, no awareness — just turn-by-turn conversation against the same handler + reasoning service. Useful for testing prompts, exercising tools, and offline development.

```
python -m kernos.chat
```

**Required env:** an LLM provider credential (`ANTHROPIC_API_KEY` or equivalent — see [LLM Provider](#llm-provider)). No adapter credentials needed.

**First-run checklist:**
1. Install + configure the LLM provider as you would for any other launcher.
2. Optionally set `KERNOS_INSTANCE_ID=cli:<your-name>` to give the local instance a stable id.
3. Run `python -m kernos.chat`. Type to talk; Ctrl-C to exit.

---

## Environment Configuration

### LLM Provider

Pick one provider by setting `KERNOS_LLM_PROVIDER`:

**Anthropic (default)**
```
KERNOS_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

**OpenAI Codex (ChatGPT OAuth)**
```
KERNOS_LLM_PROVIDER=openai-codex
# Either place credentials at .credentials/openai-codex.json, or set via env:
OPENAI_CODEX_ACCESS_TOKEN=...
OPENAI_CODEX_REFRESH_TOKEN=...
OPENAI_CODEX_EXPIRES=...
OPENAI_CODEX_ACCOUNT_ID=...
OPENAI_CODEX_MODEL=gpt-5.5
```

**Ollama (local)** — configure via fallback-chain environment variables. See the provider chain section below.

### Messaging Adapters

Kernos supports multiple messaging channels simultaneously. Each adapter is independent; configure any you want to use.

**Discord**
```
DISCORD_BOT_TOKEN=your-discord-bot-token
DISCORD_OWNER_ID=your-discord-user-id
KERNOS_INSTANCE_ID=discord:your-discord-user-id
```

The `KERNOS_INSTANCE_ID` ties your Discord and SMS identities to the same Kernos instance. Format is `discord:USER_ID`.

**Twilio SMS** (optional)
```
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_PHONE_NUMBER=+1...
OWNER_PHONE_NUMBER=+1...
```

**Telegram** (optional)
```
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
```

When `TELEGRAM_BOT_TOKEN` is set, Kernos starts its Telegram poller on server startup; when unset, the Telegram adapter is skipped cleanly.

### Google Calendar (optional)

For calendar integration via MCP:

```
GOOGLE_OAUTH_CREDENTIALS_PATH=/path/to/your/gcp-oauth.keys.json
```

Create OAuth credentials at Google Cloud Console → APIs & Services → Credentials. Node.js is required because the Google Calendar MCP server runs via `npx`.

### Optional Services

```
BRAVE_API_KEY=...          # Web search via Brave Search API
VOYAGE_API_KEY=...         # Enhanced embeddings (falls back to hash-only dedup)
```

These are not required. Missing credentials degrade cleanly — search falls back to what's available, embeddings fall back to hash-based dedup.

### Storage

```
KERNOS_DATA_DIR=./data     # Root directory for all persistent data
```

Kernos creates subdirectories under this for instances, spaces, members, conversation logs, and diagnostics.

### Provider Fallback Chains

Kernos uses three named provider chains (`primary`, `simple`, `cheap`) built from environment configuration. Default construction uses the primary provider (Anthropic or Codex) for all three chains, but chains can be customized. See `kernos/providers/chains.py` for chain construction.

Ollama endpoints are configured through chain-specific variables.

### Debug & Testing

```
# Override compaction threshold for testing
# (default 8000 estimated tokens; set 500 to trigger after ~5-6 exchanges)
KERNOS_COMPACTION_THRESHOLD=500
```

## Auto-Update

**`KERNOS_AUTO_UPDATE`** — controls whether Kernos checks for and applies updates at startup.

### `on` (default)

On each startup, Kernos checks `origin/main` for new commits. If the remote is ahead of your local clone, Kernos pulls the new code, reinstalls dependencies, and restarts itself before proceeding to normal startup. The first member turn after an update surfaces a brief summary of what changed.

The update is skipped without blocking startup if:

- The install directory is not a git clone
- The working tree has uncommitted or untracked changes
- The network is unavailable or the remote is unreachable
- The local history has diverged from `origin/main` (non-fast-forwardable)

### `off`

Skip the startup update check entirely. Use this when you want to control update cadence manually (running `git pull && pip install -e . && systemctl restart kernos` on your own schedule).

### Daily background pull — `KERNOS_AUTO_UPDATE_TIME`

When `KERNOS_AUTO_UPDATE=on`, Kernos also runs a daily background pull at the configured local time. Default `03:00`. The pull lands code on disk and **applies on the next natural restart** — it does not force a mid-flight restart. Pick an hour you don't expect to be active.

```
KERNOS_AUTO_UPDATE_TIME=04:00
```

Format is 24-hour `HH:MM` in the server's local clock. Malformed values fall back to `03:00` with a log warning.

If a pull fails (network down, divergence, dirty tree), the daily window logs and skips — there's no retry, no escalation. The next day's window tries again.

### Verbose announcements — `KERNOS_AUTO_UPDATE_VERBOSE`

When on, the next interaction after an update lands surfaces an ephemeral message naming the new commit and recent subjects:

```
Updated to commit 3b053f3. 7 changes pulled: "Pass 5: dispatch path registry", ...
```

The announcement is **ephemeral** — it does not persist in conversation log, is not harvested into memory, and does not pass through compaction. It fires once per restart and clears itself.

```
KERNOS_AUTO_UPDATE_VERBOSE=on
```

Default `off`. The persistent whisper-based summary continues to fire when verbose is off.

### `KERNOS_UPDATE_BRANCH`

Optional override. Defaults to `main`. Set to a different branch name if you want to track a non-main line (e.g., `dogfood`, `pre-release`).

## Workspace Scope

**`KERNOS_WORKSPACE_SCOPE`** — controls filesystem access for code the agent writes and runs.

### `isolated` (default)

The agent's code execution can only read and write files inside the active space directory. Catches accidental and casually-malicious filesystem access.

A determined adversary who can write Python can bypass this via `ctypes` or native syscalls — this is not a security boundary against hostile code execution. It's a scope boundary against workspace spillover.

Use this for: multi-member Kernos, shared machines, anytime "my agent shouldn't casually wander around my filesystem" is a valuable property.

### `unleashed`

The agent's code execution has the same filesystem access the Kernos process itself has. Read anywhere, write anywhere, chdir anywhere.

Use this for: single-user operator setups where the agent is an extension of you, and you want workspace tools that can reach across your whole machine.

## Workspace Builder Backend

**`KERNOS_BUILDER`** — which tool writes and runs code for the workspace.

### `native` (default)

Kernos's own Python subprocess with best-effort isolation (clean env, scoped cwd, restricted PYTHONPATH). No external dependencies. Not a hard sandbox against hostile code execution — see [Workspace Scope](#workspace-scope) for the scope-vs-security boundary.

### `aider`

Hand workspace build tasks to [Aider](https://aider.chat), an open-source AI pair-programming tool. Aider ships as a Kernos dependency — no separate install required.

**By default, Aider uses the same model as Kernos's primary chain**, automatically:

- `KERNOS_LLM_PROVIDER=anthropic` → Aider uses Kernos's primary Anthropic model with your `ANTHROPIC_API_KEY`.
- `KERNOS_LLM_PROVIDER=ollama` → Aider uses `ollama_chat/$OLLAMA_MODEL` with `OLLAMA_API_BASE` + `OLLAMA_API_KEY` (works for both local and cloud Ollama).
- `KERNOS_LLM_PROVIDER=openai-codex` → Aider cannot consume Codex OAuth tokens; you must set `AIDER_MODEL` + `AIDER_API_KEY` explicitly.

**Override: pick a different model for Aider.** Set `AIDER_MODEL` in your `.env` to any aider-supported model string (e.g. `gpt-4o`, `ollama_chat/llama3`, `claude-opus-4-6`). When set, Aider uses that model regardless of Kernos's primary. `AIDER_API_KEY` supplies the matching credential if Kernos's primary credential doesn't cover it. A future `kernos setup llm` wizard step will surface this as a prompt; for now, the env vars are the knob.

Aider is a scoped backend — it respects `KERNOS_WORKSPACE_SCOPE=isolated`. An isolated-mode Aider invocation cannot read or write outside the active space (with a limited read-only carve-out for Python runtime paths so Aider's own imports work).

### `claude-code`, `codex`

Planned integrations for hand-off to external builder agents. Config is accepted but dispatch currently returns not-yet-implemented. Adapters ship as follow-on work.

### Scoped vs Unscoped backends

`native` and `aider` run as Python processes that Kernos can wrap with scope enforcement. `claude-code` and `codex` run as Node binaries that Kernos cannot wrap — they operate at OS level with their own filesystem access.

The Isolated scope toggle only enforces on scoped backends. If you select Isolated scope with an Unscoped backend, Kernos logs a startup warning stating that the external builder is not constrained by scope. Proceed if you trust the external builder the way you'd trust any coding agent you run in a terminal.

## Verifying Install

After `python kernos/server.py` starts, Kernos begins listening on its configured adapters. Send a message to your Discord bot or SMS number. The first interaction triggers onboarding — Kernos will ask you your name, then guide you through a conversational hatching sequence that produces your per-member agent.

Run the test suite to verify the local install:

```
pytest -q
```

A clean install with all dependencies present should show a green test run.

## Running in Production

Kernos is a single-host orchestrator (one main process, with subprocesses for code execution and external builders). Deployment options:

- **Local machine** — run under `tmux` or similar; simple, low-overhead
- **Always-on VPS** — run under systemd or supervisor; recommended for actual personal use
- **Container** — Dockerfile forthcoming; build your own from the quick-install steps

Kernos has no database server requirement (SQLite is embedded) and no external orchestration requirement. A 1GB-RAM VPS running Python 3.11+ is sufficient.

Do not run Kernos behind a public HTTP endpoint without careful consideration — adapters like Discord and Telegram are designed for authenticated-user messaging, not public HTTP surfaces. The current architecture assumes the server process is reachable by its message adapters, not by arbitrary internet traffic.

## First-boot canvases

On the first boot after a CANVAS-V1 install, Kernos seeds four canvases:

- **System Reference** (team scope, unpinned) — populated from the shipped `/docs/architecture/*.md` files plus an auto-generated kernel-tool index. Gives the agent an authoritative source for Kernos-internal questions (*what's a context space*, *how does the gate work*).
- **Workflow Patterns** (team scope, unpinned) — seeded from `/docs/workflow-patterns/`. Pattern 00 (`library-meta`) plus 18 domain patterns (software development, long-form campaign, legal case, household management, …). Judgment-input substrate for the future Gardener cohort, which will consult these patterns at canvas creation and during continuous canvas evolution. Members do not interact with this canvas directly — they see canvases shaped by its patterns.
- **Our Procedures** (team scope, unpinned) — starts empty except for an index page. Team-wide procedures land here as they emerge.
- **My Tools** (personal scope, one per member) — created when a member finishes onboarding (`bootstrap_graduated=True`). Workspace tools the member registers via `register_tool` get auto-appended as pages.

Seeding is idempotent — canvases already present at boot are skipped. Delete a canvas (archive it) to trigger re-seeding on the next restart.

**Deployment-shape note.** System Reference and Workflow Patterns seeding read from `docs/architecture/` and `docs/workflow-patterns/` on disk respectively. Source checkouts have both; pip-installed or container deployments where `docs/` is stripped will skip those canvases with warnings — Our Procedures and per-member My Tools still seed. Warnings are logged as `CANVAS_SEED_WARNING` on startup. To populate on such deployments, either copy the docs into the expected paths before boot, or author the pages via `page_write` post-boot.

**No auto-sync.** Once seeded, Kernos does not refresh System Reference from later repo updates — the canvas is now member-editable state. A `reseed_system_reference` tool is parked for future work.

## Troubleshooting

**Import errors on first run.** Confirm the virtual environment is activated and `pip install -e .` completed without errors. On some systems, Python 3.11 isn't the default; use `python3.11` explicitly.

**"No LLM provider configured" errors.** Check that `KERNOS_LLM_PROVIDER` matches a configured provider credential. The default is `anthropic`; if you're using Codex, set the variable explicitly.

**Discord bot doesn't respond.** Check that `DISCORD_BOT_TOKEN` is valid and the bot has been invited to a server or direct-messaged by the configured `DISCORD_OWNER_ID`. The bot needs message-read and message-send permissions.

**Compaction triggers incorrectly.** The default compaction threshold is tuned for production context windows. If testing with very short sessions, either wait for natural turn counts or override `KERNOS_COMPACTION_THRESHOLD`.

**MCP servers fail to connect.** Node.js must be installed and `npx` must be on the PATH. Check that the MCP server packages can be invoked directly before debugging Kernos-side integration.

## Getting Help

Kernos is a single-maintainer project at early-access stage. Report issues at the [GitHub repo](https://github.com/5000Stadia/Kernos).

## Related

- **[README](../README.md)** — project overview
- **[Roadmap](roadmap.md)** — what's coming next
