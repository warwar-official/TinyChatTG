"""Simple LM Studio OpenAI-compatible provider adapter."""
import aiohttp
import asyncio
import json
import re
import uuid
from typing import List, Optional, Dict, Any


class LMStudioProvider:
    def __init__(self, url: str, default_model: str = "default_model"):
        self.url = url.rstrip("/")
        self.default_model = default_model

    async def chat(self, messages: List[Dict[str, Any]], model: Optional[str] = None, tools=None, timeout: int = 300):
        payload = {"model": model or self.default_model, "messages": messages}
        if tools is not None:
            payload["tools"] = tools

        attempt = 0
        max_attempts = 4
        backoff = 1

        async with aiohttp.ClientSession() as session:
            while attempt < max_attempts:
                try:
                    async with session.post(f"{self.url}/v1/chat/completions", json=payload, timeout=timeout) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            
                            # Fallback logic for broken LM Studio tool calls
                            choices = data.get("choices", [])
                            if choices:
                                msg = choices[0].get("message", {})
                                content = msg.get("content", "") or ""
                                tool_calls = msg.get("tool_calls", [])
                                
                                if not tool_calls:
                                    search_text = (msg.get("reasoning_content", "") or "") + "\n" + content
                                    if "<tool_call>" in search_text:
                                        pattern = re.compile(r"<tool_call>\s*<function=([^>]+)>(.*?)</function>\s*</tool_call>", re.DOTALL)
                                        param_pattern = re.compile(r"<parameter=([^>]+)>(.*?)</parameter>", re.DOTALL)
                                        
                                        found_calls = []
                                        for match in pattern.finditer(search_text):
                                            func_name = match.group(1).strip()
                                            params_str = match.group(2)
                                            
                                            args = {}
                                            for param_match in param_pattern.finditer(params_str):
                                                p_name = param_match.group(1).strip()
                                                p_val = param_match.group(2).strip()
                                                args[p_name] = p_val
                                                
                                            found_calls.append({
                                                "id": f"call_{uuid.uuid4().hex[:8]}",
                                                "type": "function",
                                                "function": {
                                                    "name": func_name,
                                                    "arguments": json.dumps(args, ensure_ascii=False)
                                                }
                                            })
                                        
                                        if found_calls:
                                            msg["tool_calls"] = found_calls
                                            
                                    # Retry if answer is entirely empty and no tool calls found
                                    if not msg.get("tool_calls") and not content.strip():
                                        attempt += 1
                                        await asyncio.sleep(backoff)
                                        backoff = min(backoff * 2, 5)
                                        continue
                                        
                            return data
                        elif resp.status == 503:
                            await asyncio.sleep(backoff)
                            backoff *= 2
                            attempt += 1
                            continue
                        elif resp.status == 429:
                            await asyncio.sleep(backoff * 5)
                            backoff *= 2
                            attempt += 1
                            continue
                        else:
                            text = await resp.text()
                            return {"error": f"HTTP {resp.status}: {text}"}
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    attempt += 1
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 5)
            return {"error": "max retries exceeded"}

    # Convenience helper to format messages for a single user text
    async def chat_text(self, text: str, model: Optional[str] = None):
        messages = [{"role": "user", "content": text}]
        return await self.chat(messages, model=model)
