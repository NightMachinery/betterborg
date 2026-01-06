"""
Repackages messages sent in quick succession into a reply chain.

This plugin is active by default in all chats. It buffers incoming messages
for a short duration (1 second). Once the batch of messages is collected,
the originals are deleted, and their content is resent as a clean reply
chain, correctly handling text, media, and albums.
"""

import asyncio
import traceback
from collections import defaultdict
from itertools import groupby
from typing import Dict, List, Any

from telethon import events
from telethon.tl.types import Message


# --- Constants and State ---

# Delay in seconds to wait for more messages before processing a batch.
PROCESS_DELAY: float = 1.0

# In-memory state to buffer messages and track processing tasks per chat.
MESSAGE_BUFFER: Dict[int, List[Message]] = defaultdict(list)
PROCESSING_TASKS: Dict[int, asyncio.Task] = {}


# --- Core Logic ---


@borg.on(events.NewMessage)
async def message_collector(event: events.NewMessage.Event):
    """Buffers messages and schedules them for processing if no task is running."""
    chat_id = event.chat_id
    if not event.message:
        return

    # Add the new message to the buffer for this chat.
    MESSAGE_BUFFER[chat_id].append(event.message)

    # If a processing task is NOT already running for this chat, start one.
    # This prevents the timer from resetting with each new message.
    if chat_id not in PROCESSING_TASKS:
        task = borg.loop.create_task(process_buffer(chat_id))
        PROCESSING_TASKS[chat_id] = task


async def process_buffer(chat_id: int):
    """
    Waits for the process delay, then processes the message buffer for a chat,
    resending them as a reply chain and deleting the originals.
    """
    try:
        await asyncio.sleep(PROCESS_DELAY)

        # Retrieve and clear the buffer for this chat.
        messages_to_process = MESSAGE_BUFFER.pop(chat_id, [])

        if not messages_to_process:
            return

        # --- Sort and Group Messages ---
        # Ensure messages are in chronological order.
        messages_to_process.sort(key=lambda m: m.id)
        original_message_ids = [m.id for m in messages_to_process]

        # Group consecutive messages by their `grouped_id` to handle albums.
        grouped_items: List[Any] = []
        for key, group in groupby(messages_to_process, key=lambda m: m.grouped_id):
            message_group = list(group)
            if key is None:  # It's a single message, not part of an album.
                grouped_items.extend(message_group)
            else:  # It's an album.
                grouped_items.append(message_group)

        # --- Resend as Reply Chain ---
        last_sent_message_id = None
        for item in grouped_items:
            try:
                if isinstance(item, list):  # This is an album (a list of messages).
                    # Find the caption from the first message in the album that has one.
                    caption = next((m.text for m in item if m.text), "")
                    # The `file` argument can take a list of media to send as an album.
                    sent_album = await borg.send_file(
                        chat_id,
                        file=[m.media for m in item],
                        caption=caption,
                        reply_to=last_sent_message_id,
                    )
                    # The last message in the sent album becomes the new reply target.
                    if sent_album:
                        last_sent_message_id = sent_album[-1].id
                else:  # This is a single Message object.
                    # `send_message` can forward both text and media simultaneously.
                    sent_message = await borg.send_message(
                        chat_id,
                        message=item,  # Forward the entire message object
                        reply_to=last_sent_message_id,
                    )
                    if sent_message:
                        last_sent_message_id = sent_message.id
            except Exception as e:
                # If sending fails, log the error but don't stop the process.
                print(f"Failed to resend message/album in reply chain: {e}")

        # --- Cleanup ---
        # Delete all the original messages in a single batch.
        # await borg.delete_messages(chat_id, original_message_ids)

    except Exception as e:
        print(f"An error occurred in process_buffer: {e}")
        traceback.print_exc()
        # Ensure the buffer is cleared on unexpected errors.
        MESSAGE_BUFFER.pop(chat_id, None)
    finally:
        # IMPORTANT: Remove the task from tracking so a new one can be scheduled.
        PROCESSING_TASKS.pop(chat_id, None)
