# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for output_utils module."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from rich.console import Console

from dependamerge.output_utils import log_and_print


class TestLogAndPrint:
    """Tests for log_and_print function."""

    def test_log_and_print_info_level(self):
        """Test logging and printing at INFO level."""
        logger = logging.getLogger("test_logger")
        console = MagicMock(spec=Console)

        with patch("builtins.print") as mock_print:
            log_and_print(logger, console, "Test message", level="info")

        # Verify print was called (since style is None)
        mock_print.assert_called_once_with("Test message")
        # Verify console.print was NOT called when style is None
        console.print.assert_not_called()

    def test_log_and_print_with_style(self):
        """Test logging and printing with Rich style."""
        logger = logging.getLogger("test_logger")
        console = MagicMock(spec=Console)

        with patch("builtins.print") as mock_print:
            log_and_print(
                logger, console, "Styled message", style="bold red", level="info"
            )

        # Verify console.print was called with style
        console.print.assert_called_once_with("Styled message", style="bold red")
        # Verify builtin print was NOT called when style is provided
        mock_print.assert_not_called()

    def test_log_and_print_debug_level(self):
        """Test logging at DEBUG level."""
        logger = logging.getLogger("test_logger_debug")
        logger.setLevel(logging.DEBUG)
        console = MagicMock(spec=Console)

        # Capture log output
        with patch("builtins.print"):
            with patch.object(logger, "debug") as mock_debug:
                log_and_print(logger, console, "Debug message", level="debug")

        mock_debug.assert_called_once_with("Debug message")

    def test_log_and_print_warning_level(self):
        """Test logging at WARNING level."""
        logger = logging.getLogger("test_logger_warning")
        console = MagicMock(spec=Console)

        with patch("builtins.print"):
            with patch.object(logger, "warning") as mock_warning:
                log_and_print(logger, console, "Warning message", level="warning")

        mock_warning.assert_called_once_with("Warning message")

    def test_log_and_print_error_level(self):
        """Test logging at ERROR level."""
        logger = logging.getLogger("test_logger_error")
        console = MagicMock(spec=Console)

        with patch("builtins.print"):
            with patch.object(logger, "error") as mock_error:
                log_and_print(logger, console, "Error message", level="error")

        mock_error.assert_called_once_with("Error message")

    def test_log_and_print_invalid_level_defaults_to_info(self):
        """Test that invalid log level defaults to INFO."""
        logger = logging.getLogger("test_logger_invalid")
        console = MagicMock(spec=Console)

        with patch("builtins.print"):
            with patch.object(logger, "info") as mock_info:
                # Use an invalid level - should fallback to info
                log_and_print(logger, console, "Message", level="invalid")

        mock_info.assert_called_once_with("Message")

    def test_log_and_print_message_with_emoji(self):
        """Test handling of messages with emoji characters."""
        logger = logging.getLogger("test_logger_emoji")
        console = MagicMock(spec=Console)

        with patch("builtins.print") as mock_print:
            log_and_print(logger, console, "✅ Success message", level="info")

        mock_print.assert_called_once_with("✅ Success message")

    def test_log_and_print_message_with_url(self):
        """Test handling of messages with URLs."""
        logger = logging.getLogger("test_logger_url")
        console = MagicMock(spec=Console)
        message = "✅ Merged: https://github.com/owner/repo/pull/123"

        with patch("builtins.print") as mock_print:
            log_and_print(logger, console, message, level="info")

        mock_print.assert_called_once_with(message)

    def test_log_and_print_multiline_message(self):
        """Test handling of multiline messages."""
        logger = logging.getLogger("test_logger_multiline")
        console = MagicMock(spec=Console)
        message = "Line 1\nLine 2\nLine 3"

        with patch("builtins.print") as mock_print:
            log_and_print(logger, console, message, level="info")

        mock_print.assert_called_once_with(message)

    def test_log_and_print_empty_message(self):
        """Test handling of empty message."""
        logger = logging.getLogger("test_logger_empty")
        console = MagicMock(spec=Console)

        with patch("builtins.print") as mock_print:
            log_and_print(logger, console, "", level="info")

        mock_print.assert_called_once_with("")

    def test_log_and_print_default_level(self):
        """Test that default log level is INFO when not specified."""
        logger = logging.getLogger("test_logger_default")
        console = MagicMock(spec=Console)

        with patch("builtins.print"):
            with patch.object(logger, "info") as mock_info:
                # Don't specify level - should default to info
                log_and_print(logger, console, "Default level message")

        mock_info.assert_called_once_with("Default level message")

    def test_log_and_print_none_style(self):
        """Test that explicitly passing None for style uses print."""
        logger = logging.getLogger("test_logger_none_style")
        console = MagicMock(spec=Console)

        with patch("builtins.print") as mock_print:
            log_and_print(logger, console, "Message", style=None, level="info")

        mock_print.assert_called_once_with("Message")
        console.print.assert_not_called()
