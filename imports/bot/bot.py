# TinyChat (c) 2026 WarWar <somethingstrenge@gmail.com>
# This file is part of TinyChat.
# TinyChat is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License v3.

import asyncio
import asyncio
import hashlib
import html
import io
import json
import traceback

from aiogram import types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, FSInputFile
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from imports.config import CONFIG, get_bot_config
from imports.files.store import FileStore
from imports.files.converter import convert_office_to_markdown, convert_pdf_to_markdown, PANDOC_INPUT_EXTENSIONS
from imports.orchestrator import Orchestrator, PROMPTS

from imports.stt.whisper_client import STTBusyError
from imports.utils.logger import get_user_logger, init_logging
from imports.bot.globals import bot, dp, auth_store, mem_store, file_store, conv_store, logger, _user_buffers, _user_flush_tasks, PROJECT_ROOT, mcp_mgr, lm, whisper_client, typing_queue
from imports.bot.utility import markdown_to_html, compress_image_to_2mp, split_text


async def main():
    # Initialize logging
    try:
        init_logging(CONFIG.get('logging'))
    except Exception as e:
        logger.exception("Failed to initialize logging. Exception: %s", e)

    # Optionally start MCP manager (do not crash if not configured)
    try:
        if mcp_mgr:
            mcp_mgr.start()
    except Exception as e:
        logger.exception("MCP start failed: %s", e)

    # Register bot commands (if real bot)
    try:
        await bot.set_my_commands([
            BotCommand(command='new', description='Start a new conversation'),
            BotCommand(command='stop', description='Stop current generation'),
        ])
    except Exception as e:
        logger.exception("Failed to set bot commands. Exception: %s", e)
    
    # Run keep typing
    try:
        asyncio.create_task(_keep_typing())
    except Exception as e:
        logger.exception("Keep typing start failed: %s", e)

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
        except Exception as e:
            logger.exception("Failed to close bot session. Exception: %s", e)
        try:
            await orchestrator.stop()
        except Exception as e:
            logger.exception("Failed to stop orchestrator. Exception: %s", e)
        try:
            mem_store.close()
        except Exception as e:
            logger.exception("Failed to close memory store. Exception: %s", e)
        try:
            if mcp_mgr and hasattr(mcp_mgr, 'stop'):
                try:
                    mcp_mgr.stop()
                except Exception as e:
                    logger.exception("Failed to stop MCP manager. Exception: %s", e)
        except Exception as e:
            logger.exception("Failed to stop MCP manager. Exception: %s", e)

# Provide model-based merge callback to MemoryStore
async def _merge_memories(existing: str, new: str) -> str:
    try:
        
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
            except Exception as e:
                logger.exception("Failed to merge memories. Exception: %s", e)
                await asyncio.sleep(1)
                continue
                
        return existing + "\n" + new
    except Exception as e:
        logger.exception("Failed to merge memories. Exception: %s", e)
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
    except Exception as e:
        logger.exception("Failed to send approval UI. Exception: %s", e)


# Typing indicator while processing
async def _keep_typing(interval: float = 4.0):
    user_ids = []
    while True:
        try:
            try:
                command = typing_queue.get_nowait()
                id = command.get('chat_id', None)
                action = command.get('action', None)
                if id and action:
                    if action == "start":
                        user_ids.append(id)
                    elif action == "stop":
                        try:
                            user_ids.remove(id)
                        except:
                            pass
                    else:
                        logger.exception("Unknown keep_typing action: %s", action)
                else:
                    logger.exception("Unknown keep_typing command: %s", command)
            except asyncio.QueueEmpty:
                pass
            try:
                for cid in user_ids:
                    await bot.send_chat_action(cid, "typing")
            except Exception as e:
                logger.exception("Failed to send chat action. Exception: %s", e)
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return


# Status callback: sends tool status messages to user
async def _tool_status(chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, markdown_to_html(text), parse_mode="HTML")
    except Exception as e:
        logger.exception("Failed to send tool status. Exception: %s", e)
        try:
            await bot.send_message(chat_id, text)
        except Exception as e:
            logger.exception("Failed to send tool status as plain text. Exception: %s", e)


async def _answer_callback(chat_id: int, resp: dict):
    await typing_queue.put({"chat_id":chat_id,"action":"stop"})
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
            final_message = markdown_to_html(assistant_text)
            if len(final_message) > 4000:
                final_message_parts = split_text(final_message)
                for part in final_message_parts:
                    await bot.send_message(chat_id, part, parse_mode="HTML")
            else:
                await bot.send_message(chat_id, final_message, parse_mode="HTML")
        except Exception:
            final_message = resp.get('assistant')
            if len(final_message) > 4000:
                final_message_parts = split_text(final_message)
                for part in final_message_parts:
                    await bot.send_message(chat_id, part)
            else:
                await bot.send_message(chat_id, final_message,)
        return


# Send file callback: sends a file to the user
async def _send_file_callback(chat_id: int, file_path: str, real_name: str):
    try:
        f = FSInputFile(file_path, filename=real_name)
        await bot.send_document(chat_id, f)
    except Exception as e:
        logger.exception("Failed to send file %s to chat %d: %s", file_path, chat_id, e)

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
            except Exception as e:
                logger.exception("Failed to send STT busy message. Exception: %s", e)
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

    try:
        await typing_queue.put({"chat_id":chat_id,"action":"start"})
        # To ensure that action shows now, not after 4 seconds
        await bot.send_chat_action(chat_id, "typing")
    except Exception as e:
        logger.exception("Failed to create typing task. Exception: %s", e)

    try:
        await orchestrator.submit_primary(user_id, chat_id, final_text, images=images)
    except Exception as e:
        await bot.send_message(chat_id, f"Orchestrator error: {e}")
        return

# -------------------------------

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

    # log code generation (robust lookup of user display name)
    nickname = (
        getattr(message.from_user, 'username', None)
        or getattr(message.from_user, 'full_name', None)
        or getattr(message.from_user, 'first_name', None)
        or str(getattr(message.from_user, 'id', 'Unknown'))
    )
    logger.info(f"User {user_id} (@{nickname}) started the bot.")
    get_user_logger(user_id).info(f"User {user_id} (@{nickname}) started the bot.")
    await message.answer("Hello! Welcome to TinyChat. Send your authorization code to proceed. If you don't have one, please contact the administrator. All contacts are in bot description.")


@dp.message(Command("new"))
async def cmd_new(message: types.Message):
    user_id = message.from_user.id
    if not auth_store.is_authorized(user_id):
        await message.reply("Autorize first with your authorization code.")
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
    except Exception as e:
        get_user_logger(user_id).exception("Failed to start new conversation: %s", e)
        print(traceback.format_exc())
        await message.reply("Failed to start new conversation (see server logs)")


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    user_id = message.from_user.id
    if not auth_store.is_authorized(user_id):
        await message.reply("Autorize first with your authorization code.")
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

    if not auth_store.is_authorized(user_id):
        if auth_store.is_code_banned(user_id):
            await message.reply("You are temporarily banned from requesting codes due to repeated failures.")
            return
        txt = message.text.strip()
        try:
            # search for key
            res = auth_store.redeem_key(user_id, txt)
            # it is key
            if isinstance(res, dict) and res.get('ok'):
                # Granted via key
                ktype = res.get('type')
                if ktype == 'infinity':
                    await message.reply("Authorization complete. You have been granted infinite access.")
                elif ktype == 'user':
                    exp = res.get('expires_at', 0) or 0
                    if exp:
                        exp_str = datetime.fromtimestamp(exp).strftime('%Y-%m-%d %H:%M:%S')
                        await message.reply(f"Authorization complete. Access expires at {exp_str}.")
                    else:
                        await message.reply("Authorization complete. Access granted.")
                else:
                    logger.warning("Unknown key type '%s' for user %d", ktype, user_id)
                    await message.reply("Authorization complete. Access granted.")
                get_user_logger(user_id).info("user authorized via key: %s", txt)
                return
            # it is not key
            else:
                if isinstance(res, dict) and res.get('reason') and res.get('reason') != 'not_found':
                    # Inform user on explicit key failure (expired/used)
                    reason = res.get('reason')
                    if reason == 'expired':
                        await message.reply("This key has expired.")
                        return
                    elif reason == 'used_up':
                        await message.reply("This key has already been used the maximum number of times.")
                        return
                else:
                    await message.reply("Invalid code. Please check the code and try again.")
                    return
        except Exception as e:
            # redemption errors should not block normal code auth flow
            logger.exception("Failed to redeem authorization key. Exception: %s", e)
            await message.reply("An error occurred while redeeming the key. Please try again or contact support.")
            return

        await message.reply("Please run /start and authorize first.")
        return
    else:
        # Message rate limiting for authorized users
        try:
            rate = auth_store.record_message(user_id)
            if rate.get('banned'):
                # delete incoming message and notify user
                try:
                    await bot.delete_message(message.chat.id, message.message_id)
                except Exception as e:
                    logger.exception("Failed to delete processing message. Exception: %s", e)
                bans = auth_store.get_bans(user_id)
                ban_until = bans.get('message_ban_until', 0)
                if ban_until:
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
        except Exception as e:
            logger.exception("Failed to handle rate limiting. Exception: %s", e)

    # Unknown command handling: do not forward to model
    if message.text and message.text.strip().startswith('/'):
        cmd = message.text.strip().split()[0]
        allowed_cmds = ['/new', '/stop']
        if cmd not in allowed_cmds:
            await message.reply('Unknown command')
            return

    # For unsupported file types, reply with an error message instead of ignoring silently or crashing
    if message.content_type in ("audio", "video", "video_note", "poll"):
        await message.reply("File type does not suported")
        return

    # Save images if present
    imgs = []
    if message.photo:
        # save highest resolution photo in this message
        photo = message.photo[-1]
        try:
            # download to memory/temp
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
            except Exception as e:
                logger.exception("Failed to unlink temporary file. Exception: %s", e)

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
                except Exception as e:
                    logger.exception("Failed to unlink temporary file. Exception: %s", e)

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
                bio = io.BytesIO()
                await bot.download(doc, destination=bio)
                raw_bytes = bio.getvalue()
                raw_hash = hashlib.sha256(raw_bytes).hexdigest()

                res = file_store.check_converted_document_exists(user_id, raw_hash)
                if not res:
                    # Notify user that processing is starting
                    proc_msg = await bot.send_message(message.chat.id, "📄 File processing. Please wait...")

                    # Switch chat action to "upload_document" during processing
                    async def _keep_upload_action(cid: int, interval: float = 4.0):
                        try:
                            while True:
                                try:
                                    await bot.send_chat_action(cid, "upload_document")
                                except Exception as e:
                                    logger.exception("Failed to send upload document action. Exception: %s", e)
                                await asyncio.sleep(interval)
                        except asyncio.CancelledError:
                            return

                    upload_action_task = asyncio.create_task(_keep_upload_action(message.chat.id))

                    try:
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
                            except Exception as e:
                                logger.exception("Failed to unlink temporary file. Exception: %s", e)
                    finally:
                        upload_action_task.cancel()
                        try:
                            await upload_action_task
                        except Exception as e:
                            logger.exception("Failed to await upload action task. Exception: %s", e)

                    # Delete the "processing" message
                    try:
                        await bot.delete_message(message.chat.id, proc_msg.message_id)
                    except Exception as e:
                        logger.exception("Failed to delete processing message. Exception: %s", e)

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
                raw_hash = hashlib.sha256(raw_bytes).hexdigest()

                res = file_store.check_converted_document_exists(user_id, raw_hash)

                # If converted already exists, possibly retry corrupted pages
                if res:
                    payload = res.get("payload") or {}
                    if payload.get("corrupted") and payload.get("corrupted_pages"):
                        corrupted_pages = payload.get("corrupted_pages") or []

                        # Notify user that retry is starting
                        proc_msg = await bot.send_message(message.chat.id, "📄 Retrying OCR for corrupted pages. Please wait...")

                        async def _keep_upload_action(cid: int, interval: float = 4.0):
                            try:
                                while True:
                                    try:
                                        await bot.send_chat_action(cid, "upload_document")
                                    except Exception as e:
                                        logger.exception("Failed to send upload document action. Exception: %s", e)
                                    await asyncio.sleep(interval)
                            except asyncio.CancelledError:
                                return

                        upload_action_task = asyncio.create_task(_keep_upload_action(message.chat.id))

                        try:
                            temp_path = PROJECT_ROOT / "data" / "tmp"
                            temp_path.mkdir(parents=True, exist_ok=True)
                            tmp_src = temp_path / f"{doc.file_unique_id}.pdf"
                            tmp_src.write_bytes(raw_bytes)

                            media_dir = Path(payload.get("media_dir") or (PROJECT_ROOT / "data" / "files" / "documents" / raw_hash / "media"))

                            try:
                                new_md_text, new_report = await convert_pdf_to_markdown(
                                    tmp_src,
                                    gemini_provider=lm,
                                    media_dir=media_dir,
                                    force_ocr_for_pages=corrupted_pages,
                                    return_report=True,
                                )
                            finally:
                                try:
                                    tmp_src.unlink()
                                except Exception as e:
                                    logger.exception("Failed to unlink temporary file. Exception: %s", e)
                        finally:
                            upload_action_task.cancel()
                            try:
                                await upload_action_task
                            except Exception as e:
                                logger.exception("Failed to cancel upload action task. Exception: %s", e)

                        # Delete the "processing" message
                        try:
                            await bot.delete_message(message.chat.id, proc_msg.message_id)
                        except Exception as e:
                            logger.exception("Failed to delete processing message. Exception: %s", e)

                        # Load existing pages sidecar (if present)
                        pages_file = Path(res["path"]).with_name(raw_hash + "_pages.json")
                        existing_pages = []
                        try:
                            if pages_file.exists():
                                with open(pages_file, 'r', encoding='utf-8') as fh:
                                    existing_pages = json.load(fh)
                        except Exception as e:
                            logger.exception("Failed to load existing pages file. Exception: %s", e)
                            existing_pages = []

                        total_pages = new_report.get("total_pages", 0)
                        if not existing_pages or len(existing_pages) != total_pages:
                            try:
                                with open(res["path"], 'r', encoding='utf-8', errors='replace') as fh:
                                    stored_md = fh.read()
                                parts = stored_md.split("\n\n---\n\n")
                                existing_pages = []
                                for i in range(total_pages):
                                    content = parts[i] if i < len(parts) else ""
                                    existing_pages.append({"page": i+1, "content": content, "status": ("extracted" if content else "empty")})
                            except Exception as e:
                                logger.exception("Failed to process existing pages. Exception: %s", e)
                                existing_pages = [{"page": i+1, "content": "", "status": "empty"} for i in range(total_pages)]

                        # Check whether all previously corrupted pages were fixed
                        all_fixed = True
                        for pg in corrupted_pages:
                            rpt = next((p for p in new_report.get("pages", []) if p.get("page") == pg), None)
                            if not rpt or rpt.get("status") != "ok":
                                all_fixed = False
                                break

                        if all_fixed:
                            # Merge pages into existing_pages and write merged md
                            for rpt in new_report.get("pages", []):
                                pnum = rpt.get("page")
                                if pnum in corrupted_pages:
                                    idx = pnum - 1
                                    existing_pages[idx]["content"] = rpt.get("content", "")
                                    existing_pages[idx]["status"] = rpt.get("status")

                            merged_parts = [p.get("content", "") for p in existing_pages]
                            merged_md = "\n\n---\n\n".join([m for m in merged_parts if m])
                            try:
                                with open(res["path"], 'w', encoding='utf-8') as fh:
                                    fh.write(merged_md)
                                with open(pages_file, 'w', encoding='utf-8') as fh:
                                    json.dump(existing_pages, fh, ensure_ascii=False)
                                md_bytes = merged_md.encode('utf-8')
                                file_store.register_converted_document(
                                    user_id, md_bytes, clean_name, raw_hash=raw_hash, media_dir=str(media_dir), corrupted=False, corrupted_pages=[], overwrite=True
                                )
                            except Exception as e:
                                get_user_logger(user_id).warning("Failed to merge reprocessed pages: %s", e)
                                await message.reply("Failed to merge reprocessed pages. The previous version is kept.")
                        else:
                            try:
                                await message.reply("Retrying OCR did not fix corrupted pages. Keeping previous converted file.")
                            except Exception as e:
                                logger.exception("Failed to reply with failure message. Exception: %s", e)
                            try:
                                asyncio.create_task(
                                    orchestrator.queue_conversion_failure(user_id, clean_name, f"Retry OCR failed for pages: {corrupted_pages}")
                                )
                            except Exception as e:
                                logger.exception("Failed to queue conversion failure. Exception: %s", e)

                        with open(res["path"], 'r', encoding='utf-8', errors='replace') as f:
                            md_text = f.read()
                    else:
                        with open(res["path"], 'r', encoding='utf-8', errors='replace') as f:
                            md_text = f.read()
                else:
                    # If we don't have it, perform OCR
                    proc_msg = await bot.send_message(message.chat.id, "📄 File processing. Please wait...")

                    async def _keep_upload_action(cid: int, interval: float = 4.0):
                        try:
                            while True:
                                try:
                                    await bot.send_chat_action(cid, "upload_document")
                                except Exception as e:
                                    logger.exception("Failed to send upload document action. Exception: %s", e)
                                await asyncio.sleep(interval)
                        except asyncio.CancelledError:
                            return

                    upload_action_task = asyncio.create_task(_keep_upload_action(message.chat.id))

                    try:
                        temp_path = PROJECT_ROOT / "data" / "tmp"
                        temp_path.mkdir(parents=True, exist_ok=True)
                        tmp_src = temp_path / f"{doc.file_unique_id}.pdf"
                        tmp_src.write_bytes(raw_bytes)

                        media_dir = PROJECT_ROOT / "data" / "files" / "documents" / raw_hash / "media"
                        try:
                            md_text, report = await convert_pdf_to_markdown(
                                tmp_src,
                                gemini_provider=lm,
                                media_dir=media_dir,
                                return_report=True,
                            )
                        finally:
                            try:
                                tmp_src.unlink()
                            except Exception as e:
                                logger.exception("Failed to unlink temporary file. Exception: %s", e)
                    finally:
                        upload_action_task.cancel()
                        try:
                            await upload_action_task
                        except Exception as e:
                            logger.exception("Failed to await upload action task. Exception: %s", e)

                    try:
                        await bot.delete_message(message.chat.id, proc_msg.message_id)
                    except Exception as e:
                        logger.exception("Failed to delete processing message. Exception: %s", e)

                    total_pages = report.get("total_pages", 0)
                    ocr_pages = report.get("ocr_pages", 0)
                    ocr_failures = report.get("ocr_failures", 0)
                    success_count = report.get("success_count", 0)

                    pages_file = PROJECT_ROOT / "data" / "files" / "documents" / raw_hash / f"{raw_hash}_pages.json"
                    try:
                        pages_file.parent.mkdir(parents=True, exist_ok=True)
                        with open(pages_file, 'w', encoding='utf-8') as fh:
                            json.dump(report.get('pages', []), fh, ensure_ascii=False)
                    except Exception as e:
                        logger.exception("Failed to save pages file. Exception: %s", e)

                    if ocr_pages == total_pages and ocr_failures > (total_pages / 2):
                        try:
                            await message.reply("Failed to process PDF: OCR failed for majority of pages. Please provide a clearer scan or try again.")
                        except Exception as e:
                            logger.exception("Failed to reply with failure message. Exception: %s", e)
                        try:
                            asyncio.create_task(
                                orchestrator.queue_conversion_failure(user_id, clean_name, f"OCR failed for {ocr_failures}/{total_pages} pages")
                            )
                        except Exception as e:
                            logger.exception("Failed to queue conversion failure. Exception: %s", e)
                        md_text = ""
                        res = {}
                    else:
                        corrupted = False
                        corrupted_pages = []
                        if total_pages and (success_count > (total_pages / 2)) and report.get('failed_count', 0) > 0:
                            corrupted = True
                            corrupted_pages = [p.get('page') for p in report.get('pages', []) if p.get('status') not in ("ok", "extracted")]

                        md_bytes = md_text.encode("utf-8")
                        res = file_store.register_converted_document(
                            user_id, md_bytes, clean_name, raw_hash=raw_hash, media_dir=str(media_dir), corrupted=corrupted, corrupted_pages=corrupted_pages
                        )
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
                try:
                    await bot.delete_message(message.chat.id, proc_msg.message_id)
                except Exception as e:
                    logger.exception("Failed to delete processing message. Exception: %s", e)
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
        except Exception as e:
            logger.exception("Failed to delete call message. Exception: %s", e)
    except Exception as e:
        await call.answer("Failed to send decision")


### GLOBAL INITIALIZATION

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
    response_callback=_answer_callback,
    conv_store=conv_store,
    file_store=file_store,
    send_file_callback=_send_file_callback,
)

### RUN THE BOT

if __name__ == '__main__':
    asyncio.run(main())
