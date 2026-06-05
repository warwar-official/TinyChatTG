# TinyChatTG — Local LM Studio Telegram AI Bot

This repository implements a lightweight Telegram AI assistant that runs against a local LM Studio instance and uses a local memory store with resilient fallbacks.

**Quick summary**
- Local LM Studio provider adapter and chat wrapper.
- Persistent conversation store and memory (Qdrant + fastembed primary, JSON fallback).
 - Orchestrator with a unified queue, tool discovery, and approval flow.
- Telegram bot with `/start` (auth), `/new` (extract+clear), command registration, message-rate bans, and safer unknown-command handling.

**Implemented features (what's in the code today)**

- **Providers** — Adapters for both LM Studio and Gemini implemented in [imports/providers/](imports/providers/). They provide async `chat()` wrappers and handle provider-specific responses.
- **Orchestrator** — Central request flow in [imports/orchestrator.py](imports/orchestrator.py):
  - Unified queue for inbound user messages and background tasks (summarization, extraction, tool results).
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
- **File Processing** — Handles files sent to the bot:
  - **Images**: Automatically compressed to a maximum of 2 megapixels (2MP) and forwarded to the model context.
  - **Text-based files** (`.txt`, `.md`, `.py`, `.js`, etc.): Stored under `data/documents/<user_id>/`. If <= 15,000 characters, the content is shown in context. Otherwise, the model is notified to use tools to read/search the file.
  - **Unsupported files / media**: Rejects other file types (e.g. PDFs, binary, video, music) with `"File type does not suported"`.
  - **Size limit**: Restricts files to a maximum of 2MB.
- **File Interaction Tools** — Exposes three new local tools for the model:
  - `file_list` — Lists user's stored text files from newest to oldest.
  - `file_read_lines` — Returns a paginated range of lines from a file.
  - `file_search` — Searches for a query string in a file and returns matching lines and snippets (capped at 50 results).
  - Includes strict path-traversal validation (rejecting `/`, `\\`, and `..` to restrict the model to the user's folder).


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

- Consider modality fallbacks; current approach assumes models handle multimodal inputs directly.
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

**New features (added)**
- Per-tool summarization control: tools may include `allow_summarizing: true|false` in their MCP/app tool configs. The orchestrator only schedules summaries for tools that allow it.
- Setup improvements: `setup.py` now ensures default `data/configs/app_config.yaml` and `data/mcp/app_tools.yaml` are created when missing, and attempts to ensure the `pandoc` binary is available via `pypandoc`.
- Migration script: `migrate_files.py` was extended to migrate file records into the new FileStore payload shape (adds `media_dir`, `corrupted`, and `corrupted_pages` fields), mark existing users as expired `user` access (adds `access_type` and `access_expires` fields), and ensure MCP tool configs contain `allow_summarizing` when missing.
- Beta key management: A helper script `scripts/create_beta_keys.py` creates keys and inserts them into `data/state/auth.json` under a `keys` mapping. Keys contain `type`, `expires_at`, `max_uses`, and `label` metadata.

**How to create beta keys**

Run the helper script to create keys and add them to the local auth store:

```bash
python scripts/create_beta_keys.py --count 5 --duration-days 30 --max-uses 10 --label "beta-june"
```

This will print the generated keys and write them to `data/state/auth.json` under the `keys` section.

**Notes on auth changes**
- Existing users are marked with `access_type: user` and `access_expires: 0` by the migration (meaning expired). Console/admin keys (in `keys` mapping with type `infinity`) are treated as permanent and can be granted to users manually.
- The default expiry message is: "Your access expired. Update your plan or use another key." — configurable in `data/configs/app_config.yaml` under `auth.expiry_message`.

