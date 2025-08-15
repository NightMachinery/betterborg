# BetterBorg Development Commands

## Installation and Setup
```bash
# Install dependencies
pip3 install -r requirements.txt

# Additional non-Python requirements can be found in:
# NonPythonicRequirements.txt
```

## Running the Bot

### Standalone Mode (Development)
```bash
python3 stdborg.py
```

### Server Mode (FastAPI)
```bash
python3 start_server.py
# OR
uvicorn stdborg:app
```

### Plugin-Specific Instances

#### STT (Speech-to-Text) Instance
```bash
cd /path/to/betterborg && borg_session=session_stt borg_plugin_path=stt_plugins borg_brish_count=1 python3 stdborg.py
```

#### LLM Chat Instance  
```bash
cd /path/to/betterborg && borg_session=session_llm_chat borg_plugin_path=llm_chat_plugins borg_brish_count=1 python3 stdborg.py
```

## Environment Variables
- `borg_session`: Session name (default: "stdborg")
- `borg_plugin_path`: Plugin directory (default: "stdplugins")
- `borg_log_chat`: Chat ID for log messages
- `borgp`: SOCKS5 proxy port
- `borg_brish_count`: Number of brish instances

## System Requirements
- Python 3.7.2+
- macOS/Linux environment
- For shell functionality: dash, zsh, tmux, grealpath (GNU realpath)

## No Formal Testing/Linting
This project does not appear to have formal testing, linting, or formatting commands set up. Development follows a more ad-hoc approach with manual testing through the Telegram interface.