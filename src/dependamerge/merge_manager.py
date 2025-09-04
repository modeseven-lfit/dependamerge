# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, cast

from rich.console import Console

from .models import PullRequestInfo, ComparisonResult
from .github_async import GitHubAsync
from .progress_tracker import MergeProgressTracker
from .copilot_handler import CopilotCommentHandler


class MergeStatus(Enum):
    """Status of a PR merge operation."""
    PENDING = "pending"
    APPROVING = "approving"
    APPROVED = "approved"
    MERGING = "merging"
    MERGED = "merged"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


@dataclass
class MergeResult:
    """Result of a PR merge operation."""
    pr_info: PullRequestInfo
    status: MergeStatus
    error: Optional[str] = None
    attempts: int = 0
    duration: float = 0.0


class AsyncMergeManager:
    """
    Manages parallel approval and merging of pull requests.

    This class handles:
    - Concurrent approval of PRs
    - Concurrent merging with retry logic
    - Progress tracking and error handling
    - Rate limit-aware processing
    """

    def __init__(
        self,
        token: str,
        merge_method: str = "merge",
        max_retries: int = 2,
        concurrency: int = 5,
        fix_out_of_date: bool = False,
        progress_tracker: Optional[MergeProgressTracker] = None,
        dry_run: bool = False,
        debug_merge: bool = False,
        dismiss_copilot: bool = False,
    ):
        self.token = token
        self.merge_method = merge_method
        self.max_retries = max_retries
        self.concurrency = concurrency
        self.fix_out_of_date = fix_out_of_date
        self.progress_tracker = progress_tracker
        self.dry_run = dry_run
        self.debug_merge = debug_merge
        self.dismiss_copilot = dismiss_copilot
        self.log = logging.getLogger(__name__)

        # Track merge operations
        self._merge_semaphore = asyncio.Semaphore(concurrency)
        self._results: List[MergeResult] = []
        self._github_client: Optional[GitHubAsync] = None
        self._copilot_handler: Optional[CopilotCommentHandler] = None
        self._console = Console()

    def _log_and_print(self, message: str, style: Optional[str] = None) -> None:
        """Log message and also print to stdout for CLI visibility."""
        self.log.info(message)
        if style:
            self._console.print(message, style=style)
        else:
            print(message)

    def _get_mergeability_icon_and_style(self, mergeable_state: Optional[str]) -> Tuple[str, Optional[str]]:
        """Get appropriate icon and style for mergeable state."""
        if mergeable_state == "dirty":
            return "🛑", "red"
        elif mergeable_state == "behind":
            return "⚠️", "yellow"
        elif mergeable_state == "clean":
            return "✅", "green"
        elif mergeable_state == "draft":
            return "📝", "blue"
        else:
            return "🔍", None

    async def __aenter__(self):
        """Async context manager entry."""
        self._github_client = GitHubAsync(token=self.token)
        await self._github_client.__aenter__()

        # Initialize Copilot handler if dismissal is enabled
        if self.dismiss_copilot:
            self._copilot_handler = CopilotCommentHandler(
                self._github_client,
                dry_run=self.dry_run,
                debug=self.debug_merge
            )

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._github_client:
            await self._github_client.__aexit__(exc_type, exc_val, exc_tb)

    async def merge_prs_parallel(
        self,
        pr_list: List[Tuple[PullRequestInfo, Optional[ComparisonResult]]],
    ) -> List[MergeResult]:
        """
        Merge multiple PRs in parallel.

        Args:
            pr_list: List of (PullRequestInfo, ComparisonResult) tuples

        Returns:
            List of MergeResult objects with operation results
        """
        if not pr_list:
            return []

        if self.dry_run:
            self.log.info(f"🔍 DRY RUN: Would merge {len(pr_list)} PRs")
        else:
            self.log.info(f"Starting parallel merge of {len(pr_list)} PRs")

        # Create tasks for all PRs
        tasks = []
        for pr_info, comparison in pr_list:
            task = asyncio.create_task(
                self._merge_single_pr_with_semaphore(pr_info),
                name=f"merge-{pr_info.repository_full_name}#{pr_info.number}"
            )
            tasks.append(task)

        # Wait for all tasks to complete
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results and handle exceptions
        final_results: List[MergeResult] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                pr_info = pr_list[i][0]
                error_result = MergeResult(
                    pr_info=pr_info,
                    status=MergeStatus.FAILED,
                    error=str(result)
                )
                final_results.append(error_result)
                self.log.error(
                    f"Unexpected error merging PR {pr_info.repository_full_name}#{pr_info.number}: {result}"
                )
            else:
                # result is guaranteed to be MergeResult here since it's not an Exception
                final_results.append(cast(MergeResult, result))

        self._results = final_results
        return final_results

    async def _merge_single_pr_with_semaphore(self, pr_info: PullRequestInfo) -> MergeResult:
        """Merge a single PR with concurrency control."""
        async with self._merge_semaphore:
            return await self._merge_single_pr(pr_info)

    async def _merge_single_pr(self, pr_info: PullRequestInfo) -> MergeResult:
        """
        Merge a single pull request with retry logic.

        Args:
            pr_info: Pull request information

        Returns:
            MergeResult with operation status and details
        """
        start_time = time.time()
        repo_owner, repo_name = pr_info.repository_full_name.split("/")

        result = MergeResult(
            pr_info=pr_info,
            status=MergeStatus.PENDING
        )

        try:
            # Show blocking messages for all unmergeable PRs
            if not self._is_pr_mergeable(pr_info):
                icon, style = self._get_mergeability_icon_and_style(pr_info.mergeable_state)
                # Show clean, single-line message with safety check for mergeable_state
                state_text = pr_info.mergeable_state or "unknown"
                # Escape square brackets for Rich console markup
                self._console.print(f"{icon} Cannot merge PR: {pr_info.html_url} \\[{state_text}]")

            if not self._is_pr_mergeable(pr_info):
                # Get detailed status for a more informative skip message
                from .github_client import GitHubClient
                client = GitHubClient(token=self.token)
                detailed_status = client.get_pr_status_details(pr_info)

                # Determine if this is truly blocked (unmergeable) or just skipped
                if pr_info.mergeable_state == "dirty" or (pr_info.mergeable_state == "behind" and pr_info.mergeable is False):
                    result.status = MergeStatus.BLOCKED
                    icon = "🛑"
                    action = "Blocking"
                else:
                    result.status = MergeStatus.SKIPPED
                    icon = "⏭️"
                    action = "Skipping"

                result.error = f"PR is not mergeable (state: {pr_info.mergeable_state}, mergeable: {pr_info.mergeable})"

                # Use detailed status in the log message for better user feedback
                self.log.info(f"{icon}  {action} PR {pr_info.repository_full_name}#{pr_info.number}: {detailed_status}")

                # For the result error (used in CLI output), use the detailed status if it's more informative
                if detailed_status and detailed_status != "Status unclear":
                    result.error = detailed_status

                return result

            # Check for blocking reviews (changes requested)
            if self._has_blocking_reviews(pr_info):
                result.status = MergeStatus.SKIPPED
                result.error = "PR has reviews requesting changes - will not override human feedback"
                self.log.info(f"⏭️  Skipping PR {pr_info.repository_full_name}#{pr_info.number}: {result.error}")
                return result

            if self.dry_run:
                # In dry run mode, simulate the merge process without actually doing it
                self.log.info(f"🔍 DRY RUN: Would approve PR {pr_info.number} in {pr_info.repository_full_name}")
                result.status = MergeStatus.APPROVED

                self.log.info(f"🔍 DRY RUN: Would merge PR {pr_info.number} in {pr_info.repository_full_name}")
                result.status = MergeStatus.MERGED
                self.log.info(f"✅ DRY RUN: Successfully would merge PR {pr_info.number}")

                if self.progress_tracker:
                    self.progress_tracker.merge_success()
                return result

            # Step 1: Check merge requirements (including branch protection)
            if self.debug_merge:
                self._log_and_print(f"🔍 Checking merge requirements for PR {pr_info.number} in {pr_info.repository_full_name}")
            can_merge, merge_check_reason = await self._check_merge_requirements(pr_info)

            if not can_merge:
                result.status = MergeStatus.SKIPPED
                result.error = f"Merge requirements not met: {merge_check_reason}"
                self.log.info(f"⏭️  Skipping PR {pr_info.repository_full_name}#{pr_info.number}: {result.error}")
                return result

            # Step 2: Dismiss Copilot comments if enabled
            if self.dismiss_copilot and self._copilot_handler:
                if self.debug_merge:
                    self._log_and_print(f"🤖 Checking for Copilot comments to dismiss for PR {pr_info.number}")

                try:
                    dismissed_count, total_count = await self._copilot_handler.dismiss_copilot_comments_for_pr(pr_info)
                    if total_count > 0:
                        if self.dry_run:
                            self._log_and_print(f"🔍 DRY RUN: Would dismiss {dismissed_count}/{total_count} Copilot comments")
                        else:
                            self._log_and_print(f"🤖 Dismissed {dismissed_count}/{total_count} Copilot comments")
                except Exception as e:
                    self.log.warning(f"⚠️  Failed to dismiss Copilot comments for PR {pr_info.number}: {e}")

            # Step 3: Approve the PR
            result.status = MergeStatus.APPROVING
            self._log_and_print(f"👍 Approving PR {pr_info.number} in {pr_info.repository_full_name}")
            if self.progress_tracker:
                self.progress_tracker.update_operation(
                    f"Approving PR {pr_info.number} in {pr_info.repository_full_name}"
                )

            await self._approve_pr(repo_owner, repo_name, pr_info.number)
            result.status = MergeStatus.APPROVED

            # Step 4: Merge the PR with retry logic
            result.status = MergeStatus.MERGING
            self._log_and_print(f"🔀 Merging PR {pr_info.number} in {pr_info.repository_full_name}")
            if self.progress_tracker:
                self.progress_tracker.update_operation(
                    f"Merging PR {pr_info.number} in {pr_info.repository_full_name}"
                )

            merged = await self._merge_pr_with_retry(pr_info, repo_owner, repo_name)

            if merged:
                result.status = MergeStatus.MERGED
                if self.progress_tracker:
                    self.progress_tracker.merge_success()
                self._log_and_print(f"✅ Successfully merged PR {pr_info.number}")
            else:
                result.status = MergeStatus.FAILED
                result.error = "Failed to merge after all retry attempts"
                if self.progress_tracker:
                    self.progress_tracker.merge_failure()
                self.log.info(f"❌ Failed to merge PR {pr_info.number}")

        except Exception as e:
            result.status = MergeStatus.FAILED
            result.error = str(e)
            if self.progress_tracker:
                self.progress_tracker.merge_failure()
            self.log.error(f"Error processing PR {pr_info.repository_full_name}#{pr_info.number}: {e}")

        finally:
            result.duration = time.time() - start_time

        return result

    def _is_pr_mergeable(self, pr_info: PullRequestInfo) -> bool:
        """
        Check if a PR is mergeable.

        Args:
            pr_info: Pull request information

        Returns:
            True if the PR can be merged, False otherwise
        """
        # Handle different types of blocks intelligently - matches original logic
        if pr_info.mergeable_state == "blocked" and pr_info.mergeable is True:
            # This is blocked by branch protection but tool can handle it (approval, etc.)
            return True
        elif pr_info.mergeable_state == "blocked" and pr_info.mergeable is False:
            # Blocked by failing checks - we can try merging anyway
            return True
        elif not pr_info.mergeable:
            # Only skip if mergeable is False and not in the special blocked states above
            # Use appropriate icon based on state - only truly unmergeable PRs get blocked
            if pr_info.mergeable_state == "dirty" or (pr_info.mergeable_state == "behind" and pr_info.mergeable is False):
                icon = "🛑"
                action = "Blocking"
            else:
                icon = "⏭️"
                action = "Skipping"

            # Just log internally, don't show verbose messages
            skip_msg = f"{icon}  {action} unmergeable PR {pr_info.number} in {pr_info.repository_full_name} (mergeable: {pr_info.mergeable}, state: {pr_info.mergeable_state})"
            self.log.info(skip_msg)
            return False

        # All other cases are considered mergeable
        self.log.info(f"✅ PR {pr_info.number} in {pr_info.repository_full_name} is considered mergeable (mergeable: {pr_info.mergeable}, state: {pr_info.mergeable_state})")
        if self.debug_merge:
            self._log_and_print(f"✅ PR {pr_info.number} in {pr_info.repository_full_name} is considered mergeable (mergeable: {pr_info.mergeable}, state: {pr_info.mergeable_state})")
        return True

    def _has_blocking_reviews(self, pr_info: PullRequestInfo) -> bool:
        """
        Check if a PR has reviews that would block automatic approval.

        Args:
            pr_info: Pull request information

        Returns:
            True if there are blocking reviews (changes requested), False otherwise
        """
        for review in pr_info.reviews:
            if review.state == "CHANGES_REQUESTED":
                self.log.info(f"⚠️  PR {pr_info.number} has changes requested by {review.user} - will not override human feedback")
                return True
        return False

    async def _check_merge_requirements(self, pr_info: PullRequestInfo) -> tuple[bool, str]:
        """
        Check if a PR meets all requirements for merging, including branch protection rules.

        Args:
            pr_info: Pull request information

        Returns:
            Tuple of (can_merge: bool, reason: str)
        """
        if not self._github_client:
            return False, "GitHub client not initialized"

        repo_owner, repo_name = pr_info.repository_full_name.split("/")

        try:
            # Check branch protection rules
            base_branch = pr_info.base_ref or "main"
            protection_rules = await self._github_client.get_branch_protection(
                repo_owner, repo_name, base_branch
            )

            if protection_rules:
                if self.debug_merge:
                    self.log.info(f"🛡️  Branch protection rules found for {base_branch} in {pr_info.repository_full_name}")

                # Check required status checks
                required_status_checks = protection_rules.get("required_status_checks", {})
                if required_status_checks and required_status_checks.get("strict"):
                    # Would need to check actual status check results here
                    # For now, log that they exist
                    contexts = required_status_checks.get("contexts", [])
                    if contexts:
                        if self.debug_merge:
                            self.log.info(f"📋 Required status checks: {', '.join(contexts)}")

                # Check required reviews
                required_reviews = protection_rules.get("required_pull_request_reviews", {})
                if required_reviews:
                    required_count = required_reviews.get("required_approving_review_count", 0)
                    dismiss_stale = required_reviews.get("dismiss_stale_reviews", False)
                    require_code_owner = required_reviews.get("require_code_owner_reviews", False)

                    if self.debug_merge:
                        self.log.info(f"👥 Required reviews: {required_count}, dismiss stale: {dismiss_stale}, code owners: {require_code_owner}")

                    # If code owner reviews are required, our automated approval might not be sufficient
                    if require_code_owner:
                        return False, "Branch protection requires code owner reviews, automated approval may not be sufficient"

                # Check if admin enforcement is enabled
                enforce_admins = protection_rules.get("enforce_admins", {})
                if enforce_admins and enforce_admins.get("enabled"):
                    if self.debug_merge:
                        self.log.info("🔒 Admin enforcement is enabled - even admins must follow branch protection rules")
            else:
                if self.debug_merge:
                    self.log.info(f"✅ No branch protection rules found for {base_branch}")

        except Exception as e:
            # Don't fail the merge attempt if we can't check protection rules
            if self.debug_merge:
                self.log.warning(f"⚠️  Could not check branch protection rules for {pr_info.repository_full_name}: {e}")

        # Additional checks based on PR state
        if pr_info.mergeable_state == "blocked":
            # Check if Copilot comments might be the blocker
            if self.dismiss_copilot and self._copilot_handler:
                has_copilot_comments = self._copilot_handler.has_blocking_copilot_comments(pr_info)
                if has_copilot_comments:
                    summary = self._copilot_handler.get_copilot_comment_summary(pr_info)
                    if self.debug_merge:
                        self.log.info(f"🤖 Found {summary['unresolved_copilot_reviews']} unresolved Copilot reviews that may be blocking merge")
                    return True, "PR blocked but has Copilot comments that will be dismissed"
            return False, f"PR is blocked (mergeable_state: {pr_info.mergeable_state})"
        elif pr_info.mergeable_state == "behind":
            if not self.fix_out_of_date:
                return False, "PR is behind base branch and --no-fix option is set"
        elif pr_info.mergeable_state == "dirty":
            return False, f"PR has merge conflicts (mergeable_state: {pr_info.mergeable_state})"

        return True, "All merge requirements appear to be met"

    async def _approve_pr(self, owner: str, repo: str, pr_number: int) -> None:
        """
        Approve a pull request.

        Args:
            owner: Repository owner
            repo: Repository name
            pr_number: Pull request number

        Raises:
            Exception: If approval fails
        """
        if not self._github_client:
            raise RuntimeError("GitHub client not initialized")

        try:
            await self._github_client.approve_pull_request(
                owner, repo, pr_number, "Auto-approved by dependamerge"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to approve PR {owner}/{repo}#{pr_number}: {e}") from e

    async def _merge_pr_with_retry(
        self,
        pr_info: PullRequestInfo,
        owner: str,
        repo: str
    ) -> bool:
        """
        Attempt to merge a PR with retry logic.

        Args:
            pr_info: Pull request information
            owner: Repository owner
            repo: Repository name

        Returns:
            True if merged successfully, False otherwise
        """
        if not self._github_client:
            raise RuntimeError("GitHub client not initialized")

        for attempt in range(self.max_retries + 1):
            try:
                # Attempt the merge
                merged = await self._github_client.merge_pull_request(
                    owner, repo, pr_info.number, self.merge_method
                )

                if merged:
                    return True

                # Merge failed, check if we can fix it
                if attempt < self.max_retries:
                    should_retry = await self._handle_merge_failure(pr_info, owner, repo)
                    if should_retry:
                        self.log.info(f"Retrying merge for PR {owner}/{repo}#{pr_info.number} (attempt {attempt + 1})")
                        continue
                    else:
                        break

            except Exception as e:
                error_msg = str(e)

                # Enhanced error handling with specific status code checks
                if "405" in error_msg and "Method Not Allowed" in error_msg:
                    self.log.error(f"❌ Merge method not allowed for PR {owner}/{repo}#{pr_info.number}: {error_msg}")
                    self.log.error("This is likely due to branch protection rules, required status checks, or repository settings that prevent merging via API")
                    # Don't retry 405 errors as they indicate a fundamental blocker
                    break
                elif "403" in error_msg and "Forbidden" in error_msg:
                    self.log.error(f"❌ Merge forbidden for PR {owner}/{repo}#{pr_info.number}: likely requires additional permissions or has branch protection rules")
                    self.log.error(f"403 Forbidden during merge attempt {attempt + 1} for PR {owner}/{repo}#{pr_info.number}: This could be due to branch protection rules, insufficient permissions, or the PR state changed")
                elif "422" in error_msg:
                    self.log.error(f"❌ Merge validation failed for PR {owner}/{repo}#{pr_info.number}: {error_msg}")
                    self.log.error("This usually means the PR state changed or merge requirements are not met")
                else:
                    self.log.error(f"Error during merge attempt {attempt + 1} for PR {owner}/{repo}#{pr_info.number}: {e}")

                if attempt >= self.max_retries:
                    break

                # Don't retry certain error types that are unlikely to be transient
                if "405" in error_msg or ("422" in error_msg and "not mergeable" in error_msg.lower()):
                    self.log.info(f"Not retrying PR {owner}/{repo}#{pr_info.number} due to permanent error condition")
                    break

                # Wait a bit before retrying
                await asyncio.sleep(1.0)

        return False

    async def _handle_merge_failure(
        self,
        pr_info: PullRequestInfo,
        owner: str,
        repo: str
    ) -> bool:
        """
        Handle a merge failure and determine if we should retry.

        Args:
            pr_info: Pull request information
            owner: Repository owner
            repo: Repository name

        Returns:
            True if we should retry, False otherwise
        """
        if not self._github_client:
            return False

        # Check if the branch is out of date and we can fix it
        if self.fix_out_of_date and pr_info.mergeable_state == "behind":
            try:
                self.log.info(f"PR {owner}/{repo}#{pr_info.number} is behind - updating branch")
                await self._github_client.update_branch(owner, repo, pr_info.number)
                # Wait a moment for GitHub to process the update
                await asyncio.sleep(2.0)
                return True
            except Exception as e:
                self.log.error(f"Failed to update branch for PR {owner}/{repo}#{pr_info.number}: {e}")

        # For other failure types, don't retry
        return False

    def get_results_summary(self) -> Dict[str, Any]:
        """
        Get a summary of merge results.

        Returns:
            Dictionary with merge statistics
        """
        if not self._results:
            return {
                "total": 0,
                "merged": 0,
                "failed": 0,
                "skipped": 0,
                "success_rate": 0.0,
                "average_duration": 0.0
            }

        total = len(self._results)
        merged = sum(1 for r in self._results if r.status == MergeStatus.MERGED)
        failed = sum(1 for r in self._results if r.status == MergeStatus.FAILED)
        skipped = sum(1 for r in self._results if r.status == MergeStatus.SKIPPED)

        success_rate = (merged / total) * 100 if total > 0 else 0.0
        average_duration = sum(r.duration for r in self._results) / total if total > 0 else 0.0

        return {
            "total": total,
            "merged": merged,
            "failed": failed,
            "skipped": skipped,
            "success_rate": success_rate,
            "average_duration": average_duration,
            "results": self._results
        }

    def get_failed_prs(self) -> List[MergeResult]:
        """
        Get list of failed merge results.

        Returns:
            List of MergeResult objects that failed
        """
        return [r for r in self._results if r.status == MergeStatus.FAILED]

    def get_successful_prs(self) -> List[MergeResult]:
        """
        Get list of successful merge results.

        Returns:
            List of MergeResult objects that were merged successfully
        """
        return [r for r in self._results if r.status == MergeStatus.MERGED]
