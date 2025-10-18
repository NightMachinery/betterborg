# uniborg Utilities Agent Documentation

This file provides guidance for coding agents working with uniborg utility modules.

## llm_util.py - Message Helpers

### `send_info_message()` - Generic helper for informational messages

```python
async def send_info_message(
    event,
    text: str,
    *,
    auto_delete: AutoDeleteMode | bool | str = False,
    delay: int = AUTO_DELETE_TIME,
    prefix: str = BOT_META_INFO_PREFIX,
    reply_to=True,
    get_auto_delete_mode=None,
    **kwargs,
):
    """
    Sends an info message with automatic prefix and optional auto-deletion.

    Args:
        event: The event to reply to
        text: Message text (prefix will be prepended automatically)
        auto_delete: Auto-delete control:
            - False (default): No auto-deletion
            - True: Always auto-delete
            - "from_chat": Use get_auto_delete_mode callable if provided, else DISABLED
            - AutoDeleteMode enum value: Explicit mode (DISABLED/GROUP_ONLY/ALWAYS)
        delay: Delay before deletion in seconds (default: 30)
        prefix: Prefix to prepend (default: BOT_META_INFO_PREFIX)
        reply_to: How to send the message:
            - True (default): Use event.reply()
            - False/None/int/Message: Use event.respond() with this as reply_to kwarg
        get_auto_delete_mode: Optional callable(chat_id) -> AutoDeleteMode
            - Used when auto_delete="from_chat"
            - Allows plugins to inject their own auto-delete logic
        **kwargs: Additional arguments passed to reply()/respond() (e.g., parse_mode="md")

    Returns:
        The sent message object
    """
```

**Basic Usage (without auto-delete):**

```python
from uniborg.llm_util import send_info_message

# Simple info message
await send_info_message(event, "Processing complete!")

# With markdown formatting
await send_info_message(event, "**Bold** text", parse_mode="md")

# Send without replying to the triggering message
await send_info_message(event, "Broadcast message", reply_to=False)
```

**Advanced Usage (with custom auto-delete logic):**

```python
from uniborg.llm_util import send_info_message, AutoDeleteMode

# Define a function to determine auto-delete mode based on chat
def get_chat_auto_delete_mode(chat_id: int) -> AutoDeleteMode:
    # Your custom logic here
    if chat_id in special_chats:
        return AutoDeleteMode.ALWAYS
    return AutoDeleteMode.GROUP_ONLY

# Use "from_chat" with the lambda
await send_info_message(
    event,
    "This message may auto-delete",
    auto_delete="from_chat",
    get_auto_delete_mode=get_chat_auto_delete_mode
)

# Or always auto-delete regardless of chat
await send_info_message(event, "Temporary status", auto_delete=True)

# Or use explicit enum
await send_info_message(event, "Group only", auto_delete=AutoDeleteMode.GROUP_ONLY)
```

**When to use `send_info_message()`:**
- For all bot meta/informational messages (status updates, confirmations, errors)
- Replaces the pattern: `await event.reply(f"{BOT_META_INFO_PREFIX}...")`
- Provides consistent formatting across the codebase
- DO NOT use for actual LLM responses or user-generated content

**For plugin-specific wrappers:**
- See `llm_chat_plugins/AGENTS.md` for an example of creating a plugin-specific wrapper
- Plugins should create wrappers that pre-configure `get_auto_delete_mode` parameter
- This keeps plugin code clean while maintaining access to chat-specific settings

### `AutoDeleteMode` Enum

```python
class AutoDeleteMode(str, Enum):
    DISABLED = "disabled"      # Never auto-delete
    GROUP_ONLY = "group_only"  # Auto-delete in groups, keep in DMs
    ALWAYS = "always"          # Always auto-delete
```

### `AUTO_DELETE_TIME` Constant

Default time (in seconds) to wait before deleting info messages: `30`
