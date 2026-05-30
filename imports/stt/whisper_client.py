"""Groq Whisper STT client.

Handles:
- Rate limiting (sliding-window RPM counter, independent of the LLM limiter)
- Oversized voice files: split via pydub → transcribe parts → concatenate
- Error 429: honour Retry-After if ≤ 60 s, otherwise raise STTBusyError
"""
import asyncio
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import List

import aiohttp

logger = logging.getLogger(__name__)

_WHISPER_MODEL = "whisper-large-v3-turbo"
_DEFAULT_RPM = 20
_DEFAULT_MAX_SIZE = 25_000_000  # 25 MB


class STTBusyError(Exception):
    """Raised when Groq STT returns 429 with a Retry-After > 60 s."""


class WhisperClient:
    """Async Groq Whisper client with built-in rate limiting and file splitting."""

    def __init__(self, config: dict):
        self.url: str = config.get("url", "https://api.groq.com/openai/v1/audio/transcriptions")
        self.rpm: int = int(config.get("rpm", _DEFAULT_RPM))
        self.max_size: int = int(config.get("max_size", _DEFAULT_MAX_SIZE))
        self.api_key: str = os.environ.get("GROQ_API_KEY", "")

        # Sliding-window RPM rate limiter
        self._rate_lock = asyncio.Lock()
        self._req_timestamps: deque = deque()

    # ─── Rate limiting ───────────────────────────────────────────────────

    async def _acquire_rate_slot(self) -> None:
        """Block until a slot is available within the configured RPM window."""
        while True:
            async with self._rate_lock:
                now = time.time()
                # Prune entries older than 60 s
                while self._req_timestamps and now - self._req_timestamps[0] >= 60:
                    self._req_timestamps.popleft()
                if len(self._req_timestamps) < self.rpm:
                    self._req_timestamps.append(now)
                    return
                oldest = self._req_timestamps[0]
                wait_time = 60.0 - (now - oldest)
            await asyncio.sleep(min(wait_time, 1.0))

    # ─── Audio splitting ─────────────────────────────────────────────────

    def _split_audio(self, file_path: str) -> List[str]:
        """Split an oversized audio file into chunks that fit within max_size.

        Uses pydub (backed by ffmpeg). Each chunk is exported as .ogg (opus).
        Returns a list of chunk file paths (including the original dir).
        """
        from pydub import AudioSegment  # local import — only used when needed

        src = Path(file_path)
        audio = AudioSegment.from_file(str(src))

        # Estimate duration per chunk based on bitrate approximation.
        # We use a conservative 0.8× safety margin on max_size.
        safe_bytes = int(self.max_size * 0.8)
        total_bytes = src.stat().st_size
        total_ms = len(audio)  # pydub uses milliseconds

        # Proportional split: how many ms fit in safe_bytes?
        ms_per_byte = total_ms / max(total_bytes, 1)
        chunk_ms = int(safe_bytes * ms_per_byte)
        if chunk_ms <= 0:
            chunk_ms = 60_000  # fallback: 1-minute chunks

        chunks: List[str] = []
        start = 0
        idx = 0
        while start < total_ms:
            end = min(start + chunk_ms, total_ms)
            chunk = audio[start:end]
            chunk_path = src.parent / f"{src.stem}_part{idx}.ogg"
            chunk.export(str(chunk_path), format="ogg", codec="libopus")
            chunks.append(str(chunk_path))
            start = end
            idx += 1

        logger.info("Split %s into %d chunks", src.name, len(chunks))
        return chunks

    # ─── Core transcription ──────────────────────────────────────────────

    async def _transcribe_file(self, file_path: str) -> str:
        """Send one file to Groq Whisper API and return the transcript text.

        Respects rate limits. On 429 with Retry-After ≤ 60 s: waits and
        retries once. On 429 with longer delay: raises STTBusyError.
        """
        await self._acquire_rate_slot()

        async def _do_request() -> str:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            path = Path(file_path)
            async with aiohttp.ClientSession() as session:
                with open(file_path, "rb") as f:
                    form = aiohttp.FormData()
                    form.add_field(
                        "file",
                        f,
                        filename=path.name,
                        content_type="audio/ogg",
                    )
                    form.add_field("model", _WHISPER_MODEL)

                    async with session.post(self.url, headers=headers, data=form) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data.get("text", "")

                        if resp.status == 429:
                            retry_after_str = resp.headers.get("Retry-After", "")
                            try:
                                retry_after = float(retry_after_str)
                            except (ValueError, TypeError):
                                retry_after = 999  # treat unknown as long wait

                            if retry_after <= 60:
                                logger.warning(
                                    "Groq STT 429 — waiting %.1f s (Retry-After)", retry_after
                                )
                                await asyncio.sleep(retry_after)
                                return None  # signal caller to retry once

                            # Retry-After > 60 s — propagate as busy
                            raise STTBusyError(
                                f"STT rate limit retry-after={retry_after:.0f}s"
                            )

                        # Other HTTP errors
                        body = await resp.text()
                        raise RuntimeError(
                            f"Groq STT HTTP {resp.status}: {body[:300]}"
                        )

        # First attempt
        result = await _do_request()
        if result is not None:
            return result

        # One retry after 429 + sleep
        await self._acquire_rate_slot()
        result = await _do_request()
        if result is None:
            # Second 429 — give up
            raise STTBusyError("STT returned 429 twice consecutively")
        return result

    async def transcribe(self, file_path: str) -> str:
        """Transcribe a voice file, splitting it first if it exceeds max_size.

        Returns the full transcript string.
        """
        size = Path(file_path).stat().st_size

        if size <= self.max_size:
            return await self._transcribe_file(file_path)

        # File too large — split and transcribe parts sequentially
        # (sequential preserves order; also avoids hammering the rate limiter)
        logger.info("Voice file %.1f MB > limit %.1f MB — splitting",
                    size / 1e6, self.max_size / 1e6)
        chunk_paths = self._split_audio(file_path)
        parts: List[str] = []
        for chunk_path in chunk_paths:
            try:
                text = await self._transcribe_file(chunk_path)
                parts.append(text)
            finally:
                # Clean up temporary chunk files
                try:
                    Path(chunk_path).unlink(missing_ok=True)
                except Exception:
                    pass

        return " ".join(parts)
