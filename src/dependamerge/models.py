# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from typing import List, Optional

from pydantic import BaseModel


class FileChange(BaseModel):
    """Represents a file change in a pull request."""

    filename: str
    additions: int
    deletions: int
    changes: int
    status: str  # added, modified, removed, renamed


class PullRequestInfo(BaseModel):
    """Represents pull request information."""

    number: int
    title: str
    body: Optional[str]
    author: str
    head_sha: str
    base_branch: str
    head_branch: str
    state: str
    mergeable: Optional[bool]
    mergeable_state: Optional[str]  # Additional state information from GitHub
    behind_by: Optional[int]  # Number of commits behind the base branch
    files_changed: List[FileChange]
    repository_full_name: str
    html_url: str

    # Optional fields used by the interactive fix workflow
    # These enable cloning the correct repositories and pushing fixes.
    head_repo_full_name: Optional[str] = None
    head_repo_clone_url: Optional[str] = None
    base_repo_full_name: Optional[str] = None
    base_repo_clone_url: Optional[str] = None
    is_fork: Optional[bool] = None
    maintainer_can_modify: Optional[bool] = None


class ComparisonResult(BaseModel):
    """Result of comparing two pull requests."""

    is_similar: bool
    confidence_score: float
    reasons: List[str]


class UnmergeableReason(BaseModel):
    """Represents a reason why a PR cannot be merged."""

    type: str  # e.g., "merge_conflict", "failing_checks", "blocked_review"
    description: str
    details: Optional[str] = None


class CopilotComment(BaseModel):
    """Represents an unresolved Copilot feedback comment."""

    id: int
    body: str
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    created_at: str
    state: str  # "open", "resolved", etc.


class UnmergeablePR(BaseModel):
    """Represents a pull request that cannot be merged."""

    repository: str
    pr_number: int
    title: str
    author: str
    url: str
    reasons: List[UnmergeableReason]
    copilot_comments_count: int = 0
    copilot_comments: List[CopilotComment] = []
    created_at: str
    updated_at: str


class OrganizationScanResult(BaseModel):
    """Result of scanning an organization for unmergeable PRs."""

    organization: str
    total_repositories: int
    scanned_repositories: int
    total_prs: int
    unmergeable_prs: List[UnmergeablePR]
    scan_timestamp: str
    errors: List[str] = []
