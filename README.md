# TinyChatTG — Local LM Studio Telegram AI Bot

This repository implements a lightweight Telegram AI assistant that runs against a local LM Studio instance and uses a local memory store with resilient fallbacks.

**Quick summary**
- Local LM Studio provider adapter and chat wrapper.
- Persistent conversation store and memory (Qdrant + fastembed primary, JSON fallback).
- Orchestrator with primary/secondary queues, tool discovery, and approval flow.
- Telegram bot with `/start` (auth), `/new` (extract+clear), command registration, message-rate bans, and safer unknown-command handling.

**Implemented features (what's in the code today)**

- **Providers** — Adapters for both LM Studio and Gemini implemented in [imports/providers/](imports/providers/). They provide async `chat()` wrappers and handle provider-specific responses.
- **Orchestrator** — Central request flow in [imports/orchestrator.py](imports/orchestrator.py):
  - Primary queue for inbound user messages and secondary queue for background tasks (summarization, extraction, tool results).
  - Strict concurrency limits (semaphores) and RPM limits logic.
  - Integrates `ConversationStore` for persistent context and triggers summarization/extraction when conversation length exceeds limits.
  - Full tool-call loop execution, supporting both local tools and MCP tools.
  - Graceful shutdown hooks implemented.
- **MemoryStore** — fastembed + Qdrant path-mode integration with a robust JSON fallback implemented in [imports/memory/store.py](imports/memory/store.py).
  - Deduplication, path-mode search, and async model-based memory merging.
- **ConversationStore (persistent)** — per-user conversation history and summary saved under `data/state/conversations.db`.
  - Maintains `user`, `assistant`, and `tool` roles correctly for the LLM context window.
- **MCP manager** — [imports/mcp/manager.py](imports/mcp/manager.py) manages external MCP subprocesses (stdio JSON protocol), automatically handles discovery and allows execution.
- **Telegram bot** — [imports/bot.py](imports/bot.py):
  - Debounce buffer to automatically group multiple rapidly forwarded messages or image albums.
  - `/start` — persistent auth codes and abuse prevention.
  - `/new` — extracts facts into memories and starts a fresh conversation tail.
  - Inline keyboard approval flow (`Allow`/`Decline`) for tools requiring permission.
  - Real-time tool execution status updates sent to the chat.
- **AuthStore** — persistent auth, start/code bans, and message-rate limits (spam protection).
- **Logging** — app and per-user logs via [imports/utils/logger.py](imports/utils/logger.py).

- **Config & data locations**
  - Main config: [data/configs/app_config.yaml](data/configs/app_config.yaml)
  - Persistent state: `data/state/`
  - Memory DB and model cache: `data/memory/db/`, `data/memory/model/`
  - Images: `data/images/<user_id>/`

**How to run (development)**

1. Create and activate a Python 3.14 venv.
2. Install dependencies (project requirements may change):

```bash
python -m pip install -r requirements.txt
```

3. Set your Telegram token in `.env` file:

```bash
TELEGRAM_TOKEN="<your-token>"
```

4. Run the bot:

```bash
python -m imports.bot
```

5. Run unit tests:

```bash
pytest -q
```

**Prerequisites**

- **Python**: 3.10+ (project tested with 3.14). Create and activate a venv.
- **Optional services**: LM Studio (local OpenAI-compatible endpoint), TinyMCP (MCP subprocess), Qdrant (vector DB) and a fastembed-compatible embedding model for full-memory features.
- **Install deps**: `python -m pip install -r requirements.txt` (some features gracefully fallback when optional deps are missing).

**Disabling MCP / tools**

- By default the TinyMCP entry in `data/configs/app_config.yaml` is marked `enabled: false` to avoid launching external processes. If you do have an MCP you want the app to start, set `enabled: true` and provide a valid `command`/`args` in the config.
- The orchestrator will discover MCP tools at startup when an MCP manager is configured and enabled; you can also run the bot without MCP and the app will fall back to local tools (e.g., `remember_info`, `recall_info`).

**Troubleshooting**

- If the bot fails to import `aiogram`/`aiohttp`, install dependencies from `requirements.txt`.
- If LM Studio is unreachable, set the provider URL in `data/configs/app_config.yaml`.
- Logs are in the `logs/` directory (per-user logs and `app.log`). Increase verbosity in `data/configs/app_config.yaml` under `logging`.

**TODO (extracted from PROJECT_CONCEPT.md)**

- Implement modality fallbacks (e.g. use an image description model if the main model does not support multimodal input).
- Implement robust retry mechanisms and fallback models for handling provider errors (503/429).
- Document setup for optional local services (LM Studio, TinyMCP, Qdrant, fastembed).

**Known requirements & caveats**
- Dependencies like `aiogram`, `aiohttp`, `qdrant-client`, and `fastembed` are optional: the code uses fallbacks when not present but full features (Qdrant+fastembed) require them installed and a compatible environment.
- LM Studio URL is configured in [data/configs/app_config.yaml](data/configs/app_config.yaml). The default in the repo points to a local address — update as needed.
- Background model-based memory merging runs asynchronously and updates memories after the initial (fast) concatenation/merge. This design avoids blocking user flows but means merged content may appear slightly later.

**Files of interest**
- [imports/bot.py](imports/bot.py) — Telegram handlers and startup.
- [imports/orchestrator.py](imports/orchestrator.py) — core orchestration and summarization/extraction.
- [imports/memory/conversation_store.py](imports/memory/conversation_store.py) — persistent conversation history.
- [imports/memory/store.py](imports/memory/store.py) — memory backend (Qdrant + JSON fallback).
- [imports/auth/store.py](imports/auth/store.py) — persistent auth, rate-limiting and bans.
- [imports/providers/lm_studio.py](imports/providers/lm_studio.py) — LM Studio adapter.
- [imports/mcp/manager.py](imports/mcp/manager.py) — MCP subprocess manager.

**Next recommended improvements**
- Improve error boundaries for MCP subprocess failures.
- Provide more comprehensive documentation for running and deploying.
