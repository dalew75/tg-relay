Telegram Message Relay
======================

Watches Telegram chats (channels, groups, and private DMs) as a normal user
account and publishes message metadata to NATS. Sources can be seeded from
`RELAY_MAP` at startup or added at runtime with `/allow`.

This is **not** a Telegram bot. It authenticates like Telegram Desktop and
runs as your user account.

Configuration
-------------

1. Register a Telegram App at [my.telegram.org](https://my.telegram.org/).
2. Set `API_ID` and `API_HASH` in a `.env` file or your environment.
3. Set `RELAY_MAP` and `NATS_SERVER` (see below).
4. Optionally set `SESSION_NAME` for session file location. Full paths are
   accepted; use a persistent path in Docker/Dokku.
5. Optionally set `ALLOWLIST_PATH` (default `data/allowlist.json`) for
   runtime allowlist storage.

Required environment variables:

| Variable | Description |
|----------|-------------|
| `API_ID` | Telegram API ID |
| `API_HASH` | Telegram API hash |
| `RELAY_MAP` | Comma-separated `source_id:label` pairs that seed the allowlist at startup |
| `NATS_SERVER` | NATS server URL |

Common optional variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `SESSION_NAME` | `session` | Telethon session file path |
| `NATS_SUBJECT` | `telegram-messages` | NATS subject for published messages |
| `NATS_USER` | *(empty)* | NATS username |
| `NATS_PASS` | *(empty)* | NATS password |
| `ALLOWLIST_PATH` | `data/allowlist.json` | Persisted runtime allowlist |
| `FORWARD` | `True` | Legacy forwarding for unmapped messages via `default` destinations |

Why do I have to register an App?
---------------------------------

This relay acts like a normal Telegram user. To make that work, it must be
registered as a Telegram client, like Telegram Desktop. Registering an app is
quick — see [Telegram's API ID guide](https://core.telegram.org/api/obtaining_api_id).

Setting up RELAY_MAP
--------------------

`RELAY_MAP` seeds the allowlist when the relay starts. Each entry is
`source_id:destination_label`, and multiple entries are separated by commas.

Use numeric chat/user IDs. Run [listchannels](#listchannels) to discover IDs.

Example (one label per source):

```
RELAY_MAP=-1001234567890:MyChannel,-1009876543210:OtherChannel
```

Each seeded source is published to NATS with `channelName` set to its
destination label. Entries seeded from `RELAY_MAP` cannot be removed with
`/remove` until you delete them from `RELAY_MAP` and restart.

**Note:** Because entries are comma-separated, a single `RELAY_MAP` entry
cannot safely contain multiple comma-separated destination labels. Use `/allow`
or edit `ALLOWLIST_PATH` for multi-destination sources.

Runtime allowlist (`/allow`, `/remove`, `/list`)
------------------------------------------------

After startup, the relay keeps an allowlist in memory and persists runtime
changes to `ALLOWLIST_PATH`. Commands are sent **as the relay account** (outgoing
messages from the logged-in user). They are not bot commands and are not sent
by people DMing the account.

### `/allow` — start relaying a source

Allow a channel, group, or **private DM** and publish its messages to NATS.

**Reply to a message** from the source:

```
/allow
/allow My Label
/allow Label One,Label Two
```
When the command is a Telegram reply, the relay reads the source ID from the
replied message automatically. `/allow` derives the label from that source's
username, title, or name; `/allow My Label` overrides only the label. Keep the
reply context attached when sending either form.

**Or send with an explicit ID** (from logs or `listchannels.py`):

```
/allow 123456789
/allow 123456789 @someuser
```

- With no label, the relay derives one from the source username, title, or name.
- With a label argument, that text becomes the NATS `channelName` (comma-separated
  values publish duplicate NATS messages, one per label).

On success the relay replies, for example:

```
Relaying 123456789 (@someuser) -> @someuser
```

### `/remove` — stop relaying a runtime source

```
/remove          (as a reply to a message from the source)
/remove 123456789
```

Sources seeded from `RELAY_MAP` must be removed from env and the relay restarted.

### `/list` — show the current allowlist

```
/list
```

Accepting private DMs with `/allow`
-----------------------------------

When someone DMs the relay account for the first time, their messages are **not**
published until you allow them. The relay logs something like:

```
New source (private) id=123456789 chat_id=123456789 name='Alice' NOT allowlisted — to relay, reply /allow  (or send: /allow <id> [label])
```

To accept DMs from that person:

1. Open the **private chat** with them in Telegram (logged in as the relay account).
2. **Reply** to any message they sent with `/allow`,  
   **or** send `/allow 123456789` using the `id` from the log.
3. Optionally add a label: `/allow Alice` or `/allow alice-dms`.
4. Confirm the relay's reply (`Relaying ... -> ...`).
5. Their **next messages** are published to NATS on `NATS_SUBJECT`.

The allowlist is saved to `ALLOWLIST_PATH`, so allowed DMs survive restarts.

NATS payload
------------

For each allowlisted source, each message is published as JSON to `NATS_SUBJECT`
(default `telegram-messages`). Core fields:

- `chatId`, `realChatId`, `channelName`, `messageId`, `message`, `messageLink`
- `userId`, `username`
- `contractAddress` (if the message contains `CA: ...`)
- `isTradeable` (if the message mentions `pump.fun`)

List Channels <a name="listchannels"></a>
-----------------------------------------

List dialogs and numeric IDs to build `RELAY_MAP` or `/allow` targets:

```sh
./listchannels.py
```

Legacy forwarding
-----------------

Unmapped messages that are not handled by the allowlist may still be forwarded
to destinations listed under a `default` key in `RELAY_MAP`. That path is
separate from the NATS publish flow. Set `FORWARD=False` to re-send without
Telegram's forward attribution.

YouTube transcripts (separate worker)
-------------------------------------

`youtube-transcripts.py` is not started by the Docker image. Run it manually if
needed; it consumes `youtube-transcript` on NATS and publishes summaries to
`youtube-summaries`.

Docker
------

```sh
docker build -t tg-relay .
docker run \
  -e API_ID=XXXX \
  -e API_HASH=XXXX \
  -e RELAY_MAP=-1001234567890:MyChannel \
  -e NATS_SERVER=nats://nats:4222 \
  -e SESSION_NAME="/data/session" \
  -e ALLOWLIST_PATH="/data/allowlist.json" \
  -v "$PWD/data:/data" \
  tg-relay
```

Dokku
-----

1. Set up persistent storage for the session and allowlist (`data/`).
2. Configure environment variables (`API_ID`, `API_HASH`, `RELAY_MAP`, `NATS_SERVER`, etc.).
3. Git push to Dokku.
4. Scale web to 0 and cmd to 1: `dokku ps:scale tg-relay web=0 cmd=1`
