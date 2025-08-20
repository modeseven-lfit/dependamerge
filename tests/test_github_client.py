# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from unittest.mock import Mock, patch

import pytest

from dependamerge.github_client import GitHubClient
from dependamerge.models import PullRequestInfo


class TestGitHubClient:
    @patch.dict("os.environ", {"GITHUB_TOKEN": "test_token"})
    def test_init_with_env_token(self):
        client = GitHubClient()
        assert client.token == "test_token"

    def test_init_with_explicit_token(self):
        client = GitHubClient(token="explicit_token")
        assert client.token == "explicit_token"

    def test_init_without_token_raises_error(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="GitHub token is required"):
                GitHubClient()

    def test_parse_pr_url_valid(self):
        client = GitHubClient(token="test_token")
        owner, repo, pr_number = client.parse_pr_url(
            "https://github.com/lfreleng-actions/python-project-name-action/pull/22"
        )
        assert owner == "lfreleng-actions"
        assert repo == "python-project-name-action"
        assert pr_number == 22

    def test_parse_pr_url_with_trailing_slash(self):
        client = GitHubClient(token="test_token")
        owner, repo, pr_number = client.parse_pr_url(
            "https://github.com/owner/repo/pull/123/"
        )
        assert owner == "owner"
        assert repo == "repo"
        assert pr_number == 123

    def test_parse_pr_url_invalid(self):
        client = GitHubClient(token="test_token")
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            client.parse_pr_url("https://invalid-url.com")

    def test_parse_pr_url_with_files_path(self):
        client = GitHubClient(token="test_token")
        owner, repo, pr_number = client.parse_pr_url(
            "https://github.com/lfreleng-actions/python-project-name-action/pull/23/files"
        )
        assert owner == "lfreleng-actions"
        assert repo == "python-project-name-action"
        assert pr_number == 23

    def test_parse_pr_url_with_commits_path(self):
        client = GitHubClient(token="test_token")
        owner, repo, pr_number = client.parse_pr_url(
            "https://github.com/owner/repo/pull/456/commits"
        )
        assert owner == "owner"
        assert repo == "repo"
        assert pr_number == 456

    def test_parse_pr_url_with_multiple_path_segments(self):
        client = GitHubClient(token="test_token")
        owner, repo, pr_number = client.parse_pr_url(
            "https://github.com/org/repository/pull/789/files/diff"
        )
        assert owner == "org"
        assert repo == "repository"
        assert pr_number == 789

    @patch("dependamerge.github_async.GitHubAsync")
    def test_get_pull_request_info(self, mock_async_class):
        # Setup mocks
        mock_async = Mock()
        mock_async_class.return_value.__aenter__.return_value = mock_async

        # Mock PR data
        pr_data = {
            "number": 22,
            "title": "Test PR",
            "body": "Test body",
            "user": {"login": "dependabot[bot]"},
            "head": {"sha": "abc123", "ref": "update-deps"},
            "base": {"ref": "main"},
            "state": "open",
            "mergeable": True,
            "mergeable_state": "clean",
            "html_url": "https://github.com/owner/repo/pull/22",
        }

        # Mock file data
        file_data = {
            "filename": "requirements.txt",
            "additions": 1,
            "deletions": 1,
            "changes": 2,
            "status": "modified",
        }

        # Mock the async get method
        async def mock_get(*args, **kwargs):
            return pr_data

        mock_async.get = mock_get

        # Create an async iterator for get_paginated
        async def async_iterator():
            yield [file_data]

        mock_async.get_paginated.return_value = async_iterator()

        client = GitHubClient(token="test_token")
        pr_info = client.get_pull_request_info("owner", "repo", 22)

        assert isinstance(pr_info, PullRequestInfo)
        assert pr_info.number == 22
        assert pr_info.title == "Test PR"
        assert pr_info.author == "dependabot[bot]"
        assert len(pr_info.files_changed) == 1
        assert pr_info.files_changed[0].filename == "requirements.txt"

    def test_is_automation_author(self):
        client = GitHubClient(token="test_token")

        assert client.is_automation_author("dependabot[bot]")
        assert client.is_automation_author("pre-commit-ci[bot]")
        assert client.is_automation_author("renovate[bot]")
        assert not client.is_automation_author("human-user")
        assert not client.is_automation_author("random-bot")

    @patch("dependamerge.github_async.GitHubAsync")
    def test_get_pull_request_commits(self, mock_async_class):
        mock_async = Mock()
        mock_async_class.return_value.__aenter__.return_value = mock_async

        # Mock commit data
        commit_data = [
            {
                "commit": {
                    "message": "Fix bug in authentication\n\nDetailed description"
                }
            },
            {"commit": {"message": "Update tests"}},
        ]

        # Create an async iterator for get_paginated
        async def async_iterator():
            yield commit_data

        mock_async.get_paginated.return_value = async_iterator()

        client = GitHubClient(token="test_token")
        commits = client.get_pull_request_commits("owner", "repo", 22)

        assert len(commits) == 2
        assert commits[0] == "Fix bug in authentication\n\nDetailed description"
        assert commits[1] == "Update tests"

    @patch("dependamerge.github_async.GitHubAsync")
    def test_get_pull_request_commits_empty(self, mock_async_class):
        mock_async = Mock()
        mock_async_class.return_value.__aenter__.return_value = mock_async

        # Create an async iterator for empty commits
        async def async_iterator():
            yield []

        mock_async.get_paginated.return_value = async_iterator()

        client = GitHubClient(token="test_token")
        commits = client.get_pull_request_commits("owner", "repo", 22)

        assert len(commits) == 0
        assert commits == []
