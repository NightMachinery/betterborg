# BetterBorg Codebase Structure

## Entry Points
- **`stdborg.py`**: Main entry point with standalone mode and FastAPI integration
- **`start_server.py`**: Server mode startup script
- **`inline.py`**: Additional inline functionality

## Core Architecture

### Uniborg Core (`uniborg/`)
- **`uniborg.py`**: Main Uniborg class extending TelegramClient with plugin management
- **`util.py`**: Core utilities, admin functions, shell integration, file operations
- **`storage.py`**: Per-plugin storage system
- **`config.py`**: Configuration management
- **`_core.py`**: Core functionality
- **`*_util.py`**: Specialized utilities (bot, timetracker, llm, redis, history, tts, gemini_live)

### Plugin Directories
- **`stdplugins/`**: Standard plugins (main functionality)
  - `advanced_get.py`: Unix shell in Telegram (core feature)
  - `timetracker.py`: Time tracking functionality
  - `ieval.py`: Code evaluation
  - Other utility plugins
- **`llm_chat_plugins/`**: LLM chat integration
  - `llm_chat.py`: Main LLM chat implementation with multiple providers
- **`stt_plugins/`**: Speech-to-text plugins
- **`tts_plugins/`**: Text-to-speech plugins  
- **`image_gen_plugins/`**: Image generation plugins
- **`papersonegai_plugins/`**: Research/papers integration
- **`jlib_plugins/`**: Java library integration
- **`disabled_plugins/`**: Inactive plugins

## Plugin Architecture
- Plugins get automatic injection of `borg`, `logger`, `storage` variables
- Hot reload capability with file watching (watchgod)
- Per-plugin storage with automatic path management
- Event-driven architecture using Telethon decorators

## Configuration
- Environment variables for session management (`borg_session`, `borg_plugin_path`)
- SOCKS5 proxy support (`borgp`)
- Admin system with developer-defined admin users
- Plugin-specific configuration through storage system