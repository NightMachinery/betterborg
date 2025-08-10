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

# Conditionals

For enum-like conditionals, use explicit matching conditions, and raise an exception on `else` (when it signifies an unknown value).

------------------------------------------------------------------------

# Project Specific

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
- Uses SQLite databases for LLM chat history and user data
- Per-plugin storage system with automatic path management
- Storage objects are automatically injected into each plugin

### Admin System
Admin functionality is controlled through the `util.isAdmin()` function, with admin users defined in `uniborg/util.py`.
