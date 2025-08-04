<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

# Dependamerge

Automatically merge similar pull requests across GitHub organizations,
supporting both automation tools (like Dependabot, pre-commit.ci, Renovate)
and regular GitHub users.

## Overview

Dependamerge analyzes a source pull request and finds similar pull requests
across all repositories in the same GitHub organization. It then automatically
approves and merges the matching PRs, saving time on routine dependency updates,
automated maintenance tasks, and coordinated changes across all repositories.

**Supports two types of pull requests:**

- **Automation PRs**: From tools like Dependabot, pre-commit.ci, Renovate
  (original functionality)
- **Non-Automation PRs**: From regular GitHub users with SHA-based security
  validation (new feature)

## Features

- **Automated PR Detection**: Identifies pull requests created by popular
  automation tools
- **Non-Automation PR Support**: Handles PRs from regular GitHub users with
  SHA-based security validation
- **Smart Matching**: Uses content similarity algorithms to match related PRs
  across repositories
- **Bulk Operations**: Approve and merge related similar PRs with a single command
- **Security Features**: SHA-based authentication for non-automation PRs ensures
  authorized bulk merges
- **Dry Run Mode**: Preview what changes will apply without modifications
- **Rich CLI Output**: Beautiful terminal output with progress indicators and tables

## Supported Automation Tools

- Dependabot
- pre-commit.ci
- Renovate
- GitHub Actions
- Allcontributors

## Installation

```bash
# Install from source
git clone <repository-url>
cd dependamerge
pip install -e .

# Or install dependencies directly
pip install typer requests PyGithub rich pydantic
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

### Automation PRs (Original Functionality)

For pull requests from automation tools like Dependabot, pre-commit.ci, and Renovate:

```bash
dependamerge https://github.com/lfreleng-actions/python-project-name-action/pull/22
```

### Non-Automation PRs (New Feature)

For pull requests from regular GitHub users, a two-step process ensures security:

#### Step 1: Get the required SHA

```bash
dependamerge https://github.com/owner/repo/pull/123
# Output: To merge this and similar PRs, run again with: --override a1b2c3d4e5f6g7h8
```

#### Step 2: Use the SHA to proceed

```bash
dependamerge https://github.com/owner/repo/pull/123 --override a1b2c3d4e5f6g7h8
```

The SHA hash generates based on:

- The PR author's GitHub username
- The first line of the commit message
- This ensures PRs from the same author with matching commits can be bulk merged

### Basic Usage

```bash
dependamerge https://github.com/lfreleng-actions/python-project-name-action/pull/22
```

### Dry Run (Preview Mode)

```bash
dependamerge https://github.com/owner/repo/pull/123 --dry-run
```

### Custom Options

```bash
dependamerge https://github.com/owner/repo/pull/123 \
  --threshold 0.9 \
  --merge-method squash \
  --fix \
  --token your_github_token
```

### Command Options

- `--dry-run`: Show what changes will apply without making them
- `--threshold FLOAT`: Similarity threshold for matching PRs (0.0-1.0, default: 0.8)
- `--merge-method TEXT`: Merge method - merge, squash, or rebase (default: merge)
- `--fix`: Automatically fix out-of-date branches before merging
- `--token TEXT`: GitHub token (alternative to GITHUB_TOKEN env var)
- `--override TEXT`: SHA hash to override non-automation PR restriction

## How It Works

### For Automation PRs

1. **Parse Source PR**: Analyzes the provided pull request URL and extracts metadata
2. **Validation**: Ensures the PR is from a recognized automation tool
3. **Organization Scan**: Lists all repositories in the same GitHub organization
4. **PR Discovery**: Finds all open pull requests in each repository
5. **Content Matching**: Compares PRs using different similarity metrics:
   - Title similarity (normalized to remove version numbers)
   - File change patterns
   - Author matching
6. **Approval & Merge**: For matching PRs above the threshold:
   - Adds an approval review
   - Merges the pull request
7. **Source PR Merge**: Merges the original source PR that served as the baseline

### For Non-Automation PRs

1. **Parse Source PR**: Analyzes the provided pull request URL and extracts metadata
2. **Non-Automation Detection**: Identifies that PR is from a regular user
3. **SHA Generation**: Creates unique SHA based on author + commit message
4. **Override Validation**: If `--override` provided, validates SHA matches expectations
5. **Author-Specific Scan**: Finds PRs from the same author
6. **Content Matching**: Same similarity algorithms as automation PRs
7. **Approval & Merge**: Merges matching PRs from the same author

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

### Dependabot PR

```bash
# Merge a Dependabot dependency update across all repos
dependamerge https://github.com/myorg/repo1/pull/45
```

### pre-commit.ci PR

```bash
# Merge pre-commit hook updates
dependamerge https://github.com/myorg/repo1/pull/12 --threshold 0.85
```

### Non-Automation User PR

```bash
# First run to get the SHA
dependamerge https://github.com/myorg/repo1/pull/89
# Output: To merge this and similar PRs, run again with: --override f1a2b3c4d5e6f7g8

# Second run with the override SHA
dependamerge https://github.com/myorg/repo1/pull/89 --override f1a2b3c4d5e6f7g8
```

### Dry Run with Fix Option

```bash
# See what changes will apply and automatically fix out-of-date branches
dependamerge https://github.com/myorg/repo1/pull/78 --dry-run --fix --threshold 0.9
```

### Non-Automation PR Example

```bash
# Step 1: Get the SHA for the non-automation PR
dependamerge https://github.com/owner/repo/pull/123

# Step 2: Merge using the obtained SHA
dependamerge https://github.com/owner/repo/pull/123 --override a1b2c3d4e5f6g7h8
```

## Safety Features

### For All PRs

- **Mergeable Check**: Verifies PRs are in a mergeable state before attempting merge
- **Auto-Fix**: Automatically update out-of-date branches when using `--fix` option
- **Detailed Status**: Shows specific reasons why PRs cannot merge
  (conflicts, blocked by checks, etc.)
- **Similarity Threshold**: Configurable confidence threshold prevents
  incorrect matches
- **Dry Run Mode**: Always test with `--dry-run` first
- **Detailed Logging**: Shows which PRs match and why they match

### Security for Automation PRs

- **Automation-Focused**: Processes PRs from recognized automation tools

### Security for Non-Automation PRs

- **SHA-Based Authentication**: Requires unique SHA hash for each author/commit combination
- **Author Isolation**: Merges PRs from the same author as source PR
- **Commit Binding**: SHA changes if commit message changes, preventing replay attacks
- **No Cross-Author Attacks**: One author's SHA cannot work for another
  author's PRs

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

```bash
git clone <repository-url>
cd dependamerge
pip install -e ".[dev]"
```

### Running Tests

```bash
pytest
```

### Code Quality

```bash
# Format code
black src tests

# Lint code
flake8 src tests

# Type checking
mypy src
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

## License

MIT License - see LICENSE file for details.

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

- Check the command help: `dependamerge --help`
- Enable verbose output with environment variables
- Review the similarity scoring in dry-run mode

## Security Considerations

- Store GitHub tokens securely (environment variables, not in code)
- Use tokens with minimal required permissions
- Rotate access tokens periodically
- Review PR changes in dry-run mode first
- Be cautious with low similarity thresholds
