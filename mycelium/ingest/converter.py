"""R3.1: Multimodal document converter — auto-detect format, convert to text.

Supported formats:
  Text:   .txt, .md, .csv, .html, .xml, .json
  Docling: .pdf, .docx, .pptx, .xlsx, .html (rich) — requires `docling`
  Image:  .jpg, .png, .webp — placeholder (requires Vision LLM, future)
  Audio:  .mp3, .wav, .flac — placeholder (requires ASR, future)

Graceful fallback: if docling not installed → text-only extraction.
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path

import structlog

log = structlog.get_logger()

# ── MIME routing ─────────────────────────────────────────

_TEXT_MIMES = {
    "text/plain", "text/markdown", "text/csv",
    "text/html",  "text/xml",
}

_DOCLING_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",   # .docx
    "application/vnd.openxmlformats-officedocument.presentationml.presentation", # .pptx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",         # .xlsx
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/msword",
    "text/latex",
}

_IMAGE_MIMES = {
    "image/jpeg", "image/png", "image/webp",
    "image/gif",  "image/tiff",
}

_AUDIO_MIMES = {
    "audio/mpeg", "audio/wav", "audio/flac",
    "audio/mp4",  "audio/ogg", "audio/webm",
}

# Extension overrides (mimetypes module sometimes wrong)
_EXT_MIME: dict[str, str] = {
    ".md":    "text/markdown",
    ".tex":   "text/latex",
    ".latex": "text/latex",
}


def _detect_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


# ── Converter ────────────────────────────────────────────


def convert(path: Path) -> str:
    """Auto-detect format and convert to text. Raises ValueError on failure."""
    mime = _detect_mime(path)
    log.info("converter_detect", path=path.name, mime=mime)

    # Plain text
    if mime in _TEXT_MIMES:
        return path.read_text(encoding="utf-8", errors="replace")

    # JSON
    if mime == "application/json":
        return _extract_json(path.read_text(encoding="utf-8"))

    # Docling-supported formats
    if mime in _DOCLING_MIMES:
        return _docling_convert(path, mime)

    # Image (future: Vision LLM)
    if mime in _IMAGE_MIMES:
        raise ValueError(
            f"Image extraction not yet implemented ({path.name}). "
            "Coming with Vision LLM integration."
        )

    # Audio (future: ASR)
    if mime in _AUDIO_MIMES:
        raise ValueError(
            f"Audio transcription not yet implemented ({path.name}). "
            "Coming with ASR integration."
        )

    # Fallback: try text
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if text.strip():
            log.info("converter_fallback_text", path=path.name)
            return text
    except Exception:
        pass

    raise ValueError(f"Unsupported format: {mime} ({path.name})")


# ── Docling ──────────────────────────────────────────────


_docling_available: bool | None = None


def _check_docling() -> bool:
    global _docling_available
    if _docling_available is None:
        try:
            import docling  # noqa: F401
            _docling_available = True
        except ImportError:
            _docling_available = False
    return _docling_available


def _docling_convert(path: Path, mime: str) -> str:
    """Convert via Docling library. Falls back to text extraction."""
    if not _check_docling():
        # Graceful fallback: try reading as text
        log.warning("docling_not_installed", path=path.name,
                    hint="pip install docling")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                return text
        except Exception:
            pass
        raise ValueError(
            f"Cannot convert {path.name} ({mime}): "
            "install docling for PDF/DOCX/PPTX support: pip install docling"
        )

    from docling.document_converter import DocumentConverter as DC

    log.info("docling_converting", path=path.name, mime=mime)
    result   = DC().convert(str(path))
    markdown = result.document.export_to_markdown()

    if not markdown.strip():
        raise ValueError(f"Docling returned empty text for {path.name}")

    log.info("docling_done", path=path.name, chars=len(markdown))
    return markdown


# ── JSON helper ──────────────────────────────────────────


def _extract_json(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    parts: list[str] = []
    _walk(data, parts)
    return "\n".join(parts)


def _walk(obj: object, parts: list[str]) -> None:
    if isinstance(obj, str):
        s = obj.strip()
        if s:
            parts.append(s)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk(v, parts)
    elif isinstance(obj, list):
        for item in obj:
            _walk(item, parts)
