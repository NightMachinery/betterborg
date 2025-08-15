# BetterBorg Project Overview

## Purpose
BetterBorg is a highly modular and extensible Telegram userbot/bot built on Telethon and asyncio. It provides a Unix shell interface directly in Telegram through the "advanced_get" plugin (described as the "crust" of this fork), along with various AI integrations including LLM chat, speech-to-text, and text-to-speech capabilities.

## Core Features
- **Unix Shell in Telegram**: Execute shell commands and exchange files through Telegram interface
- **LLM Chat Integration**: Multiple LLM providers via litellm with streaming support
- **Speech Processing**: STT (speech-to-text) and TTS (text-to-speech) capabilities  
- **Image Generation**: Native Gemini image generation support
- **Time Tracking**: Built-in time tracking with webhook support
- **Plugin Architecture**: Hot-reloadable modular plugin system
- **FastAPI Integration**: REST API server capabilities

## Tech Stack
- **Core**: Python 3.7.2+, asyncio, Telethon (Telegram client)
- **Web Framework**: FastAPI with Pydantic settings, uvicorn
- **AI/ML**: litellm, google-genai with live support
- **Storage**: SQLite (via Peewee), SQLAlchemy, Redis
- **Media Processing**: Pillow, eyed3, typed-ffmpeg
- **Other Key Dependencies**: aiohttp, watchgod, brish, plotly, icecream

## Public Bot Instances
- **Transcribe Bot**: https://t.me/llm_stt_bot
- **Chat Bot**: https://t.me/vlm_chat_bot  
- **Say Bot**: https://t.me/say_this_bot