# TinyChatTG — Local LM Studio Telegram AI Bot

This repository implements a lightweight Telegram AI assistant that runs against a local LM Studio instance and uses a local memory store with resilient fallbacks.

**Quick summary**
- Local LM Studio provider adapter and chat wrapper.
- Persistent conversation store and memory (Qdrant + fastembed primary, JSON fallback).
- Orchestrator with primary/secondary queues, tool discovery, and approval flow.
- Telegram bot with `/start` (auth), `/new` (extract+clear), command registration, message-rate bans, and safer unknown-command handling.

**Implemented features (what's in the code today)**

- **LM Studio provider** — adapter implemented in [imports/providers/lm_studio.py](imports/providers/lm_studio.py). It provides `chat()` and `chat_text()` wrappers and handles provider-specific responses and retries.

- **MemoryStore** — fastembed + Qdrant path-mode integration with a robust JSON fallback implemented in [imports/memory/store.py](imports/memory/store.py).
  - Deduplication and merge thresholds.
  - Immediate concatenation merge for similar memories and optional async model-based merge via a `merge_callback` (background task updates both Qdrant and JSON stores).
  - `add_memory()`, `search()`, and helper methods.

- **ConversationStore (persistent)** — per-user conversation history and summary saved under `data/state/conversations.json`. Implemented at [imports/memory/conversation_store.py](imports/memory/conversation_store.py).
  - Stores every message as `{role,text,ts,meta?}` so user/assistant/tool entries are preserved.
  - Methods: `append_message()`, `get_history()`, `set_history()`, `clear_history()`, `set_summary()`, `get_summary()`.

- **Orchestrator** — central request flow in [imports/orchestrator.py](imports/orchestrator.py):
  - Primary queue (inbound user messages) and secondary queue (background tasks: summarization, extraction, merge updates).
  - Integrates `ConversationStore` for persistent context and triggers summarization/extraction when conversation is large.
  - Discovers MCP tools (if MCP manager is configured), maps permissions, and builds `functions` schema passed to the model.
  - Records assistant replies, function call intents, and tool outputs into the conversation history.
  - Summarization + extraction: uses the model to produce a concise conversation summary and extract facts, which are stored in memory.

- **Manual memory tool** — local tool `manual_memory` to add memories from the bot, implemented in [imports/tools/manual_memory.py](imports/tools/manual_memory.py).

- **MCP manager skeleton** — [imports/mcp/manager.py](imports/mcp/manager.py) launches and talks to an MCP subprocess (stdio JSON protocol) and exposes `list_tools()` and `send_sync()` used by the orchestrator.

- **AuthStore (persistent auth + bans + rate limits)** — implemented in [imports/auth/store.py](imports/auth/store.py).
  - Persisted codes for `/start` flow, start/code bans for abuse, message-rate tracking (`record_message()`), and temporary message bans (burst/rate limits).

- **Telegram bot** — [imports/bot.py](imports/bot.py):
  - `/start` — generate auth codes and persist attempts.
  - `/new` — runs summarization/extraction on the user's persisted conversation and clears the conversational context; errors are logged to server and per-user logs.
  - Unknown `/...` commands are rejected rather than forwarded to the model.
  - Per-message rate checks: deletes messages when banned, notifies user.
  - Registers commands at startup and initializes logging from config.

- **Logging** — app and per-user logs via [imports/utils/logger.py](imports/utils/logger.py); `init_logging()` reads `CONFIG['logging']` and writes to `logs/app.log` and per-user logs in `logs/`.

- **Config & data locations**
  - Main config: [data/configs/app_config.yaml](data/configs/app_config.yaml)
  - Persistent state: `data/state/` (contains `auth.json`, `conversations.json`)
  - Memory DB and model cache: `data/memory/db/`, `data/memory/model/`
  - Images: `data/images/<user_id>/`

**How to run (development)**

1. Create and activate a Python 3.14 venv.
2. Install dependencies (project requirements may change):

```bash
python -m pip install -r requirements.txt
```

3. Set your Telegram token (optional for bot testing):

```bash
export TELEGRAM_TOKEN="<your-token>"
```

4. Run the bot:

```bash
python -m imports.bot
```

5. Run unit tests:

```bash
pytest -q
```

Notes: some tests (integration tests) require a running LM Studio. Use `RUN_LM_STUDIO=1 pytest -q` to run those selectively.

**Prerequisites**

- **Python**: 3.10+ (project tested with 3.14). Create and activate a venv.
- **Optional services**: LM Studio (local OpenAI-compatible endpoint), TinyMCP (MCP subprocess), Qdrant (vector DB) and a fastembed-compatible embedding model for full-memory features.
- **Install deps**: `python -m pip install -r requirements.txt` (some features gracefully fallback when optional deps are missing).

**Running tests**

- Unit tests do not require external services: run `pytest -q`.
- Integration tests that call a real LM Studio are gated. Run them explicitly with `RUN_LM_STUDIO=1 pytest -q` or individually: `RUN_LM_STUDIO=1 pytest tests/test_lm_provider_integration.py`.

**Disabling MCP / tools**

- By default the TinyMCP entry in `data/configs/app_config.yaml` is marked `enabled: false` to avoid launching external processes. If you do have an MCP you want the app to start, set `enabled: true` and provide a valid `command`/`args` in the config.
- The orchestrator will discover MCP tools at startup when an MCP manager is configured and enabled; you can also run the bot without MCP and the app will fall back to local tools (e.g., `manual_memory`).

**Dev stubs & mocking**

- For local development and CI you can stub or monkeypatch the provider and MCP manager. Examples:

```python
# stub provider
class FakeProvider:
  async def chat(self, messages, functions=None):
    return {'choices':[{'message':{'content':'stub reply'}}]}

# stub MCP manager
class StubMCP:
  def list_tools(self):
    return []
  def send_sync(self, obj):
    return {'result': None}
```

**Troubleshooting**

- If the bot fails to import `aiogram`/`aiohttp`, install dependencies from `requirements.txt`.
- If LM Studio is unreachable, set the provider URL in `data/configs/app_config.yaml` or run integration tests with `RUN_LM_STUDIO=1` only in environments where LM Studio is available.
- Logs are in the `logs/` directory (per-user logs and `app.log`). Increase verbosity in `data/configs/app_config.yaml` under `logging`.

**TODO (extracted from PROJECT_CONCEPT.md)**

- Implement robust orchestrator queueing (primary + secondary) and concurrency/RPM limits.
- Complete MCP stdio protocol integration: tool discovery, `list_tools()`, `send_sync()`.
- Add tool approval UI and permission mapping (approve/decline flow).
- Finish memory merging pipeline: model-based merge callback + background updates.
- Implement multi-image/forwarded message handling and modality fallbacks.
- Add summarization/extraction triggers when conversation length exceeds threshold.
- Add graceful shutdown hooks for orchestrator and MCP subprocesses.
- Add unit tests for `ConversationStore`, `MemoryStore` merge behavior, and tool-call loop.
- Document setup for LM Studio, TinyMCP, Qdrant, and fastembed (README).

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
- Harden the model prompts for memory merging and summarization; add unit tests for merge behavior.
- Add explicit tests for `ConversationStore` persistence and for the `/new` flow.
- Improve tool permission mappings and UI for approvals.
- Add graceful shutdown hooks for orchestrator and MCP to ensure background tasks finish.

If you want, I can now run the test suite (I will install missing deps first), or run a quick smoke test against your LM Studio instance — tell me which to run.

---
Generated on 22 May 2026
TinyChatTG — Minimal AI chat app for Telegram using LM Studio + local memory

Quickstart

1. Copy `.env.example` to `.env` and set `TELEGRAM_TOKEN`.
2. (Optional) Install dependencies:

```bash
pip install -r requirements.txt
```

3. Start the bot:

```bash
python -m imports.bot
```

Notes
- LM Studio endpoint is set in `data/configs/app_config.yaml`.
- MCP configuration (TinyMCP) is defined in the same config file.
- Memory store uses local folder `data/memory` (simple JSON-backed fallback included).

Next steps
- Implement orchestrator queueing, tool adapters, and full Qdrant+fastembed integration.
