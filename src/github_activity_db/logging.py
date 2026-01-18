"""Centralized logging configuration using loguru.

Provides:
- Configurable log levels from Settings
- CLI flag override (--verbose/--quiet)
- Standard library interception (SQLAlchemy, httpx)
- Structured context binding for repo/PR tracking
- Optional file rotation logging
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

if TYPE_CHECKING:
    from loguru import Logger

# Type alias for log levels
LogLevel = Literal["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Module-level flag to track if logging has been configured
_configured = False


class InterceptHandler(logging.Handler):
    """Handler to intercept standard library logging and route to loguru.

    This enables control over SQLAlchemy, httpx, and other library logs.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Route stdlib log record to loguru."""
        from types import FrameType

        # Get corresponding loguru level
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = str(record.levelno)

        # Find caller from where originated the logged message
        frame: FrameType | None = logging.currentframe()
        depth = 2
        while frame is not None:
            if frame.f_code.co_filename != logging.__file__:
                break
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(
    level: LogLevel = "INFO",
    *,
    verbose: bool = False,
    quiet: bool = False,
    log_file: Path | None = None,
    rotation: str = "10 MB",
    retention: str = "7 days",
    serialize: bool = False,
) -> Logger:
    """Configure logging for the application.

    Args:
        level: Base log level from config
        verbose: If True, use DEBUG level (overrides level)
        quiet: If True, use WARNING level (overrides level)
        log_file: Optional path for file logging with rotation
        rotation: When to rotate log file (e.g., "10 MB", "1 day")
        retention: How long to keep rotated logs
        serialize: If True, output JSON format (useful for file logs)

    Returns:
        Configured logger instance

    Note:
        verbose takes precedence over quiet if both are True.
    """
    global _configured

    # Determine effective level
    effective_level: LogLevel
    if verbose:
        effective_level = "DEBUG"
    elif quiet:
        effective_level = "WARNING"
    else:
        effective_level = level

    # Clear any existing handlers
    logger.remove()

    # Console handler with formatting
    logger.add(
        sys.stderr,
        level=effective_level,
        format=(
            "<dim>{time:HH:mm:ss}</dim> | "
            "<level>{level: <8}</level> | "
            "<cyan>{extra[name]}</cyan> - "
            "<level>{message}</level>"
        ),
        colorize=True,
        backtrace=True,
        diagnose=True,
        filter=lambda record: "name" in record["extra"],
    )

    # Fallback handler for logs without 'name' extra (e.g., from intercepted stdlib)
    logger.add(
        sys.stderr,
        level=effective_level,
        format=(
            "<dim>{time:HH:mm:ss}</dim> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan> - "
            "<level>{message}</level>"
        ),
        colorize=True,
        backtrace=True,
        diagnose=True,
        filter=lambda record: "name" not in record["extra"],
    )

    # Optional file handler with rotation
    if log_file:
        logger.add(
            log_file,
            level="DEBUG",  # Always capture everything to file
            format=(
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
                "{level: <8} | "
                "{extra[name]}:{function}:{line} | "
                "{extra} | "
                "{message}"
            ),
            rotation=rotation,
            retention=retention,
            compression="gz",
            serialize=serialize,
            filter=lambda record: "name" in record["extra"],
        )

    # Intercept standard library logging
    _intercept_stdlib_logging(effective_level)

    _configured = True
    return logger


def _intercept_stdlib_logging(level: LogLevel) -> None:
    """Intercept standard library loggers and route to loguru.

    This captures logs from:
    - SQLAlchemy (sqlalchemy.engine)
    - httpx (used by githubkit)
    - Other libraries using stdlib logging
    """
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    # Set specific library levels based on our level
    if level in ("TRACE", "DEBUG"):
        # Show SQLAlchemy SQL at debug level
        logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)
    else:
        # Suppress SQLAlchemy at INFO and above
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    # httpx: generally quiet unless DEBUG
    httpx_level = logging.DEBUG if level == "DEBUG" else logging.WARNING
    logging.getLogger("httpx").setLevel(httpx_level)
    logging.getLogger("httpcore").setLevel(httpx_level)


def get_logger(name: str) -> Logger:
    """Get a logger with the given name bound as context.

    Usage:
        from github_activity_db.logging import get_logger
        logger = get_logger(__name__)

        # With additional context binding
        logger = logger.bind(repo="prebid/prebid-server", pr=123)
        logger.info("Processing PR")  # Logs with repo and pr context

    Args:
        name: Logger name (typically __name__)

    Returns:
        Logger instance with name bound
    """
    return logger.bind(name=name)


def bind_repo(owner: str, repo: str) -> Logger:
    """Bind repository context to logger.

    Args:
        owner: Repository owner
        repo: Repository name

    Returns:
        Logger with repo context bound
    """
    return logger.bind(name="sync", repo=f"{owner}/{repo}")


def bind_pr(owner: str, repo: str, pr_number: int) -> Logger:
    """Bind PR context to logger.

    Args:
        owner: Repository owner
        repo: Repository name
        pr_number: PR number

    Returns:
        Logger with repo and PR context bound
    """
    return logger.bind(name="sync", repo=f"{owner}/{repo}", pr=pr_number)


class LogContext:
    """Context manager for temporary log context binding.

    Usage:
        with LogContext(repo="owner/repo", pr=123):
            logger.info("Processing")  # Has repo and pr context
        logger.info("After")  # No longer has context
    """

    def __init__(self, **context: Any) -> None:
        """Initialize with context to bind."""
        self._context = context
        self._token: Any = None

    def __enter__(self) -> Logger:
        """Enter context and bind values."""
        self._token = logger.contextualize(**self._context)
        self._token.__enter__()
        return logger

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Exit context and unbind values."""
        if self._token:
            self._token.__exit__(exc_type, exc_val, exc_tb)


def is_configured() -> bool:
    """Check if logging has been configured."""
    return _configured


def reset_logging() -> None:
    """Reset logging state (primarily for testing)."""
    global _configured
    logger.remove()
    _configured = False
