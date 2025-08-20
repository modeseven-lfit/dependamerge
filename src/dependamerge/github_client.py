# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

import os
from datetime import datetime
from typing import Dict, List, Optional, TYPE_CHECKING

import requests
import urllib3.exceptions
from github import Github, GithubException
from github.CommitStatus import CommitStatus
from github.PullRequest import PullRequest
from github.Repository import Repository

from .models import (
    CopilotComment,
    FileChange,
    OrganizationScanResult,
    PullRequestInfo,
    UnmergeablePR,
    UnmergeableReason,
)

if TYPE_CHECKING:
    from .progress_tracker import ProgressTracker


class GitHubClient:
    """GitHub API client for managing pull requests."""

    def __init__(self, token: Optional[str] = None):
        """Initialize GitHub client with token."""
        self.token = token or os.getenv("GITHUB_TOKEN")
        if not self.token:
            raise ValueError(
                "GitHub token is required. Set GITHUB_TOKEN environment variable."
            )
        self.github = Github(self.token)

    def parse_pr_url(self, url: str) -> tuple[str, str, int]:
        """Parse GitHub PR URL to extract owner, repo, and PR number."""
        # Expected format: https://github.com/owner/repo/pull/123[/files|/commits|etc]
        parts = url.rstrip("/").split("/")
        if len(parts) < 7 or "github.com" not in url or "pull" not in parts:
            raise ValueError(f"Invalid GitHub PR URL: {url}")

        # Find the 'pull' segment and get the PR number from the next segment
        try:
            pull_index = parts.index("pull")
            if pull_index + 1 >= len(parts):
                raise ValueError("PR number not found after 'pull'")

            owner = parts[pull_index - 2]
            repo = parts[pull_index - 1]
            pr_number = int(parts[pull_index + 1])

            return owner, repo, pr_number
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid GitHub PR URL: {url}") from e

    def get_pull_request_info(
        self, owner: str, repo: str, pr_number: int
    ) -> PullRequestInfo:
        """Get detailed information about a pull request."""
        try:
            repository = self.github.get_repo(f"{owner}/{repo}")
            pr = repository.get_pull(pr_number)

            # Get file changes
            files_changed = []
            for file in pr.get_files():
                files_changed.append(
                    FileChange(
                        filename=file.filename,
                        additions=file.additions,
                        deletions=file.deletions,
                        changes=file.changes,
                        status=file.status,
                    )
                )

            return PullRequestInfo(
                number=pr.number,
                title=pr.title,
                body=pr.body,
                author=pr.user.login,
                head_sha=pr.head.sha,
                base_branch=pr.base.ref,
                head_branch=pr.head.ref,
                state=pr.state,
                mergeable=pr.mergeable,
                mergeable_state=pr.mergeable_state,
                behind_by=getattr(pr, "behind_by", None),
                files_changed=files_changed,
                repository_full_name=repository.full_name,
                html_url=pr.html_url,
            )
        except (
            urllib3.exceptions.NameResolutionError,
            urllib3.exceptions.MaxRetryError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.RequestException,
        ) as e:
            raise RuntimeError(f"Network error while fetching PR info: {e}") from e
        except GithubException as e:
            raise RuntimeError(f"Failed to fetch PR info: {e}") from e

    def get_pull_request_commits(
        self, owner: str, repo: str, pr_number: int
    ) -> List[str]:
        """Get commit messages from a pull request."""
        try:
            repository = self.github.get_repo(f"{owner}/{repo}")
            pr = repository.get_pull(pr_number)

            commits = pr.get_commits()
            commit_messages = []

            for commit in commits:
                if commit.commit.message:
                    commit_messages.append(commit.commit.message)

            return commit_messages
        except (
            urllib3.exceptions.NameResolutionError,
            urllib3.exceptions.MaxRetryError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.RequestException,
        ) as e:
            raise RuntimeError(f"Network error while fetching PR commits: {e}") from e
        except GithubException as e:
            raise RuntimeError(f"Failed to fetch PR commits: {e}") from e

    def get_organization_repositories(self, org_name: str) -> List[Repository]:
        """Get all repositories in an organization."""
        try:
            org = self.github.get_organization(org_name)
            return list(org.get_repos())
        except (
            urllib3.exceptions.NameResolutionError,
            urllib3.exceptions.MaxRetryError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.RequestException,
        ) as e:
            raise RuntimeError(
                f"Network error while fetching organization repositories: {e}"
            ) from e
        except GithubException as e:
            raise RuntimeError(f"Failed to fetch organization repositories: {e}") from e

    def get_open_pull_requests(self, repository: Repository) -> List[PullRequest]:
        """Get all open pull requests for a repository."""
        try:
            return list(repository.get_pulls(state="open"))
        except (
            urllib3.exceptions.NameResolutionError,
            urllib3.exceptions.MaxRetryError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.RequestException,
        ) as e:
            raise RuntimeError(
                f"Network error while fetching PRs for {repository.full_name}: {e}"
            ) from e
        except GithubException as e:
            print(f"Warning: Failed to fetch PRs for {repository.full_name}: {e}")
            return []

    def approve_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        message: str = "Auto-approved by dependamerge",
    ) -> bool:
        """Approve a pull request."""
        try:
            repository = self.github.get_repo(f"{owner}/{repo}")
            pr = repository.get_pull(pr_number)
            pr.create_review(body=message, event="APPROVE")
            return True
        except GithubException as e:
            print(f"Failed to approve PR {pr_number}: {e}")
            return False

    def merge_pull_request(
        self, owner: str, repo: str, pr_number: int, merge_method: str = "merge"
    ) -> bool:
        """Merge a pull request with detailed error handling."""
        try:
            repository = self.github.get_repo(f"{owner}/{repo}")
            pr = repository.get_pull(pr_number)

            # Check if PR can be merged based on mergeable state and mergeable flag
            if not self._should_attempt_merge(pr):
                print(
                    f"PR {pr_number} is not mergeable (state: {pr.mergeable_state}, mergeable: {pr.mergeable})."
                )
                return False

            result = pr.merge(merge_method=merge_method)
            return bool(result.merged)
        except GithubException as e:
            print(f"Failed to merge PR {pr_number}: {e}")
            return False

    def is_automation_author(self, author: str) -> bool:
        """Check if the author is a known automation tool."""
        automation_authors = {
            "dependabot[bot]",
            "pre-commit-ci[bot]",
            "renovate[bot]",
            "github-actions[bot]",
            "allcontributors[bot]",
        }
        return author in automation_authors

    def get_pr_status_details(self, pr_info: PullRequestInfo) -> str:
        """Get detailed status information for a PR."""
        if pr_info.state != "open":
            return f"Closed ({pr_info.state})"

        # Check for draft status first
        if pr_info.mergeable_state == "draft":
            return "Draft PR"

        # Handle blocked state - need to determine why it's blocked
        if pr_info.mergeable_state == "blocked" and pr_info.mergeable is True:
            # This means technically mergeable but blocked by branch protection
            # We need to check what's blocking it to provide intelligent status
            block_reason = self._analyze_block_reason(pr_info)
            return block_reason

        if pr_info.mergeable is False:
            # Check for specific reasons why it's not mergeable
            if pr_info.mergeable_state == "dirty":
                return "Merge conflicts"
            elif pr_info.mergeable_state == "behind":
                return "Rebase required"
            elif pr_info.mergeable_state == "blocked":
                return "Blocked by checks"
            else:
                return f"Not mergeable ({pr_info.mergeable_state or 'unknown'})"

        if pr_info.mergeable_state == "behind":
            return "Rebase required"

        # If mergeable is True and mergeable_state is clean, it's ready
        if pr_info.mergeable is True and pr_info.mergeable_state == "clean":
            return "Ready to merge"

        # For any other combination where mergeable is True but state is unclear
        if pr_info.mergeable is True:
            return "Ready to merge"

        # Fallback for unclear states
        return f"Status unclear ({pr_info.mergeable_state or 'unknown'})"

    def _analyze_block_reason(self, pr_info: PullRequestInfo) -> str:
        """Analyze why a PR is blocked and return appropriate status."""
        try:
            repo_owner, repo_name = pr_info.repository_full_name.split("/")
            repository = self.github.get_repo(f"{repo_owner}/{repo_name}")
            pr = repository.get_pull(pr_info.number)

            # Check if there are any reviews
            reviews = list(pr.get_reviews())
            approved_reviews = [r for r in reviews if r.state == "APPROVED"]

            # Check commit status and check runs
            commit = repository.get_commit(pr.head.sha)

            # Get commit statuses (legacy status API) and get latest per context
            # Use pagination to avoid loading all statuses at once for repos with many checks
            latest_statuses: Dict[str, CommitStatus] = {}
            for status in commit.get_statuses():
                context = status.context
                if (
                    context not in latest_statuses
                    or status.updated_at > latest_statuses[context].updated_at
                ):
                    latest_statuses[context] = status
                # Limit to first 50 statuses to avoid performance issues
                if len(latest_statuses) >= 50:
                    break

            failing_statuses = [
                s for s in latest_statuses.values() if s.state in ["failure", "error"]
            ]
            pending_statuses = [
                s for s in latest_statuses.values() if s.state == "pending"
            ]

            # Get check runs (GitHub Actions, etc.) - limit to avoid performance issues
            check_runs = list(commit.get_check_runs()[:50])
            failing_checks = [
                c
                for c in check_runs
                if c.conclusion in ["failure", "cancelled", "timed_out"]
            ]
            pending_checks = [
                c for c in check_runs if c.status in ["queued", "in_progress"]
            ]

            # Determine the primary blocking reason - prioritize review requirements
            if failing_statuses or failing_checks:
                return "Blocked by failing checks"
            elif not approved_reviews:
                # Check if there are any real pending checks (not just stale statuses)
                if (
                    pending_checks
                ):  # Only consider pending check runs, not stale statuses
                    return "Blocked by pending checks"
                else:
                    # All checks passed but no approval - needs review
                    return "Requires approval"
            elif pending_statuses or pending_checks:
                return "Blocked by pending checks"
            else:
                # Has approvals but still blocked - might be other branch protection rules
                return "Blocked by branch protection"

        except Exception:
            # Fallback if we can't analyze the specific reason
            return "Blocked"

    def _should_attempt_merge(self, pr) -> bool:
        """
        Determine if we should attempt to merge a PR based on its mergeable state.

        Returns True if merge should be attempted, False otherwise.
        """
        # If mergeable is explicitly False, only attempt merge for blocked state
        # where branch protection might resolve after approval
        if pr.mergeable is False:
            # For blocked state, we can attempt merge as approval might resolve the block
            # For other states (dirty, behind), don't attempt as they need manual fixes
            return bool(pr.mergeable_state == "blocked")

        # If mergeable is None, GitHub is still calculating - be conservative
        if pr.mergeable is None:
            # Only attempt if state suggests it might work
            return bool(pr.mergeable_state in ["clean", "blocked"])

        # If mergeable is True, attempt merge for most states except draft
        if pr.mergeable is True:
            return bool(pr.mergeable_state != "draft")

        # Fallback to False for any unexpected cases
        return False

    def fix_out_of_date_pr(self, owner: str, repo: str, pr_number: int) -> bool:
        """Fix an out-of-date PR by updating the branch."""
        try:
            repository = self.github.get_repo(f"{owner}/{repo}")
            pr = repository.get_pull(pr_number)

            if pr.mergeable_state != "behind":
                print(f"PR {pr_number} is not behind the base branch")
                return False

            # Update the branch using GitHub's update branch API
            pr.update_branch()
            return True
        except GithubException as e:
            print(f"Failed to update PR {pr_number}: {e}")
            return False

    def scan_organization_for_unmergeable_prs(
        self, org_name: str, progress_tracker: Optional["ProgressTracker"] = None
    ) -> OrganizationScanResult:
        """Scan an entire GitHub organization for unmergeable pull requests.

        Args:
            org_name: Name of the GitHub organization to scan
            progress_tracker: Optional progress tracker for real-time updates
        """
        scan_timestamp = datetime.now().isoformat()
        errors = []
        unmergeable_prs = []
        total_prs = 0
        scanned_repositories = 0

        try:
            if progress_tracker:
                progress_tracker.update_operation("Getting organization details...")
            org = self.github.get_organization(org_name)

            if progress_tracker:
                progress_tracker.update_operation("Listing repositories...")
            repositories = list(org.get_repos())
            total_repositories = len(repositories)

            if progress_tracker:
                progress_tracker.update_total_repositories(total_repositories)

            for repo in repositories:
                try:
                    if progress_tracker:
                        progress_tracker.start_repository(repo.full_name)

                    scanned_repositories += 1
                    repo_unmergeable_prs = self._scan_repository_for_unmergeable_prs(
                        repo, progress_tracker
                    )
                    unmergeable_prs.extend(repo_unmergeable_prs)

                    # Count all open PRs in this repository
                    if progress_tracker:
                        progress_tracker.update_operation(f"Counting PRs in {repo.full_name}")
                    repo_prs = list(repo.get_pulls(state='open'))
                    total_prs += len(repo_prs)

                    if progress_tracker:
                        progress_tracker.complete_repository(len(repo_unmergeable_prs))

                except Exception as e:
                    if progress_tracker:
                        progress_tracker.add_error()
                    errors.append(f"Error scanning repository {repo.full_name}: {str(e)}")
                    continue

        except Exception as e:
            errors.append(f"Error accessing organization {org_name}: {str(e)}")
            total_repositories = 0

        return OrganizationScanResult(
            organization=org_name,
            total_repositories=total_repositories,
            scanned_repositories=scanned_repositories,
            total_prs=total_prs,
            unmergeable_prs=unmergeable_prs,
            scan_timestamp=scan_timestamp,
            errors=errors
        )

    def _scan_repository_for_unmergeable_prs(
        self, repo: Repository, progress_tracker: Optional["ProgressTracker"] = None
    ) -> List[UnmergeablePR]:
        """Scan a single repository for unmergeable pull requests.

        Args:
            repo: GitHub repository to scan
            progress_tracker: Optional progress tracker for real-time updates
        """
        unmergeable_prs = []

        try:
            # Get all open pull requests
            prs = repo.get_pulls(state='open')

            for pr in prs:
                try:
                    if progress_tracker:
                        progress_tracker.analyze_pr(pr.number, repo.full_name)

                    # Skip PRs with incomplete data
                    if not pr.user or not pr.title or not pr.html_url:
                        if progress_tracker:
                            progress_tracker.add_error()
                            progress_tracker.update_operation(f"Skipping incomplete PR #{pr.number} in {repo.full_name}")
                        continue

                    # Check if PR is unmergeable
                    unmergeable_reasons = self._analyze_pr_mergeability(pr)

                    if unmergeable_reasons:
                        if progress_tracker:
                            progress_tracker.update_operation(
                                f"Getting Copilot comments for PR #{pr.number}"
                            )
                        # Get Copilot comments count
                        copilot_comments = self._get_copilot_comments(pr)

                        # Safe date handling
                        created_at = pr.created_at.isoformat() if pr.created_at else datetime.now().isoformat()
                        updated_at = pr.updated_at.isoformat() if pr.updated_at else datetime.now().isoformat()

                        unmergeable_pr = UnmergeablePR(
                            repository=repo.full_name,
                            pr_number=pr.number,
                            title=pr.title,
                            author=pr.user.login,
                            url=pr.html_url,
                            reasons=unmergeable_reasons,
                            copilot_comments_count=len(copilot_comments),
                            copilot_comments=copilot_comments,
                            created_at=created_at,
                            updated_at=updated_at
                        )

                        unmergeable_prs.append(unmergeable_pr)

                except Exception:
                    if progress_tracker:
                        progress_tracker.add_error()
                        progress_tracker.update_operation(f"Error analyzing PR #{pr.number} in {repo.full_name} - continuing...")
                    # Silently handle errors to avoid spamming the console
                    continue

        except Exception:
            # Silently handle repository-level errors
            if progress_tracker:
                progress_tracker.add_error()
                progress_tracker.update_operation(f"Error accessing repository {repo.full_name} - skipping...")

        return unmergeable_prs

    def _analyze_pr_mergeability(self, pr: PullRequest) -> List[UnmergeableReason]:
        """Analyze a PR to determine why it cannot be merged."""
        reasons = []

        # Check if PR is draft
        if pr.draft:
            reasons.append(UnmergeableReason(
                type="draft",
                description="Pull request is in draft state"
            ))

        # Check mergeable state
        if pr.mergeable is False:
            if pr.mergeable_state == "dirty":
                reasons.append(UnmergeableReason(
                    type="merge_conflict",
                    description="Pull request has merge conflicts",
                    details="Branch cannot be automatically merged due to conflicts"
                ))
            elif pr.mergeable_state == "behind":
                reasons.append(UnmergeableReason(
                    type="behind_base",
                    description="Pull request is behind the base branch",
                    details="Branch needs to be updated with latest changes"
                ))

        # Check for failing status checks
        failing_checks = self._get_failing_status_checks(pr)
        if failing_checks:
            reasons.append(UnmergeableReason(
                type="failing_checks",
                description="Required status checks are failing",
                details=f"Failing checks: {', '.join(failing_checks)}"
            ))

        # Check for blocking reviews
        if self._has_blocking_reviews(pr):
            reasons.append(UnmergeableReason(
                type="blocked_review",
                description="Pull request has blocking reviews",
                details="One or more reviewers have requested changes"
            ))

        # Filter out standard code review requirements (approved reviews)
        # We only consider it unmergeable if there are actual blocking issues
        filtered_reasons = [r for r in reasons if r.type != "needs_approval"]

        return filtered_reasons

    def _get_failing_status_checks(self, pr: PullRequest) -> List[str]:
        """Get list of failing status check names for a PR."""
        failing_checks: List[str] = []

        try:
            # Check if head repo exists (can be None for deleted forks)
            if not pr.head.repo:
                return failing_checks

            # Check if head.sha exists
            if not pr.head or not pr.head.sha:
                return failing_checks

            commit = pr.head.repo.get_commit(pr.head.sha)

            # Get commit statuses (legacy status API)
            latest_statuses: Dict[str, CommitStatus] = {}
            for status in commit.get_statuses():
                context = status.context
                if (
                    context not in latest_statuses
                    or status.updated_at > latest_statuses[context].updated_at
                ):
                    latest_statuses[context] = status
                # Limit to avoid performance issues
                if len(latest_statuses) >= 50:
                    break

            # Add failing statuses
            for status in latest_statuses.values():
                if status.state in ["failure", "error"] and self._is_blocking_failure(status.context):
                    failing_checks.append(status.context)

            # Get check runs (GitHub Actions, etc.)
            try:
                check_runs = list(commit.get_check_runs()[:50])
                for check in check_runs:
                    if check.conclusion in ["failure", "cancelled", "timed_out"] and self._is_blocking_failure(check.name):
                        failing_checks.append(check.name)
            except Exception:
                # Some repos may not have check runs available
                pass

        except Exception as e:
            # Silently handle errors to avoid spamming the console
            # Most common errors are permission issues or deleted forks
            pass

        return failing_checks

    def _is_blocking_failure(self, check_name: str) -> bool:
        """Determine if a failing check should block merging."""
        # Skip non-blocking checks
        non_blocking_patterns = [
            "codecov",
            "sonarcloud",
            "license/cla",
            "continuous-integration/travis-ci/push",  # Only PR checks are blocking
        ]

        check_name_lower = check_name.lower()
        for pattern in non_blocking_patterns:
            if pattern in check_name_lower:
                return False

        return True

    def _has_blocking_reviews(self, pr: PullRequest) -> bool:
        """Check if PR has blocking review requests."""
        try:
            reviews = list(pr.get_reviews())

            # Check for "changes requested" reviews that haven't been dismissed
            for review in reviews:
                if review.state == "CHANGES_REQUESTED":
                    return True

        except Exception:
            # Silently handle errors to avoid spamming the console
            pass

        return False

    def _get_copilot_comments(self, pr: PullRequest) -> List[CopilotComment]:
        """Get unresolved Copilot feedback comments for a PR."""
        copilot_comments = []

        try:
            # Get review comments
            for comment in pr.get_review_comments():
                if self._is_copilot_comment(comment):
                    copilot_comments.append(CopilotComment(
                        id=comment.id,
                        body=comment.body,
                        file_path=comment.path,
                        line_number=comment.line,
                        created_at=comment.created_at.isoformat(),
                        state="open"  # Review comments don't have state
                    ))

            # Get issue comments
            for comment in pr.get_issue_comments():
                if self._is_copilot_comment(comment):
                    copilot_comments.append(CopilotComment(
                        id=comment.id,
                        body=comment.body,
                        created_at=comment.created_at.isoformat(),
                        state="open"
                    ))

        except Exception:
            # Silently handle errors to avoid spamming the console
            pass

        return copilot_comments

    def _is_copilot_comment(self, comment) -> bool:
        """Check if a comment is from GitHub Copilot."""
        # Check if comment is from Copilot user
        if hasattr(comment, 'user') and comment.user:
            copilot_users = ["github-copilot[bot]", "copilot"]
            if comment.user.login in copilot_users:
                return True

        # Check comment content for Copilot patterns
        if hasattr(comment, 'body') and comment.body:
            copilot_patterns = [
                "github copilot",
                "copilot suggestion",
                "ai suggestion",
                "🤖",  # Robot emoji often used by Copilot
            ]
            body_lower = comment.body.lower()
            return any(pattern in body_lower for pattern in copilot_patterns)

        return False
