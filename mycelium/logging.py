"""Structured logging: file (JSON) + console."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog


def setup_logging(
    level:        str         = "INFO",
    fmt:          str         = "auto",
    log_dir:      Path | None = None,
    max_bytes:    int         = 10_485_760,
    backup_count: int         = 3,
    *,
    console:      bool        = True,
) -> None:
    """Configure structlog → file (JSON lines) + optional console.

    Args:
        level:        DEBUG | INFO | WARNING | ERROR.
        fmt:          auto (console if DEBUG else JSON) | json | console.
        log_dir:      Dir for mycelium.log. None = no file output.
        max_bytes:    Max file size before rotation.
        backup_count: Rotated backup count.
        console:      Enable console output. False = file-only (for CLI with progress).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = []

    # ── Console handler (optional) ─────────────────────────────
    if console:
        if fmt == "json":
            console_renderer: structlog.types.Processor = (
                structlog.processors.JSONRenderer()
            )
        elif fmt == "console" or (fmt == "auto" and level.upper() == "DEBUG"):
            console_renderer = structlog.dev.ConsoleRenderer()
        else:
            console_renderer = structlog.processors.JSONRenderer()

        console_h = logging.StreamHandler()
        console_h.setFormatter(structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                console_renderer,
            ],
        ))
        handlers.append(console_h)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_dir / "mycelium.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
        )
        fh.setFormatter(structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        ))
        handlers.append(fh)

    # ── Root logger ──────────────────────────────────────────
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(log_level)
    for h in handlers:
        root.addHandler(h)

    # ── structlog → stdlib integration ───────────────────────
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
