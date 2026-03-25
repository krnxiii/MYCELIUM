"""VaultStorage: human-readable file storage with content-hash index."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path

import structlog
from pydantic import BaseModel

from mycelium.config import VaultSettings

log = structlog.get_logger()

_TEXT_TYPES = {
    "text/plain", "text/markdown", "text/csv",
    "text/html",  "text/xml",
}


class VaultEntry(BaseModel):
    """Stored file metadata."""

    content_hash:  str
    vault_path:    Path               # absolute
    relative_path: str                # relative to vault root
    original_name: str                # filename only
    mime_type:     str
    size_bytes:    int
    signal_uuid:   str | None = None


class VaultStorage:
    """Human-readable file storage with content-hash index.

    Files live at vault/{category}/{name} (readable paths).
    .index.json maps relative_path -> {content_hash, signal_uuid}.
    """

    def __init__(self, settings: VaultSettings | None = None) -> None:
        self._root = (settings or VaultSettings()).path
        (self._root / "_AGENT").mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # ── Core storage ──────────────────────────────────────

    def store(
        self,
        source: Path | bytes,
        *,
        name:     str = "",
        category: str = "",
    ) -> VaultEntry:
        """Store file in vault/{category}/{name}. Dedup by content hash."""
        if isinstance(source, bytes):
            data = source
            name = name or "unnamed"
        else:
            data = source.read_bytes()
            name = name or source.name

        content_hash = hashlib.sha256(data).hexdigest()

        # Dedup: same content -> return existing entry
        existing_path = self.find_by_hash(content_hash)
        if existing_path:
            log.debug("vault_dedup", hash=content_hash[:16])
            return self.get_by_path(existing_path)  # type: ignore[return-value]

        # Determine category from MIME if not provided
        if not category:
            mime, _ = mimetypes.guess_type(name)
            if not mime:
                # mimetypes doesn't know .md — fallback by extension
                ext = Path(name).suffix.lower()
                mime = _EXT_MIME.get(ext, "application/octet-stream")
            category = _mime_category(mime)
        category = _sanitize_category(category)

        # Place file at vault/{category}/{name}
        cat_dir = self._root / category
        cat_dir.mkdir(parents=True, exist_ok=True)

        safe      = name.replace("/", "_").replace("\\", "_")
        dest_path = _unique_path(cat_dir, safe)
        dest_path.write_bytes(data)

        rel_path = str(dest_path.relative_to(self._root))
        mime, _  = mimetypes.guess_type(name)

        entry = VaultEntry(
            content_hash  = content_hash,
            vault_path    = dest_path,
            relative_path = rel_path,
            original_name = name,
            mime_type     = mime or "application/octet-stream",
            size_bytes    = len(data),
        )

        # Persist to index
        index = self._load_index()
        index[rel_path] = {"content_hash": content_hash, "signal_uuid": None}
        self._save_index(index)

        log.info("vault_stored", hash=content_hash[:16], path=rel_path, size=len(data))
        return entry

    def get(self, content_hash: str) -> VaultEntry | None:
        """Find entry by content hash (index lookup)."""
        rel_path = self.find_by_hash(content_hash)
        if not rel_path:
            return None
        return self.get_by_path(rel_path)

    def get_by_path(self, relative_path: str) -> VaultEntry | None:
        """Find entry by relative path in vault."""
        index = self._load_index()
        meta  = index.get(relative_path)
        if not meta:
            return None

        abs_path = self._root / relative_path
        if not abs_path.exists():
            return None

        mime, _ = mimetypes.guess_type(relative_path)
        return VaultEntry(
            content_hash  = meta["content_hash"],
            vault_path    = abs_path,
            relative_path = relative_path,
            original_name = Path(relative_path).name,
            mime_type     = mime or "application/octet-stream",
            size_bytes    = abs_path.stat().st_size,
            signal_uuid   = meta.get("signal_uuid"),
        )

    def read(self, content_hash: str) -> bytes | None:
        """Read file bytes by content hash."""
        entry = self.get(content_hash)
        return entry.vault_path.read_bytes() if entry else None

    def extract_text(self, entry: VaultEntry) -> str:
        """Extract text content from stored file."""
        from mycelium.ingest.converter import convert

        try:
            return convert(entry.vault_path)
        except ValueError:
            log.warning("extract_text_fallback", path=entry.original_name)
            if entry.mime_type in _TEXT_TYPES:
                return entry.vault_path.read_text(encoding="utf-8", errors="replace")
            if entry.mime_type == "application/json":
                return _extract_json(entry.vault_path.read_text(encoding="utf-8"))
            try:
                return entry.vault_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                return ""

    # ── Index operations ──────────────────────────────────

    def find_by_hash(self, content_hash: str) -> str | None:
        """Find relative path for content hash. Returns None if not found."""
        for path, meta in self._load_index().items():
            if meta.get("content_hash") == content_hash:
                return path
        return None

    def update_signal_uuid(self, relative_path: str, uuid: str) -> None:
        """Set signal_uuid for an indexed file (called after ingest)."""
        index = self._load_index()
        if relative_path in index:
            index[relative_path]["signal_uuid"] = uuid
            self._save_index(index)

    def register(self, relative_path: str) -> VaultEntry | None:
        """Register an existing vault file in the index (no copy).

        Use for files manually placed in vault by the user.
        Returns None if file doesn't exist.
        """
        abs_path = self._root / relative_path
        if not abs_path.exists():
            return None

        data = abs_path.read_bytes()
        content_hash = hashlib.sha256(data).hexdigest()

        # Dedup: same content already indexed elsewhere
        existing = self.find_by_hash(content_hash)
        if existing and existing != relative_path:
            log.debug("vault_register_dedup", hash=content_hash[:16])
            return self.get_by_path(existing)

        mime, _ = mimetypes.guess_type(relative_path)
        entry = VaultEntry(
            content_hash  = content_hash,
            vault_path    = abs_path,
            relative_path = relative_path,
            original_name = Path(relative_path).name,
            mime_type     = mime or "application/octet-stream",
            size_bytes    = len(data),
        )

        index = self._load_index()
        if relative_path not in index:
            index[relative_path] = {
                "content_hash": content_hash,
                "signal_uuid": None,
            }
            self._save_index(index)
            log.info("vault_registered", path=relative_path)

        return entry

    @staticmethod
    def mime_category(mime: str) -> str:
        """Category from MIME type."""
        return _mime_category(mime)

    # ── Internal ──────────────────────────────────────────

    @property
    def _index_path(self) -> Path:
        return self._root / ".index.json"

    def _load_index(self) -> dict[str, dict]:
        """Load index: {relative_path: {content_hash, signal_uuid}}."""
        if self._index_path.exists():
            try:
                data = json.loads(self._index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
            # Detect old format {hash: {category, original_name}}
            if data:
                sample = next(iter(data.values()), None)
                if isinstance(sample, dict) and "content_hash" not in sample:
                    log.warning("vault_index_old_format", hint="re-ingest files")
                    return {}
            return data
        return {}

    def _save_index(self, index: dict) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text(
            json.dumps(index, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ── Module helpers ────────────────────────────────────────


_EXT_MIME: dict[str, str] = {
    ".md":       "text/markdown",
    ".markdown": "text/markdown",
    ".rst":      "text/x-rst",
    ".org":      "text/x-org",
    ".tex":      "text/x-tex",
    ".rtf":      "text/rtf",
}


_CORTEX = "CORTEX"


def _mime_category(mime: str) -> str:
    """Category from MIME type (nested under cortex/)."""
    if mime.startswith("text/") or mime == "application/pdf":
        return f"{_CORTEX}/documents"
    if mime.startswith("image/"):
        return f"{_CORTEX}/images"
    if mime.startswith("audio/"):
        return f"{_CORTEX}/audio"
    if mime.startswith("video/"):
        return f"{_CORTEX}/video"
    if mime in ("application/json", "text/csv"):
        return f"{_CORTEX}/data"
    return f"{_CORTEX}/_other"


def _sanitize_category(cat: str) -> str:
    """Safe directory name from category string (allows / for nesting)."""
    safe = cat.strip().lower().replace(" ", "_")
    return "".join(c for c in safe if c.isalnum() or c in "_-/") or "_other"


def _unique_path(directory: Path, name: str) -> Path:
    """Return unique file path, adding _2, _3 suffix on collision."""
    dest = directory / name
    if not dest.exists():
        return dest
    stem   = Path(name).stem
    suffix = Path(name).suffix
    i = 2
    while True:
        dest = directory / f"{stem}_{i}{suffix}"
        if not dest.exists():
            return dest
        i += 1


def _extract_json(raw: str) -> str:
    """Recursively extract string values from JSON."""
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
