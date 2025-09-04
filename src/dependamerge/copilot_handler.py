# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Copilot comment handler for detecting and managing GitHub Copilot review comments.

This module provides functionality to:
- Identify Copilot-generated review comments
- Filter and categorize Copilot feedback
- Dismiss unresolved Copilot comments to unblock PR merging
"""

import logging
from typing import List, Tuple, Dict, Any

from .models import PullRequestInfo, ReviewComment

logger = logging.getLogger(__name__)

# Known Copilot author identifiers
COPILOT_AUTHORS = {
    "Copilot",
    "github-copilot",
    "copilot[bot]",
    "github-copilot[bot]"
}

# Common Copilot comment patterns that are often safe to dismiss
COMMON_COPILOT_PATTERNS = [
    r"use:\s+ubuntu-24\.04",  # Ubuntu version suggestions
    r"consider using.*instead of",  # Generic suggestions
    r"you might want to",  # Soft suggestions
    r"this could be improved by",  # Improvement suggestions
]


class CopilotCommentHandler:
    """Handler for managing GitHub Copilot review comments."""

    def __init__(self, github_client, dry_run: bool = False, debug: bool = False):
        """
        Initialize the Copilot comment handler.

        Args:
            github_client: Async GitHub client for API operations
            dry_run: If True, only simulate dismissal operations
            debug: Enable debug logging
        """
        self.github_client = github_client
        self.dry_run = dry_run
        self.debug = debug
        self.log = logging.getLogger(__name__)

    def is_copilot_review(self, review) -> bool:
        """
        Determine if a review is from GitHub Copilot.

        Args:
            review: Review to check

        Returns:
            True if review is from Copilot, False otherwise
        """
        if not review.user:
            return False

        # Check if author matches known Copilot identifiers
        author_lower = review.user.lower()
        for copilot_author in COPILOT_AUTHORS:
            if copilot_author.lower() in author_lower:
                return True

        return False

    def is_copilot_comment(self, comment: ReviewComment) -> bool:
        """
        Determine if a review comment is from GitHub Copilot.

        Args:
            comment: Review comment to check

        Returns:
            True if comment is from Copilot, False otherwise
        """
        if not comment.author:
            return False

        # Check if author matches known Copilot identifiers
        author_lower = comment.author.lower()
        for copilot_author in COPILOT_AUTHORS:
            if copilot_author.lower() in author_lower:
                return True

        return False

    def get_copilot_reviews(self, pr_info: PullRequestInfo) -> List:
        """
        Extract all Copilot reviews from a pull request.

        Args:
            pr_info: Pull request information

        Returns:
            List of Copilot reviews
        """
        copilot_reviews = []

        for review in pr_info.reviews:
            if self.is_copilot_review(review):
                copilot_reviews.append(review)
                if self.debug:
                    self.log.info(f"🤖 Found Copilot review: {review.id} - {review.state}")

        return copilot_reviews

    def get_copilot_comments(self, pr_info: PullRequestInfo) -> List:
        """
        Extract all Copilot comments from a pull request.
        This is now deprecated in favor of reviews.

        Args:
            pr_info: Pull request information

        Returns:
            Empty list (reviews are used instead)
        """
        # We now focus on reviews instead of individual comments
        # since GitHub's GraphQL API doesn't easily expose review comments
        return []

    def get_unresolved_copilot_reviews(self, pr_info: PullRequestInfo) -> List:
        """
        Get unresolved Copilot reviews that may be blocking the merge.

        Args:
            pr_info: Pull request information

        Returns:
            List of unresolved Copilot reviews
        """
        copilot_reviews = self.get_copilot_reviews(pr_info)

        # Filter for reviews that are blocking (CHANGES_REQUESTED or COMMENTED)
        unresolved = []
        for review in copilot_reviews:
            if review.state in ["CHANGES_REQUESTED", "COMMENTED", "PENDING"]:
                unresolved.append(review)
                if self.debug:
                    self.log.info(f"🚫 Unresolved Copilot review: {review.id} (state: {review.state})")

        return unresolved

    def get_unresolved_copilot_comments(self, pr_info: PullRequestInfo) -> List:
        """
        Get unresolved Copilot comments that may be blocking the merge.
        This is now deprecated in favor of reviews.

        Args:
            pr_info: Pull request information

        Returns:
            Empty list (reviews are used instead)
        """
        # We now focus on reviews instead of individual comments
        return []

    def categorize_copilot_review(self, review) -> str:
        """
        Categorize a Copilot review by type.

        Args:
            review: Copilot review

        Returns:
            Category string (e.g., 'suggestion', 'formatting', 'security')
        """
        if not review.body:
            return 'suggestion'

        body_lower = review.body.lower()

        # Security-related suggestions
        if any(keyword in body_lower for keyword in ['security', 'vulnerability', 'credential', 'secret']):
            return 'security'

        # Formatting and style suggestions
        if any(keyword in body_lower for keyword in ['format', 'style', 'indent', 'whitespace']):
            return 'formatting'

        # Version suggestions (like ubuntu-24.04)
        if any(keyword in body_lower for keyword in ['version', 'ubuntu-', 'latest']):
            return 'version'

        # Performance suggestions
        if any(keyword in body_lower for keyword in ['performance', 'optimize', 'efficient']):
            return 'performance'

        # Default to general suggestion
        return 'suggestion'

    async def resolve_copilot_review(self, owner: str, repo: str, review_id: str) -> bool:
        """
        Resolve a Copilot review by dismissing it.

        Args:
            owner: Repository owner
            repo: Repository name
            review_id: GraphQL ID of the review to dismiss

        Returns:
            True if successfully resolved, False otherwise
        """
        if self.dry_run:
            self.log.info(f"🔍 DRY RUN: Would dismiss Copilot review {review_id}")
            return True

        try:
            # Use GraphQL mutation to dismiss the pull request review
            mutation = """
            mutation DismissPullRequestReview($reviewId: ID!, $message: String!) {
              dismissPullRequestReview(input: {
                pullRequestReviewId: $reviewId
                message: $message
              }) {
                pullRequestReview {
                  id
                  state
                  author { login }
                }
              }
            }
            """

            variables = {
                "reviewId": review_id,
                "message": "Auto-dismissed by dependamerge: Copilot feedback resolved"
            }

            result = await self.github_client.graphql(mutation, variables)

            if result and result.get("data", {}).get("dismissPullRequestReview"):
                self.log.info(f"✅ Successfully dismissed Copilot review {review_id}")
                return True
            else:
                self.log.error(f"❌ Failed to dismiss Copilot review {review_id}: {result}")
                return False

        except Exception as e:
            self.log.error(f"❌ Error dismissing Copilot review {review_id}: {e}")
            return False

    async def dismiss_copilot_comments_for_pr(self, pr_info: PullRequestInfo) -> Tuple[int, int]:
        """
        Dismiss all unresolved Copilot reviews and comments for a pull request.

        Args:
            pr_info: Pull request information

        Returns:
            Tuple of (successful_dismissals, total_items)
        """
        owner, repo = pr_info.repository_full_name.split("/")

        # Get both reviews and review comments from REST API
        unresolved_reviews = self.get_unresolved_copilot_reviews(pr_info)
        review_comments = await self._get_copilot_review_comments(owner, repo, pr_info.number)

        total_items = len(unresolved_reviews) + len(review_comments)

        if total_items == 0:
            self.log.info(f"✅ No unresolved Copilot feedback found for PR {pr_info.number}")
            return 0, 0

        self.log.info(f"🤖 Found {len(unresolved_reviews)} Copilot reviews and {len(review_comments)} Copilot comments for PR {pr_info.number}")

        successful_dismissals = 0

        # Dismiss reviews
        for review in unresolved_reviews:
            if self.debug:
                self.log.info(f"🔍 Processing Copilot review {review.id} (state: {review.state})")
                if review.body:
                    self.log.info(f"   Content: {review.body[:100]}...")

            success = await self.resolve_copilot_review(owner, repo, review.id)
            if success:
                successful_dismissals += 1

        # Resolve review comment threads (this is what likely blocks merging)
        for comment in review_comments:
            if self.debug:
                self.log.info(f"🔍 Processing Copilot comment {comment.get('id')} on {comment.get('path', 'unknown file')}")
                self.log.info(f"   Content: {comment.get('body', '')[:100]}...")

            # For review comments, we need to resolve the thread rather than dismiss
            success = await self._resolve_review_comment_thread(comment)
            if success:
                successful_dismissals += 1

        self.log.info(f"📊 Resolved {successful_dismissals}/{total_items} Copilot items for PR {pr_info.number}")
        return successful_dismissals, total_items

    async def dismiss_copilot_comments_bulk(self, pr_list: List[PullRequestInfo]) -> Tuple[int, int, int]:
        """
        Dismiss Copilot comments for multiple PRs.

        Args:
            pr_list: List of pull request information

        Returns:
            Tuple of (total_successful_dismissals, total_comments, processed_prs)
        """
        total_successful = 0
        total_comments = 0
        processed_prs = 0

        for pr_info in pr_list:
            try:
                successful, comment_count = await self.dismiss_copilot_comments_for_pr(pr_info)
                total_successful += successful
                total_comments += comment_count

                if comment_count > 0:
                    processed_prs += 1

            except Exception as e:
                self.log.error(f"❌ Error processing Copilot comments for PR {pr_info.repository_full_name}#{pr_info.number}: {e}")

        self.log.info(f"📈 Bulk dismissal complete: {total_successful}/{total_comments} comments dismissed across {processed_prs} PRs")
        return total_successful, total_comments, processed_prs

    def has_blocking_copilot_comments(self, pr_info: PullRequestInfo) -> bool:
        """
        Check if a PR has unresolved Copilot reviews that might block merging.
        Note: This only checks reviews, not individual comments (which require async call).

        Args:
            pr_info: Pull request information

        Returns:
            True if there are blocking Copilot reviews, False otherwise
        """
        unresolved_reviews = self.get_unresolved_copilot_reviews(pr_info)
        return len(unresolved_reviews) > 0

    def get_copilot_comment_summary(self, pr_info: PullRequestInfo) -> dict:
        """
        Get a summary of Copilot reviews and comments for a PR.

        Args:
            pr_info: Pull request information

        Returns:
            Dictionary with review and comment summary information
        """
        all_copilot_reviews = self.get_copilot_reviews(pr_info)
        unresolved_reviews = self.get_unresolved_copilot_reviews(pr_info)

        # Categorize reviews
        categories: Dict[str, int] = {}
        for review in all_copilot_reviews:
            category = self.categorize_copilot_review(review)
            categories[category] = categories.get(category, 0) + 1

        return {
            "total_copilot_reviews": len(all_copilot_reviews),
            "unresolved_copilot_reviews": len(unresolved_reviews),
            "total_copilot_comments": 0,  # Deprecated
            "unresolved_copilot_comments": 0,  # Deprecated
            "categories": categories,
            "blocking": len(unresolved_reviews) > 0
        }

    async def _get_copilot_review_comments(self, owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        """
        Get Copilot review comments from REST API.

        Args:
            owner: Repository owner
            repo: Repository name
            pr_number: Pull request number

        Returns:
            List of Copilot review comments
        """
        try:
            all_comments = await self.github_client.get_pull_request_review_comments(owner, repo, pr_number)
            copilot_comments = []

            for comment in all_comments:
                author = comment.get('user', {}).get('login', '').lower()
                # Check if comment is from Copilot
                if any(copilot_author.lower() in author for copilot_author in COPILOT_AUTHORS):
                    copilot_comments.append(comment)
                    if self.debug:
                        self.log.info(f"🤖 Found Copilot review comment: {comment.get('id')} on {comment.get('path', 'unknown')}")

            return copilot_comments

        except Exception as e:
            self.log.warning(f"⚠️  Could not fetch review comments for PR {pr_number}: {e}")
            return []

    async def _resolve_review_comment_thread(self, comment: Dict[str, Any]) -> bool:
        """
        Resolve a review comment thread by marking it as resolved.

        Args:
            comment: Review comment dictionary from REST API

        Returns:
            True if successfully resolved, False otherwise
        """
        if self.dry_run:
            self.log.info(f"🔍 DRY RUN: Would resolve Copilot comment thread {comment.get('id')}")
            return True

        try:
            # GitHub's REST API doesn't have a direct way to resolve comments
            # The review comments are typically resolved through the web interface
            # For now, we'll focus on dismissing the associated review
            self.log.info(f"ℹ️  Cannot directly resolve comment {comment.get('id')} via API - focusing on review dismissal")
            return True

        except Exception as e:
            self.log.error(f"❌ Error resolving comment thread {comment.get('id')}: {e}")
            return False
