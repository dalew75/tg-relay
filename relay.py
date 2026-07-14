#!/usr/bin/env python
import asyncio
import logging
import json
from pprint import pformat
import re
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
nats_client = None
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
    global nats_client, mgmt_group_id, mgmt_group_entity, env_ids
    nats_client = await nats.connect(
        config.NATS_SERVER,
        user=config.NATS_USER or None,
        password=config.NATS_PASS or None,
    )

    user = await client.get_me()
    logger.info('Started serving as {}'.format(user.first_name))
    await client.get_dialogs()

    allowlist.update(load_store(Path(config.ALLOWLIST_PATH)))

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
        if target in env_ids and target not in sources_to_persist():
            await event.reply(
                f"{target} is seeded by RELAY_MAP — remove it from RELAY_MAP and restart."
            )
            return True
        if remove_source(allowlist, target):
            await save_allowlist(event)
            await event.reply(
                f"Reset {target}; it will be treated as new on the next message."
            )
        else:
            await event.reply(f"{target} is not in the allowlist.")
        return True

    if cmd == "/disallow":
        pending = (
            parse_pending_notification((reply.message or "") if reply else "")
            if reply is not None
            else None
        )
        label = (
            (pending or {}).get("name")
            or await derive_label(target)
        )
        kind = pending_kind or (source_kind(event) if reply is not None else "source")
        entry = disallow_source(allowlist, target, label, kind)
        await save_allowlist(event)
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
            await event.reply(f"Stopped relaying {target}")
        else:
            await event.reply(f"{target} is not allowlisted.")
        return True

    label = " ".join(label_args)
    derived = label or await derive_label(target)
    destinations = (
        [destination.strip() for destination in label.split(",") if destination.strip()]
        if label
        else [derived]
    )
    kind = pending_kind or (source_kind(event) if reply is not None else "source")
    entry = add_source(
        allowlist,
        target,
        destinations,
        label or derived,
        kind,
        status=STATUS_ALLOWED,
    )
    await save_allowlist(event)
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
        return

    source_id = source_id_from_chat_id(event.chat_id)
    if mgmt_group_id is not None and source_id == mgmt_group_id:
        return

    await log_unseen_source(event, source_id)
    real_chat_id = event.chat_id
    message_link = get_message_link(event)
    entry = allowlist.get(source_id)

    if is_denied(entry):
        return

    if is_allowed(entry):
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
                "userId": event.message.from_id.user_id if event.message.from_id else None,
                "username": (
                    event.message.sender.username
                    if event.message.sender
                    and getattr(event.message.sender, 'username', None)
                    else None
                ),
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

    if is_pending(entry):
        return

    await handle_new_pending_source(event, source_id)

loop = asyncio.get_event_loop()
loop.run_until_complete(setup())
client.run_until_disconnected()
