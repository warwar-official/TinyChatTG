import asyncio
import json
import logging
import re
import uuid
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional, Callable, List

import yaml
from imports.utils.logger import get_user_logger

from imports.tools.remember_info import add_memory as mm_add
from imports.tools.recall_info import search_memory as ms_search

logger = logging.getLogger(__name__)


def _load_prompts() -> Dict[str, str]:
    """Load prompts from data/configs/prompts.yaml."""
    p = Path(__file__).resolve().parents[1] / 'data' / 'configs' / 'prompts.yaml'
    if not p.exists():
        return {}
    try:
        with open(p, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


PROMPTS = _load_prompts()


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks from model output."""
    if not text:
        return text
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()


class Orchestrator:
    def __init__(self, config: Dict[str, Any], provider, mem_store, mcp_mgr=None,
                 approval_callback: Optional[Callable] = None,
                 status_callback: Optional[Callable] = None,
                 conv_store=None):
        self.config = config
        self.provider = provider
        self.mem_store = mem_store
        self.mcp_mgr = mcp_mgr
        self.approval_callback = approval_callback
        self.status_callback = status_callback
        self.conv_store = conv_store

        self.primary_queue: asyncio.Queue = asyncio.Queue()
        self.secondary_queue: asyncio.Queue = asyncio.Queue()

        # Concurrency & rate limit configuration
        concurrency_conf = (self.config or {}).get('concurrency', {})
        self.max_concurrent = concurrency_conf.get('max_concurrent', 1)
        self.requests_per_minute = concurrency_conf.get('requests_per_minute', None)
        self._rate_lock = asyncio.Lock()
        self._req_timestamps = deque()
        self.model_semaphore = asyncio.Semaphore(self.max_concurrent)

        self._tasks: List[asyncio.Task] = []
        self.pending_approvals: Dict[str, asyncio.Future] = {}
        self._user_pending_approval: Dict[int, str] = {}   # user_id -> approval_id
        self._user_job_tasks: Dict[int, asyncio.Task] = {}  # user_id -> in-flight task
        self._user_interrupt: Dict[int, dict] = {}          # user_id -> {type, text, images}
        self._stopped_users: set = set()                    # users whose queued jobs should be skipped

        self.tools = {'tools': {}}
        if self.mcp_mgr:
            self.tools = self.mcp_mgr.get_all_tools()
            
        # conversation storage for per-user context
        if self.conv_store is None:
            from imports.memory.conversation_store import ConversationStore
            self.conv_store = ConversationStore()

        # Provide a default model-based merge callback to MemoryStore if none provided
        try:
            if not getattr(self.mem_store, 'merge_callback', None) or not callable(getattr(self.mem_store, 'merge_callback', None)):
                self.mem_store.merge_callback = self._merge_memory_callback
        except Exception:
            pass

        bot_conf = (self.config or {}).get('bot', {})
        self.max_messages = bot_conf.get('max_messages', 15)
        self.last_messages_tail = bot_conf.get('last_messages_tail', 5)
        self.max_tool_iterations = bot_conf.get('max_tool_iterations', 5)

        # Load prompts
        self.personality_prompt = PROMPTS.get('personality_prompt', 'You are a helpful AI assistant.')
        self.tool_using_explanation_prompt = PROMPTS.get('tool_using_explanation_prompt', 'You have access to tools that can fetch information, manage memories, and perform other actions.\nAlways use available tools if you don\'t know the answer or need to memorize important facts.')
        self.conversation_summarize_prompt = PROMPTS.get('conversation_summarize_prompt', 'Summarize the following conversation into a short summary (max 500 characters).')
        self.fact_extraction_prompt = PROMPTS.get('fact_extraction_prompt', 'Extract important facts from the conversation. Return a JSON array of objects with keys "title" and "text". Return only JSON.')
        self.tool_summarize_prompt = PROMPTS.get('tool_summarize_prompt', 'Summarize the following tool result concisely.')
        self.tool_summarize_user_template = PROMPTS.get('tool_summarize_user_template', 'Tool: {tool_name}\nResult:\n{tool_result}\n\nSummarize this tool result concisely.')
        self.context_template = PROMPTS.get('context_template', '# ASSISTANT PERSONALITY INFO\n{personality}\n\n# TOOL USING EXPLANATION\nYou have access to tools that can fetch information, manage memories, and perform other actions.\nAlways use available tools if you don\'t know the answer or need to memorize important facts.\n\n# USEFUL RUNTIME INFORMATION\nCurrent system time is {runtime_time}.\n{summary_section}\n{memories_section}')


    async def start(self):
        if self.mcp_mgr:
            self.tools = self.mcp_mgr.get_all_tools()
        concurrency_conf = (self.config or {}).get('concurrency', {})
        primary_workers = concurrency_conf.get('primary_workers', 1)
        secondary_workers = concurrency_conf.get('secondary_workers', 1)
        for _ in range(primary_workers):
            self._tasks.append(asyncio.create_task(self._primary_worker()))
        for _ in range(secondary_workers):
            self._tasks.append(asyncio.create_task(self._secondary_worker()))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        # attempt to stop MCP manager if present
        try:
            if self.mcp_mgr and hasattr(self.mcp_mgr, 'stop'):
                try:
                    self.mcp_mgr.stop()
                except Exception:
                    pass
        except Exception:
            pass

    async def submit_primary(self, user_id: int, chat_id: int, text: str, images: Optional[List[str]] = None) -> Dict[str, Any]:
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        await self.primary_queue.put({
            "user_id": int(user_id), 
            "chat_id": int(chat_id), 
            "text": text, 
            "images": images, 
            "future": fut
        })
        return await fut

    async def _primary_worker(self):
        while True:
            job = await self.primary_queue.get()
            fut = job.get('future')
            user_id = job.get('user_id')

            # Skip queued jobs for users who sent /stop
            if user_id and user_id in self._stopped_users:
                self._stopped_users.discard(user_id)
                if fut and not fut.done():
                    fut.set_result({"error": "stopped"})
                continue

            try:
                task = asyncio.create_task(self._handle_primary_job(job))
                if user_id:
                    self._user_job_tasks[user_id] = task
                try:
                    res = await task
                except asyncio.CancelledError:
                    res = {"error": "stopped"}
                finally:
                    if user_id and self._user_job_tasks.get(user_id) is task:
                        self._user_job_tasks.pop(user_id, None)
                if fut and not fut.done():
                    fut.set_result(res)
            except Exception as e:
                logger.exception("Primary worker error")
                if fut and not fut.done():
                    fut.set_exception(e)

    async def _secondary_worker(self):
        while True:
            job = await self.secondary_queue.get()
            try:
                coro = job.get('coro')
                if asyncio.iscoroutine(coro):
                    await coro
                elif callable(coro):
                    await coro()
            except Exception as e:
                logger.warning("Secondary worker error: %s", e)

    async def approval_response(self, approval_id: str, approved: bool):
        fut = self.pending_approvals.get(approval_id)
        if fut and not fut.done():
            fut.set_result(bool(approved))

    async def interrupt_user(self, user_id: int, interrupt_type: str,
                             text: Optional[str] = None,
                             images: Optional[List[str]] = None) -> None:
        """Decline any pending approval and inject an interrupt signal into the tool loop."""
        self._user_interrupt[user_id] = {"type": interrupt_type, "text": text, "images": images}
        approval_id = self._user_pending_approval.get(user_id)
        if approval_id:
            fut = self.pending_approvals.get(approval_id)
            if fut and not fut.done():
                fut.set_result(False)

    async def stop_user(self, user_id: int) -> None:
        """Cancel any in-flight generation and pending approval for this user."""
        self._user_interrupt[user_id] = {"type": "stop"}
        # Decline pending approval
        approval_id = self._user_pending_approval.get(user_id)
        if approval_id:
            fut = self.pending_approvals.get(approval_id)
            if fut and not fut.done():
                fut.set_result(False)
        # Cancel in-flight job task
        task = self._user_job_tasks.get(user_id)
        if task and not task.done():
            task.cancel()
        # Mark so subsequent queued jobs are also skipped
        self._stopped_users.add(user_id)

    async def _acquire_rate_slot(self):
        """Simple requests-per-minute limiter for model calls."""
        if not self.requests_per_minute:
            return
        while True:
            async with self._rate_lock:
                now = time.time()
                # prune entries older than 60 seconds
                while self._req_timestamps and now - self._req_timestamps[0] >= 60:
                    self._req_timestamps.popleft()
                if len(self._req_timestamps) < self.requests_per_minute:
                    self._req_timestamps.append(now)
                    return
                oldest = self._req_timestamps[0]
                wait_time = 60 - (now - oldest)
            # sleep outside the lock to allow other coroutines to progress
            await asyncio.sleep(min(wait_time, 1))

    # ─── Context Assembly ───────────────────────────────────────────────

    def _build_context(self, user_id: int, current_text: str) -> List[Dict[str, str]]:
        """Build the full message context for the model.

        Order:
        1. System prompt
        2. Previous conversation summary (if exists)
        3. Relevant memories
        4. Conversation history (last N messages, current user message already appended)
        """
        messages = []
        
        # Format sections
        from datetime import datetime
        now_str = datetime.now().strftime('%A, %B %d, %Y at %I:%M %p')
        
        summary_section = ""
        try:
            summary = self.conv_store.get_summary(user_id)
            if summary:
                summary_section = f"[Previous conversation summary]\n{summary}\n"
        except Exception as e:
            logger.warning("Failed to get summary for user %d: %s", user_id, e)

        memories_section = ""
        try:
            mems = self.mem_store.search(user_id, current_text, top_k=5)
            if mems:
                mem_lines = []
                for m in mems:
                    text = m.get('text', '')
                    if text:
                        title = m.get('meta', {}).get('title', 'Memory')
                        mem_lines.append(f"{title}: {text}")
                mem_text = '\n'.join(mem_lines)
                if mem_text:
                    template = getattr(self, 'memories_template', '[Relevant memories]\n{memories}\n')
                    memories_section = template.format(memories=mem_text)
        except Exception as e:
            logger.warning("Failed to search memories for user %d: %s", user_id, e)

        # Build final system content using the template
        sys_content = self.context_template.format(
            personality=self.personality_prompt,
            tool_using_explanation=self.tool_using_explanation_prompt,
            runtime_time=now_str,
            summary_section=summary_section,
            memories_section=memories_section
        ).strip()

        messages.append({"role": "system", "content": sys_content})

        # 4. Conversation history (already includes current user message)
        try:
            history = self.conv_store.get_history(user_id)
            # Take last max_messages entries
            tail = history[-self.max_messages:] if len(history) > self.max_messages else history
            for entry in tail:
                role = entry.get('role', 'user')
                text = entry.get('text', '')
                meta = entry.get('meta', {}) or {}
                if not text:
                    continue
                # System entries from summarization — skip (summary is added above)
                if role == 'system':
                    continue
                # Tool results — present as tool role for OpenAI API compat
                if role == 'tool':
                    tool_name = meta.get('tool', 'tool') if isinstance(meta, dict) else 'tool'
                    call_id = meta.get('tool_call_id', 'call_0') if isinstance(meta, dict) else 'call_0'
                    is_summarized = meta.get('summarized', False) if isinstance(meta, dict) else False
                    prefix = "[summarized tool result] " if is_summarized else ""
                    messages.append({"role": "tool", "tool_call_id": call_id, "name": tool_name, "content": f"{prefix}{text}"})
                elif role == 'assistant':
                    # Check if this is a function_call record
                    if isinstance(meta, dict) and meta.get('function_call'):
                        tool_name = meta.get('tool', 'tool')
                        call_id = meta.get('tool_call_id', 'call_0')
                        args_str = meta.get('args', '{}')
                        messages.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": args_str
                                }
                            }]
                        })
                    else:
                        messages.append({"role": "assistant", "content": text})
                else:
                    images = meta.get('images') if isinstance(meta, dict) else None
                    if images:
                        import base64
                        import mimetypes
                        content = []
                        if text:
                            content.append({"type": "text", "text": text})
                        for img_path in images:
                            try:
                                with open(img_path, "rb") as f:
                                    b64 = base64.b64encode(f.read()).decode('utf-8')
                                mime, _ = mimetypes.guess_type(img_path)
                                mime = mime or 'image/jpeg'
                                content.append({
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{mime};base64,{b64}"}
                                })
                            except Exception as e:
                                logger.warning(f"Could not load image {img_path}: {e}")
                        
                        if not content:
                            content = text
                        messages.append({"role": role, "content": content})
                    else:
                        messages.append({"role": role, "content": text})
        except Exception as e:
            logger.warning("Failed to get history for user %d: %s", user_id, e)
            # At minimum, include the current message
            messages.append({"role": "user", "content": current_text})

        # --- Strict context cleanup to prevent 400 errors ---
        valid_messages = []
        expected_tool_call_ids = set()
        
        # 1. Keep all system messages
        idx = 0
        while idx < len(messages) and messages[idx].get("role") == "system":
            valid_messages.append(messages[idx])
            idx += 1
            
        # 2. Drop any leading non-user messages (e.g. orphaned assistant responses due to truncation)
        while idx < len(messages) and messages[idx].get("role") != "user":
            idx += 1
            
        # 3. Validate the rest of the conversation
        for i in range(idx, len(messages)):
            msg = messages[i]
            role = msg.get("role")
            
            if role == "assistant":
                valid_messages.append(msg)
                if "tool_calls" in msg:
                    for tc in msg["tool_calls"]:
                        expected_tool_call_ids.add(tc["id"])
            elif role == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id in expected_tool_call_ids:
                    valid_messages.append(msg)
                # Orphaned tool results are silently dropped
            else:
                valid_messages.append(msg)
                
        # Fallback if we somehow deleted the entire conversation
        if len(valid_messages) == sum(1 for m in valid_messages if m.get("role") == "system"):
            valid_messages.append({"role": "user", "content": current_text})
            
        return valid_messages

    # ─── Primary Job Handler ────────────────────────────────────────────

    async def _handle_primary_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        user_id = job.get('user_id')
        chat_id = job.get('chat_id')
        text = job.get('text')
        images = job.get('images')
        ulog = get_user_logger(user_id)

        # Append user message to persistent conversation history
        start_id = None
        try:
            meta = {"images": images} if images else None
            start_id = self.conv_store.append_message(user_id, 'user', text, meta=meta)
        except Exception as e:
            ulog.error("Failed to append user message: %s", e)

        # Build functions list from tools config
        functions = self._build_functions_list()

        # Build context and call model (with tool loop)
        res = await self._tool_loop(user_id, chat_id, text, functions, ulog)
        
        # If an error occurred, rollback — but NOT for graceful stop/new signals
        non_rollback_errors = {"stopped", "stop", "new"}
        if res.get("error") and res.get("error") not in non_rollback_errors and start_id is not None:
            try:
                self.conv_store.delete_since_id(user_id, start_id)
                ulog.info("Rolled back conversation history since id %s due to error", start_id)
            except Exception as e:
                ulog.error("Failed to rollback conversation history: %s", e)

        # Check if conversation exceeded message limit — schedule summarization
        try:
            msg_count = self.conv_store.get_history_count(user_id)
        except Exception:
            msg_count = 0

        if msg_count > self.max_messages:
            try:
                history = self.conv_store.get_history(user_id)
                convo_copy = list(history)
                await self.secondary_queue.put({"coro": self._summarize_and_extract(user_id, convo_copy)})
            except Exception as e:
                ulog.error("Failed to schedule summarization: %s", e)

        return res

    async def _tool_loop(self, user_id: int, chat_id: int, current_text: str,
                         functions: List[Dict], ulog: logging.Logger) -> Dict[str, Any]:
        """Execute the model call with tool-call loop.

        1. Build context → call model
        2. If model returns text → done
        3. If model returns function_call → execute tool → feed result back → repeat
        4. Max iterations to prevent infinite loops
        """
        raw_tools = self.tools.get('tools') or {}

        for iteration in range(self.max_tool_iterations):
            # Build context fresh each iteration (includes any new tool results appended to history)
            messages = self._build_context(user_id, current_text)

            # Log the model context for debugging (trim large contexts)
            try:
                preview = json.dumps(messages, ensure_ascii=False)
                if len(preview) > 8000:
                    preview = preview[:8000] + '...'
                ulog.debug("Model context: %s", preview)
            except Exception:
                try:
                    ulog.debug("Model context (repr): %s", repr(messages)[:8000])
                except Exception:
                    pass

            # Rate limiting + semaphore for model calls
            await self._acquire_rate_slot()
            async with self.model_semaphore:
                resp = await self.provider.chat(messages, tools=functions if functions else None)

            if not resp:
                return {"error": "empty response from model"}

            if isinstance(resp, dict) and resp.get('error'):
                return {"error": resp.get('error')}

            # Parse response
            choice = resp.get('choices', [None])[0] if isinstance(resp, dict) and resp.get('choices') else resp
            message = choice.get('message', {}) if isinstance(choice, dict) else {}
            tool_calls = message.get('tool_calls') if isinstance(message, dict) else None

            # Log model response for debugging
            try:
                preview_r = json.dumps(resp, ensure_ascii=False)
                if len(preview_r) > 8000:
                    preview_r = preview_r[:8000] + '...'
                ulog.debug("Model response: %s", preview_r)
            except Exception:
                try:
                    ulog.debug("Model response (repr): %s", repr(resp)[:8000])
                except Exception:
                    pass

            if not tool_calls:
                # Plain text response — done
                assistant_text = message.get('content') if isinstance(message, dict) else str(resp)
                assistant_text = _strip_thinking(assistant_text)
                try:
                    self.conv_store.append_message(user_id, 'assistant', assistant_text)
                except Exception as e:
                    ulog.error("Failed to append assistant message: %s", e)
                return {"assistant": assistant_text}

            # ── Function call handling ──────────────────────────────────
            for t_call in (tool_calls if isinstance(tool_calls, list) else []):
                if not isinstance(t_call, dict) or t_call.get('type') != 'function':
                    continue
                func = t_call.get('function', {})
                tool_name = func.get('name')
                args_raw = func.get('arguments', '{}')
                try:
                    tool_args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except Exception:
                    tool_args = {"raw": args_raw}

                tool_conf = raw_tools.get(tool_name, {})
                ulog.info("Tool call: %s args=%s", tool_name, json.dumps(tool_args, ensure_ascii=False))

                # Record assistant's function-call intent into conversation
                call_id = t_call.get('id') or f"call_{uuid.uuid4().hex[:8]}"
                try:
                    call_text = f"[function_call] name={tool_name} args={json.dumps(tool_args, ensure_ascii=False)}"
                    self.conv_store.append_message(user_id, 'assistant', call_text, meta={
                        'function_call': True, 
                        'tool': tool_name,
                        'tool_call_id': call_id,
                        'args': args_raw
                    })
                except Exception as e:
                    ulog.error("Failed to record function call: %s", e)

                if not tool_conf or tool_conf.get('visible') is False:
                    ulog.warning("Model tried to call unknown or hidden tool: %s", tool_name)
                    text_result = f"Error: Unknown tool '{tool_name}'"
                    try:
                        self.conv_store.append_message(user_id, 'tool', text_result, meta={'tool': tool_name, 'tool_call_id': call_id})
                    except Exception as e:
                        ulog.error("Failed to append tool result: %s", e)
                    continue

                # Brief status — show only tool name, no args
                if self.status_callback:
                    try:
                        await self.status_callback(chat_id, f"Using: <i><b>{tool_name}</b>...</i>")
                    except Exception:
                        pass

                # Approval flow
                if tool_conf.get('permissions') == 'ask' or tool_conf.get('require_approval'):
                    approval_id = uuid.uuid4().hex
                    loop = asyncio.get_event_loop()
                    fut = loop.create_future()
                    self.pending_approvals[approval_id] = fut
                    self._user_pending_approval[user_id] = approval_id
                    if self.approval_callback:
                        try:
                            await self.approval_callback(approval_id, user_id, {"name": tool_name, "args": tool_args})
                        except Exception:
                            pass
                    approved = await fut
                    self._user_pending_approval.pop(user_id, None)
                    self.pending_approvals.pop(approval_id, None)

                    if not approved:
                        interrupt = self._user_interrupt.pop(user_id, None)
                        i_type = interrupt.get("type") if interrupt else None
                        i_text = interrupt.get("text") if interrupt else None
                        i_imgs = interrupt.get("images") if interrupt else None
                        declined_note = f"Tool '{tool_name}' was declined."

                        if i_type == "message":
                            # User sent a new message instead of tapping approve/decline
                            try:
                                self.conv_store.append_message(user_id, 'tool', declined_note,
                                                               meta={'tool': tool_name, 'tool_call_id': call_id})
                            except Exception:
                                pass
                            if i_text is not None:
                                try:
                                    img_meta = {"images": i_imgs} if i_imgs else None
                                    self.conv_store.append_message(user_id, 'user', i_text, meta=img_meta)
                                except Exception:
                                    pass
                            current_text = i_text or current_text
                            continue

                        elif i_type in ("stop", "new"):
                            try:
                                self.conv_store.append_message(user_id, 'tool', declined_note,
                                                               meta={'tool': tool_name, 'tool_call_id': call_id})
                            except Exception:
                                pass
                            return {"error": i_type}

                        else:
                            # Manual button decline
                            try:
                                self.conv_store.append_message(user_id, 'tool', declined_note,
                                                               meta={'tool': tool_name, 'tool_call_id': call_id})
                            except Exception:
                                pass
                            if self.status_callback:
                                try:
                                    await self.status_callback(chat_id, f"<i><b>{tool_name}</b> declined</i>")
                                except Exception:
                                    pass
                            continue

                # Execute tool
                result = await self._execute_tool(tool_name, tool_args, tool_conf, user_id, ulog)

                # Convert result to text
                if isinstance(result, dict):
                    text_result = str(result.get('result') or result.get('entry') or result.get('text') or result)
                else:
                    text_result = str(result)

                # Truncate if too long
                max_chars = self.mem_store.max_tool_response_chars if hasattr(self.mem_store, 'max_tool_response_chars') else 15000
                if len(text_result) > max_chars:
                    text_result = text_result[:max_chars] + "\n... [truncated]"

                # Append tool result to conversation history
                try:
                    self.conv_store.append_message(user_id, 'tool', text_result, meta={'tool': tool_name, 'tool_call_id': call_id})
                except Exception as e:
                    ulog.error("Failed to append tool result: %s", e)

                # If tool result is long, schedule summarization on secondary queue
                if len(text_result) > 3000:
                    await self.secondary_queue.put({
                        "coro": self._summarize_tool_result(user_id, tool_name, text_result)
                    })

                # Send error status if tool produced error
                if isinstance(result, dict) and result.get('error'):
                    if self.status_callback:
                        try:
                            await self.status_callback(chat_id, f"<i><b>{tool_name}</b>: \"error: {result.get('error')}\"</i>")
                        except Exception:
                            pass

                ulog.info("Tool result: %s", text_result[:500])
            # Loop continues — model will see the tool result in context and produce next response

        # If we exhausted iterations, return an error
        return {"error": "Too many tool call iterations, stopping."}

    async def _execute_tool(self, tool_name: str, tool_args: Dict, tool_conf: Dict, user_id: int, ulog: logging.Logger) -> Any:
        """Execute a tool call — local handler or MCP."""
        provider = tool_conf.get('_provider')
        if provider == 'app':
            handler = tool_conf.get('handler')
            if handler and 'remember_info' in handler:
                try:
                    return mm_add(self.mem_store, user_id, tool_args)
                except Exception as e:
                    ulog.error("Local tool '%s' failed: %s", tool_name, e)
                    return {"error": str(e)}
            elif handler and 'memory_search' in handler:
                try:
                    return ms_search(self.mem_store, user_id, tool_args)
                except Exception as e:
                    ulog.error("Local tool '%s' failed: %s", tool_name, e)
                    return {"error": str(e)}
            return {"error": "Unknown local app tool handler"}
        else:
            try:
                if self.mcp_mgr:
                    res = self.mcp_mgr.send_tool_call(provider, tool_name, tool_args, user_id)
                    if asyncio.iscoroutine(res):
                        return await res
                    return res
                else:
                    return {"error": "no handler and no MCP configured"}
            except Exception as e:
                ulog.error("MCP tool '%s' failed: %s", tool_name, e)
                return {"error": str(e)}

    def _build_functions_list(self) -> List[Dict]:
        """Build functions schema list from tools config for function-calling models."""
        functions = []
        raw_tools = self.tools.get('tools') or {}
        for tname, tcfg in raw_tools.items():
            if tcfg.get('visible', True) and 'schema' in tcfg:
                functions.append({
                    "type": "function",
                    "function": {
                        "name": tname,
                        "description": tcfg.get('description', ''),
                        "parameters": tcfg.get('schema'),
                    }
                })
        return functions

    # ─── Secondary Tasks ────────────────────────────────────────────────

    async def _summarize_tool_result(self, user_id: int, tool_name: str, tool_result: str):
        """Summarize a long tool result and replace it in conversation history."""
        ulog = get_user_logger(user_id)
        try:
            summary_msgs = [
                {"role": "system", "content": self.tool_summarize_prompt},
                {"role": "user", "content": self.tool_summarize_user_template.format(
                    tool_name=tool_name, tool_result=tool_result
                )},
            ]
            await self._acquire_rate_slot()
            async with self.model_semaphore:
                resp = await self.provider.chat(summary_msgs)

            summary = self._extract_text_from_response(resp)
            if not summary:
                return

            # Find and replace the last tool result for this tool in conversation
            history = self.conv_store.get_history(user_id)
            replaced = False
            for i in range(len(history) - 1, -1, -1):
                entry = history[i]
                if entry.get('role') == 'tool' and entry.get('text') == tool_result:
                    meta = entry.get('meta', {}) or {}
                    if isinstance(meta, dict) and meta.get('tool') == tool_name:
                        entry['text'] = f"[summarized] {summary}"
                        if isinstance(meta, dict):
                            meta['summarized'] = True
                        entry['meta'] = meta
                        replaced = True
                        break
            if replaced:
                self.conv_store.set_history(user_id, history)
                ulog.info("Summarized tool result for %s", tool_name)
        except Exception as e:
            ulog.warning("Failed to summarize tool result: %s", e)



    async def _summarize_and_extract(self, user_id: int, conversation: List[Any]):
        """Summarize conversation and extract important facts to memory store."""
        ulog = get_user_logger(user_id)

        # Build conversation text
        try:
            parts = []
            for entry in conversation:
                if isinstance(entry, dict):
                    role = entry.get('role', '')
                    text = entry.get('text') or entry.get('content') or ''
                    if role:
                        parts.append(f"[{role}] {text}")
                    else:
                        parts.append(str(text))
                else:
                    parts.append(str(entry))
            conv_text = "\n".join([p for p in parts if p])
        except Exception:
            conv_text = "\n".join([str(x) for x in conversation])

        # Summarize (with semaphore)
        try:
            summary_msgs = [
                {"role": "system", "content": self.conversation_summarize_prompt},
                {"role": "user", "content": conv_text},
            ]
            await self._acquire_rate_slot()
            async with self.model_semaphore:
                summ_resp = await self.provider.chat(summary_msgs)
            summary = _strip_thinking(self._extract_text_from_response(summ_resp))
        except Exception as e:
            ulog.warning("Summarization failed, using truncation: %s", e)
            summary = conv_text[:500]

        # Extract facts as JSON from model (with semaphore)
        extracted = []
        try:
            extract_msgs = [
                {"role": "system", "content": self.fact_extraction_prompt},
                {"role": "user", "content": conv_text},
            ]
            await self._acquire_rate_slot()
            async with self.model_semaphore:
                ex_resp = await self.provider.chat(extract_msgs)
            content = _strip_thinking(self._extract_text_from_response(ex_resp))
            
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

            # Attempt to parse JSON from the model
            import re
            try:
                parsed = json.loads(clean_content)
                if isinstance(parsed, list):
                    for it in parsed:
                        title = it.get('title') if isinstance(it, dict) else None
                        text = it.get('text') if isinstance(it, dict) else None
                        if text and isinstance(text, str):
                            if 50 <= len(text) <= 350 and re.search(r'[a-zA-Z]', text):
                                extracted.append({'title': title or '', 'text': text})
            except Exception:
                # fallback: naive split
                for line in clean_content.splitlines():
                    line = line.strip()
                    if not line or line in ("{", "}", "[", "]", "},", "],"):
                        continue
                    if 50 <= len(line) <= 350 and re.search(r'[a-zA-Z]', line):
                        extracted.append({'title': '', 'text': line})
        except Exception as e:
            ulog.warning("Fact extraction failed: %s", e)

        # Store extracted memories
        for item in extracted:
            try:
                res = self.mem_store.add_memory(user_id, item.get('text', ''), meta={'title': item.get('title', ''), 'source': 'extracted'})
                try:
                    ulog.info("add_memory result: %s", str(res))
                except Exception:
                    pass
            except Exception as e:
                ulog.warning("Failed to add extracted memory: %s", e)

        # Replace conversation with summarized context + tail in persistent store
        if self.last_messages_tail and len(conversation) > self.last_messages_tail:
            cut_idx = len(conversation) - self.last_messages_tail
            found_user_idx = -1
            # Search backward for the closest user message (up to 15 messages back)
            for i in range(cut_idx, max(-1, cut_idx - 15), -1):
                msg = conversation[i]
                role = msg.get('role') if isinstance(msg, dict) else 'user'
                if role == 'user':
                    found_user_idx = i
                    break
            
            if found_user_idx != -1:
                tail = conversation[found_user_idx:]
            else:
                # Fallback: exact tail but insert a fallback user message at the front
                tail = conversation[cut_idx:]
                fallback_msg = {
                    'role': 'user',
                    'text': 'Previous conversation truncated. Rely in your answer on summary, memories and messages that left',
                    'ts': 0
                }
                tail.insert(0, fallback_msg)
        else:
            tail = conversation

        tail_clean = []
        for t in tail:
            if isinstance(t, dict):
                tail_clean.append(t)
            else:
                tail_clean.append({'role': 'user', 'text': str(t), 'ts': 0})

        try:
            self.conv_store.set_history(user_id, tail_clean)
            self.conv_store.set_summary(user_id, summary)
            ulog.info("Conversation summarized and trimmed, %d memories extracted", len(extracted))
        except Exception as e:
            ulog.error("Failed to save summarized conversation: %s", e)

    async def _merge_memory_callback(self, existing: str, new: str) -> str:
        """Use the model to produce a concise merged memory text for two fragments."""
        try:
            merge_prompt = PROMPTS.get('memory_merge_prompt', 'Merge two memory fragments into one concise memory. If the two memories are contradictory, meaningless, or contain no useful text to remember, output exactly the word REJECT.')
            merge_template = PROMPTS.get('memory_merge_user_template', 'Memory A:\n{existing}\n\nMemory B:\n{new}\n\nReturn a single merged memory text, concise.')
            msgs = [
                {"role": "system", "content": merge_prompt},
                {"role": "user", "content": merge_template.format(existing=existing, new=new)},
            ]
            await self._acquire_rate_slot()
            
            for _ in range(3):
                try:
                    async with self.model_semaphore:
                        resp = await self.provider.chat(msgs)
                    content = self._extract_text_from_response(resp) or ""
                    
                    if not content.strip():
                        continue
                        
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
                    import asyncio
                    await asyncio.sleep(1)
                    continue
                    
            return existing + "\n" + new
        except Exception:
            return existing + "\n" + new

    def _extract_text_from_response(self, resp: Any) -> str:
        """Extract text content from a model response dict."""
        if not resp:
            return ''
        try:
            if isinstance(resp, dict) and resp.get('choices'):
                choice = resp['choices'][0]
                msg = choice.get('message', {}) if isinstance(choice, dict) else {}
                return msg.get('content', '') if isinstance(msg, dict) else str(resp)
            return str(resp)
        except Exception:
            return str(resp)
