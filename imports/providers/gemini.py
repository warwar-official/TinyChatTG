"""Google Gemini provider adapter.

Exposes the same ``chat`` / ``chat_text`` interface as ``LMStudioProvider``
so it can be used as a drop-in replacement.

Message format accepted  : OpenAI-style list[dict] with keys
    role    – "system" | "user" | "assistant" | "tool"
    content – str  (or None when the message carries tool_calls)
    tool_calls (optional) – list of OpenAI-style tool-call dicts
    tool_call_id (optional) – str  (for role=="tool" result messages)
    name     (optional)    – str  (for role=="tool" result messages)

Response format returned  : OpenAI-style dict  {"choices": [...]}
    so that the rest of the application does not have to know which
    backend is actually used.

Tools format accepted     : OpenAI function-calling schema list[dict]:
    {"type": "function",
     "function": {"name": ..., "description": ..., "parameters": {...}}}

Gemini API reference used : v1beta/models/{model}:generateContent
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-2.0-flash"
MAX_ATTEMPTS = 4


# ---------------------------------------------------------------------------
# Conversion helpers  (OpenAI  <->  Gemini)
# ---------------------------------------------------------------------------

def _convert_messages_to_gemini(
    messages: List[Dict[str, Any]],
) -> tuple[Optional[str], List[Dict[str, Any]]]:
    """Convert OpenAI-style messages to Gemini ``contents`` + ``system_instruction``.

    Returns
    -------
    system_instruction : str | None
    contents           : list[dict]   – Gemini ``contents`` field
    """
    system_parts: list[str] = []
    contents: list[dict] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []

        if role == "system":
            if isinstance(content, list):
                sys_text = "\n".join([p.get("text", "") for p in content if p.get("type") == "text"])
                if sys_text:
                    system_parts.append(sys_text)
            elif content:
                system_parts.append(content)
            continue

        if role == "assistant":
            gemini_role = "model"
            parts: list[dict] = []
            if content:
                if isinstance(content, list):
                    for p in content:
                        if p.get("type") == "text" and p.get("text"):
                            parts.append({"text": p["text"]})
                else:
                    parts.append({"text": content})
            for tc in tool_calls:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {"raw": raw_args}
                parts.append({
                    "functionCall": {
                        "name": fn.get("name", ""),
                        "args": args,
                    }
                })
            if not parts:
                parts.append({"text": ""})
            contents.append({"role": gemini_role, "parts": parts})

        elif role == "tool":
            # Tool result – must follow the model turn that requested it
            tc_id = msg.get("tool_call_id", "")
            fn_name = msg.get("name", tc_id)
            try:
                result = json.loads(content) if content else {}
            except (json.JSONDecodeError, TypeError):
                result = {"output": content}
            contents.append({
                "role": "user",
                "parts": [{
                    "functionResponse": {
                        "name": fn_name,
                        "response": result,
                    }
                }]
            })

        else:  # user
            user_parts = []
            if isinstance(content, list):
                for p in content:
                    if p.get("type") == "text" and p.get("text"):
                        user_parts.append({"text": p["text"]})
                    elif p.get("type") == "image_url":
                        url = p.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            try:
                                header, b64 = url.split(",", 1)
                                mime = header.split(";")[0].split(":")[1]
                                user_parts.append({
                                    "inlineData": {
                                        "mimeType": mime,
                                        "data": b64
                                    }
                                })
                            except Exception:
                                pass
            else:
                if content:
                    user_parts.append({"text": content})
            if not user_parts:
                user_parts.append({"text": ""})
            contents.append({
                "role": "user",
                "parts": user_parts,
            })

    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return system_instruction, contents


def _convert_tools_to_gemini(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert OpenAI function-calling tools to Gemini ``tools`` format."""
    function_declarations: list[dict] = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        fn = tool.get("function", {})
        decl: dict = {"name": fn.get("name", "")}
        if fn.get("description"):
            decl["description"] = fn["description"]
        if fn.get("parameters"):
            decl["parameters"] = fn["parameters"]
        function_declarations.append(decl)
    return [{"functionDeclarations": function_declarations}]


def _convert_gemini_response_to_openai(data: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a Gemini generateContent response to OpenAI chat-completion shape."""
    candidates = data.get("candidates", [])
    if not candidates:
        return {"choices": [{"message": {"role": "assistant", "content": ""}}]}

    candidate = candidates[0]
    gemini_content = candidate.get("content", {})
    parts = gemini_content.get("parts", [])

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []

    for part in parts:
        if part.get("thought"):
            reasoning_parts.append(part.get("text") or "")
        elif "text" in part:
            text_parts.append(part["text"])
        elif "functionCall" in part:
            fc = part["functionCall"]
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": fc.get("name", ""),
                    "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False),
                },
            })

    reasoning: str = "\n".join(reasoning_parts) or ""
    message: dict = {"role": "assistant", "content": "\n".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls

    finish_reason_map = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "TOOL_CALLS": "tool_calls",
    }
    raw_finish = candidate.get("finishReason", "STOP")
    finish_reason = finish_reason_map.get(raw_finish, "stop")
    if tool_calls:
        finish_reason = "tool_calls"

    usage_meta = data.get("usageMetadata", {})
    usage = {
        "prompt_tokens": usage_meta.get("promptTokenCount", 0),
        "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
        "total_tokens": usage_meta.get("totalTokenCount", 0),
    }

    return {
        "choices": [{"message": message, "reasoning": reasoning, "finish_reason": finish_reason}],
        "usage": usage,
        "model": data.get("modelVersion", ""),
    }


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class GeminiProvider:
    """Async Google Gemini provider with an OpenAI-compatible interface."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = DEFAULT_MODEL,
    ) -> None:
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.default_model = default_model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        timeout: int = 300,
    ) -> Dict[str, Any]:
        """Send a chat request and return an OpenAI-style response dict."""
        used_model = model or self.default_model
        system_instruction, contents = _convert_messages_to_gemini(messages)

        payload: dict = {"contents": contents}
        payload["generationConfig"] = {"thinkingConfig": {"thinkingLevel": "high"}}
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        if tools:
            payload["tools"] = _convert_tools_to_gemini(tools)
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        url = f"{GEMINI_BASE_URL}/{used_model}:generateContent?key={self.api_key}"

        attempt = 0
        backoff = 1.0

        async with aiohttp.ClientSession() as session:
            while attempt < MAX_ATTEMPTS:
                try:
                    async with session.post(
                        url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as resp:
                        if resp.status == 200:
                            raw = await resp.json()
                            result = _convert_gemini_response_to_openai(raw)

                            # Retry on completely empty response (no text, no tools)
                            choices = result.get("choices", [])
                            if choices:
                                msg = choices[0].get("message", {})
                                has_content = bool((msg.get("content") or "").strip())
                                has_tools = bool(msg.get("tool_calls"))
                                if not has_content and not has_tools:
                                    attempt += 1
                                    await asyncio.sleep(backoff)
                                    backoff = min(backoff * 2, 5)
                                    continue

                            return result

                        elif resp.status in (429, 500, 503):
                            if resp.status == 500:
                                wait = 5.0
                            else:
                                wait = backoff * (5 if resp.status == 429 else 1)
                            await asyncio.sleep(wait)
                            backoff = min(backoff * 2, 30)
                            attempt += 1
                            continue
                        else:
                            text = await resp.text()
                            logger.error("Gemini API error %s: %s", resp.status, text)
                            return {"error": f"HTTP {resp.status}: {text}"}

                except aiohttp.ClientError as exc:
                    logger.warning("Gemini request failed (attempt %d): %s", attempt + 1, exc)
                    attempt += 1
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 5)

        return {"error": "max retries exceeded"}

    async def chat_text(
        self,
        text: str,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Convenience wrapper: single user-text message."""
        messages = [{"role": "user", "content": text}]
        return await self.chat(messages, model=model)
