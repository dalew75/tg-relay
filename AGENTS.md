# Repository Guidelines

## Project Overview

`tg-relay` is a small Python Telegram user-client relay. It authenticates with Telethon as a normal Telegram account, watches allowlisted chats (channels, groups, and private DMs), and publishes selected message metadata to NATS. Sources are seeded from `RELAY_MAP` at startup and can be changed at runtime via `/allow`, `/remove`, and `/list`. A separate root script can consume YouTube transcript requests from NATS, write transcript text files, and publish transcript summaries.

This is not a Telegram bot. Changes that touch runtime behavior usually need Telegram API credentials, a persisted Telethon session, and a reachable NATS server to verify end to end.

## Architecture & Data Flow

- `relay.py` is the main process and Docker entrypoint.
  1. Imports `config.py`, constructs `TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)`, and calls `client.start()` at module import/startup.
  2. `setup()` connects a module-global `nats_client` to `config.NATS_SERVER`, optionally using `NATS_USER`/`NATS_PASS`.
  3. `setup()` loads the persisted allowlist from `config.ALLOWLIST_PATH` via `allowlist.load_store()`.
  4. `setup()` parses `config.RELAY_MAP` into the module-global `RELAY_MAP` and seeds any missing keys into the in-memory allowlist with `kind="env"`.
  5. `@client.on(events.NewMessage)` registers `my_event_handler(event)`.
  6. Outgoing messages from the relay account are checked for `/allow`, `/remove`, `/disallow`, `/reset`, and `/list` via `allowlist.parse_command()` and `handle_command()`. Non-command outgoing messages are dropped unless `RELAY_OUTGOING=True`, in which case they continue to step 7 (but never create `pending` entries or unseen-source logs).
  7. For `allowed` sources, the handler publishes JSON to `config.NATS_SUBJECT`. Payload fields include `chatId`, `realChatId`, `channelName`, `messageId`, `message`, `messageLink`, `userId`, `username`, and `isOutgoing` (`userId` falls back to the relay account id for outgoing messages without `from_id`); `contractAddress` and `isTradeable` are conditional.
  8. Unknown sources are marked `status=pending` in `ALLOWLIST_PATH`. Optional `MGMT_NOTIFY` posts one notification per new source to `MGMT_GROUP` (disabled by default).
  9. `denied` sources are silently ignored; `pending` sources are not relayed or re-notified. `MGMT_GROUP` is never relayed or marked pending, even if it also appears in `RELAY_MAP`.
  10. Runtime allowlist changes are persisted to `ALLOWLIST_PATH` through `save_allowlist()`.
  11. The bottom of the file runs `loop.run_until_complete(setup())` and `client.run_until_disconnected()` without an `if __name__ == "__main__"` guard.
- `allowlist.py` holds JSON load/save, source add/remove, `source_id_from_chat_id()` (via Telethon `resolve_id`), command parsing, and listing format helpers.
- `youtube-transcripts.py` is a separate NATS worker, not launched by the Dockerfile.
  1. Subscribes to literal NATS subject `youtube-transcript`.
  2. Expects JSON with `youtubeLink`; optional `chatId` is propagated.
  3. Extracts a YouTube video ID, fetches the page title, selects an English generated transcript, writes `<video_id>_<sanitized title>.txt` in the current working directory, and publishes JSON to literal subject `youtube-summaries`.
- `listchannels.py` is an authenticated Telethon helper that prints `Channel | ID` for dialogs to help build `RELAY_MAP` or `/allow` targets.
- `config.py` is the eager environment schema for all scripts. Because all runtime scripts import it, missing required variables can fail even helper commands.

Known behavior notes when editing:
- `RELAY_MAP` is comma-separated `source_id:label` entries. Commas cannot safely separate multiple destination labels within one entry because the same delimiter splits top-level entries; use `/allow` or `allowlist.json` for multi-destination sources.
- README previously documented semicolon-separated multi-relay syntax and chat-to-chat forwarding as the primary path; runtime NATS publishing is driven by the allowlist. Legacy forwarding still exists for `RELAY_MAP.get('default', [])`, but setup cannot create a string `default` key because it casts keys to `int`.
- `/allow`, `/remove`, and `/list` only run on **outgoing** messages (`event.out`) from the relay account, not on inbound commands from other users. With `RELAY_OUTGOING=True`, non-command outgoing messages in `allowed` sources are also published; command replies posted inside an allowed chat may be published too.

## Key Directories

There is no package directory or `src/` tree. Repository-owned code lives at the project root.

- `relay.py` — primary Telegram-to-NATS relay with runtime allowlist commands.
- `allowlist.py` — persisted allowlist storage and command helpers.
- `youtube-transcripts.py` — standalone YouTube transcript NATS consumer/producer.
- `config.py` — environment-backed runtime configuration.
- `listchannels.py` — Telegram dialog ID discovery utility.
- `README.md` — user-facing setup and `/allow` documentation.
- Root `*.txt` transcript files — generated artifacts, not source or fixtures unless a future change explicitly promotes them.
- `venv/`, `.env`, `*.session*`, and `data/` — local runtime/secrets/state; do not inspect or commit their contents.

## Development Commands

No Makefile, package scripts, CI workflow, formatter, linter, type checker, or test runner is configured.

Observed/documented commands:

```sh
# Install runtime dependencies locally
python -m pip install -r requirements.txt

# Start the main relay locally, after required env/session setup
python relay.py

# List Telegram dialogs and numeric IDs for RELAY_MAP
./listchannels.py
# or
python listchannels.py

# Start the transcript worker manually, if needed
python youtube-transcripts.py

# Build the Docker image
docker build -t tg-relay .

# Run via Docker
docker run -e API_ID=XXXX -e API_HASH=XXXX -e RELAY_MAP=... -e NATS_SERVER=... -e SESSION_NAME="/data/session" -e ALLOWLIST_PATH="/data/allowlist.json" -v "$PWD/data:/data" tg-relay

# Dokku scaling from README
dokku ps:scale tg-relay web=0 cmd=1
```

Use focused Python syntax checks when changing code because no automated suite exists:

```sh
python -m py_compile relay.py allowlist.py youtube-transcripts.py config.py listchannels.py
```

Keep `README.md` and `AGENTS.md` synchronized with every committed behavior,
configuration, command, architecture, or workflow change. Update both before
staging future commits; do not commit runtime changes with stale documentation.

## Code Conventions & Common Patterns

- Style is plain Python scripts, not a package: module-level functions, module-level clients, and mutable globals (`client`, `nats_client`, `RELAY_MAP`, `allowlist`, `runtime_ids`).
- Names are mostly `snake_case` for functions (`get_real_chat_id`, `handle_command`, `save_runtime_sources`). Some locals and NATS fields use camelCase (`channelName`, `contractAddress`); prefer existing local style when making narrow edits, but use `snake_case` for new helpers.
- Four-space indentation. No project formatter is configured; keep diffs minimal and avoid broad restyling.
- Logging pattern: `logging.basicConfig(level=logging.INFO)` plus `logger = logging.getLogger(__name__)`; operational failures are logged with `logger.error(...)`/`logger.warning(...)`.
- Async pattern: long-running processes use `asyncio`, Telethon callbacks, and `nats-py` async publish/subscribe. Avoid blocking calls in new async paths; existing `youtube-transcripts.py` currently performs `requests`, transcript fetching, and file I/O inline in the event loop.
- Error handling differs by script:
  - `relay.py` catches allowlist save failures and entity lookup failures in command helpers; setup, parsing, and publish failures can still propagate.
  - `youtube-transcripts.py` catches `JSONDecodeError`, broad processing errors, file-write errors, and NATS publish errors; helper failures return sentinel values such as `None`, `"Unknown"`, or `""`.
- Dependency/config pattern: scripts import `config` directly and instantiate external clients directly. There is no dependency injection layer, factory, schema model, or class hierarchy.
- NATS messages are JSON dictionaries encoded to bytes with `json.dumps(...).encode()` or `.encode('utf-8')`.
- Telegram session state is controlled by `SESSION_NAME`; prefer explicit paths for deployed/persistent sessions.
- Allowlist entries are shaped `{destinations: list[str], label: str, kind: str}` with `kind` in `env`, `user`, `chat`, `channel`, or `source`.

## Important Files

- `config.py` — authoritative config surface:
  - Required: `API_ID`, `API_HASH`, `RELAY_MAP`, `NATS_SERVER`.
  - Defaults: `FORWARD=True`, `SESSION_NAME='session'`, `NATS_SUBJECT='telegram-messages'`, `NATS_USER=''`, `NATS_PASS=''`, `ALLOWLIST_PATH='data/allowlist.json'`, `MGMT_GROUP=''`, `MGMT_NOTIFY=False`, `RELAY_OUTGOING=False`.
- `relay.py` — key functions:
  - `setup()` connects NATS, loads allowlist, and seeds `RELAY_MAP` into allowlist.
  - `get_real_chat_id(chat)` normalizes Telethon peer IDs.
  - `get_message_link(event)` builds public or `/c/` Telegram links.
  - `extract_ca(text)` parses `CA:` alphanumeric tokens.
  - `log_unseen_source(event, source_id)` logs first contact and `/allow` hints for private DMs.
  - `notify_mgmt_group(event, source_id)` optionally posts pending-approval notifications when `MGMT_NOTIFY=True`.
  - `handle_new_pending_source(event, source_id)` marks unknown sources `pending` and persists to `ALLOWLIST_PATH`.
  - `handle_command(event, parsed)` implements `/allow`, `/remove`, `/disallow`, `/reset`, `/list`.
  - `my_event_handler(event)` handles inbound Telegram messages and publishes NATS payloads for allowlisted sources.
- `allowlist.py` — key functions:
  - `load_store(path)` / `save_store(path, sources)` — JSON persistence under `{"sources": {...}}`.
  - `add_source()` / `mark_pending()` / `disallow_source()` / `remove_source()` — mutate allowlist entries with `status` in `allowed`, `pending`, or `denied`.
  - `source_id_from_chat_id(chat_id)` — normalized source ID for channels, groups, and users.
  - `parse_command(text)` — split `/command` and args from outgoing message text.
  - `format_pending_notification(...)` / `parse_pending_notification(text)` — MGMT_GROUP notification body and reply parsing.
  - `format_listing(sources)` — human-readable allowlist for `/list` and logs.
- `youtube-transcripts.py` — key functions:
  - `get_video_id(youtube_url)` parses supported YouTube URL forms.
  - `get_video_title(video_id)` fetches the page title via `requests`.
  - `download_transcript(video_id)` selects generated English transcripts.
  - `message_handler(msg)` decodes NATS input, writes transcript files, and publishes summaries.
- `listchannels.py` — prints dialog names and IDs using Telethon.
- `requirements.txt` — runtime dependencies only; no dev/test dependencies.
- `Dockerfile` — `python:3-alpine`, `WORKDIR /app`, `COPY . .`, `pip install -r requirements.txt`, `CMD "./relay.py"`.
- `.gitignore` / `.dockerignore` — exclude `.env`, session files, and `data/`; `.dockerignore` does not exclude `venv/`, `__pycache__/`, generated transcript `.txt`, or the root `.deb` artifact.

## Runtime/Tooling Preferences

- Runtime: Python 3. Docker currently uses floating `python:3-alpine`; local scripts use `#!/usr/bin/env python`.
- Package manager: plain `pip` with `requirements.txt`. There is no lockfile, `pyproject.toml`, Poetry, Pipenv, uv, Conda, or dev requirements file.
- Core dependencies: `Telethon==1.36.0`, `nats-py==2.8.0`, `python-decouple==3.8`, `youtube-transcript-api>=0.6.0`, and `requests`.
- Treat `.env` and `*.session*` as secrets/runtime state. Do not read, print, commit, or include their values in generated artifacts.
- First Telegram login may be interactive through Telethon unless a valid session file already exists. Persistent storage is required for Docker/Dokku-style deployments.
- Both relay and transcript flows require a reachable NATS server. NATS subjects are partly configurable (`NATS_SUBJECT` for relay output) and partly hard-coded (`youtube-transcript`, `youtube-summaries`).
- Persist `ALLOWLIST_PATH` alongside the session in deployments so runtime `/allow` changes survive restarts.
- Docker context is broad because `Dockerfile` uses `COPY . .`; avoid adding large/generated root files, and update `.dockerignore` if build reproducibility matters.

## Testing & QA

There is no first-party automated test suite or configured QA tooling.

- No `tests/`, `test_*.py`, `*_test.py`, `pytest.ini`, `tox.ini`, `.coveragerc`, CI workflow, lint config, formatter config, or type-check config was found.
- `requirements.txt` contains runtime dependencies only; no `pytest`, coverage, mocking, linting, or typing tools are declared.
- Do not treat dependency tests under `venv/` as project tests; `venv/` is ignored local state.
- Minimum safe check for code edits:

```sh
python -m py_compile relay.py allowlist.py youtube-transcripts.py config.py listchannels.py
```

- Behavioral verification for relay changes requires live Telegram + NATS setup:
  - Valid `API_ID`, `API_HASH`, `RELAY_MAP`, `NATS_SERVER`, and persistent `SESSION_NAME`.
  - Confirm `relay.py` publishes the expected JSON payload on `NATS_SUBJECT` for an allowlisted channel or DM.
  - For private DM support: DM the relay account, send `/allow` as a reply from the relay account, confirm persistence in `ALLOWLIST_PATH`, and confirm subsequent messages publish to NATS.
  - For commands: verify `/list` output and `/remove` stops publishing without affecting `RELAY_MAP`-seeded entries until env changes.
- Behavioral verification for transcript changes requires a NATS message on `youtube-transcript` with `youtubeLink`, network access to YouTube/transcript endpoints, and confirmation of both local transcript file output and `youtube-summaries` publish.
- Because `relay.py` and `listchannels.py` execute Telethon startup at import/top level, future unit tests should isolate those side effects before importing modules directly.

## Future work (low priority)

- **MGMT_GROUP notification flooding:** `MGMT_NOTIFY` defaults off because failed Telegram posts previously retried on every message from the same unknown source. A future fix should add per-source rate limits, digest/batch pending notifications, exponential backoff on send failures, and clearer operator controls before re-enabling broadcast notify by default.
