#!/usr/bin/env python
import asyncio
import logging
import json
from pprint import pformat
import re

from telethon import TelegramClient, events
import nats
import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)
client.start()

RELAY_MAP = {}
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

    for x in config.RELAY_MAP.split(','):
        if not x:
            continue

        key, value = x.split(':', 1)
        RELAY_MAP[int(key)] = [v.strip() for v in value.split(',')]
            
    logger.info(f"RELAY_MAP: {RELAY_MAP}")


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

@client.on(events.NewMessage)
async def my_event_handler(event):
    # Debug logging to see what chats are being processed
    # Keep track of which chats we've already logged info for
    if not hasattr(my_event_handler, 'logged_chats'):
        my_event_handler.logged_chats = set()

    # Only log chat info the first time we see this chat
    if event.chat and event.chat.id not in my_event_handler.logged_chats:
        # Get multiple forms of the chat ID for debugging
        raw_chat_id = event.chat.id
        
        # Import Telethon utilities for proper ID handling
        from telethon.utils import get_peer_id, resolve_id
        from telethon.tl.types import PeerChannel, PeerChat, PeerUser
        
        # Get the peer ID (this is the actual ID Telegram uses)
        peer_id = get_peer_id(event.chat)
        
        # Try to get the entity ID
        entity_id = event.chat_id if hasattr(event, 'chat_id') else None
        
        logger.info(
            f"New message received from previously unseen chat: "
            f"\n  Chat ID (raw event.chat.id): {raw_chat_id}"
            f"\n  Chat ID (get_peer_id): {peer_id}"
            f"\n  Chat ID (event.chat_id): {entity_id}"
            f"\n  Chat Type: {type(event.chat).__name__}"
            f"\n  Chat Title: {event.chat.title if hasattr(event.chat, 'title') else 'None'}"
            f"\n  Is Channel: {event.is_channel}"
            f"\n  Is Group: {event.is_group}"
            f"\n  Is Private: {event.is_private}"
            f"\n  Has megagroup attr: {hasattr(event.chat, 'megagroup')}"
            f"\n  Megagroup: {event.chat.megagroup if hasattr(event.chat, 'megagroup') else 'N/A'}"
            f"\n  RELAY_MAP keys: {list(RELAY_MAP.keys())}"
        )
        my_event_handler.logged_chats.add(event.chat.id)
    
    message_link = get_message_link(event)
    
    # Get the real chat ID (with proper negative formatting for groups/channels)
    real_chat_id = get_real_chat_id(event.chat) if event.chat else None
    
    for chat_id, channelNames in RELAY_MAP.items():
        if event.chat and event.chat.id == chat_id:
            
            for channelName in channelNames:
                logger.info(f'Channel: {channelName}')
                logger.info(f'Message: {event.message.message}')
                contractAddress = extract_ca(event.message.message)
                is_tradeable = bool(re.search(r"pump\.fun", event.message.message, re.IGNORECASE))
                
                subject = config.NATS_SUBJECT
                logger.info(f'NATS subject: {subject}')
                message = {
                    "chatId": event.chat.id,
                    "realChatId": real_chat_id,  # Use the real chat ID
                    "channelName": channelName,
                    "messageId": event.message.id,
                    "message": event.message.message,
                    "messageLink": message_link,
                    "userId": event.message.from_id.user_id if event.message.from_id else None,
                    "username": event.message.sender.username if event.message.sender else None
                }
                if contractAddress:
                    message["contractAddress"] = contractAddress
                if is_tradeable:
                    message["isTradeable"] = is_tradeable
                await nats_client.publish(subject, json.dumps(message).encode())
                
                logger.info(contractAddress)
                logger.info(event.message.message)
            break
    else:
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
