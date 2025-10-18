# llm_chat.py Agent Documentation

This file provides guidance for coding agents working with `llm_chat.py`.

## Helper Functions

### `send_info_message()` - Send informational/meta messages to users

```python
async def send_info_message(
    event,
    text: str,
    *,
    auto_delete: AutoDeleteMode | bool | str = False,
    delay: int = AUTO_DELETE_TIME,
    prefix: str = BOT_META_INFO_PREFIX,
    reply_to=True,
    **kwargs,
):
    """
    Sends an informational message with automatic prefix and optional auto-deletion.

    Args:
        event: The Telegram event to respond to
        text: The message text (BOT_META_INFO_PREFIX will be automatically prepended)
        auto_delete: Control auto-deletion behavior:
            - False (default): No auto-deletion
            - True: Always auto-delete
            - "from_chat": Use the chat's auto_delete_info_p setting
            - AutoDeleteMode enum value: Explicit mode (DISABLED/GROUP_ONLY/ALWAYS)
        delay: Seconds to wait before auto-deleting (default: 30)
        prefix: Prefix to prepend to the message (default: BOT_META_INFO_PREFIX)
        reply_to: How to send the message:
            - True (default): Use event.reply()
            - False/None/int/Message: Use event.respond() with this as reply_to kwarg
        **kwargs: Additional arguments passed to reply()/respond() (e.g., parse_mode="md")

    Returns:
        The sent message object
    """
```

**Usage Examples:**

```python
# Simple info message (no auto-delete)
await send_info_message(event, "Processing complete!")

# With markdown formatting
await send_info_message(event, "**Bold** text", parse_mode="md")

# Auto-delete based on chat settings (useful for admin messages)
await send_info_message(event, "Admin only action", auto_delete="from_chat")

# Always auto-delete
await send_info_message(event, "Temporary status", auto_delete=True)

# Send without replying to the triggering message
await send_info_message(event, "Broadcast message", reply_to=False)
```

**When to use `send_info_message()`:**
- For all bot meta/informational messages (status updates, confirmations, errors, etc.)
- Replaces the pattern: `await event.reply(f"{BOT_META_INFO_PREFIX}...")`
- Provides consistent formatting and automatic auto-deletion support
- DO NOT use for actual LLM responses or user-generated content

**When NOT to use `send_info_message()`:**
- For LLM-generated responses
- For messages that will be edited later (use manual construction with BOT_META_INFO_PREFIX)
- For user-generated content forwarding
