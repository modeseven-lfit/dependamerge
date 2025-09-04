<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

# Dependamerge

Find blocked pull requests in GitHub organizations and automatically merge
similar pull requests across GitHub organizations, supporting both automation
tools (like Dependabot, pre-commit.ci, Renovate) and regular GitHub users.

## Overview

Dependamerge provides two main functions:

1. **Finding Blocked PRs**: Check entire GitHub organizations to identify
   pull requests with conflicts, failing checks, or other blocking issues
2. **Automated Merging**: Analyze a source pull request and find similar pull
   requests across all repositories in the same GitHub organization, then
   automatically approve and merge the matching PRs

This saves time on routine dependency updates, maintenance tasks, and
coordinated changes across all repositories while providing visibility into
unmergeable PRs that need attention.

**Works with any pull request** regardless of author, automation tool, or origin.

## Features

### Finding Blocked PRs

- **Comprehensive PR Analysis**: Checks all repositories in a GitHub
  organization for unmergeable pull requests
- **Blocking Reason Detection**: Identifies specific reasons preventing PR
  merges (conflicts, failing checks, blocked reviews)
- **Copilot Integration**: Counts unresolved GitHub Copilot feedback comments
  (column shown when present)
- **Smart Filtering**: Excludes standard code review requirements, focuses on
  technical blocking issues
- **Detailed Reporting**: Provides comprehensive tables and summaries of
  problematic PRs
- **Real-time Progress**: Live progress display shows checking status and
  current operations

### Automated Merging

- **Universal PR Support**: Works with any pull request regardless of author
  or automation tool
- **Smart Matching**: Uses content similarity algorithms to match related PRs
  across repositories
- **Bulk Operations**: Approve and merge related similar PRs with a single command
- **Security Features**: SHA-based authentication for non-automation PRs
  ensures authorized bulk merges
- **Dry Run Mode**: Preview what changes will apply without modifications

### General Features

- **Rich CLI Output**: Beautiful terminal output with progress indicators and tables
- **Real-time Progress**: Live progress updates for both checking and merge operations
- **Output Formats**: Support for table and JSON output formats
- **Error Handling**: Graceful handling of API rate limits and repository
  access issues

## Supported Pull Requests

- Any pull request from any author
- Manual pull requests from developers
- Automation tool pull requests (Dependabot, Renovate, etc.)
- Bot-generated pull requests
- Coordinated changes across repositories

## Installation (uv + hatch)

This project now uses:

- hatchling + hatch-vcs for dynamic (tag-based) versioning
- uv for environment + dependency management (produces/consumes `uv.lock`)

### Quick Start (Run Without Cloning)

Use `uvx` to run the latest published version directly from PyPI
(no virtualenv management needed):

```bash
# Show help (latest release)
uvx dependamerge --help

# Run a specific tagged release
uvx dependamerge==0.1.0 https://github.com/owner/repo/pull/123
```

### Local Development Install

```bash
# 1. Install uv (if not already installed)
# macOS/Linux (script):
curl -LsSf https://astral.sh/uv/install.sh | sh
# or with pipx:
pipx install uv

# 2. Clone the repository
git clone <repository-url>
cd dependamerge

# 3. Create & activate a virtual environment (optional but recommended)
uv venv .venv
source .venv/bin/activate  # (On Windows: .venv\Scripts\activate)

# 4. Install project + dev dependencies (uses dependency group 'dev')
uv sync --group dev
```

The first sync will generate `uv.lock`. Commit that file to ensure reproducible builds.

### Editable Workflow

`uv sync` installs the project in editable (PEP 660) mode automatically.
After making changes you can run:

```bash
uv run dependamerge --help
```

### Building & Publishing

Dynamic version comes from Git tags (e.g. tag `v0.2.0` → version `0.2.0`):

```bash
# Build wheel + sdist
uv build

# (Optional) Inspect dist/
ls dist/

# Publish to PyPI (ensure you have credentials configured)
uv publish
```

If you build before tagging, a local scheme like `0.0.0+local`
(or similar) may appear—tag first for clean releases.

### Updating / Adding Dependencies

Edit `pyproject.toml` and then:

```bash
uv sync
```

To add a dev dependency:

```bash
uv add --group dev pytest-cov
```

### Running a One-Off Version (Isolation)

```bash
# Run a specific version in an ephemeral environment
uvx dependamerge==0.1.0 --help
```

## Authentication

You need a GitHub personal access token with appropriate permissions:

1. Go to GitHub Settings → Developer settings → Personal access tokens
2. Create a token with these scopes:
   - `repo` (for private repositories)
   - `public_repo` (for public repositories)
   - `read:org` (to list organization repositories)

Set the token as an environment variable:

```bash
export GITHUB_TOKEN=your_token_here
```

Or pass it directly to the command using `--token`.

## Usage

### Finding Blocked PRs (New Feature)

Find blocked pull requests in an entire GitHub organization:

```bash
# Basic organization check for blocked PRs
dependamerge blocked myorganization

# Check with JSON output
dependamerge blocked myorganization --format json

# Disable real-time progress display
dependamerge blocked myorganization --no-progress
```

The blocked command will:

- Analyze all repositories in the organization
- Identify PRs with technical blocking issues
- Report blocking reasons (merge conflicts, failing workflows, etc.)
- Count unresolved GitHub Copilot feedback comments (displayed when present)
- Exclude standard code review requirements from blocking reasons

### Basic Pull Request Merging

For any pull request from any author:

```bash
dependamerge merge https://github.com/lfreleng-actions/python-project-name-action/pull/22
```

### Optional Security Validation

For extra security, you can use the --override flag with SHA-based validation:

```bash
dependamerge merge https://github.com/owner/repo/pull/123 \
  --override a1b2c3d4e5f6g7h8
```

The SHA hash derives from:

- The PR author's GitHub username
- The first line of the commit message
- This provides an extra layer of validation for sensitive operations

### Basic Merge Usage

```bash
dependamerge merge \
  https://github.com/lfreleng-actions/python-project-name-action/pull/22
```

### Dry Run (Preview Mode)

```bash
dependamerge merge https://github.com/owner/repo/pull/123 --dry-run
```

### Custom Merge Options

```bash
dependamerge merge https://github.com/owner/repo/pull/123 \
  --threshold 0.9 \
  --merge-method squash \
  --fix \
  --no-progress \
  --token your_github_token
```

### Command Options

#### Blocked Command Options

- `--format TEXT`: Output format - table or json (default: table)

- `--progress/--no-progress`: Show real-time progress updates (default: progress)
- `--token TEXT`: GitHub token (alternative to GITHUB_TOKEN env var)

#### Merge Command Options

- `--dry-run`: Show what changes will apply without making them
- `--threshold FLOAT`: Similarity threshold for matching PRs (0.0-1.0,
  default: 0.8)
- `--merge-method TEXT`: Merge method - merge, squash, or rebase (default: merge)
- `--fix`: Automatically fix out-of-date branches before merging
- `--progress/--no-progress`: Show real-time progress updates (default: progress)
- `--token TEXT`: GitHub token (alternative to GITHUB_TOKEN env var)
- `--override TEXT`: SHA hash for extra security validation

## How It Works

### Pull Request Processing

1. **Parse Source PR**: Analyzes the provided pull request URL and extracts metadata
2. **Organization Check**: Lists all repositories in the same GitHub organization
3. **PR Discovery**: Finds all open pull requests in each repository
4. **Content Matching**: Compares PRs using different similarity metrics:
   - Title similarity (normalized to remove version numbers)
   - File change patterns
   - Author matching
5. **Optional Validation**: If `--override` provided, validates SHA for extra security
6. **Approval & Merge**: For matching PRs above the threshold:
   - Adds an approval review
   - Merges the pull request
7. **Source PR Merge**: Merges the original source PR that served as the baseline

## Similarity Matching

The tool uses different algorithms to determine if PRs are similar:

### Title Normalization

- Removes version numbers (e.g., "1.2.3", "v2.0.0")
- Removes commit hashes
- Removes dates
- Normalizes whitespace

### File Change Analysis

- Compares changed filenames using Jaccard similarity
- Accounts for path normalization
- Ignores version-specific filename differences

### Confidence Scoring

Combines different factors:

- Title similarity score
- File change similarity score
- Author matching (same automation tool)

## Examples

### Example: Finding Blocked PRs

```bash
# Check organization for blocked PRs
dependamerge blocked myorganization

# Get detailed JSON output
dependamerge blocked myorganization --format json > unmergeable_prs.json

# Check without progress display
dependamerge blocked myorganization --no-progress
```

### Example: Automated Merging

#### Dependency Update PR

```bash
# Merge a dependency update across all repos
dependamerge merge https://github.com/myorg/repo1/pull/45
```

#### Documentation Update PR

```bash
# Merge documentation updates
dependamerge merge https://github.com/myorg/repo1/pull/12 --threshold 0.85
```

#### Feature PR with Security Validation

```bash
# Merge with optional security validation
dependamerge merge https://github.com/myorg/repo1/pull/89 \
  --override f1a2b3c4d5e6f7g8
```

#### Dry Run with Fix Option

```bash
# See what changes will apply and automatically fix out-of-date branches
dependamerge merge https://github.com/myorg/repo1/pull/78 \
  --dry-run --fix --threshold 0.9 --progress
```

## Safety Features

### For All PRs

- **Mergeable Check**: Verifies PRs are in a mergeable state before attempting merge
- **Auto-Fix**: Automatically update out-of-date branches when using `--fix` option
- **Detailed Status**: Shows specific reasons preventing PR merges (conflicts,
  blocked by checks, etc.)
- **Similarity Threshold**: Configurable confidence threshold prevents incorrect
  matches
- **Dry Run Mode**: Always test with `--dry-run` first
- **Detailed Logging**: Shows which PRs match and why they match

### Security for All PRs

- **SHA-Based Validation**: Provides unique SHA hash for security
- **Author Isolation**: When using SHA validation, processes PRs from the same
  author as source PR
- **Commit Binding**: SHA changes if commit message changes, preventing replay
  attacks
- **Cross-Author Protection**: When enabled, one author's SHA cannot work for
  another author's PRs

## Enhanced URL Support

The tool now supports GitHub PR URLs with path segments:

```bash
# These URL formats now work:
dependamerge https://github.com/owner/repo/pull/123
dependamerge https://github.com/owner/repo/pull/123/
dependamerge https://github.com/owner/repo/pull/123/files
dependamerge https://github.com/owner/repo/pull/123/commits
dependamerge https://github.com/owner/repo/pull/123/files/diff
```

This enhancement allows you to copy URLs directly from GitHub's PR pages
without worrying about the specific tab you're viewing.

## Development

### Setup Development Environment

(If you already followed the Installation section, you can skip these repeated steps.)

```bash
git clone <repository-url>
cd dependamerge
uv venv .venv
source .venv/bin/activate
uv sync --group dev
```

The `dev` dependency group mirrors the legacy `.[dev]` extra.

### Running Tests

```bash
uv run pytest
```

You can pass args as usual:

```bash
uv run pytest -k "similarity and not slow" -vv
```

### Code Quality

```bash
# Format (Black)
uv run black src tests

# Lint (Flake8 – still present)
uv run flake8 src tests

# Type checking
uv run mypy src

# (Optional) Ruff (if/when added)
# uv run ruff check .
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

## License

Apache-2.0 License - see LICENSE file for details.

## Troubleshooting

### Common Issues

#### Authentication Error

```text
Error: GitHub token needed
```

Solution: Set `GITHUB_TOKEN` environment variable or use `--token` flag.

#### Permission Error

```text
Failed to fetch organization repositories
```

Solution: Ensure your token has `read:org` scope.

#### No Similar PRs Found

- Check that other repositories have open automation PRs
- Try lowering the similarity threshold with `--threshold 0.7`
- Use `--dry-run` to see detailed matching information

#### Merge Failures

- Ensure PRs are in mergeable state (no conflicts)
- Check that you have write permissions to the target repositories
- Verify the repository settings permit the merge method

### Getting Help

- Check the command help (local dev): `uv run dependamerge --help`
- For PyPI usage: `uvx dependamerge --help`
- Enable verbose output with environment variables
- Review similarity scoring in dry-run mode (`--dry-run`)

## Security Considerations

- Store GitHub tokens securely (environment variables, not in code)
- Use tokens with minimal required permissions
- Rotate access tokens periodically
- Review PR changes in dry-run mode first
- Be cautious with low similarity thresholds
