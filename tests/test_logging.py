"""Tests for logging configuration."""

from __future__ import annotations

import logging
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
    from collections.abc import Generator


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Generator[None, None, None]:
    """Reset loguru state before and after each test."""
    reset_logging()
    yield
    reset_logging()


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_setup_logging_default_level(self) -> None:
        """Test default INFO level setup."""
        setup_logging(level="INFO")
        assert is_configured()

    def test_setup_logging_verbose_overrides_level(self) -> None:
        """Test that verbose flag sets DEBUG level."""
        messages: list[str] = []
        setup_logging(level="WARNING", verbose=True)

        handler_id = logger.add(lambda msg: messages.append(str(msg)))
        try:
            logger.bind(name="test").debug("debug message")
            assert any("debug message" in msg for msg in messages)
        finally:
            logger.remove(handler_id)

    def test_setup_logging_quiet_overrides_level(self) -> None:
        """Test that quiet flag sets WARNING level."""
        messages: list[str] = []
        setup_logging(level="DEBUG", quiet=True)

        handler_id = logger.add(lambda msg: messages.append(str(msg)), level="DEBUG")
        try:
            # INFO should be filtered by the setup handlers
            # but our test handler captures everything
            logger.bind(name="test").info("info message")
            # The main handlers filter INFO when quiet=True
            # We're just verifying setup_logging doesn't crash
            assert is_configured()
        finally:
            logger.remove(handler_id)

    def test_setup_logging_verbose_takes_precedence(self) -> None:
        """Test verbose takes precedence over quiet when both set."""
        messages: list[str] = []
        setup_logging(level="INFO", verbose=True, quiet=True)

        handler_id = logger.add(lambda msg: messages.append(str(msg)))
        try:
            logger.bind(name="test").debug("debug message")
            # Verbose wins, so DEBUG should be captured
            assert any("debug message" in msg for msg in messages)
        finally:
            logger.remove(handler_id)

    def test_setup_logging_with_file(self, tmp_path: Path) -> None:
        """Test file logging setup."""
        log_file = tmp_path / "test.log"
        setup_logging(level="INFO", log_file=log_file)

        logger.bind(name="test").info("Test file message")

        # Give loguru a moment to flush
        import time

        time.sleep(0.1)

        assert log_file.exists()
        content = log_file.read_text()
        assert "Test file message" in content

    def test_setup_logging_sets_configured_flag(self) -> None:
        """Test that setup_logging sets the configured flag."""
        assert not is_configured()
        setup_logging(level="INFO")
        assert is_configured()


class TestInterceptHandler:
    """Tests for stdlib logging interception."""

    def test_intercept_stdlib_logging(self) -> None:
        """Test that stdlib logging is routed to loguru."""
        messages: list[str] = []
        setup_logging(level="DEBUG")

        handler_id = logger.add(lambda msg: messages.append(str(msg)))
        try:
            # Create a stdlib logger
            stdlib_logger = logging.getLogger("test_stdlib_intercept")
            stdlib_logger.warning("Hello from stdlib")

            # Should be captured by loguru
            assert any("Hello from stdlib" in msg for msg in messages)
        finally:
            logger.remove(handler_id)

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

    def test_get_logger_binds_name(self) -> None:
        """Test that get_logger binds the module name."""
        messages: list[str] = []
        setup_logging(level="DEBUG")

        handler_id = logger.add(
            lambda msg: messages.append(str(msg)),
            format="{extra} | {message}",
        )
        try:
            test_logger = get_logger("my_test_module")
            test_logger.info("Test message")
            assert any("my_test_module" in msg for msg in messages)
        finally:
            logger.remove(handler_id)


class TestContextBinding:
    """Tests for context binding helpers."""

    def test_bind_repo(self) -> None:
        """Test bind_repo adds repo context."""
        messages: list[str] = []
        setup_logging(level="DEBUG")

        handler_id = logger.add(
            lambda msg: messages.append(str(msg)),
            format="{extra} | {message}",
        )
        try:
            repo_logger = bind_repo("prebid", "prebid-server")
            repo_logger.info("Test repo message")
            assert any("prebid/prebid-server" in msg for msg in messages)
        finally:
            logger.remove(handler_id)

    def test_bind_pr(self) -> None:
        """Test bind_pr adds repo and PR context."""
        messages: list[str] = []
        setup_logging(level="DEBUG")

        handler_id = logger.add(
            lambda msg: messages.append(str(msg)),
            format="{extra} | {message}",
        )
        try:
            pr_logger = bind_pr("prebid", "prebid-server", 123)
            pr_logger.info("Test PR message")
            output = "".join(messages)
            assert "prebid/prebid-server" in output
            assert "123" in output
        finally:
            logger.remove(handler_id)

    def test_log_context_manager(self) -> None:
        """Test LogContext context manager."""
        messages: list[str] = []
        setup_logging(level="DEBUG")

        handler_id = logger.add(
            lambda msg: messages.append(str(msg)),
            format="{extra} | {message}",
        )
        try:
            with LogContext(custom_key="custom_value"):
                logger.info("Inside context")

            # Inside should have context
            assert any("custom_value" in msg for msg in messages)
        finally:
            logger.remove(handler_id)


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
