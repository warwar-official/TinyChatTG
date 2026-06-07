# TinyChatTG — Local LM Studio / Gemini Telegram AI Bot

TinyChatTG is a lightweight Telegram assistant that runs locally against a provider (LM Studio or Gemini), keeps per-user conversation state, and exposes a small set of tools for file handling and memory management. This README has been updated to match the current code in this repository — links and feature references point to actual modules present in the source tree.

Quick summary
- Local provider adapters for LM Studio and Gemini (async chat wrappers).
- Persistent per-user conversation history (SQLite) and a memory backend with Qdrant (path mode) and a JSON fallback.
- Orchestrator handling job queuing, concurrency/rate limits, tool calls and an approval flow.
- Telegram bot handlers including `/start`, `/new`, and `/stop`, with auth codes and rate protections.

Implemented features (what's actually in the code)

- **Providers** — adapters live in [imports/providers/](imports/providers/). The repository provides `lm_studio` and `gemini` adapters (see [imports/providers/lm_studio.py](imports/providers/lm_studio.py) and [imports/providers/gemini.py](imports/providers/gemini.py)).

- **Orchestrator** — core request flow is implemented in [imports/orchestrator.py](imports/orchestrator.py):
  - Unified queue for inbound jobs and background coroutines.
  - Concurrency and simple requests-per-minute limiting (semaphores + timestamp queue).
  - Integrates `ConversationStore` for per-user context and can schedule summarization/extraction.
  - Tool discovery (from MCP manager when configured) and a tool-call loop with an approval UI bridge.

- **MemoryStore** — lightweight memory backend in [imports/memory/store.py](imports/memory/store.py) with:
  - fastembed (if available) + Qdrant (path-mode) when installed and configured.
  - Deterministic pseudo-embedding + JSON fallback when optional deps are missing.
  - Deduplication and simple merge thresholds, plus an async merge callback hook.

- **ConversationStore (persistent)** — per-user conversation history + summaries backed by SQLite at `data/state/conversations.db` (see [imports/memory/conversation_store.py](imports/memory/conversation_store.py)).

- **MCP manager** — MCP subprocess management and tool discovery in [imports/mcp/manager.py](imports/mcp/manager.py). It launches configured stdio-based MCP servers, performs a handshake and lists available tools.

- **Telegram bot** — handlers and startup are in [imports/bot/bot.py](imports/bot/bot.py):
  - `/start` — generates/accepts auth codes and performs abuse protection.
  - `/new` — extracts memories from the existing conversation and clears the context.
  - `/stop` — cancels in-flight jobs.
  - Inline approval buttons (`Allow` / `Decline`) are used for tool calls that require user consent.

- **AuthStore** — persistent auth, key redemption, start/code bans, and message-rate enforcement are implemented in [imports/auth/store.py](imports/auth/store.py).

- **File processing & FileStore** — the code that handles files is under [imports/files/](imports/files/):
  - Physical files are stored under `data/files/documents/` and `data/files/images/` with a deduplicated hash-based layout. Metadata is indexed in Qdrant when available (see [imports/files/store.py](imports/files/store.py)).
  - Document conversion helpers are in [imports/files/converter.py](imports/files/converter.py) (Pandoc integration and PDF→Markdown support via PyMuPDF + OCR).
  - The bot compresses large images to a ~2 megapixel target before further processing (`compress_image_to_2mp` in [imports/bot/utility.py](imports/bot/utility.py)).

- **File-facing tools** — tools exposed to the orchestrator are implemented in [imports/tools/](imports/tools/). Notable tool handlers include:
  - `file_list` (imports/tools/file_list.py → `list_files`) — list user's stored files.
  - `file_read_lines` (imports/tools/file_read_lines.py → `read_file_lines`) — return a range of lines and optionally embed images referenced in converted Markdown.
  - `file_grep` (imports/tools/file_grep.py → `grep_file`) — search for a query string inside a file (returns snippets, capped results).
  - `file_find_by_name`, `file_find_by_similarity`, `file_create`, `file_add_lines`, `file_replace_lines`, `file_send` — see [imports/tools/](imports/tools/) for exact handlers and schemas.
  - Tools perform path-safety validation (rejecting path separators and `..`) to prevent traversal.

Config & data locations
- Main config: `data/configs/app_config.yaml` (loaded by [imports/config.py](imports/config.py)).
- Persistent state: `data/state/` (conversation DB, auth.json, orchestrator state).
- File storage: `data/files/documents/`, `data/files/images/` (see FileStore docstring).
- Memory DB / model cache: `data/memory/db/`, `data/memory/model/`.

How to run (development)

1. Create and activate a Python 3.10+ venv.
2. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

3. Set your Telegram token as an environment variable (e.g., in a `.env` file or export):

```bash
export TELEGRAM_TOKEN="<your-token>"
```

4. Run the bot (execute the bot module directly):

```bash
python imports/bot/bot.py
```

5. Run unit tests:

```bash
pytest -q
```

Prerequisites & notes
- **Python**: 3.10+ (project may be tested on newer interpreters).
- Optional services: LM Studio (local OpenAI-compatible endpoint), TinyMCP (MCP subprocess), Qdrant (vector DB) and a fastembed-compatible embedding model for full-memory features. The app falls back to JSON/placeholder implementations when optional deps are missing.
- **STT / Whisper**: the default `whisper` config in `data/configs/app_config.yaml` contains a `max_size` (25,000,000 bytes = 25MB) used by the STT client; this is NOT a global file upload cap — different components may impose different limits.

Troubleshooting
- If imports fail when running scripts from the `scripts/` directory, run them from the repository root or add the repo root to `PYTHONPATH`. Alternatively install the package in editable mode:

```bash
python -m pip install -e .
```

- If the bot fails to import `aiogram`/`aiohttp`, install dependencies from `requirements.txt`.
- Logs are in the `logs/` directory; increase verbosity via `data/configs/app_config.yaml` under `logging`.

Files of interest
- [imports/bot/bot.py](imports/bot/bot.py) — Telegram handlers and startup.
- [imports/orchestrator.py](imports/orchestrator.py) — core orchestration and tool loop.
- [imports/memory/conversation_store.py](imports/memory/conversation_store.py) — persistent conversation history.
- [imports/memory/store.py](imports/memory/store.py) — memory backend (Qdrant + JSON fallback).
- [imports/auth/store.py](imports/auth/store.py) — persistent auth and rate-limiting.
- [imports/mcp/manager.py](imports/mcp/manager.py) — MCP subprocess manager.
- [imports/files/store.py](imports/files/store.py) — FileStore and document handling.

If you'd like, I can also:
- run the test suite and report failures, or
- apply the same file-locking pattern we added to `AuthStore` to other small JSON-backed stores in the repo.

---

This README was refreshed to match the repository code. If you want different wording or more operational detail (deployment, Docker, systemd unit, example `app_config.yaml`), tell me which sections to expand.
