# TinyChat (c) 2026 WarWar <somethingstrenge@gmail.com>
# This file is part of TinyChat.
# TinyChat is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License v3.

import asyncio
import logging

from aiogram import Bot, Dispatcher
from imports.bot.utility import build_provider
from imports.auth.store import AuthStore
from imports.config import CONFIG, get_telegram_token
from imports.files.store import FileStore
from imports.mcp.manager import MCPManager
from imports.memory.store import MemoryStore
from imports.memory.conversation_store import ConversationStore
from imports.stt.whisper_client import WhisperClient
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
auth_store = AuthStore(PROJECT_ROOT / 'data' / 'state' / 'auth.json')
TELEGRAM_TOKEN = get_telegram_token()
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN is not set in environment variables or config.")
else:
    bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
logger = logging.getLogger(__name__)
lm = build_provider()
mem_store = MemoryStore(CONFIG)
mcp_mgr = MCPManager(CONFIG.get('mcp') or {}) if CONFIG.get('mcp') else None
conv_store = ConversationStore()
file_store = FileStore(project_root=PROJECT_ROOT, embed_fn=mem_store.embed_fn, embed_dim=mem_store.embed_dimension, qdrant_client=mem_store.qdrant)
whisper_client = WhisperClient(CONFIG.get('whisper', {}))
_user_buffers = {}
_user_flush_tasks = {}
typing_queue = asyncio.Queue()