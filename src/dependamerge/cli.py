# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

import hashlib
from typing import List, Optional, Tuple

import requests
import typer
from typer.core import TyperGroup
import click
import urllib3.exceptions
from github.Repository import Repository
from rich.console import Console
from rich.table import Table

from .github_client import GitHubClient
from .models import ComparisonResult, PullRequestInfo, UnmergeablePR
from .pr_comparator import PRComparator
from .progress_tracker import ProgressTracker, MergeProgressTracker

# Constants
MAX_RETRIES = 2

class DefaultCommandGroup(TyperGroup):
    def __init__(self, *args, default="merge", **kwargs):
        super().__init__(*args, **kwargs)
        self.default_command_name = default

    def parse_args(self, ctx, args):
        # If the first token isn't a known subcommand and isn't an option,
        # treat it as arguments to the default command.
        if args and not args[0].startswith("-"):
            cmd = self.get_command(ctx, args[0])
            if cmd is None and self.default_command_name:
                args.insert(0, self.default_command_name)
        return super().parse_args(ctx, args)


app = typer.Typer(
    cls=DefaultCommandGroup,
    help="Scan GitHub organizations for unmergeable PRs and automatically merge pull requests"
)
console = Console(markup=False)




def _generate_override_sha(
    pr_info: PullRequestInfo, commit_message_first_line: str
) -> str:
    """
    Generate a SHA hash based on PR author info and commit message.

    Args:
        pr_info: Pull request information containing author details
        commit_message_first_line: First line of the commit message to use as salt

    Returns:
        SHA256 hash string
    """
    # Create a string combining author info and commit message first line
    combined_data = f"{pr_info.author}:{commit_message_first_line.strip()}"

    # Generate SHA256 hash
    sha_hash = hashlib.sha256(combined_data.encode("utf-8")).hexdigest()

    # Return first 16 characters for readability
    return sha_hash[:16]


def _validate_override_sha(
    provided_sha: str, pr_info: PullRequestInfo, commit_message_first_line: str
) -> bool:
    """
    Validate that the provided SHA matches the expected one for this PR.

    Args:
        provided_sha: SHA provided by user via --override flag
        pr_info: Pull request information
        commit_message_first_line: First line of commit message

    Returns:
        True if SHA is valid, False otherwise
    """
    expected_sha = _generate_override_sha(pr_info, commit_message_first_line)
    return provided_sha == expected_sha


@app.command()
def merge(
    pr_url: str = typer.Argument(..., help="GitHub pull request URL"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what changes will apply without making them"
    ),
    similarity_threshold: float = typer.Option(
        0.8, "--threshold", help="Similarity threshold for matching PRs (0.0-1.0)"
    ),
    merge_method: str = typer.Option(
        "merge", "--merge-method", help="Merge method: merge, squash, or rebase"
    ),
    token: Optional[str] = typer.Option(
        None, "--token", help="GitHub token (or set GITHUB_TOKEN env var)"
    ),
    override: Optional[str] = typer.Option(
        None, "--override", help="SHA hash to override non-automation PR restriction"
    ),
    fix: bool = typer.Option(
        False, "--fix", help="Automatically fix out-of-date branches before merging"
    ),
    show_progress: bool = typer.Option(
        True, "--progress/--no-progress", help="Show real-time progress updates"
    ),
):
    """
    Merge automation pull requests across an organization.

    This command will:
    1. Analyze the provided PR
    2. Find similar PRs in the organization
    3. Approve and merge matching PRs

    For automation PRs (dependabot, pre-commit-ci, etc.):
    - Merges similar PRs from the same automation tool

    For non-automation PRs:
    - Requires --override flag with SHA hash
    - Only merges PRs from the same author
    - SHA is generated from author + commit message
    """
    # Initialize progress tracker
    progress_tracker = None

    try:
        # Parse PR URL first to get organization info
        github_client = GitHubClient(token)
        owner, repo_name, pr_number = github_client.parse_pr_url(pr_url)

        # Initialize progress tracker with organization name
        if show_progress:
            progress_tracker = MergeProgressTracker(owner)
            progress_tracker.start()
            # Check if Rich display is available
            if not progress_tracker.rich_available:
                console.print(f"Analyzing PR: {pr_url}")
                console.print("Progress updates will be shown as simple text...")
            progress_tracker.update_operation(f"Analyzing source PR #{pr_number}")
        else:
            console.print(f"Analyzing PR: {pr_url}")

        # Initialize comparator
        comparator = PRComparator(similarity_threshold)

        if progress_tracker:
            progress_tracker.update_operation("Getting source PR details...")

        try:
            source_pr: PullRequestInfo = github_client.get_pull_request_info(
                owner, repo_name, pr_number
            )
        except (
            urllib3.exceptions.NameResolutionError,
            urllib3.exceptions.MaxRetryError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.RequestException,
        ) as e:
            console.print(
                "Network Error: Failed to connect to GitHub API while fetching source PR."
            )
            console.print(f"Details: {e}")
            console.print("Please check your internet connection and try again.")
            raise typer.Exit(1) from e

        # Display source PR info
        _display_pr_info(source_pr, "Source PR", github_client)

        # Check if source PR is from automation or has valid override
        if not github_client.is_automation_author(source_pr.author):
            # Get commit messages to generate SHA
            commit_messages = github_client.get_pull_request_commits(
                owner, repo_name, pr_number
            )
            first_commit_line = (
                commit_messages[0].split("\n")[0] if commit_messages else ""
            )

            # Generate expected SHA for this PR
            expected_sha = _generate_override_sha(source_pr, first_commit_line)

            if not override:
                console.print("Source PR is not from a recognized automation tool.")
                console.print(
                    f"To merge this and similar PRs, run again with: --override {expected_sha}"
                )
                console.print(
                    f"This SHA is based on the author '{source_pr.author}' and commit message '{first_commit_line[:50]}...'",
                    style="dim",
                )
                return

            # Validate provided override SHA
            if not _validate_override_sha(override, source_pr, first_commit_line):
                console.print(
                    "Error: Invalid override SHA. Expected SHA for this PR and author is:"
                )
                console.print(f"--override {expected_sha}")
                raise typer.Exit(1)

            console.print(
                "Override SHA validated. Proceeding with non-automation PR merge."
            )

        # Get organization repositories
        if progress_tracker:
            progress_tracker.update_operation("Getting organization repositories...")
        else:
            console.print(f"\nScanning organization: {owner}")

        try:
            repositories: List[Repository] = (
                github_client.get_organization_repositories(owner)
            )
        except (
            urllib3.exceptions.NameResolutionError,
            urllib3.exceptions.MaxRetryError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.RequestException,
        ) as e:
            console.print(
                "Network Error: Failed to connect to GitHub API while fetching organization repositories."
            )
            console.print(f"Details: {e}")
            console.print("Please check your internet connection and try again.")
            raise typer.Exit(1) from e
        console.print(f"Found {len(repositories)} repositories")
        #     progress.update(task, description=f"Found {len(repositories)} repositories")

        # Find similar PRs
        similar_prs: List[Tuple[PullRequestInfo, ComparisonResult]] = []

        if progress_tracker:
            progress_tracker.update_operation("Listing repositories...")

        repositories = github_client.get_organization_repositories(owner)
        total_repos = len(repositories)

        if progress_tracker:
            progress_tracker.update_total_repositories(total_repos)
        else:
            console.print(f"Found {total_repos} repositories")

        # Find matching PRs across all repositories
        all_similar_prs = []

        for repo_index, repository in enumerate(repositories):
            if progress_tracker:
                progress_tracker.start_repository(repository.full_name)
                progress_tracker.update_operation(f"Getting open PRs from {repository.full_name}")

            try:
                open_prs = github_client.get_open_pull_requests(repository)

                if progress_tracker:
                    progress_tracker.update_operation(f"Analyzing {len(open_prs)} PRs in {repository.full_name}")

                matching_prs_in_repo = []
                for pr in open_prs:
                    if pr.number == source_pr.number and repository.full_name == source_pr.repository_full_name:
                        continue  # Skip the source PR itself

                    # Check if PR should be considered based on override status
                    is_automation = github_client.is_automation_author(pr.user.login)
                    source_is_automation = github_client.is_automation_author(source_pr.author)

                    # If source PR is automation, only consider automation PRs
                    # If source PR is non-automation with override, only consider non-automation PRs from same author
                    if source_is_automation:
                        if not is_automation:
                            continue
                    else:
                        if is_automation or pr.user.login != source_pr.author:
                            continue

                    if progress_tracker:
                        progress_tracker.analyze_pr(pr.number, repository.full_name)

                    target_pr = PullRequestInfo(
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
                        files_changed=[],  # We'll populate this if needed
                        repository_full_name=repository.full_name,
                        html_url=pr.html_url,
                    )

                    comparison = comparator.compare_pull_requests(source_pr, target_pr)
                    if comparison.is_similar:
                        matching_prs_in_repo.append((target_pr, comparison))
                        if progress_tracker:
                            progress_tracker.found_similar_pr()

                all_similar_prs.extend(matching_prs_in_repo)

                if progress_tracker:
                    progress_tracker.complete_repository(len(matching_prs_in_repo))

            except Exception as e:
                if progress_tracker:
                    progress_tracker.add_error()
                console.print(f"Warning: Error scanning {repository.full_name}: {e}")
                continue

        # Stop progress tracker and show results
        if progress_tracker:
            progress_tracker.stop()
            summary = progress_tracker.get_summary()
            console.print(f"\n✅ Analysis completed in {summary['elapsed_time']}")
            console.print(f"📊 Analyzed {summary['total_prs_analyzed']} PRs across {summary['completed_repositories']} repositories")
            console.print(f"🔍 Found {summary['similar_prs_found']} similar PRs")
            if summary['errors_count'] > 0:
                console.print(f"⚠️  {summary['errors_count']} errors encountered during analysis")
            console.print()

        if not all_similar_prs:
            console.print("❌ No similar PRs found in the organization")

        console.print(f"Found {len(all_similar_prs)} similar PRs:")

        for target_pr, comparison in all_similar_prs:
            console.print(f"  • {target_pr.repository_full_name}#{target_pr.number}: {target_pr.title}")
            console.print(f"    Similarity: {comparison.confidence_score:.2f} - {', '.join(comparison.reasons)}")

        if dry_run:
            console.print("\n🔍 Dry run mode - no changes will be made")
            return

        # Merge similar PRs
        console.print(f"\nMerging {len(all_similar_prs)} similar PRs...")

        merged_count = 0
        for target_pr, comparison in all_similar_prs:
            repo_owner, repo_name = target_pr.repository_full_name.split("/")

            if fix:
                # Try to fix out-of-date PRs
                if target_pr.mergeable_state == "behind":
                    console.print(f"🔧 Fixing out-of-date PR {target_pr.repository_full_name}#{target_pr.number}")
                    github_client.fix_out_of_date_pr(repo_owner, repo_name, target_pr.number)

            success = _merge_single_pr(
                target_pr,
                github_client,
                merge_method,
                console
            )

            if success:
                merged_count += 1
                if progress_tracker:
                    progress_tracker.merge_success()
            else:
                if progress_tracker:
                    progress_tracker.merge_failure()

        # Finally merge the source PR
        source_repo_owner, source_repo_name = source_pr.repository_full_name.split("/")

        if fix and source_pr.mergeable_state == "behind":
            console.print(f"🔧 Fixing out-of-date source PR {source_pr.repository_full_name}#{source_pr.number}")
            github_client.fix_out_of_date_pr(source_repo_owner, source_repo_name, source_pr.number)

        console.print(f"\nMerging source PR #{source_pr.number} in {source_pr.repository_full_name}...")
        source_success = _merge_single_pr(
            source_pr,
            github_client,
            merge_method,
            console
        )

        if source_success:
            merged_count += 1
            if progress_tracker:
                progress_tracker.merge_success()
        else:
            if progress_tracker:
                progress_tracker.merge_failure()

        total_to_merge = len(all_similar_prs) + 1
        console.print(f"\n✅ Successfully merged {merged_count}/{total_to_merge} PRs")

        if progress_tracker:
            final_summary = progress_tracker.get_summary()
            console.print(f"📈 Final Results: {final_summary['prs_merged']} merged, {final_summary['merge_failures']} failed")

    except Exception as e:
        # Ensure progress tracker is stopped even if merge fails
        if progress_tracker:
            progress_tracker.stop()
        console.print(f"Error: {e}")
        raise typer.Exit(1) from e


def _display_pr_info(pr: PullRequestInfo, title: str, github_client: GitHubClient) -> None:
    """Display pull request information in a formatted table."""
    table = Table(title=title)
    table.add_column("Property", style="cyan")
    table.add_column("Value", style="green")

    # Get proper status instead of raw mergeable field
    status = github_client.get_pr_status_details(pr)

    table.add_row("Repository", pr.repository_full_name)
    table.add_row("PR Number", str(pr.number))
    table.add_row("Title", pr.title)
    table.add_row("Author", pr.author)
    table.add_row("State", pr.state)
    table.add_row("Status", status)
    table.add_row("Files Changed", str(len(pr.files_changed)))
    table.add_row("URL", pr.html_url)

    console.print(table)


def _merge_single_pr(
    pr_info: PullRequestInfo,
    github_client: GitHubClient,
    merge_method: str,
    console: Console,
) -> bool:
    """
    Merge a single pull request.

    Returns True if successfully merged, False otherwise.
    """
    repo_owner, repo_name = pr_info.repository_full_name.split("/")

    # Get initial status
    status = github_client.get_pr_status_details(pr_info)

    # Handle different types of blocks intelligently
    if pr_info.mergeable_state == "blocked" and pr_info.mergeable is True:
        # This is likely blocked by branch protection (review required, etc.)
        # Don't show "attempting anyway" message since this is expected and handleable
        pass
    elif pr_info.mergeable_state == "blocked" and pr_info.mergeable is False:
        console.print(
            f"PR {pr_info.number} is blocked by failing checks - attempting merge anyway"
        )
    elif not pr_info.mergeable:
        console.print(
            f"Skipping unmergeable PR {pr_info.number} in {pr_info.repository_full_name} ({status})"
        )
        return False

    # Approve PR
    console.print(f"Approving PR {pr_info.number} in {pr_info.repository_full_name}")
    if not github_client.approve_pull_request(repo_owner, repo_name, pr_info.number):
        console.print(f"Failed to approve PR {pr_info.number} ❌")
        return False

    # Attempt merge with retry logic for different failure conditions
    for attempt in range(MAX_RETRIES + 1):
        if attempt == 0:
            console.print(
                f"Merging PR {pr_info.number} in {pr_info.repository_full_name}"
            )
        else:
            console.print(
                f"Merging PR {pr_info.number} in {pr_info.repository_full_name} (retry {attempt})"
            )

        merge_result = github_client.merge_pull_request(
            repo_owner, repo_name, pr_info.number, merge_method
        )

        if merge_result:
            console.print(f"Successfully merged PR {pr_info.number} ✅")
            return True

        # If merge failed, check if we can fix the issue and retry
        if attempt < MAX_RETRIES:
            # Only refresh PR info if current state suggests it might be fixable
            should_retry = False

            if (
                pr_info.mergeable_state == "behind"
                or pr_info.mergeable_state == "unknown"
            ):
                # These states might benefit from refreshing and potentially fixing
                try:
                    updated_pr_info = github_client.get_pull_request_info(
                        repo_owner, repo_name, pr_info.number
                    )

                    # Check if branch is out of date and can be fixed
                    if updated_pr_info.mergeable_state == "behind":
                        console.print(
                            f"PR {pr_info.number} is out of date - updating branch and retrying"
                        )
                        if github_client.fix_out_of_date_pr(
                            repo_owner, repo_name, pr_info.number
                        ):
                            console.print(
                                f"Successfully updated PR {pr_info.number} branch ✅"
                            )
                            pr_info = updated_pr_info  # Update for next attempt
                            should_retry = True
                        else:
                            console.print(
                                f"Failed to update PR {pr_info.number} branch ❌"
                            )
                    elif updated_pr_info.mergeable_state != pr_info.mergeable_state:
                        # State changed, worth retrying with new state
                        pr_info = updated_pr_info
                        should_retry = True

                except Exception as e:
                    console.print(f"Warning: Failed to refresh PR info for retry: {e}")

            if should_retry:
                continue
            else:
                # Other types of merge failures - no point in retrying
                break

    if MAX_RETRIES > 0:
        console.print(
            f"Failed to merge PR {pr_info.number} after {MAX_RETRIES} retries ❌"
        )
    else:
        console.print(
            f"Failed to merge PR {pr_info.number} ❌"
        )
    return False


@app.command()
def scan(
    organization: str = typer.Argument(..., help="GitHub organization name to scan"),
    token: Optional[str] = typer.Option(
        None, "--token", help="GitHub token (or set GITHUB_TOKEN env var)"
    ),
    output_format: str = typer.Option(
        "table", "--format", help="Output format: table, json"
    ),
    show_copilot: bool = typer.Option(
        True, "--show-copilot/--hide-copilot", help="Show Copilot comment counts"
    ),
    show_progress: bool = typer.Option(
        True, "--progress/--no-progress", help="Show real-time progress updates"
    ),
):
    """
    Scan a GitHub organization for unmergeable pull requests.

    This command will:
    1. Scan all repositories in the organization
    2. Identify pull requests that cannot be merged
    3. Report blocking reasons (conflicts, failing checks, etc.)
    4. Count unresolved Copilot feedback comments

    Standard code review requirements are not considered blocking.
    """
    # Initialize progress tracker
    progress_tracker = None

    try:
        # Initialize GitHub client
        github_client = GitHubClient(token)

        if show_progress:
            progress_tracker = ProgressTracker(organization)
            progress_tracker.start()
            # Check if Rich display is available
            if not progress_tracker.rich_available:
                console.print(f"🔍 Scanning organization: {organization}")
                console.print("Progress updates will be shown as simple text...")
        else:
            console.print(f"🔍 Scanning organization: {organization}")
            console.print("This may take a few minutes for large organizations...")

        # Perform the scan
        scan_result = github_client.scan_organization_for_unmergeable_prs(
            organization, progress_tracker
        )

        # Stop progress tracker before displaying results
        if progress_tracker:
            progress_tracker.stop()
            if progress_tracker.rich_available:
                console.print()  # Add blank line after progress display
            else:
                console.print()  # Clear the fallback display line

            # Show scan summary
            summary = progress_tracker.get_summary()
            console.print(f"✅ Scan completed in {summary['elapsed_time']}")
            console.print(f"📊 Analyzed {summary['total_prs_analyzed']} PRs across {summary['completed_repositories']} repositories")
            if summary['errors_count'] > 0:
                console.print(f"⚠️  {summary['errors_count']} errors encountered during scan")
            console.print()  # Add blank line before results

        # Display results
        _display_scan_results(scan_result, output_format, show_copilot)

    except Exception as e:
        # Ensure progress tracker is stopped even if scan fails
        if progress_tracker:
            progress_tracker.stop()
        console.print(f"Error: {e}")
        raise typer.Exit(1) from e


def _display_scan_results(scan_result, output_format: str, show_copilot: bool):
    """Display the organization scan results."""

    if output_format == "json":
        import json
        console.print(json.dumps(scan_result.dict(), indent=2, default=str))
        return

    # Table format
    if not scan_result.unmergeable_prs:
        console.print("🎉 No unmergeable pull requests found!")
        return

    # Create summary table
    summary_table = Table(title=f"Organization Scan Summary: {scan_result.organization}")
    summary_table.add_column("Metric", style="cyan")
    summary_table.add_column("Value", style="white")

    summary_table.add_row("Total Repositories", str(scan_result.total_repositories))
    summary_table.add_row("Scanned Repositories", str(scan_result.scanned_repositories))
    summary_table.add_row("Total Open PRs", str(scan_result.total_prs))
    summary_table.add_row("Unmergeable PRs", str(len(scan_result.unmergeable_prs)))

    if scan_result.errors:
        summary_table.add_row("Errors", str(len(scan_result.errors)), style="red")

    console.print(summary_table)
    console.print()

    # Create detailed unmergeable PRs table
    pr_table = Table(title="Unmergeable Pull Requests")
    pr_table.add_column("Repository", style="cyan")
    pr_table.add_column("PR", style="white")
    pr_table.add_column("Title", style="white", max_width=40)
    pr_table.add_column("Author", style="yellow")
    pr_table.add_column("Blocking Reasons", style="red")

    if show_copilot:
        pr_table.add_column("Copilot Comments", style="blue")

    for pr in scan_result.unmergeable_prs:
        reasons = [reason.description for reason in pr.reasons]
        reasons_text = "\n".join(reasons) if reasons else "Unknown"

        row_data = [
            pr.repository,
            f"#{pr.pr_number}",
            pr.title,
            pr.author,
            reasons_text
        ]

        if show_copilot:
            row_data.append(str(pr.copilot_comments_count))

        pr_table.add_row(*row_data)

    console.print(pr_table)

    # Show errors if any
    if scan_result.errors:
        console.print()
        error_table = Table(title="Errors Encountered During Scan")
        error_table.add_column("Error", style="red")

        for error in scan_result.errors:
            error_table.add_row(error)

        console.print(error_table)


if __name__ == "__main__":
    app()
