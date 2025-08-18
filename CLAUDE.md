# AGENT.md

This file provides guidance to coding agents when working with code in this repository.

------------------------------------------------------------------------

-   DRY.

    -   Find common patterns in the code that can refactored into shared code.

-   Use dependency injection to improve code flexibility - let components receive their dependencies from outside instead of hardcoding them. For example, pass configurations as arguments or inject service instances through constructors. However, never inconvenience the user. The dependencies must always be optional to provide.

-   Do NOT add comments about what you have changed, e.g., `newly added`. The user uses version control software to manually review the changes.

------------------------------------------------------------------------

# Functions

-   Have any non-obvious function arguments be keyword arguments. Have at most two positional arguments. Use `(pos_arg1, ..., *, kwarg,)` to enforce keyword argument usage.

# Dataclasses

Define dataclasses when you need to return multiple items, not tuples.

# Conditionals

For enum-like conditionals, use explicit matching conditions, and raise an exception on `else` (when it signifies an unknown value).

------------------------------------------------------------------------

# Project Specific

## Initial Context

Start by reading these files completely: uniborg/*.py stt_plugins/*.py llm_chat_plugins/*.py .

When a file is too big to read directly, read its first 500 lines, and also search for various definitions inside it to get a map of the file (e.g., classes, functions, ...). Then read parts that you need from this map.

## Development Commands

### Installation and Setup
```bash
pip3 install -r requirements.txt
```

### Running the Bot
The main entry point is `stdborg.py` which can be run in two modes:

**Standalone mode (development):**
```bash
python3 stdborg.py
```

**Server mode (with FastAPI):**
```bash
python3 start_server.py
# or with uvicorn directly:
uvicorn stdborg:app
```

### Plugin-Specific Instances
Different bot instances can be run with different plugin sets using environment variables:

**STT (Speech-to-Text) instance:**
```bash
cd /path/to/betterborg && borg_session=session_stt borg_plugin_path=stt_plugins borg_brish_count=1 python3 stdborg.py
```

**LLM Chat instance:**
```bash
cd /path/to/betterborg && borg_session=session_llm_chat borg_plugin_path=llm_chat_plugins borg_brish_count=1 python3 stdborg.py
```

## Architecture Overview

### Core Components
- **Uniborg**: The main Telegram client class (`uniborg/uniborg.py`), extends TelegramClient with plugin loading and management capabilities
- **Plugin System**: Modular architecture where functionality is organized into plugins in separate directories
- **Storage**: Each plugin gets its own storage instance for persistent data
- **FastAPI Integration**: REST API server with timetracker webhook support

### Plugin Architecture
Plugins are Python files that get the `borg`, `logger`, and `storage` variables injected automatically:

```python
# Example plugin structure
from telethon import events

@borg.on(events.NewMessage(pattern='pattern'))
async def handler(event):
    await event.reply('response')
```

### Plugin Directories
- **`stdplugins/`**: Standard plugins (main functionality)
- **`llm_chat_plugins/`**: LLM chat integration plugins
- **`stt_plugins/`**: Speech-to-text plugins  
- **`papersonegai_plugins/`**: Papers/research integration
- **`jlib_plugins/`**: Java library integration
- **`disabled_plugins/`**: Inactive plugins

### Key Plugins
- **`advanced_get.py`**: Unix shell in Telegram with file exchange capabilities (the "crust" of this fork)
- **`llm_chat.py`**: LLM integration with multiple providers via litellm
- **`timetracker.py`**: Time tracking functionality with webhook support
- **`stt.py`**: Speech-to-text processing

### Environment Variables
- `borg_session`: Session name (default: "stdborg")
- `borg_plugin_path`: Plugin directory (default: "stdplugins") 
- `borg_log_chat`: Chat ID for log messages
- `borgp`: SOCKS5 proxy port
- `borg_brish_count`: Number of brish instances

### Plugin Loading and Hot Reload
- Plugins are automatically loaded from the specified plugin directory
- File watching enables hot reload on plugin modifications
- Plugins can be reloaded individually using the `reload_plugin` method

### Database and Storage
- Per-plugin storage system with automatic path management
- Storage objects are automatically injected into each plugin

### Admin System
The (user)bot has some admins which are the bot developers (nothing to do with Telegram admins). These admins can be detected using the `util.isAdmin()` function, with admin users defined in `uniborg/util.py`.

### Bot Commands Registration
When adding new slash commands to any plugin (e.g., `llm_chat_plugins/llm_chat.py`, `stt_plugins/stt.py`), you **MUST** update the `BOT_COMMANDS` list in that plugin to register them with Telegram.

The preferred pattern is to decouple the handler definition from its registration. This allows for dynamic pattern creation, for example, using a `BOT_USERNAME` variable that is only available after the bot has initialized.

**Important Notes:**
- Commands in `BOT_COMMANDS` automatically appear in Telegram's command menu.
- The `command` field should match the pattern handler (without the `/` prefix).
- Provide clear, concise descriptions for user guidance.
- Commands are registered with Telegram via `SetBotCommandsRequest` using the BOT_COMMANDS list.
- Missing commands from this list won't appear in the Telegram UI command suggestions.
- The `/help` command of the plugin should include information about the important commands the plugin has. You might need to update the info this command shows.

**Registration Pattern:**
```python
# 1. Define BOT_COMMANDS list
BOT_COMMANDS = [
    {"command": "mycommand", "description": "Brief description of mycommand"},
    # ... other commands
]

# 2. Define the command handler function WITHOUT a decorator
async def my_command_handler(event):
    # handler implementation

# 3. Define a registration function to attach handlers to events
def register_handlers():
    """Dynamically registers all event handlers."""
    # This allows using variables initialized later, like BOT_USERNAME
    bot_username_suffix_re = f"(?:{re.escape(BOT_USERNAME)})?" if BOT_USERNAME else ""
    
    borg.on(events.NewMessage(pattern=rf"(?i)/mycommand{bot_username_suffix_re}"))(my_command_handler)
    # ... register other handlers ...

# 4. Create an initialization function for the plugin
async def initialize_my_plugin():
    # ... perform initial setup, like getting BOT_USERNAME ...
    
    # Now, register the handlers with the now-defined variables
    register_handlers()

    # Register the commands with Telegram's UI
    await borg(
        SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code="en",
            commands=[
                BotCommand(c["command"], c["description"]) for c in BOT_COMMANDS
            ],
        )
    )

# 5. Schedule the plugin's initialization to run on the event loop
borg.loop.create_task(initialize_my_plugin())
```

