"""Minimal aiogram-based bot skeleton integrating LM Studio provider and MemoryStore.

This is a starting point: auth flow, text/image handling, `/new` command, and typing indicator.
"""
import os
import asyncio
from pathlib import Path
from typing import Dict, Any

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from imports.config import CONFIG, get_provider, get_bot_config, get_mcp, get_telegram_token
from imports.providers.lm_studio import LMStudioProvider
from imports.memory.store import MemoryStore
from imports.mcp.manager import MCPManager
from imports.orchestrator import Orchestrator
from imports.utils.logger import get_user_logger, init_logging
from imports.memory.conversation_store import ConversationStore
from aiogram.types import BotCommand
from imports.auth.store import AuthStore

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


if not TELEGRAM_TOKEN:
    bot = DummyBot()
else:
    bot = Bot(token=TELEGRAM_TOKEN)

dp = Dispatcher()

provider_conf = get_provider('lmstudio') or {}
lm = LMStudioProvider(provider_conf.get('url', 'http://192.168.50.212:1234'), provider_conf.get('default_model', 'default_model'))
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
    text = f"Tool `{tool_call.get('name')}` wants to run with args: {tool_call.get('args')}\nAllow?"
    try:
        await bot.send_message(user_id, text, reply_markup=kb)
    except Exception:
        pass


# Status callback: sends tool status messages to user
async def _tool_status(chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, text, parse_mode="Markdown")
    except Exception:
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            pass


orchestrator = Orchestrator(
    CONFIG, lm, mem_store,
    mcp_mgr=mcp_mgr,
    approval_callback=_approval_ui,
    status_callback=_tool_status,
    conv_store=conv_store,
)


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

    # print code to server console and log it
    print(f"Auth code for user {user_id}: {code}")
    get_user_logger(user_id).info(f"Auth code generated: {code}")
    await message.answer("Initialization code was printed to server console. Please paste it here to authorize.")


@dp.message(Command("new"))
async def cmd_new(message: types.Message):
    user_id = message.from_user.id
    if not auth_store.is_authorized(user_id):
        await message.reply("Authorize first with /start")
        return
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
            await bot.send_message(user_id, f"You are temporarily banned for spamming until {ban_until}.")
            return
    except Exception:
        pass

    # Unknown command handling: do not forward to model
    if message.text and message.text.strip().startswith('/'):
        cmd = message.text.strip().split()[0]
        allowed_cmds = ['/start', '/new']
        if cmd not in allowed_cmds:
            await message.reply('Unknown command')
            return

    # Reject unsupported content types
    if message.content_type in ("audio", "voice", "video", "document", "video_note", "poll"):
        await message.reply("Invalid input")
        return

    # Save images if present
    imgs = []
    if message.photo:
        # Support media groups (albums) by grouping files under media_group_id
        mg = getattr(message, 'media_group_id', None)
        if mg:
            folder = PROJECT_ROOT / 'data' / 'images' / str(user_id) / str(mg)
        else:
            folder = PROJECT_ROOT / 'data' / 'images' / str(user_id)
        folder.mkdir(parents=True, exist_ok=True)
        # save highest resolution photo in this message
        photo = message.photo[-1]
        file_path = folder / f"{photo.file_unique_id}.jpg"
        try:
            await bot.download(photo, destination=str(file_path))
            imgs.append(str(file_path))
        except Exception as e:
            get_user_logger(user_id).warning("Failed to download photo: %s", e)

    # Mark forwarded
    forwarded_note = ""
    if getattr(message, 'forward_origin', None):
        forwarded_note = "[forwarded message]\n"

    # Keep sending typing action while the model generates a response
    async def _keep_typing(chat_id: int, interval: float = 4.0):
        try:
            while True:
                try:
                    await bot.send_chat_action(chat_id, "typing")
                except Exception:
                    pass
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    typing_task = None
    try:
        typing_task = asyncio.create_task(_keep_typing(message.chat.id))
    except Exception:
        typing_task = None

    # Build simple chat payload
    user_text = message.text or ("[image]" if imgs else "")
    if forwarded_note:
        user_text = forwarded_note + user_text

    try:
        resp = await orchestrator.submit_primary(user_id, message.chat.id, user_text)
    except Exception as e:
        await message.reply(f"Orchestrator error: {e}")
        return
    finally:
        # stop typing indicator
        if typing_task:
            typing_task.cancel()
            try:
                await typing_task
            except Exception:
                pass

    # Response can be assistant text or tool result
    if not resp:
        await message.reply("Empty response from orchestrator")
        return

    if resp.get('error'):
        await message.reply(f"Error: {resp.get('error')}")
        return

    if resp.get('assistant'):
        try:
            await message.reply(resp.get('assistant'), parse_mode="Markdown")
        except Exception:
            await message.reply(resp.get('assistant'))
        return

    if resp.get('tool'):
        tool = resp.get('tool')
        result = resp.get('result')
        if isinstance(result, dict) and result.get('error'):
            await message.reply(f"__{tool}: \"error: {result.get('error')}\"__")
        else:
            await message.reply(f"__{tool}: \"{result}\"__")
        return


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
                print(f"[MCP Report] {r}")
    except Exception as e:
        print("MCP start failed:", e)

    # Register bot commands (if real bot)
    try:
        if TELEGRAM_TOKEN:
            await bot.set_my_commands([BotCommand(command='start', description='Authorize'), BotCommand(command='new', description='Start new conversation')])
    except Exception:
        pass

    # Start orchestrator background tasks
    try:
        await orchestrator.start()
    except Exception as e:
        print("Orchestrator start failed:", e)

    print("Bot started. Waiting for messages...")
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
