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
    nats_client = await nats.connect(config.NATS_SERVER)

    user = await client.get_me()
    logger.info('Started serving as {}'.format(user.first_name))
    await client.get_dialogs()

    for x in config.RELAY_MAP.split(';'):
        if not x:
            return

        key, values = x.split(':', 1)
        values = values.split(',')
        if key == 'default':
            RELAY_MAP[key] = [int(x) for x in values]
        else:
            RELAY_MAP[int(key)] = [int(x) for x in values]


def get_message_link(event):
    if event.is_channel:
        if event.chat.username:
            return f"https://t.me/{event.chat.username}/{event.message.id}"
        else:
            return f"https://t.me/c/{event.chat.id}/{event.message.id}"
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
    message_link = get_message_link(event)
    for chat_id, relays in RELAY_MAP.items():
        if event.chat and event.chat.id == chat_id:
            
            for relay in relays:
                logger.info('----------------------------------------------------')
                logger.info('Sending message from {} to {}'.format(event.chat.id, relay))
                contractAddress = extract_ca(event.message.message)
                is_tradeable = bool(re.search(r"pump\.fun", event.message.message, re.IGNORECASE))
                
                subject = config.NATS_SUBJECT
                message = {
                    "chat_id": event.chat.id,
                    "contractAddress": contractAddress,
                    "isTradeable": is_tradeable,
                    "message": event.message.message,
                    "message_link": message_link
                }
                await nats_client.publish(subject, json.dumps(message).encode())
                
                logger.info(contractAddress)
                logger.info(event.message.message)
                # if config.FORWARD:
                #     await client.forward_messages(relay, event.message)
                # else:
                #     await client.send_message(relay, event.message)
            break
    else:
        for relay in RELAY_MAP.get('default', []):
            logger.info('Sending message from {} to {}'.format(event.chat.id, relay))
            if config.FORWARD:
                await client.forward_messages(relay, event.message)
            else:
                message_text = event.message.message
                if message_link:
                    message_text += f"\n\n[Original Message]({message_link})"
                await client.send_message(relay, message_text, link_preview=False)

loop = asyncio.get_event_loop()
loop.run_until_complete(setup())
client.run_until_disconnected()
