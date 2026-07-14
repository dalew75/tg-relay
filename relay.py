#!/usr/bin/env python
import asyncio
import logging
import json
from pprint import pformat
import re
from pathlib import Path

from allowlist import (
    add_source,
    format_listing,
    load_store,
    parse_command,
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
runtime_ids = set()
seen_sources = set()
nats_client = None


async def setup():
    global nats_client
    nats_client = await nats.connect(
        config.NATS_SERVER,
        user=config.NATS_USER or None,
        password=config.NATS_PASS or None,
    )

    user = await client.get_me()
    logger.info('Started serving as {}'.format(user.first_name))
    await client.get_dialogs()

    allowlist.update(load_store(Path(config.ALLOWLIST_PATH)))
    runtime_ids.update(allowlist.keys())

    for x in config.RELAY_MAP.split(','):
        if not x:
            continue

        key, value = x.split(':', 1)
        kid = int(key)
        RELAY_MAP[kid] = [v.strip() for v in value.split(',')]
        if kid not in allowlist:
            add_source(allowlist, kid, RELAY_MAP[kid], value.strip(), "env")

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

def get_message_link(event):
    if event.is_channel:
        if event.chat is None:
            return None
        if event.chat.username:
            return f"https://t.me/{event.chat.username}/{event.message.id}"
        else:
            # For private channels, use the unmarked ID (without -100 prefix)
            chat_id = str(event.chat.id).replace('-100', '')
            return f"https://t.me/c/{chat_id}/{event.message.id}"
    else:
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
    status = "ALLOWED" if source_id in allowlist else "NOT allowlisted"
    hint = (
        ""
        if source_id in allowlist
        else f" — to relay, reply /allow  (or send: /allow {source_id} {name!r})"
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


def runtime_sources():
    return {
        source_id: entry
        for source_id, entry in allowlist.items()
        if source_id in runtime_ids
    }


async def save_runtime_sources(event):
    try:
        save_store(Path(config.ALLOWLIST_PATH), runtime_sources())
        return True
    except OSError as error:
        logger.error("Unable to save allowlist: %s", error)
        await event.reply(f"Allowlist changed in memory but could not be saved: {error}")
        return False


async def command_target(event, args):
    if event.message.is_reply:
        reply = await event.message.get_reply_message()
        chat_id = reply.chat_id if reply is not None else event.chat_id
        return source_id_from_chat_id(chat_id), reply, args
    if args and args[0].isdigit():
        return int(args[0]), None, args[1:]
    return None, None, []


async def handle_command(event, parsed):
    cmd, args = parsed

    if cmd == "/list":
        await event.reply(format_listing(allowlist) or "(empty)")
        return True

    if cmd not in {"/allow", "/remove"}:
        return False

    target, reply, label_args = await command_target(event, args)
    if target is None:
        action = "allow" if cmd == "/allow" else "remove"
        await event.reply(
            f"Usage: reply /{action}"
            + (" [label]" if cmd == "/allow" else "")
            + f"  — or —  /{action} <id>"
            + (" [label]" if cmd == "/allow" else "")
        )
        return True

    if cmd == "/remove":
        if target in runtime_ids:
            remove_source(allowlist, target)
            runtime_ids.discard(target)
            await save_runtime_sources(event)
            await event.reply(f"Stopped relaying {target}")
        elif target in allowlist:
            await event.reply(
                f"{target} is seeded by RELAY_MAP — remove it from RELAY_MAP and restart."
            )
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
    kind = source_kind(reply) if reply is not None else "source"
    entry = add_source(allowlist, target, destinations, label or derived, kind)
    runtime_ids.add(target)
    await save_runtime_sources(event)
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

    source_id = source_id_from_chat_id(event.chat_id)
    await log_unseen_source(event, source_id)
    real_chat_id = event.chat_id
    message_link = get_message_link(event)
    entry = allowlist.get(source_id)
    if entry:
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

    # logger.info(f"Message from chat ID {event.chat.id if event.chat else 'None'} not found in RELAY_MAP")
    # logger.info(f"Available RELAY_MAP chat IDs: {list(RELAY_MAP.keys())}")
    for channelName in RELAY_MAP.get('default', []):
        logger.info('Sending message from {} to {}'.format(event.chat.id, channelName))
        if config.FORWARD:
            await client.forward_messages(channelName, event.message)
        else:
            message_text = event.message.message
            if message_link:
                message_text += f"\n\n[Original Message]({message_link})"
            await client.send_message(channelName, message_text, link_preview=False)

loop = asyncio.get_event_loop()
loop.run_until_complete(setup())
client.run_until_disconnected()
