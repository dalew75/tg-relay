#!/usr/bin/env python
import asyncio
import logging
import json
from pprint import pformat
import re
import os

import nats
import config
import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

nats_client = None

def get_video_id(youtube_url):
    """
    Extract the video ID from a YouTube URL.
    Args:
        youtube_url (str): The YouTube URL.
    Returns:
        str: The extracted video ID or None if not found.
    """
    pattern = r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})'
    match = re.search(pattern, youtube_url)
    return match.group(1) if match else None

def get_video_title(video_id):
    """
    Get the title of the YouTube video.
    Args:
        video_id (str): The YouTube video ID.
    Returns:
        str: The title of the video or "Unknown" if not found.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        matches = re.findall(r'<title>(.*?)</title>', response.text)
        return matches[0].replace(" - YouTube", "") if matches else "Unknown"
    except requests.RequestException as e:
        logger.error(f"Error fetching video title: {e}")
        return "Unknown"

def download_transcript(video_id):
    """
    Download the transcript and return as a string.
    Args:
        video_id (str): The YouTube video ID.
    Returns:
        str: The transcript text or an empty string if an error occurs.
    """
    try:
        # Create an instance of YouTubeTranscriptApi
        yt_api = YouTubeTranscriptApi()
        transcript_list = yt_api.list(video_id)
        transcript = transcript_list.find_generated_transcript(['en'])

        formatter = TextFormatter()
        transcript_text = formatter.format_transcript(transcript.fetch())

        # Remove timecodes and speaker names
        transcript_text = re.sub(r'\[\d+:\d+:\d+\]', '', transcript_text)
        transcript_text = re.sub(r'<\w+>', '', transcript_text)
        return transcript_text
    except Exception as e:
        logger.error(f"Error downloading transcript: {e}")
        return ""

async def process_youtube_transcript(youtube_link):
    """
    Process a YouTube link to extract and save the transcript.
    Args:
        youtube_link (str): The YouTube URL to process.
    """
    logger.info(f"Processing YouTube link: {youtube_link}")
    
    video_id = get_video_id(youtube_link)
    if not video_id:
        logger.error(f"Invalid YouTube URL: {youtube_link}")
        return

    logger.info(f"Extracted video ID: {video_id}")
    
    # Get video title
    video_title = get_video_title(video_id)
    logger.info(f"Video title: {video_title}")
    
    # Download transcript
    transcript_text = download_transcript(video_id)
    if not transcript_text:
        logger.error("Unable to download transcript")
        return

    # Create filename and save transcript
    file_name = f"{video_id}_{video_title}.txt"
    file_name = re.sub(r'[\\/*?:"<>|]', '', file_name)  # Remove invalid characters
    
    try:
        with open(file_name, 'w', encoding='utf-8') as file:
            file.write(transcript_text)
        logger.info(f"Transcript saved to {file_name}")
        
    except Exception as e:
        logger.error(f"Error saving transcript to file: {e}")

async def message_handler(msg):
    """Handle incoming messages from NATS subscription"""
    try:
        # Parse the incoming message as JSON
        data = json.loads(msg.data.decode())
        logger.info(f"Received YouTube transcript message: {pformat(data)}")
        
        # Check if the message contains a youtubeLink field
        if 'youtubeLink' in data:
            youtube_link = data['youtubeLink']
            await process_youtube_transcript(youtube_link)
        else:
            logger.warning("Message does not contain 'youtubeLink' field")
            
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON from message: {e}")
        logger.error(f"Raw message data: {msg.data}")
    except Exception as e:
        logger.error(f"Error processing message: {e}")

async def setup():
    """Setup NATS connection and subscription"""
    global nats_client
    try:
        nats_client = await nats.connect(config.NATS_SERVER)
        logger.info(f"Connected to NATS server: {config.NATS_SERVER}")
        
        # Subscribe to the 'youtube-transcript' subject
        await nats_client.subscribe('youtube-transcript', cb=message_handler)
        logger.info("Subscribed to 'youtube-transcript' subject")
        
        # Keep the connection alive
        while True:
            await asyncio.sleep(1)
            
    except Exception as e:
        logger.error(f"Error in setup: {e}")
        if nats_client:
            await nats_client.close()

async def main():
    """Main function to run the YouTube transcript subscriber"""
    try:
        await setup()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        if nats_client:
            await nats_client.close()

if __name__ == "__main__":
    asyncio.run(main())
