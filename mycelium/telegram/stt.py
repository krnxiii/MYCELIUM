"""Speech-to-text providers for voice message transcription."""

from __future__ import annotations

import io
from typing import Protocol

import structlog

log = structlog.get_logger()


class STTProvider(Protocol):
    """Speech-to-text provider interface."""
    async def transcribe(self, audio: bytes) -> str: ...


class WhisperLocalSTT:
    """Local faster-whisper via OpenAI-compatible HTTP API (Docker container)."""

    def __init__(self,
                 url:      str = "http://whisper:8000",
                 model:    str = "medium",
                 language: str = "auto") -> None:
        self._url      = url.rstrip("/")
        self._model    = model
        self._language = language

    async def transcribe(self, audio: bytes) -> str:
        import httpx

        data: dict[str, str] = {"model": self._model}
        if self._language != "auto":
            data["language"] = self._language

        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"{self._url}/v1/audio/transcriptions",
                files={"file": ("voice.ogg", io.BytesIO(audio), "audio/ogg")},
                data=data,
            )
            resp.raise_for_status()
            return resp.json()["text"].strip()


class DeepgramSTT:
    """Deepgram Nova-3 cloud API (direct REST, no SDK dependency)."""

    _URL = "https://api.deepgram.com/v1/listen"

    def __init__(self, api_key: str, language: str = "auto") -> None:
        self._api_key  = api_key
        self._language = language

    async def transcribe(self, audio: bytes) -> str:
        import httpx

        params: dict[str, str] = {
            "model":          "nova-3",
            "punctuate":      "true",
            "smart_format":   "true",
            "detect_language": "true",
        }
        if self._language != "auto":
            params.pop("detect_language", None)
            params["language"] = self._language

        headers = {
            "Authorization": f"Token {self._api_key}",
            "Content-Type":  "audio/ogg",
        }
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                self._URL,
                params=params,
                headers=headers,
                content=audio,
            )
            resp.raise_for_status()
            data = resp.json()
            transcript = (
                data["results"]["channels"][0]["alternatives"][0]["transcript"]
                .strip()
            )
            log.debug("deepgram.result", transcript=transcript[:100],
                       audio_bytes=len(audio),
                       confidence=data["results"]["channels"][0]["alternatives"][0].get("confidence"))
            return transcript


def create_stt(provider: str, **kwargs: str) -> STTProvider | None:
    """Factory: create STT provider from config."""
    match provider:
        case "whisper-local":
            return WhisperLocalSTT(
                url=kwargs.get("url", "http://whisper:8000"),
                model=kwargs.get("model", "medium"),
                language=kwargs.get("language", "auto"),
            )
        case "deepgram":
            api_key = kwargs.get("api_key", "")
            if not api_key:
                log.error("stt.deepgram_no_key",
                          hint="Set MYCELIUM_TELEGRAM__STT_API_KEY")
                return None
            return DeepgramSTT(
                api_key=api_key,
                language=kwargs.get("language", "auto"),
            )
        case _:
            return None  # voice disabled
