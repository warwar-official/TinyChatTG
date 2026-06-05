"""Minimal aiogram-based bot skeleton integrating LM Studio provider and MemoryStore.

This is a starting point: auth flow, text/image handling, `/new` command, and typing indicator.
"""
import os
import asyncio
from pathlib import Path
from typing import Dict, Any
import re
import html
import math
from PIL import Image

def compress_image_to_2mp(image_path: str) -> None:
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            total_pixels = width * height
            if total_pixels > 2000000:
                scale = math.sqrt(2000000 / total_pixels)
                new_width = int(width * scale)
                new_height = int(height * scale)
                resample_filter = getattr(Image, 'Resampling', Image).LANCZOS
                resized_img = img.resize((new_width, new_height), resample=resample_filter)
                resized_img.save(image_path, format=img.format or 'JPEG')
    except Exception as e:
        logging.getLogger(__name__).warning(f"Error compressing image {image_path}: {e}")


import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, BotCommand

from imports.config import CONFIG, get_provider, get_bot_config, get_telegram_token
from imports.providers.lm_studio import LMStudioProvider
from imports.providers.gemini import GeminiProvider
from imports.memory.store import MemoryStore
from imports.files.store import FileStore
from imports.mcp.manager import MCPManager
from imports.orchestrator import Orchestrator
from imports.utils.logger import get_user_logger, init_logging
from imports.memory.conversation_store import ConversationStore
from imports.auth.store import AuthStore
from imports.stt.whisper_client import WhisperClient, STTBusyError

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Basic persistent auth store
auth_store = AuthStore(PROJECT_ROOT / 'data' / 'state' / 'auth.json')

TELEGRAM_TOKEN = get_telegram_token()


class DummyBot:
    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        return None

    async def send_chat_action(self, chat_id, action):
        return None

    async def delete_message(self, chat_id, message_id):
        return None

    async def download(self, file, destination):
        return None

    class session:
        @staticmethod
        async def close():
            return None

def _markdown_to_html(text: str) -> str:
    """Convert simple CommonMark-like syntax to Telegram-safe HTML.

    - Preserves fenced code blocks (```...```) and inline code (`...`).
    - Converts nested `_**bold**_` and `**_italic_**` combos to nested tags.
    - Converts `**...**` and `__...__` to `<b>`, and `_..._` and `*...*` to `<i>`.
    This is a pragmatic converter for typical assistant outputs; it intentionally
    HTML-escapes text before injecting tags to avoid accidental HTML.
    """
    if not text:
        return text

    # 1) LaTeX arrow replacements
    latex_reps = {
        r"\\rightarrow\b": "→",
        r"\\leftarrow\b": "←",
        r"\\Rightarrow\b": "⇒",
        r"\\Leftarrow\b": "⇐",
        r"\\leftrightarrow\b": "↔",
        r"\\Leftrightarrow\b": "⇔",
        r"\\longrightarrow\b": "⟶",
        r"\\longleftarrow\b": "⟵",
        r"\\implies\b": "⟹",
        r"\\iff\b": "⟺",
        r"\\to\b": "→",
    }
    for k, v in latex_reps.items():
        text = re.sub(k, v, text)

    # 2) Math symbol replacements
    math_symbols = {
        r"\\sim\b": "~",
        r"\\le\b": "≤",
        r"\\ge\b": "≥",
        r"\\leq\b": "≤",
        r"\\geq\b": "≥",
        r"\\approx\b": "≈",
        r"\\neq\b": "≠",
        r"\\%": "%",
        r"\\times\b": "×",
        r"\\div\b": "÷",
        r"\\pm\b": "±",
        r"\\cdot\b": "·",
        r"\\degree\b": "°",
    }
    for k, v in math_symbols.items():
        text = re.sub(k, v, text)

    # 3) \text{...} replacement
    text = re.sub(r"\\text\{([^}]+)\}", r"\1", text)

    # 4) Clean up double dollars
    text = re.sub(r"\$\$(.*?)\$\$", r"\1", text, flags=re.DOTALL)

    # 5) Clean up single dollars
    def _cb_single_dollar(m):
        content = m.group(1)
        contains_math_op = any(op in content for op in ('+', '-', '*', '/', '=', '<', '>', '^', '_', '~', '≤', '≥', '≈', '≠', '±', '·', '°', '×', '÷', '%'))
        is_short = len(content.strip()) <= 5 and ' ' not in content.strip()
        
        words = set(re.findall(r'\b[a-zA-Z]+\b', content.lower()))
        has_stopwords = bool(words & {'and', 'or', 'the', 'is', 'of', 'to', 'for', 'in', 'with', 'on', 'at', 'by', 'an', 'a'})
        
        if contains_math_op or (is_short and len(content.strip()) > 0) or (not has_stopwords and len(content.strip()) > 0):
            return content
        else:
            return f"${content}$"
            
    text = re.sub(r"\$([^\$]+?)\$", _cb_single_dollar, text)


    # 1) Extract fenced code blocks
    code_blocks: dict[str, str] = {}
    def _cb_code(m):
        key = f"@@CODEBLOCK{len(code_blocks)}@@"
        code_blocks[key] = m.group(1)
        return key
    text = re.sub(r"```(.*?)```", _cb_code, text, flags=re.DOTALL)

    # 2) Extract inline code
    inline_codes: dict[str, str] = {}
    def _cb_inline(m):
        key = f"@@INLCODE{len(inline_codes)}@@"
        inline_codes[key] = m.group(1)
        return key
    text = re.sub(r"`([^`]+?)`", _cb_inline, text)

    # 3) Escape remaining text to HTML
    text = html.escape(text)

    # 3.5) Typography and list/header adjustments (outside code spans)
    # Replace long em-dash with shorter en-dash
    text = text.replace('—', '–')

    # Replace Markdown header markers (#, ##, etc.) at start of line with a chevron
    # Add a newline before it for better readability
    # Use multiline flag so ^ matches line starts
    text = re.sub(r'(?m)^(\s*)#+\s+', r'\n\1➤ ', text)

    # Replace unordered list markers '*' or '-' at start of line with a bullet '•'
    text = re.sub(r'(?m)^([ \t]*)[\*-][ \t]+', r"\1• ", text)

    # 4) Convert nested combinations first
    # _**text**_  -> <i><b>text</b></i>
    text = re.sub(r"(?<![A-Za-z0-9])_(\*\*(.+?)\*\*)_(?![A-Za-z0-9])", lambda m: f"<i><b>{m.group(2)}</b></i>", text, flags=re.DOTALL)
    # **_text_** -> <b><i>text</i></b>
    text = re.sub(r"\*\*(\_(.+?)\_)\*\*", lambda m: f"<b><i>{m.group(2)}</i></b>", text, flags=re.DOTALL)

    # 5) Simple strong/italic replacements
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: f"<b>{m.group(1)}</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", lambda m: f"<b>{m.group(1)}</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?<![A-Za-z0-9])_(.+?)_(?![A-Za-z0-9])", lambda m: f"<i>{m.group(1)}</i>", text, flags=re.DOTALL)
    text = re.sub(r"(?<![A-Za-z0-9])\*(.+?)\*(?![A-Za-z0-9])", lambda m: f"<i>{m.group(1)}</i>", text, flags=re.DOTALL)

    # 6) Reinsert inline code (escaped inside code tag)
    for k, v in inline_codes.items():
        text = text.replace(k, f"<code>{html.escape(v)}</code>")

    # 7) Reinsert code blocks
    for k, v in code_blocks.items():
        text = text.replace(k, f"<pre><code>{html.escape(v)}</code></pre>")

    return text


if not TELEGRAM_TOKEN:
    bot = DummyBot()
else:
    bot = Bot(token=TELEGRAM_TOKEN)

dp = Dispatcher()
logger = logging.getLogger(__name__)

def _build_provider():
    """Instantiate the correct AI provider based on app_config.yaml → models.main_model."""
    main_model_cfg = (CONFIG.get('models') or {}).get('main_model') or {}
    provider_name = main_model_cfg.get('provider', 'lmstudio')

    if provider_name == 'gemini':
        prov_cfg = get_provider('gemini') or {}
        # Do NOT read API key from config; prefer environment variable only.
        model = main_model_cfg.get('model') or prov_cfg.get('default_model') or 'gemini-2.0-flash'
        return GeminiProvider(default_model=model)

    # default: lmstudio
    prov_cfg = get_provider('lmstudio') or {}
    return LMStudioProvider(
        prov_cfg.get('url') or 'http://192.168.50.212:1234',
        prov_cfg.get('default_model', 'default_model'),
    )

lm = _build_provider()
mem_store = MemoryStore(CONFIG)
# MCP configuration - pass all mcp blocks to the new manager
mcp_cfg = CONFIG.get('mcp') or {}
mcp_mgr = None
try:
    mcp_mgr = MCPManager(mcp_cfg)
except Exception:
    mcp_mgr = None

# Conversation store (SQLite)
conv_store = ConversationStore()

# Provide model-based merge callback to MemoryStore
async def _merge_memories(existing: str, new: str) -> str:
    try:
        from imports.orchestrator import PROMPTS
        merge_prompt = PROMPTS.get('memory_merge_prompt', 'Merge two memory fragments into one concise memory. If the two memories are contradictory, meaningless, or contain no useful text to remember, output exactly the word REJECT.')
        merge_template = PROMPTS.get('memory_merge_user_template', 'Memory A:\n{existing}\n\nMemory B:\n{new}\n\nReturn a single merged memory text, concise.')
        msgs = [
            {"role": "system", "content": merge_prompt},
            {"role": "user", "content": merge_template.format(existing=existing, new=new)},
        ]
        
        for _ in range(3):
            try:
                resp = await lm.chat(msgs)
                if isinstance(resp, dict) and resp.get('choices'):
                    ch = resp['choices'][0]
                    msg = ch.get('message', {})
                    content = msg.get('content') or ""
                    
                    if not content.strip():
                        continue
                        
                    # Remove markdown backticks if present
                    clean_content = content.strip()
                    if clean_content.startswith("```"):
                        lines = clean_content.splitlines()
                        if len(lines) >= 2:
                            if lines[0].startswith("```"):
                                lines = lines[1:]
                            if lines[-1].startswith("```"):
                                lines = lines[:-1]
                        clean_content = "\n".join(lines).strip()

                    if clean_content.upper().startswith('REJECT'):
                        return ""
                    return clean_content
            except Exception:
                await asyncio.sleep(1)
                continue
                
        return existing + "\n" + new
    except Exception:
        return existing + "\n" + new

mem_store.merge_callback = _merge_memories


# Approval callback: sends approval UI to user and waits for user's decision via callback_query handler
async def _approval_ui(approval_id: str, user_id: int, tool_call: Dict[str, Any]):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Allow", callback_data=f"tool_approve:{approval_id}:1"),
         InlineKeyboardButton(text="Decline", callback_data=f"tool_approve:{approval_id}:0")]
    ])
    args_preview = str(tool_call.get('args', ''))
    if len(args_preview) > 320:
        args_preview = args_preview[:317] + "..."
    text = (
        f"🔧 <b>{html.escape(str(tool_call.get('name', '?')))}</b> wants to run:\n"
        f"<code>{html.escape(args_preview)}</code>\n\nAllow?"
    )
    try:
        await bot.send_message(user_id, text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


# Status callback: sends tool status messages to user
async def _tool_status(chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, _markdown_to_html(text), parse_mode="HTML")
    except Exception:
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            pass


# Send file callback: sends a file to the user
async def _send_file_callback(chat_id: int, file_path: str, real_name: str):
    from aiogram.types import FSInputFile
    try:
        f = FSInputFile(file_path, filename=real_name)
        await bot.send_document(chat_id, f)
    except Exception as e:
        logger.error("Failed to send file %s to chat %d: %s", file_path, chat_id, e)
        raise

# Initialize FileStore sharing embed_fn and Qdrant client with MemoryStore
file_store = FileStore(
    project_root=PROJECT_ROOT,
    embed_fn=mem_store.embed_fn,
    embed_dim=mem_store.embed_dimension,
    qdrant_client=mem_store.qdrant
)


orchestrator = Orchestrator(
    CONFIG, lm, mem_store,
    mcp_mgr=mcp_mgr,
    approval_callback=_approval_ui,
    status_callback=_tool_status,
    conv_store=conv_store,
    file_store=file_store,
    send_file_callback=_send_file_callback,
)

# STT (Whisper) client — reads GROQ_API_KEY from env
whisper_client = WhisperClient(CONFIG.get('whisper', {}))


# --- Forward Debounce Buffer ---
# Keeps track of incoming messages per user so we can group them together
# if they arrive in rapid succession (e.g. forwarded albums).
_user_buffers: Dict[int, Dict[str, list]] = {}
_user_flush_tasks: Dict[int, asyncio.Task] = {}


async def _download_audio(message: types.Message, user_id: int):
    """Download a voice message and save it to data/audio/<user_id>/.

    Returns the saved file path string, or None on failure.
    """
    voice = message.voice
    if not voice:
        return None
    folder = PROJECT_ROOT / 'data' / 'audio' / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    file_path = folder / f"{voice.file_unique_id}.ogg"
    try:
        await bot.download(voice, destination=str(file_path))
        get_user_logger(user_id).info("Downloaded voice → %s", file_path)
        return str(file_path)
    except Exception as e:
        get_user_logger(user_id).warning("Failed to download voice: %s", e)
        return None


async def _transcribe_all(paths: list, user_id: int, chat_id: int) -> list:
    """Transcribe multiple voice files concurrently.

    Returns a list of formatted transcription strings.
    On STTBusyError, notifies the user and skips that file.
    On any other error, logs and skips silently.
    """
    ulog = get_user_logger(user_id)

    async def _one(path: str):
        try:
            text = await whisper_client.transcribe(path)
            if text and text.strip():
                return f"[audio message transcription]\n{text.strip()}"
            return None
        except STTBusyError:
            ulog.warning("STT service busy for %s", path)
            try:
                await bot.send_message(
                    chat_id,
                    "🎙 STT service is busy, please try again later.",
                )
            except Exception:
                pass
            return None
        except Exception as e:
            ulog.error("Transcription failed for %s: %s", path, e)
            return None

    results = await asyncio.gather(*[_one(p) for p in paths])
    return [r for r in results if r is not None]


async def _flush_user_buffer(user_id: int, chat_id: int):
    """Wait for a brief debounce period, then submit all accumulated messages and images."""
    await asyncio.sleep(2.0)
    
    buffer_data = _user_buffers.pop(user_id, {})
    _user_flush_tasks.pop(user_id, None)
    
    parts = buffer_data.get("text_parts", [])
    images = buffer_data.get("images", [])
    audio_paths = buffer_data.get("audio_paths", [])

    if not parts and not images and not audio_paths:
        return

    # Transcribe any buffered voice messages first (concurrent, respects STT rate limiter)
    if audio_paths:
        transcriptions = await _transcribe_all(audio_paths, user_id, chat_id)
        # Prepend transcriptions so they appear before any typed text
        parts = transcriptions + parts

    # Combine all accumulated parts into one single message context
    final_text = "\n\n".join(parts)
    
    # Typing indicator while processing
    async def _keep_typing(cid: int, interval: float = 4.0):
        try:
            while True:
                try:
                    await bot.send_chat_action(cid, "typing")
                except Exception:
                    pass
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    typing_task = None
    try:
        typing_task = asyncio.create_task(_keep_typing(chat_id))
    except Exception:
        pass

    try:
        resp = await orchestrator.submit_primary(user_id, chat_id, final_text, images=images)
    except Exception as e:
        await bot.send_message(chat_id, f"Orchestrator error: {e}")
        return
    finally:
        if typing_task:
            typing_task.cancel()
            try:
                await typing_task
            except Exception:
                pass

    if not resp:
        await bot.send_message(chat_id, "Empty response from orchestrator")
        return

    # Graceful stop/new — no error message needed
    if resp.get('error') in ('stopped', 'stop', 'new'):
        return

    if resp.get('error'):
        await bot.send_message(chat_id, f"Error: {resp.get('error')}")
        return

    if resp.get('assistant'):
        try:
            assistant_text = resp.get('assistant')
            await bot.send_message(chat_id, _markdown_to_html(assistant_text), parse_mode="HTML")
        except Exception:
            await bot.send_message(chat_id, resp.get('assistant'))
        return

    if resp.get('tool'):
        tool = resp.get('tool')
        result = resp.get('result')
        if isinstance(result, dict) and result.get('error'):
            text = f"__{tool}: \"error: {result.get('error')}\"__"
        else:
            text = f"__{tool}: \"{result}\"__"
        try:
            await bot.send_message(chat_id, _markdown_to_html(text), parse_mode="HTML")
        except Exception:
            await bot.send_message(chat_id, text)

# -------------------------------


def _gen_code() -> str:
    import random
    return f"{random.randint(100000, 999999)}"


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    cfg = get_bot_config()
    ttl = cfg.get('auth_code_ttl_seconds', 60)
    # track start attempts and possible bans
    auth_store.add_start_attempt(user_id)
    if auth_store.is_start_banned(user_id):
        await message.answer("You are temporarily banned from starting the bot due to repeated attempts. Try later.")
        get_user_logger(user_id).warning("start attempt blocked due to start_ban")
        return

    # generate code (respecting rate-limits and code bans)
    try:
        code = auth_store.generate_code(user_id, ttl=ttl)
    except PermissionError as e:
        err = str(e)
        if err == 'code_rate_limited':
            await message.answer("Please wait before requesting a new code (1 minute limit).")
        elif err == 'start_banned':
            await message.answer("You are temporarily banned from requesting codes.")
        elif err == 'code_banned':
            await message.answer("You are temporarily banned from requesting codes due to repeated failures.")
        else:
            await message.answer("Cannot generate code at the moment.")
        get_user_logger(user_id).warning(f"code generation blocked: {err}")
        return

    # log code generation (robust lookup of user display name)
    nickname = (
        getattr(message.from_user, 'username', None)
        or getattr(message.from_user, 'full_name', None)
        or getattr(message.from_user, 'first_name', None)
        or str(getattr(message.from_user, 'id', 'Unknown'))
    )
    logger.info(f"Auth code for user {user_id} (@{nickname}): {code}")
    get_user_logger(user_id).info(f"Auth code generated for @{nickname}: {code}")
    await message.answer("Initialization code was printed to server console, message owner (@LordWarWar) for it. Then paste it here to authorize.")


@dp.message(Command("new"))
async def cmd_new(message: types.Message):
    user_id = message.from_user.id
    if not auth_store.is_authorized(user_id):
        await message.reply("Authorize first with /start")
        return

    # Interrupt any pending approval so the decline is recorded before summarization
    if user_id in orchestrator._user_pending_approval:
        await orchestrator.interrupt_user(user_id, "new", None)
        await asyncio.sleep(0.2)  # let the tool loop handle the interrupt

    # Extract memories from conversation and clear context
    try:
        history = conv_store.get_history(user_id)
        if history:
            # schedule summarization/extraction immediately
            await orchestrator._summarize_and_extract(user_id, history)
        # clear history after extraction
        conv_store.clear_history(user_id)
        await message.reply("Started a new conversation: memories extracted and context cleared.")
    except Exception:
        import traceback
        get_user_logger(user_id).exception("Failed to start new conversation")
        print(traceback.format_exc())
        await message.reply("Failed to start new conversation (see server logs)")


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    user_id = message.from_user.id
    if not auth_store.is_authorized(user_id):
        await message.reply("Authorize first with /start")
        return
    # Cancel any pending debounce flush
    task = _user_flush_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    # Clear debounce buffer
    _user_buffers.pop(user_id, None)
    # Stop in-flight orchestrator job + pending approval
    await orchestrator.stop_user(user_id)
    await message.reply("⏹ Stopped.")


@dp.message()
async def handle_all(message: types.Message):
    user_id = message.from_user.id

    # Check for auth code reply
    if message.text and auth_store.verify_code(user_id, message.text.strip()):
        await message.reply("Authorization complete. You can now use the bot.")
        get_user_logger(user_id).info("user authorized")
        return

    if not auth_store.is_authorized(user_id):
        await message.reply("Please run /start and authorize first.")
        return

    # Message rate limiting for authorized users
    try:
        rate = auth_store.record_message(user_id)
        if rate.get('banned'):
            # delete incoming message and notify user
            try:
                await bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            bans = auth_store.get_bans(user_id)
            ban_until = bans.get('message_ban_until', 0)
            if ban_until:
                from datetime import datetime
                ban_until_str = datetime.fromtimestamp(ban_until).strftime('%Y-%m-%d %H:%M:%S')
            else:
                ban_until_str = "a while"
            await bot.send_message(user_id, f"You are temporarily banned for spamming until {ban_until_str}.")
            
            # Clear user's debounce buffer to drop pending forwarded messages
            _user_buffers.pop(user_id, None)
            task = _user_flush_tasks.pop(user_id, None)
            if task and not task.done():
                task.cancel()
            
            # Stop any running orchestrator task for this user
            await orchestrator.stop_user(user_id)
            return
    except Exception:
        pass

    # Unknown command handling: do not forward to model
    if message.text and message.text.strip().startswith('/'):
        cmd = message.text.strip().split()[0]
        allowed_cmds = ['/new', '/stop']
        if cmd not in allowed_cmds:
            await message.reply('Unknown command')
            return

    # Handle video_note, poll and other non-file/non-document unsupported content types
    if message.content_type in ("video_note", "poll"):
        await message.reply("Invalid input")
        return

    # For audio and video, they are file types that are not supported.
    if message.content_type in ("audio", "video"):
        await message.reply("File type does not suported")
        return

    # Save images if present
    imgs = []
    if message.photo:
        # save highest resolution photo in this message
        photo = message.photo[-1]
        try:
            # download to memory/temp
            import io
            bio = io.BytesIO()
            await bot.download(photo, destination=bio)
            bio.seek(0)
            
            # Temporary save to compress
            temp_path = PROJECT_ROOT / "data" / "tmp"
            temp_path.mkdir(parents=True, exist_ok=True)
            tmp_file = temp_path / f"{photo.file_unique_id}.jpg"
            with open(tmp_file, "wb") as f:
                f.write(bio.read())
            
            compress_image_to_2mp(str(tmp_file))
            
            with open(tmp_file, "rb") as f:
                compressed_bytes = f.read()
            
            # Remove temp file
            try:
                tmp_file.unlink()
            except Exception:
                pass

            res = file_store.register_image(user_id, compressed_bytes, "image.jpg")
            imgs.append(res["path"])
            
            if res.get("is_new"):
                # Queue description task
                asyncio.create_task(
                    orchestrator.queue_image_describe(user_id, res["hash_name"], res["real_name"], message.text or getattr(message, 'caption', None) or "")
                )
        except Exception as e:
            get_user_logger(user_id).warning("Failed to process photo: %s", e)

    # Process documents
    file_context = ""
    if message.document:
        doc = message.document
        if doc.file_size > 20 * 1024 * 1024:
            await message.reply("File is too big (max 20 MB)")
            return

        orig_name = doc.file_name or "file"
        clean_name = Path(orig_name).name
        file_ext = Path(clean_name).suffix.lower()
        mime_type = doc.mime_type or ""

        image_extensions = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tiff', '.tif'}
        text_extensions = {
            '.txt', '.md', '.html', '.htm', '.py', '.js', '.css', '.json', '.xml',
            '.csv', '.yaml', '.yml', '.ini', '.conf', '.log', '.sh', '.bat', '.ts',
            '.tsx', '.jsx', '.c', '.cpp', '.h', '.hpp', '.java', '.go', '.rs', '.php',
            '.sql', '.rb', '.pl', '.pm',
        }
        from imports.files.converter import PANDOC_INPUT_EXTENSIONS
        pandoc_extensions = PANDOC_INPUT_EXTENSIONS
        pdf_extensions = {'.pdf'}

        is_image = mime_type.startswith('image/') or file_ext in image_extensions
        is_text = (
            mime_type.startswith('text/')
            or file_ext in text_extensions
            or mime_type in {'application/json', 'application/xml', 'application/javascript', 'application/x-javascript'}
        )
        is_pandoc = file_ext in pandoc_extensions
        is_pdf = file_ext in pdf_extensions or mime_type == 'application/pdf'

        if is_image:
            try:
                import io
                bio = io.BytesIO()
                await bot.download(doc, destination=bio)
                bio.seek(0)

                temp_path = PROJECT_ROOT / "data" / "tmp"
                temp_path.mkdir(parents=True, exist_ok=True)
                tmp_file = temp_path / f"{doc.file_unique_id}{file_ext}"
                with open(tmp_file, "wb") as f:
                    f.write(bio.read())

                compress_image_to_2mp(str(tmp_file))

                with open(tmp_file, "rb") as f:
                    compressed_bytes = f.read()

                try:
                    tmp_file.unlink()
                except Exception:
                    pass

                res = file_store.register_image(user_id, compressed_bytes, clean_name)
                imgs.append(res["path"])

                if res.get("is_new"):
                    asyncio.create_task(
                        orchestrator.queue_image_describe(user_id, res["hash_name"], res["real_name"], message.text or getattr(message, 'caption', None) or "")
                    )
            except Exception as e:
                get_user_logger(user_id).warning("Failed to process image document: %s", e)
                await message.reply("Failed to process image.")
                return

        elif is_text:
            try:
                import io
                bio = io.BytesIO()
                await bot.download(doc, destination=bio)
                doc_bytes = bio.getvalue()

                res = file_store.register_document(user_id, doc_bytes, clean_name)

                with open(res["path"], 'r', encoding='utf-8', errors='replace') as f:
                    content_str = f.read()

                num_chars = len(content_str)
                if num_chars <= 15000:
                    file_context = f"[File from user: {clean_name}]\n{content_str}"
                else:
                    file_context = f"[File from user: {clean_name}]\nFile is too big to incontext view, so model should use tools for access to it."

                if res.get("is_new"):
                    asyncio.create_task(
                        orchestrator.queue_document_describe(user_id, res["hash_name"], res["real_name"], message.text or getattr(message, 'caption', None) or "")
                    )
            except Exception as e:
                get_user_logger(user_id).warning("Failed to process text file: %s", e)
                await message.reply("Failed to process text file.")
                return

        elif is_pandoc:
            # ── Office document via Pandoc ──────────────────────────────────
            try:
                import io
                from imports.files.converter import convert_office_to_markdown

                bio = io.BytesIO()
                await bot.download(doc, destination=bio)
                raw_bytes = bio.getvalue()

                import hashlib
                raw_hash = hashlib.sha256(raw_bytes).hexdigest()

                res = file_store.check_converted_document_exists(user_id, raw_hash)
                if not res:
                    temp_path = PROJECT_ROOT / "data" / "tmp"
                    temp_path.mkdir(parents=True, exist_ok=True)
                    tmp_src = temp_path / f"{doc.file_unique_id}{file_ext}"
                    tmp_src.write_bytes(raw_bytes)

                    # Determine media directory (named by hash of raw bytes)
                    media_dir = PROJECT_ROOT / "data" / "files" / "documents" / raw_hash / "media"

                    try:
                        md_text = await convert_office_to_markdown(tmp_src, media_dir)
                    finally:
                        try:
                            tmp_src.unlink()
                        except Exception:
                            pass

                    md_bytes = md_text.encode("utf-8")
                    res = file_store.register_converted_document(
                        user_id, md_bytes, clean_name, raw_hash=raw_hash, media_dir=str(media_dir)
                    )
                else:
                    with open(res["path"], 'r', encoding='utf-8', errors='replace') as f:
                        md_text = f.read()

                if len(md_text) <= 15000:
                    file_context = f"[File from user: {clean_name}]\n{md_text}"
                else:
                    file_context = f"[File from user: {clean_name}]\nFile is too big to incontext view, so model should use tools for access to it."

                if res.get("is_new"):
                    asyncio.create_task(
                        orchestrator.queue_document_describe(user_id, res["hash_name"], res["real_name"], message.text or getattr(message, 'caption', None) or "")
                    )
            except Exception as e:
                get_user_logger(user_id).warning("Failed to process office document: %s", e)
                await message.reply(f"Failed to convert document: {e}")
                return

        elif is_pdf:
            # ── PDF via PyMuPDF + Gemini OCR ────────────────────────────────
            try:
                import io
                from imports.files.converter import convert_pdf_to_markdown

                import hashlib
                raw_hash = hashlib.sha256(raw_bytes).hexdigest()
                
                res = file_store.check_converted_document_exists(user_id, raw_hash)
                
                # If we don't have it, perform OCR
                if not res:
                    # Notify user that processing is starting
                    proc_msg = await bot.send_message(message.chat.id, "📄 File processing. Please wait...")

                    # Switch chat action to "upload_document" during OCR
                    async def _keep_upload_action(cid: int, interval: float = 4.0):
                        try:
                            while True:
                                try:
                                    await bot.send_chat_action(cid, "upload_document")
                                except Exception:
                                    pass
                                await asyncio.sleep(interval)
                        except asyncio.CancelledError:
                            return

                    upload_action_task = asyncio.create_task(_keep_upload_action(message.chat.id))

                    try:
                        temp_path = PROJECT_ROOT / "data" / "tmp"
                        temp_path.mkdir(parents=True, exist_ok=True)
                        tmp_src = temp_path / f"{doc.file_unique_id}.pdf"
                        tmp_src.write_bytes(raw_bytes)

                        # Determine media directory (named by hash of raw bytes)
                        media_dir = PROJECT_ROOT / "data" / "files" / "documents" / raw_hash / "media"
                        try:
                            md_text = await convert_pdf_to_markdown(
                                tmp_src,
                                gemini_provider=lm if isinstance(lm, GeminiProvider) else GeminiProvider(),
                                media_dir=media_dir,
                            )
                        finally:
                            try:
                                tmp_src.unlink()
                            except Exception:
                                pass
                    finally:
                        upload_action_task.cancel()
                        try:
                            await upload_action_task
                        except Exception:
                            pass

                    # Delete the "processing" message
                    try:
                        await bot.delete_message(message.chat.id, proc_msg.message_id)
                    except Exception:
                        pass

                    md_bytes = md_text.encode("utf-8")
                    res = file_store.register_converted_document(
                        user_id, md_bytes, clean_name, raw_hash=raw_hash, media_dir=str(media_dir)
                    )
                else:
                    with open(res["path"], 'r', encoding='utf-8', errors='replace') as f:
                        md_text = f.read()

                if len(md_text) <= 15000:
                    file_context = f"[File from user: {clean_name}]\n{md_text}"
                else:
                    file_context = f"[File from user: {clean_name}]\nFile is too big to incontext view, so model should use tools for access to it."

                if res.get("is_new"):
                    asyncio.create_task(
                        orchestrator.queue_document_describe(user_id, res["hash_name"], res["real_name"], message.text or getattr(message, 'caption', None) or "")
                    )
            except Exception as e:
                get_user_logger(user_id).warning("Failed to process PDF: %s", e)
                # Clean up the processing message if still around
                try:
                    await bot.delete_message(message.chat.id, proc_msg.message_id)
                except Exception:
                    pass
                await message.reply(f"Failed to process PDF: {e}")
                return

        else:
            await message.reply("File type not supported")
            return


    # Save voice message if present
    voice_path = None
    if message.voice:
        voice_path = await _download_audio(message, user_id)

    # Build forwarded-message context
    forwarded_note = ""
    forward_origin = getattr(message, 'forward_origin', None)
    if forward_origin:
        origin_type = getattr(forward_origin, 'type', None) or ""
        try:
            # User forward
            sender_name = (
                getattr(getattr(forward_origin, 'sender_user', None), 'full_name', None)
                or getattr(getattr(forward_origin, 'sender_user', None), 'username', None)
                # Hidden-user forward
                or getattr(forward_origin, 'sender_user_name', None)
                # Channel / chat forward
                or getattr(getattr(forward_origin, 'chat', None), 'title', None)
                or getattr(getattr(forward_origin, 'sender_chat', None), 'title', None)
            )
        except Exception:
            sender_name = None
        if sender_name:
            forwarded_note = f"[Forwarded from: {sender_name}]\n"
        else:
            forwarded_note = "[Forwarded message]\n"

    # Build user text: prefer message.text; for photos use caption; add image note
    caption = getattr(message, 'caption', None) or ""
    raw_text = (message.text or caption or "").strip()

    parts: list[str] = []
    if forwarded_note:
        parts.append(forwarded_note.strip())
    if imgs:
        # Include original file names of images if known, else a generic note
        image_names = [Path(p).name for p in imgs]
        parts.append(f"[image: {', '.join(image_names)}]")
    if file_context:
        parts.append(file_context)
    if raw_text:
        parts.append(raw_text)

    user_text = "\n".join(parts) if parts else "[image]" if imgs else ""

    # If this user has a pending approval, intercept: decline the tool and inject the new message
    if user_id in orchestrator._user_pending_approval:
        await orchestrator.interrupt_user(user_id, "message", user_text, images=imgs if imgs else None)
        return

    if user_text or imgs or voice_path:
        # Buffer this message, images, and voice paths
        if user_id not in _user_buffers:
            _user_buffers[user_id] = {"text_parts": [], "images": [], "audio_paths": []}

        if user_text:
            _user_buffers[user_id]["text_parts"].append(user_text)
        if imgs:
            _user_buffers[user_id]["images"].extend(imgs)
        if voice_path:
            _user_buffers[user_id].setdefault("audio_paths", []).append(voice_path)

        # Cancel existing flush task if present to reset the 2.0s timer
        existing_task = _user_flush_tasks.get(user_id)
        if existing_task and not existing_task.done():
            existing_task.cancel()

        # Schedule a new flush task
        _user_flush_tasks[user_id] = asyncio.create_task(
            _flush_user_buffer(user_id, message.chat.id)
        )



async def main():
    # Initialize logging
    try:
        init_logging(CONFIG.get('logging'))
    except Exception:
        pass

    # Optionally start MCP manager (do not crash if not configured)
    try:
        if mcp_mgr:
            mcp_mgr.start()
            for r in mcp_mgr.reports:
                logger.info("[MCP Report] %s", r)
    except Exception as e:
        logger.exception("MCP start failed: %s", e)

    # Register bot commands (if real bot)
    try:
        if TELEGRAM_TOKEN:
            await bot.set_my_commands([
                BotCommand(command='new', description='Start a new conversation'),
                BotCommand(command='stop', description='Stop current generation'),
            ])
    except Exception:
        pass

    # Start orchestrator background tasks
    try:
        await orchestrator.start()
    except Exception as e:
        logger.exception("Orchestrator start failed: %s", e)
    logger.info("Bot started. Waiting for messages...")
    try:
        await dp.start_polling(bot)
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass
        try:
            await orchestrator.stop()
        except Exception:
            pass
        try:
            mem_store.close()
        except Exception:
            pass
        try:
            if mcp_mgr and hasattr(mcp_mgr, 'stop'):
                try:
                    mcp_mgr.stop()
                except Exception:
                    pass
        except Exception:
            pass


@dp.callback_query()
async def handle_tool_approval(call: types.CallbackQuery):
    data = call.data or ""
    if not data.startswith("tool_approve:"):
        return
    parts = data.split(":")
    if len(parts) < 3:
        await call.answer("Invalid approval data")
        return
    approval_id = parts[1]
    decision = parts[2]
    approved = decision == '1'
    try:
        await orchestrator.approval_response(approval_id, approved)
        await call.answer("Decision sent")
        try:
            await call.message.delete()
        except Exception:
            pass
    except Exception as e:
        await call.answer("Failed to send decision")


if __name__ == '__main__':
    asyncio.run(main())
