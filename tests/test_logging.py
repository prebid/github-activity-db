"""Tests for logging configuration."""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from loguru import logger

from github_activity_db.logging import (
    LogContext,
    bind_pr,
    bind_repo,
    get_logger,
    is_configured,
    reset_logging,
    setup_logging,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from loguru import Record


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Generator[None, None, None]:
    """Reset loguru state before and after each test."""
    reset_logging()
    yield
    reset_logging()


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def emitted(capsys: pytest.CaptureFixture[str]) -> list[tuple[str, str, str]]:
    """Log a DEBUG, an INFO, and a WARNING message from a named logger: the level, name, and
    message of each line the console handler wrote."""
    named = logger.bind(name="test")
    named.debug("debug message")
    named.info("info message")
    named.warning("warning message")
    logger.complete()
    parsed: list[tuple[str, str, str]] = []
    # time | level | name - message
    for line in _ANSI_ESCAPE.sub("", capsys.readouterr().err).splitlines():
        _, level, rest = line.split(" | ", 2)
        name, message = rest.split(" - ", 1)
        parsed.append((level.strip(), name, message))
    return parsed


@pytest.fixture
def records() -> Iterator[list[Record]]:
    """Every record logged during the test, as loguru hands it to a sink."""
    collected: list[Record] = []
    handler_id = logger.add(lambda message: collected.append(message.record), level="DEBUG")
    yield collected
    logger.remove(handler_id)


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_setup_logging_default_level(self) -> None:
        """Test default INFO level setup."""
        setup_logging(level="INFO")
        assert is_configured()

    def test_setup_logging_verbose_overrides_level(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test that verbose flag sets DEBUG level."""
        setup_logging(level="WARNING", verbose=True)
        assert emitted(capsys) == [
            ("DEBUG", "test", "debug message"),
            ("INFO", "test", "info message"),
            ("WARNING", "test", "warning message"),
        ]

    def test_setup_logging_quiet_overrides_level(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test that quiet flag sets WARNING level."""
        setup_logging(level="DEBUG", quiet=True)
        assert emitted(capsys) == [("WARNING", "test", "warning message")]

    def test_setup_logging_verbose_takes_precedence(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test verbose takes precedence over quiet when both set."""
        setup_logging(level="INFO", verbose=True, quiet=True)
        assert [level for level, _, _ in emitted(capsys)] == ["DEBUG", "INFO", "WARNING"]

    def test_setup_logging_with_file(self, tmp_path: Path) -> None:
        """Test file logging setup."""
        log_file = tmp_path / "test.log"
        setup_logging(level="INFO", log_file=log_file)

        logger.bind(name="test").info("Test file message")
        logger.complete()

        # time | level | name:function:line | extra | message
        fields = [line.split(" | ") for line in log_file.read_text().splitlines()]
        assert [
            (level.strip(), where.split(":")[:2], ast.literal_eval(extra), message)
            for _, level, where, extra, message in fields
        ] == [
            (
                "INFO",
                ["test", "test_setup_logging_with_file"],
                {"name": "test"},
                "Test file message",
            )
        ]

    @pytest.mark.parametrize("bound", [True, False], ids=["named logger", "stdlib or unnamed"])
    def test_a_logged_traceback_shows_no_local_values(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], bound: bool
    ) -> None:
        """No handler prints a frame's local variables (loguru ``diagnose``; gap 75)."""
        log_file = tmp_path / "app.log"
        setup_logging(level="INFO", log_file=log_file)
        target = get_logger("test") if bound else logger
        credential = "sentinel-credential-9c1d"
        try:
            raise RuntimeError(f"failed with a {len(credential)}-character value")
        except RuntimeError:
            target.exception("operation failed")
        logger.complete()
        stderr = capsys.readouterr().err
        written = log_file.read_text() if log_file.exists() else ""
        assert "failed with a 24-character value" in stderr
        if bound:  # the file handler takes only named loggers
            assert "failed with a 24-character value" in written
        assert credential not in stderr
        assert credential not in written

    def test_setup_logging_sets_configured_flag(self) -> None:
        """Test that setup_logging sets the configured flag."""
        assert not is_configured()
        setup_logging(level="INFO")
        assert is_configured()


class TestInterceptHandler:
    """Tests for stdlib logging interception."""

    def test_intercept_stdlib_logging(self) -> None:
        """Test that stdlib logging is routed to loguru at its level."""
        setup_logging(level="DEBUG")
        records: list[Record] = []
        handler_id = logger.add(lambda message: records.append(message.record))
        try:
            stdlib_logger = logging.getLogger("test_stdlib_intercept")
            stdlib_logger.warning("Hello from stdlib")
        finally:
            logger.remove(handler_id)
        assert [(record["level"].name, record["message"]) for record in records] == [
            ("WARNING", "Hello from stdlib")
        ]

    def test_sqlalchemy_logging_controlled(self) -> None:
        """Test SQLAlchemy logger level is controlled."""
        setup_logging(level="INFO")

        sa_logger = logging.getLogger("sqlalchemy.engine")
        # At INFO level, SA engine should be WARNING or higher
        assert sa_logger.level >= logging.WARNING


class TestGetLogger:
    """Tests for get_logger function."""

    def test_get_logger_returns_logger(self) -> None:
        """Test that get_logger returns a logger instance."""
        test_logger = get_logger("my_module")
        # Should be able to call logging methods
        assert hasattr(test_logger, "info")
        assert hasattr(test_logger, "debug")
        assert hasattr(test_logger, "error")

    def test_get_logger_binds_name(self, records: list[Record]) -> None:
        """Test that get_logger binds the module name."""
        test_logger = get_logger("my_test_module")
        test_logger.info("Test message")
        assert [(record["extra"], record["message"]) for record in records] == [
            ({"name": "my_test_module"}, "Test message")
        ]


class TestContextBinding:
    """Tests for context binding helpers."""

    def test_bind_repo(self, records: list[Record]) -> None:
        """Test bind_repo adds repo context."""
        repo_logger = bind_repo("prebid", "prebid-server")
        repo_logger.info("Test repo message")
        assert [record["extra"] for record in records] == [
            {"name": "sync", "repo": "prebid/prebid-server"}
        ]

    def test_bind_pr(self, records: list[Record]) -> None:
        """Test bind_pr adds repo and PR context."""
        pr_logger = bind_pr("prebid", "prebid-server", 123)
        pr_logger.info("Test PR message")
        assert [record["extra"] for record in records] == [
            {"name": "sync", "repo": "prebid/prebid-server", "pr": 123}
        ]

    def test_log_context_manager(self, records: list[Record]) -> None:
        """Test LogContext context manager: the context is bound inside it and gone after."""
        with LogContext(custom_key="custom_value"):
            logger.info("Inside context")
        logger.info("After context")
        assert [(record["message"], record["extra"]) for record in records] == [
            ("Inside context", {"custom_key": "custom_value"}),
            ("After context", {}),
        ]


class TestLogLevels:
    """Tests for log level handling."""

    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
    def test_log_level_accepted(self, level: str) -> None:
        """Test that various log levels are accepted."""
        # Should not raise
        setup_logging(level=level)  # type: ignore[arg-type]
        assert is_configured()


class TestResetLogging:
    """Tests for reset_logging function."""

    def test_reset_logging_clears_configured(self) -> None:
        """Test that reset_logging clears the configured flag."""
        setup_logging(level="INFO")
        assert is_configured()

        reset_logging()
        assert not is_configured()
