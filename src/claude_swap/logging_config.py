"""Logging configuration for Claude Swap."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from claude_swap.paths import get_backup_root, validate_backup_layout, validate_engine_path


class _LazyDirRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that creates its parent dir on first emit.

    Keeps the backup root from being materialized just because the switcher
    was instantiated. Necessary so a no-op run (e.g. ``ccs accounts --status`` with
    no managed accounts) doesn't lay down ``cache/`` or log files inside the
    XDG path, which would later trip the legacy → XDG migration collision
    check if a legacy directory appeared between runs.
    """

    def _open(self):  # type: ignore[override]
        log_path = Path(self.baseFilename)
        if log_path.absolute().is_relative_to(get_backup_root().absolute()):
            from claude_swap.admission import AdmissionClosed, EngineAdmissionGate

            try:
                with EngineAdmissionGate(get_backup_root()).operation():
                    validate_backup_layout(get_backup_root(), create=True)
                    validate_engine_path(log_path)
                    Path(self.baseFilename).parent.mkdir(
                        parents=True, exist_ok=True
                    )
                    return super()._open()
            except AdmissionClosed:
                # Logging after purge must not materialize a new account
                # engine just to report that it is empty/closed.
                return open(os.devnull, "a", encoding="utf-8")
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        return super()._open()


def setup_logging(log_dir: Path, debug: bool = False) -> logging.Logger:
    """Setup logging with file and optional console output.

    The log directory is *not* created eagerly; it materializes on the first
    log record actually written, via ``_LazyDirRotatingFileHandler``.

    Args:
        log_dir: Directory to store log files.
        debug: Enable debug logging to console.

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger("claude-swap")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    # Clear any existing handlers
    logger.handlers.clear()

    # File handler - opens lazily so the dir is only created when something
    # is actually logged.
    log_file = log_dir / "claude-swap.log"
    file_handler = _LazyDirRotatingFileHandler(
        log_file,
        maxBytes=1024 * 1024,  # 1MB
        backupCount=3,
        delay=True,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(file_handler)

    # Console handler for debug mode
    if debug:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(console_handler)

    return logger
