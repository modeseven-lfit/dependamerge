# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Dependamerge - Automatically merge automation PRs across GitHub organizations."""

from ._version import __version__
from .system_utils import get_default_workers, get_performance_core_count

__all__ = ["__version__", "get_default_workers", "get_performance_core_count"]
