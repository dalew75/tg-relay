#!/usr/bin/env python
import asyncio
import logging
import json
from pprint import pformat
import re
import time
from pathlib import Path

from allowlist import (
    STATUS_ALLOWED,
    add_source,
    disallow_source,
    format_listing,
    format_pending_notification,
    is_allowed,
    is_denied,
    is_pending,
    load_store,
    mark_pending,
    parse_command,
    parse_pending_notification,
    remove_source,
    save_store,
    source_id_from_chat_id,
    source_record,
)

from telethon import TelegramClient, events
import nats
import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)
client.start()

RELAY_MAP = {}
allowlist = {}
env_ids = set()
seen_sources = set()
# source_id -> {lastMessageAt (epoch ms), messageCount, lastPreview}; in memory only
activity = {}
PREVIEW_CHARS = 120
nats_client = None
my_id = None
mgmt_group_id = None
mgmt_group_entity = None


async def resolve_mgmt_group(raw: str):
    from telethon.utils import get_peer_id

    key = int(raw.split(':', 1)[0].strip())
    candidates = [key]
    if key > 0:
        candidates.append(int(f"-100{key}"))
    for candidate in candidates:
        try:
            entity = await client.get_entity(candidate)
            return entity, source_id_from_chat_id(get_peer_id(entity))
        except Exception:
            continue
    raise ValueError(f"Unable to resolve MGMT_GROUP from {raw!r}")


async def setup():
    global nats_client, mgmt_group_id, mgmt_group_entity, env_ids, my_id
    nats_client = await nats.connect(
        config.NATS_SERVER,
        user=config.NATS_USER or None,
        password=config.NATS_PASS or None,
    )

    user = await client.get_me()
    my_id = user.id
    logger.info('Started serving as {}'.format(user.first_name))
    await client.get_dialogs()

    allowlist.update(load_store(Path(config.ALLOWLIST_PATH)))
    await nats_client.subscribe(config.SOURCES_SUBJECT, cb=handle_sources_request)

    for x in config.RELAY_MAP.split(','):
        if not x:
            continue

        key, value = x.split(':', 1)
        kid = int(key)
        env_ids.add(kid)
        RELAY_MAP[kid] = [v.strip() for v in value.split(',')]
        if kid not in allowlist:
            add_source(
                allowlist,
                kid,
                RELAY_MAP[kid],
                value.strip(),
                "env",
                status=STATUS_ALLOWED,
            )

    if config.MGMT_GROUP:
        mgmt_group_entity, mgmt_group_id = await resolve_mgmt_group(
            config.MGMT_GROUP
        )
        logger.info(
            "MGMT_GROUP: source_id=%s title=%r notify=%s",
            mgmt_group_id,
            getattr(mgmt_group_entity, 'title', None),
            config.MGMT_NOTIFY,
        )
    elif config.MGMT_NOTIFY:
        logger.warning("MGMT_NOTIFY is enabled but MGMT_GROUP is not set")

    logger.info(f"RELAY_MAP: {RELAY_MAP}")
    logger.info(f"Allowlist ({len(allowlist)} sources): {format_listing(allowlist)}")


def get_real_chat_id(chat):
    """
    Get the real chat ID that Telegram uses internally.
    Uses Telethon's get_peer_id utility to get the correct ID.
    """
    from telethon.utils import get_peer_id
    
    # get_peer_id returns the actual ID that Telegram uses
    # For supergroups/channels: negative with -100 prefix
    # For regular groups: negative
    # For users: positive
    return get_peer_id(chat)

def get_chat_link(event):
    if event.chat is None:
        return None
    username = getattr(event.chat, 'username', None)
    if username:
        return f"https://t.me/{username}"
    if event.is_channel or event.is_group:
        chat_id = str(event.chat.id).replace('-100', '')
        return f"https://t.me/c/{chat_id}"
    if event.is_private:
        return f"tg://user?id={event.chat_id}"
    return None


def get_message_link(event):
    if event.chat is None:
        return None
    username = getattr(event.chat, 'username', None)
    if username:
        return f"https://t.me/{username}/{event.message.id}"
    if event.is_channel or event.is_group:
        chat_id = str(event.chat.id).replace('-100', '')
        return f"https://t.me/c/{chat_id}/{event.message.id}"
    return None
    
def extract_ca(text):
    # Regex pattern to match the alphanumeric string after "CA:"
    pattern = r"CA:\s*([A-Za-z0-9]+)"
    
    # Search for the pattern in the given text
    match = re.search(pattern, text)
    
    # If a match is found, return the matched group (the alphanumeric string)
    if match:
        return match.group(1)
    else:
        return None

async def safe_name(event):
    source_id = source_id_from_chat_id(event.chat_id)
    try:
        entity = await event.get_chat()
        return (
            getattr(entity, 'title', None)
            or getattr(entity, 'username', None)
            or getattr(entity, 'first_name', None)
            or str(source_id)
        )
    except Exception:
        return str(source_id)


async def resolve_sender(event):
    try:
        # get_sender() falls back to an API fetch when the entity isn't cached
        sender = await event.get_sender()
    except Exception as e:
        logger.warning(f"Could not resolve sender {event.sender_id}: {e}")
        return None, None
    if sender is None:
        return None, None
    username = getattr(sender, 'username', None)
    if not username:
        for entry in getattr(sender, 'usernames', None) or []:
            if getattr(entry, 'active', False):
                username = entry.username
                break
    display_name = (
        " ".join(
            filter(None, [getattr(sender, 'first_name', None), getattr(sender, 'last_name', None)])
        )
        or getattr(sender, 'title', None)
        or None
    )
    return username, display_name


async def log_unseen_source(event, source_id):
    if source_id is None or source_id in seen_sources:
        return
    seen_sources.add(source_id)
    name = await safe_name(event)
    kind = (
        "private"
        if event.is_private
        else ("group" if event.is_group else "channel")
    )
    status = "ALLOWED" if is_allowed(allowlist.get(source_id)) else (
        "DENIED" if is_denied(allowlist.get(source_id)) else (
            "PENDING" if is_pending(allowlist.get(source_id)) else "NOT allowlisted"
        )
    )
    hint = ""
    if source_id not in allowlist:
        hint = (
            f" — to relay, reply /allow  (or send: /allow {source_id} {name!r})"
        )
    logger.info(
        f"New source ({kind}) id={source_id} chat_id={event.chat_id} "
        f"name={name!r} {status}{hint}"
    )


async def derive_label(source_id):
    try:
        entity = await client.get_entity(source_id)
        username = getattr(entity, 'username', None)
        if username:
            return f"@{username}"
        return (
            getattr(entity, 'first_name', None)
            or getattr(entity, 'title', None)
            or str(source_id)
        )
    except Exception:
        return str(source_id)


def source_kind(event):
    if event.is_private:
        return "user"
    if event.is_group:
        return "chat"
    return "channel"


def message_preview(event):
    text = event.message.message
    if text:
        return text
    if event.message.media:
        return f"[{type(event.message.media).__name__}]"
    return "[empty]"


async def mark_pending_source(event, source_id):
    name = await safe_name(event)
    kind = (
        "user"
        if event.is_private
        else ("chat" if event.is_group else "channel")
    )
    mark_pending(allowlist, source_id, name, kind)
    await save_allowlist()
    await publish_source_event(source_id)
    logger.info("Marked source %s (%s) as pending", source_id, name)
    return name, kind


async def notify_mgmt_group(event, source_id):
    # TODO(low): Reduce MGMT_GROUP flooding risk — rate-limit/digest notifications,
    # backoff on Telegram send failures, and cap pending posts per time window.
    if mgmt_group_entity is None or not config.MGMT_NOTIFY:
        return False
    if source_id == mgmt_group_id:
        return False

    name = await safe_name(event)
    kind = (
        "user"
        if event.is_private
        else ("chat" if event.is_group else "channel")
    )
    text = format_pending_notification(
        kind=kind,
        name=name,
        source_id=source_id,
        chat_id=event.chat_id,
        message_preview=message_preview(event),
        chat_link=get_chat_link(event),
        message_link=get_message_link(event),
    )
    try:
        await client.send_message(
            mgmt_group_entity,
            text,
            link_preview=False,
            parse_mode='html',
        )
        logger.info(
            "Posted pending notification for %s (%s) to MGMT_GROUP",
            source_id,
            name,
        )
        return True
    except Exception as error:
        logger.error(
            "Unable to notify MGMT_GROUP %s: %s",
            mgmt_group_id,
            error,
        )
        return False


async def handle_new_pending_source(event, source_id):
    entry = allowlist.get(source_id)
    if entry is not None:
        return

    notified = await notify_mgmt_group(event, source_id)
    if not notified and config.MGMT_NOTIFY:
        logger.warning(
            "MGMT_GROUP notify failed for %s; marking pending anyway to avoid retries",
            source_id,
        )
    await mark_pending_source(event, source_id)


def sources_to_persist():
    persisted = {}
    for source_id, entry in allowlist.items():
        if source_id not in env_ids:
            persisted[source_id] = entry
        elif not is_allowed(entry):
            persisted[source_id] = entry
    return persisted


async def save_allowlist(event=None):
    try:
        save_store(Path(config.ALLOWLIST_PATH), sources_to_persist())
        return True
    except OSError as error:
        logger.error("Unable to save allowlist: %s", error)
        if event is not None:
            await event.reply(
                f"Allowlist changed in memory but could not be saved: {error}"
            )
        return False


def record(source_id):
    entry = allowlist.get(source_id)
    if entry is None:
        return None
    return source_record(
        source_id,
        entry,
        env=source_id in env_ids,
        activity=activity.get(source_id),
    )


async def publish_source_event(source_id):
    entry = allowlist.get(source_id)
    payload = (
        {"op": "changed", "id": source_id, "source": record(source_id)}
        if entry is not None
        else {"op": "removed", "id": source_id}
    )
    try:
        await nats_client.publish(
            config.SOURCES_EVENT_SUBJECT, json.dumps(payload).encode()
        )
    except Exception as error:
        logger.error("Unable to publish source event for %s: %s", source_id, error)


def note_activity(event, source_id):
    seen = activity.setdefault(source_id, {"messageCount": 0})
    seen["lastMessageAt"] = int(time.time() * 1000)
    seen["messageCount"] += 1
    seen["lastPreview"] = message_preview(event)[:PREVIEW_CHARS]


async def allow_source_id(target, label, kind, event=None):
    """Allow `target`; `label` may list comma-separated destinations. Returns (entry, destinations)."""
    derived = label or await derive_label(target)
    destinations = (
        [destination.strip() for destination in label.split(",") if destination.strip()]
        if label
        else [derived]
    )
    entry = add_source(
        allowlist,
        target,
        destinations,
        label or derived,
        kind,
        status=STATUS_ALLOWED,
    )
    await save_allowlist(event)
    await publish_source_event(target)
    return entry, destinations


async def deny_source_id(target, label, kind, event=None):
    entry = disallow_source(allowlist, target, label or await derive_label(target), kind)
    await save_allowlist(event)
    await publish_source_event(target)
    return entry


async def reset_source_id(target, event=None):
    """Forget `target`. Returns an error text, or None once it is gone."""
    if target in env_ids and target not in sources_to_persist():
        return f"{target} is seeded by RELAY_MAP — remove it from RELAY_MAP and restart."
    if not remove_source(allowlist, target):
        return f"{target} is not in the allowlist."
    await save_allowlist(event)
    await publish_source_event(target)
    return None


def list_records(status="all"):
    records = [
        record(source_id)
        for source_id, entry in allowlist.items()
        if status == "all" or entry.get("status", STATUS_ALLOWED) == status
    ]
    records.sort(key=lambda r: (r["lastMessageAt"] or 0, r["label"].lower()), reverse=True)
    return records


async def sources_api(request):
    op = request.get("op")
    if op == "list":
        status = request.get("status") or "all"
        if status not in {"all", "allowed", "pending", "denied"}:
            return {"success": False, "error": f"unknown status {status!r}"}
        return {
            "success": True,
            "subject": config.NATS_SUBJECT,
            "sources": list_records(status),
        }
    if op not in {"allow", "deny", "reset"}:
        return {"success": False, "error": f"unknown op {op!r}"}
    try:
        target = int(request.get("id"))
    except (TypeError, ValueError):
        return {"success": False, "error": "id must be a numeric source id"}
    if mgmt_group_id is not None and target == mgmt_group_id:
        return {"success": False, "error": "MGMT_GROUP is never relayed"}
    existing = allowlist.get(target) or {}
    kind = existing.get("kind", "source")
    if op == "reset":
        error = await reset_source_id(target)
        if error:
            return {"success": False, "error": error}
        return {"success": True, "id": target, "source": None}
    if op == "deny":
        await deny_source_id(target, existing.get("label"), kind)
        return {"success": True, "id": target, "source": record(target)}
    label = str(request.get("label") or "").strip()
    await allow_source_id(target, label, kind)
    return {"success": True, "id": target, "source": record(target)}


async def handle_sources_request(msg):
    try:
        request = json.loads(msg.data.decode() or "{}")
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        response = await sources_api(request)
    except Exception as error:
        logger.error("Sources request failed: %s", error)
        response = {"success": False, "error": str(error)}
    if msg.reply:
        try:
            await msg.respond(json.dumps(response).encode())
        except Exception as error:
            logger.error("Unable to answer sources request: %s", error)


async def command_target(event, args):
    if event.message.is_reply:
        reply = await event.message.get_reply_message()
        if reply is not None:
            pending = parse_pending_notification(reply.message or "")
            if pending:
                return pending["source_id"], reply, args, pending["kind"]
            return (
                source_id_from_chat_id(reply.chat_id),
                reply,
                args,
                None,
            )
    if args and args[0].isdigit():
        return int(args[0]), None, args[1:], None
    return None, None, [], None


async def handle_command(event, parsed):
    cmd, args = parsed

    if cmd == "/list":
        await event.reply(format_listing(allowlist) or "(empty)")
        return True

    if cmd not in {"/allow", "/remove", "/disallow", "/reset"}:
        return False

    target, reply, label_args, pending_kind = await command_target(event, args)
    if target is None:
        if cmd == "/reset":
            await event.reply("Usage: reply /reset  — or —  /reset <id>")
            return True
        action = "allow" if cmd == "/allow" else "remove" if cmd == "/remove" else "disallow"
        await event.reply(
            f"Usage: reply /{action}"
            + (" [label]" if cmd == "/allow" else "")
            + f"  — or —  /{action} <id>"
            + (" [label]" if cmd == "/allow" else "")
        )
        return True

    if cmd == "/reset":
        error = await reset_source_id(target, event)
        await event.reply(
            error or f"Reset {target}; it will be treated as new on the next message."
        )
        return True

    if cmd == "/disallow":
        pending = (
            parse_pending_notification((reply.message or "") if reply else "")
            if reply is not None
            else None
        )
        label = (pending or {}).get("name")
        kind = pending_kind or (source_kind(event) if reply is not None else "source")
        entry = await deny_source_id(target, label, kind, event)
        await event.reply(f"Ignoring {target} ({entry['label']})")
        return True

    if cmd == "/remove":
        if target in env_ids:
            await event.reply(
                f"{target} is seeded by RELAY_MAP — remove it from RELAY_MAP and restart."
            )
        elif is_allowed(allowlist.get(target)):
            remove_source(allowlist, target)
            await save_allowlist(event)
            await publish_source_event(target)
            await event.reply(f"Stopped relaying {target}")
        else:
            await event.reply(f"{target} is not allowlisted.")
        return True

    label = " ".join(label_args)
    kind = pending_kind or (source_kind(event) if reply is not None else "source")
    entry, destinations = await allow_source_id(target, label, kind, event)
    await event.reply(
        f"Relaying {target} ({entry['label']}) -> {', '.join(destinations)}"
    )
    return True


@client.on(events.NewMessage)
async def my_event_handler(event):
    if event.out:
        parsed = parse_command(event.message.message or "")
        if parsed and await handle_command(event, parsed):
            return
        if not config.RELAY_OUTGOING:
            return

    source_id = source_id_from_chat_id(event.chat_id)
    if mgmt_group_id is not None and source_id == mgmt_group_id:
        return

    await log_unseen_source(event, source_id)
    note_activity(event, source_id)
    real_chat_id = event.chat_id
    message_link = get_message_link(event)
    entry = allowlist.get(source_id)

    if is_denied(entry):
        return

    if is_allowed(entry):
        username, display_name = await resolve_sender(event)
        user_id = event.sender_id
        if user_id is None and event.out:
            user_id = my_id
        for channelName in entry["destinations"]:
            logger.info(f'Channel: {channelName}')
            logger.info(f'Message: {event.message.message}')
            contractAddress = extract_ca(event.message.message)
            is_tradeable = bool(re.search(r"pump\.fun", event.message.message, re.IGNORECASE))
            message = {
                "chatId": source_id,
                "realChatId": real_chat_id,
                "channelName": channelName,
                "messageId": event.message.id,
                "message": event.message.message,
                "messageLink": message_link,
                "userId": user_id,
                "username": username,
                "displayName": display_name,
                "isOutgoing": bool(event.out),
            }
            if contractAddress:
                message["contractAddress"] = contractAddress
            if is_tradeable:
                message["isTradeable"] = is_tradeable
            await nats_client.publish(
                config.NATS_SUBJECT, json.dumps(message).encode()
            )
            logger.info(contractAddress)
            logger.info(event.message.message)
        return

    if is_pending(entry) or event.out:
        return

    await handle_new_pending_source(event, source_id)

loop = asyncio.get_event_loop()
loop.run_until_complete(setup())
client.run_until_disconnected()
