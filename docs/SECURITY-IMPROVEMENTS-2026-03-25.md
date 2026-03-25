<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

# Security Improvements Plan — 2026-03-25

This document records the findings from a security audit of the
dependamerge codebase, triggered by GitHub Dependabot and CodeQL
code-scanning alerts. It describes each reported vulnerability,
analyses its root cause within our code, and defines a concrete
remediation plan for a follow-up agentic coding session.

---

## Table of Contents

1. [Dependabot Alert: Pygments ReDoS (CVE-2026-4539)](#1-dependabot-alert-pygments-redos-cve-2026-4539)
2. [CodeQL Alerts #33–#28: Incomplete URL Substring Sanitization](#2-codeql-alerts-3328-incomplete-url-substring-sanitization)
3. [CodeQL Alerts #27–#26: Clear-Text Logging of Sensitive Information](#3-codeql-alerts-2726-clear-text-logging-of-sensitive-information)
4. [Codebase Audit: Additional Findings](#4-codebase-audit-additional-findings)
5. [Remediation Plan](#5-remediation-plan)
6. [Verification Criteria](#6-verification-criteria)

---

## 1. Dependabot Alert: Pygments ReDoS (CVE-2026-4539)

<!-- markdownlint-disable MD013 -->

| Field              | Value                                                    |
| ------------------ | -------------------------------------------------------- |
| **Advisory**       | GHSA-5239-wwwm-4pmq / CVE-2026-4539                      |
| **Severity**       | LOW (CVSS v3.1: 3.3 / CVSS v4.0: 1.9)                    |
| **Component**      | `pygments/lexers/archetype.py` — `AdlLexer`              |
| **Affected**       | All versions up to and including 2.19.2                  |
| **Our dependency** | Transitive — `pygments` pulled in by `rich`              |
| **Our version**    | 2.19.2 (pinned in `uv.lock`)                             |
| **Attack vector**  | Local only; requires feeding crafted input to `AdlLexer` |

<!-- markdownlint-enable MD013 -->

### Nature of the Problem

A regular expression in the Archetype Definition Language lexer uses
nested repeating quantifiers for GUID matching:

```text
(\d|[a-fA-F])+((\d|[a-fA-F])+){3,}
```

This causes catastrophic backtracking (ReDoS) on crafted hex-like
input strings. A proof-of-concept demonstrates a 4–8 second hang.

### Upstream Status

<!-- markdownlint-disable MD013 -->

| Resource                                                        | Status                                           |
| --------------------------------------------------------------- | ------------------------------------------------ |
| [Issue #3058](https://github.com/pygments/pygments/issues/3058) | Open — original report filed 2026-03-07          |
| [Issue #3065](https://github.com/pygments/pygments/issues/3065) | Open — security advisory tracking                |
| [PR #3064](https://github.com/pygments/pygments/pull/3064)      | Open — community fix, not yet reviewed or merged |
| PyPI `fixed_in` field                                           | Empty — no patched release exists                |

<!-- markdownlint-enable MD013 -->

### Impact Assessment for Dependamerge

**Negligible.** Dependamerge does not directly use Pygments, nor does
it process untrusted Archetype Definition Language input. The package
is a transitive dependency via `rich` (terminal UI library). The
`AdlLexer` is never invoked in any dependamerge code path.

### Decision

**No action required at this time.** We cannot bump to a fixed version
because none exists. When Pygments publishes a patched release:

1. Run `uv lock --upgrade-package pygments`
2. Verify tests pass
3. Commit the updated lock file

This item should be tracked and revisited when the upstream fix lands.

---

## 2. CodeQL Alerts #33–#28: Incomplete URL Substring Sanitization

**CodeQL rule:** `py/incomplete-url-substring-sanitization`
**Severity:** High (CVSS 7.8)

### Alerts Summary

<!-- markdownlint-disable MD013 -->

| Alert | File                             | Line | Code Pattern                                         |
| ----- | -------------------------------- | ---- | ---------------------------------------------------- |
| #33   | `src/dependamerge/url_parser.py` | 128  | `"github.com" in host or "github" in host`           |
| #32   | `tests/test_netrc.py`            | 199  | Test string containing hostname with substring check |
| #31   | `tests/test_netrc.py`            | 198  | Test string containing hostname with substring check |
| #30   | `tests/test_netrc.py`            | 119  | Test string containing hostname with substring check |
| #29   | `tests/test_gerrit_client.py`    | 515  | `"gerrit.example.org" in client.base_url`            |
| #28   | `tests/test_gerrit_client.py`    | 190  | `"gerrit.example.org" in repr_str`                   |

<!-- markdownlint-enable MD013 -->

### Root Cause Analysis

The core issue is in `src/dependamerge/url_parser.py`, functions
`_is_github_url()` and `_is_gerrit_url()`. These use Python's `in`
operator to check whether a hostname substring appears within a
host string:

```python
# url_parser.py line 128 — VULNERABLE
if "github.com" in host or "github" in host:
    return True
```

```python
# url_parser.py line 149–153 — VULNERABLE
if "gerrit" in host:
    if "/c/" in path or path.startswith("/changes/"):
        return True
```

#### Why This Is Dangerous

A substring check on a URL host component can be bypassed by
crafted hostnames:

- `"github.com" in "evil-github.com.attacker.net"` → `True`
- `"github" in "not-a-github-server.evil.com"` → `True`
- `"gerrit" in "my-gerrit-exploit.evil.org"` → `True`

An attacker who can influence the URL being processed could direct
the tool to treat a malicious server as a trusted GitHub or Gerrit
instance, potentially leaking credentials or triggering operations
against an attacker-controlled endpoint.

#### Test File Alerts

The test file alerts (#28–#32) are false positives in the sense
that they test string containment on `repr()` output or
`client.base_url` — standard assertion patterns. However, they
reflect the same underlying anti-pattern and should be updated to
use precise assertions for consistency, clarity, and to avoid
re-triggering the scanner.

### Correct Approach

The fix must use `urllib.parse.urlparse()` to extract the actual
hostname component, then validate using:

1. **Exact equality** — `hostname == "github.com"`
2. **Subdomain matching with leading dot** —
   `hostname.endswith(".github.com")`

This prevents all bypass vectors because the hostname is
structurally isolated by the URL parser.

### Affected Code Locations

#### Production Code

<!-- markdownlint-disable MD013 -->

| File               | Function           | Lines   | Issue                                                  |
| ------------------ | ------------------ | ------- | ------------------------------------------------------ |
| `url_parser.py`    | `_is_github_url()` | 118–135 | `"github.com" in host` and `"github" in host`          |
| `url_parser.py`    | `_is_gerrit_url()` | 138–160 | `"gerrit" in host`                                     |
| `github_client.py` | `parse_pr_url()`   | 35–50   | `"github.com" not in url` (substring check on raw URL) |

<!-- markdownlint-enable MD013 -->

#### Test Code

<!-- markdownlint-disable MD013 -->

| File                          | Lines         | Issue                                    |
| ----------------------------- | ------------- | ---------------------------------------- |
| `tests/test_netrc.py`         | 119, 198, 199 | `in` assertions on hostname strings      |
| `tests/test_gerrit_client.py` | 190, 515      | `in` assertions on `base_url` / `repr()` |

<!-- markdownlint-enable MD013 -->

---

## 3. CodeQL Alerts #27–#26: Clear-Text Logging of Sensitive Information

**CodeQL rule:** `py/clear-text-logging-sensitive-data`
**Severity:** High (CVSS 7.5)

### Clear-Text Logging Alerts Summary

<!-- markdownlint-disable MD013 -->

| Alert | File                        | Line | Code Pattern                                 |
| ----- | --------------------------- | ---- | -------------------------------------------- |
| #27   | `src/dependamerge/netrc.py` | 791  | `log.debug(...)` with credential source info |
| #26   | `src/dependamerge/netrc.py` | 773  | `log.debug(...)` with credential source info |

<!-- markdownlint-enable MD013 -->

### Clear-Text Logging Root Cause

In `resolve_gerrit_credentials()`, debug log messages include
parameters that CodeQL's taint analysis traces back to sensitive
data sources (environment variables, CLI arguments, netrc file
contents). The flagged lines are:

```python
# netrc.py line 773 — FLAGGED
log.debug(
    "Using credentials from environment variables %s/%s",
    env_username_var,
    env_password_var,
)
```

```python
# netrc.py line 791 — FLAGGED
log.debug(
    "Using credentials from fallback environment variables %s/%s",
    fallback_env_username_var,
    fallback_env_password_var,
)
```

While these specific lines log **variable names** (e.g.,
`"GERRIT_USERNAME"`) rather than **variable values**, CodeQL's
data-flow analysis flags them because the string values originate
from function parameters that share a taint path with credential
retrieval operations. Additionally, there are nearby lines where
`netrc_creds.login` (a username) is logged:

```python
# netrc.py line 747–750
log.debug(
    "Using credentials from .netrc for %s (login: %s) in %s",
    normalized_host,
    netrc_creds.login,
    netrc_path,
)
```

### Broader Audit: Credential Handling in the Codebase

A full audit of the codebase reveals that the **only** truly
sensitive credential handled by dependamerge is:

1. **`GITHUB_TOKEN`** — GitHub Personal Access Token, sourced from:
   - `--token` CLI flag
   - `GITHUB_TOKEN` environment variable

2. **Gerrit HTTP credentials** — sourced from:
   - `--username` / `--password` CLI flags
   - `GERRIT_USERNAME` / `GERRIT_PASSWORD` environment variables
   - `GERRIT_HTTP_USER` / `GERRIT_HTTP_PASSWORD` environment variables
   - `.netrc` file entries

3. **Netrc passwords** — stored in `.netrc` file entries

#### Current Protections (Already in Place)

<!-- markdownlint-disable MD013 MD060 -->

| Protection                                          | Location                   | Status  |
| --------------------------------------------------- | -------------------------- | ------- |
| `GerritCredentials.__repr__()` masks password       | `netrc.py:100–106`         | ✅ Good |
| `NetrcCredentials.__repr__()` masks password        | `netrc.py:133–138`         | ✅ Good |
| `GerritRestClient.__repr__()` uses `_mask_secret()` | `gerrit/client.py:422–427` | ✅ Good |
| `_mask_secret()` helper for partial masking         | `gerrit/client.py:87–93`   | ✅ Good |
| `git_ops._redact()` scrubs tokens from git output   | `git_ops.py:74–85`         | ✅ Good |
| `GitError` redacts stdout/stderr in `__init__`      | `git_ops.py:134–141`       | ✅ Good |
| `run_git()` logs only redacted command strings      | `git_ops.py:213`           | ✅ Good |
| Verbose mode scopes DEBUG to `dependamerge.*` only  | `cli.py:664–669`           | ✅ Good |

<!-- markdownlint-enable MD013 MD060 -->

#### Gaps Identified

<!-- markdownlint-disable MD013 -->

| Gap                                                                       | Location              | Risk    |
| ------------------------------------------------------------------------- | --------------------- | ------- |
| No `__repr__` on `GitHubClient` — `self.token` is a plain `str` attribute | `github_client.py:30` | Low     |
| No `__repr__` on `GitHubAsync` — `self.token` is a plain `str` attribute  | `github_async.py:215` | Low     |
| No `__repr__` on `AsyncMergeManager` — `self.token` stored                | `merge_manager.py:94` | Low     |
| No `__repr__` on `AsyncCloseManager` — `self.token` stored                | `close_manager.py:60` | Low     |
| `GitError.args_vec` stores raw (unredacted) command args                  | `git_ops.py:141`      | Low-Med |
| `netrc_creds.login` logged in debug output                                | `netrc.py:749`        | Low     |
| Environment variable names logged near credential retrieval               | `netrc.py:773,791`    | Low     |
| `httpx` exception `.request.headers` contains auth bearer token           | `github_async.py:548` | Low     |

<!-- markdownlint-enable MD013 -->

---

## 4. Codebase Audit: Additional Findings

### 4.1. `github_client.py` — URL Validation

The `parse_pr_url()` method at line 35 uses a substring check:

```python
if len(parts) < 7 or "github.com" not in url or "pull" not in parts:
    raise ValueError(f"Invalid GitHub PR URL: {url}")
```

This is the same `"github.com" not in url` anti-pattern flagged
by CodeQL but applied to the full URL string. It should use
`urlparse` for proper host extraction.

### 4.2. Token Redaction Patterns in `git_ops.py`

The `_TOKEN_PATTERNS` list covers `ghp_*` (GitHub classic tokens)
and `glpat-*` (GitLab tokens), but does **not** cover:

- GitHub fine-grained tokens (`github_pat_*` prefix)
- GitHub App installation tokens (`ghs_*` prefix)
- GitHub user-to-server tokens (`ghu_*` prefix)
- GitHub OAuth tokens (no consistent prefix)

### 4.3. `resolve_conflicts.py` — Token in Clone URLs

Line 448–449 embeds the token in a clone URL:

```python
return clone_url.replace(
    "https://", f"https://x-access-token:{token}@"
)
```

While `run_git()` redacts this before logging, the raw URL exists
in memory and in `GitResult.args` / `GitError.args_vec`.

---

## 5. Remediation Plan

### Phase 1: URL Substring Sanitization (Alerts #28–#33)

#### Task 1.1: Create a Centralised URL Validation Module

Create a new utility function or extend the existing `url_parser.py`
module with proper hostname validation:

```python
# Proposed: url_parser.py additions

def _host_matches(
    hostname: str,
    target: str,
    *,
    allow_subdomains: bool = True,
) -> bool:
    """Check if hostname matches target using secure comparison.

    Uses exact equality or subdomain matching with a leading dot
    to prevent substring bypass attacks.

    This function is the ONLY approved way to check hostnames in
    this codebase. Do NOT use Python's `in` operator on hostname
    strings — CodeQL rule py/incomplete-url-substring-sanitization.

    Args:
        hostname: The parsed hostname to check (lowercase).
        target: The target hostname to match against.
        allow_subdomains: If True, also matches *.target.

    Returns:
        True if hostname matches target exactly or is a subdomain.
    """
    if not hostname or not target:
        return False
    hostname = hostname.lower()
    target = target.lower()
    if hostname == target:
        return True
    if allow_subdomains and hostname.endswith(f".{target}"):
        return True
    return False
```

#### Task 1.2: Refactor `_is_github_url()`

Replace substring checks with proper host validation:

```python
def _is_github_url(host: str, path: str) -> bool:
    """Check if URL is a GitHub URL using secure host comparison.

    Uses exact hostname matching (not substring checks) to prevent
    bypass attacks via crafted hostnames. See CodeQL rule
    py/incomplete-url-substring-sanitization.
    """
    # Secure host matching — prevents "evil-github.com" bypass
    if _host_matches(host, "github.com"):
        return True

    # Path-based detection for GitHub Enterprise with unknown hosts
    if "/pull/" in path:
        return True

    return False
```

#### Task 1.3: Refactor `_is_gerrit_url()`

Replace substring checks with structural validation:

```python
def _is_gerrit_url(host: str, path: str) -> bool:
    """Check if URL is a Gerrit URL using structural validation.

    Uses Gerrit's distinctive URL structure (/c/.../+/) rather
    than hostname substring matching to identify Gerrit servers.
    See CodeQL rule py/incomplete-url-substring-sanitization.
    """
    # Primary: Gerrit change URL structure is definitive
    if "/c/" in path and "/+/" in path:
        return True

    # Secondary: known Gerrit hosts with recognizable paths
    # Note: We do NOT use `"gerrit" in host` — that is a
    # substring check vulnerable to bypass. Instead we check
    # for Gerrit-specific path patterns which are structurally
    # unambiguous.
    if path.startswith("/changes/"):
        return True

    return False
```

**Design decision:** The Gerrit hostname heuristic
(`"gerrit" in host`) should be removed entirely. Gerrit instances
can have any hostname, and the path-based detection (`/c/.../+/`
and `/changes/`) is both more reliable and more secure. If a
specific set of known Gerrit hosts must be supported, they should
be listed in an explicit allowlist, not matched by substring.

#### Task 1.4: Refactor `GitHubClient.parse_pr_url()`

Replace `"github.com" not in url` with `urlparse`-based validation:

```python
from urllib.parse import urlparse

def parse_pr_url(self, url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not _host_matches(host, "github.com"):
        raise ValueError(f"Invalid GitHub PR URL: {url}")
    # ... rest of parsing logic
```

**Note:** This function should import and reuse `_host_matches`
from `url_parser.py` to keep validation centralised.

#### Task 1.5: Update Test Assertions

Replace `in` assertions on URL/host strings with exact or
`startswith`/`==` assertions:

```python
# Before (triggers CodeQL):
assert "gerrit.example.org" in client.base_url

# After (precise and scanner-safe):
assert client.base_url == "https://gerrit.example.org/"
# — or —
assert client.base_url.startswith("https://gerrit.example.org/")
```

For `repr()` assertions, use exact string matching or
`repr_str.startswith(...)` instead of substring containment.

### Phase 2: Clear-Text Logging Remediation (Alerts #26–#27)

#### Task 2.1: Remove Credential Data from Log Statements

In `resolve_gerrit_credentials()` and `get_credentials_for_host()`,
ensure NO credential values (usernames, passwords, tokens) appear
in log messages. Log only:

- The **source** of credentials (e.g., "CLI arguments", ".netrc",
  "environment variables")
- The **hostname** being authenticated against
- The **file path** of the netrc file (not its contents)

Specifically, remove `netrc_creds.login` from log messages:

```python
# Before (line 747-750):
log.debug(
    "Using credentials from .netrc for %s (login: %s) in %s",
    normalized_host,
    netrc_creds.login,  # <-- REMOVE: username is sensitive
    netrc_path,
)

# After:
log.debug(
    "Using credentials from .netrc for host %s (file: %s)",
    normalized_host,
    netrc_path,
)
```

#### Task 2.2: Add CodeQL Suppression Comments

For log lines that CodeQL flags but which genuinely log only
non-sensitive data (e.g., the string `"GERRIT_USERNAME"` as a
variable name, not its value), add explicit suppression comments
with justification:

```python
log.debug(  # CodeQL: not logging credential values, only
    "Resolved credentials from environment variable names: "
    "%s/%s",  # noqa: py/clear-text-logging-sensitive-data
    env_username_var,   # This is the string "GERRIT_USERNAME"
    env_password_var,   # This is the string "GERRIT_PASSWORD"
)
```

However, the preferred approach is to restructure the code so
that the taint path is broken and CodeQL does not flag it at all.
This means logging a fixed descriptive string rather than passing
through the parameter values:

```python
# Preferred: break the taint chain entirely
log.debug(
    "Resolved credentials from primary environment variables"
)
```

#### Task 2.3: Add Safe `__repr__` Methods

Add `__repr__` methods that explicitly mask tokens to all classes
that store sensitive credentials:

```python
# github_client.py
class GitHubClient:
    def __repr__(self) -> str:
        """Safe repr that never exposes the token value."""
        return "GitHubClient(token=***)"

# github_async.py
class GitHubAsync:
    def __repr__(self) -> str:
        """Safe repr that never exposes the token value."""
        return (
            f"GitHubAsync(api_url={self.api_url!r}, token=***)"
        )

# merge_manager.py
class AsyncMergeManager:
    def __repr__(self) -> str:
        """Safe repr that never exposes the token value."""
        return "AsyncMergeManager(token=***)"

# close_manager.py
class AsyncCloseManager:
    def __repr__(self) -> str:
        """Safe repr that never exposes the token value."""
        return "AsyncCloseManager(token=***)"
```

### Phase 3: Defence-in-Depth Hardening

#### Task 3.1: Expand Token Redaction Patterns

Update `git_ops.py` `_TOKEN_PATTERNS` to cover additional GitHub
token formats:

```python
_TOKEN_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9]{20,}", re.IGNORECASE),
    re.compile(r"ghs_[A-Za-z0-9]{20,}", re.IGNORECASE),
    re.compile(r"ghu_[A-Za-z0-9]{20,}", re.IGNORECASE),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}", re.IGNORECASE),
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}", re.IGNORECASE),
    # JWT-like long tokens (best-effort)
    re.compile(
        r"[A-Za-z0-9-_]{20,}\.[A-Za-z0-9-_]{20,}"
        r"\.[A-Za-z0-9-_]{10,}"
    ),
]
```

#### Task 3.2: Redact `GitError.args_vec`

The `GitError` class stores raw (unredacted) command arguments in
`self.args_vec`. This should be redacted:

```python
class GitError(RuntimeError):
    def __init__(self, message, *, args, returncode, stdout, stderr):
        # ... existing redaction for message ...
        self.args_vec = tuple(_redact(str(a)) for a in args)
        # ... rest unchanged ...
```

#### Task 3.3: Sanitize Exceptions from `httpx`

When `raise_for_status()` fires, the resulting `HTTPStatusError`
carries a `.request` attribute with full headers including the
`Authorization: Bearer <token>` header. Add a wrapper that catches
and re-raises with a sanitized exception:

```python
try:
    response.raise_for_status()
except httpx.HTTPStatusError as exc:
    # Strip auth headers from the request before propagating
    # to prevent token leakage via exception inspection
    raise GitHubAPIError(
        status_code=exc.response.status_code,
        message=str(exc),
    ) from None  # Use 'from None' to avoid chaining the
                 # original exception with its auth headers
```

### Phase 4: Documentation Updates

#### Task 4.1: Update README.md Security Section

Expand the existing `## Security Considerations` section to
document the credential protection guarantees:

1. **Token Redaction** — All git operations redact tokens from
   logs and error messages via `git_ops._redact()`
2. **Repr Safety** — All classes storing credentials implement
   `__repr__` with masking
3. **Log Hygiene** — No credential values are ever written to
   log output at any level
4. **Scoped Debug Logging** — `--verbose` mode only enables
   DEBUG for the `dependamerge.*` logger namespace; third-party
   libraries (including `httpx`) remain at WARNING
5. **URL Validation** — All hostname checks use
   `urlparse()`-based exact matching, not substring checks

#### Task 4.2: Add Inline Code Comments

At each remediated location, add a comment explaining the security
rationale so future contributors do not regress:

```python
# SECURITY: Use _host_matches() for hostname validation.
# Do NOT use `"github.com" in host` — this is a substring
# check that can be bypassed by crafted hostnames.
# See: CodeQL py/incomplete-url-substring-sanitization
```

```python
# SECURITY: Never log credential values (tokens, passwords,
# usernames). Log only the credential SOURCE (e.g., "netrc",
# "environment variable") and non-sensitive metadata.
# See: CodeQL py/clear-text-logging-sensitive-data
```

---

## 6. Verification Criteria

After implementing all changes, the following must be verified:

### Functional

- [ ] All 745+ existing tests pass (`uv run pytest tests/ -x -q`)
- [ ] All pre-commit hooks pass
- [ ] URL parsing correctly identifies GitHub and Gerrit URLs
- [ ] URL parsing correctly rejects crafted bypass URLs:
  - `https://evil-github.com.attacker.net/owner/repo/pull/1`
  - `https://not-a-gerrit-server.evil.org/c/project/+/123`
- [ ] Credential resolution works from all sources (CLI, env, netrc)
- [ ] `--verbose` mode does not expose any credential values

### Security

- [ ] CodeQL alerts #26–#33 no longer trigger on updated code
- [ ] `repr()` of all credential-bearing objects shows masked values
- [ ] `git_ops._redact()` covers all known GitHub token formats
- [ ] No credential values appear in any log output at any level
- [ ] Exception objects do not carry auth headers after sanitization

### Documentation

- [ ] README.md security section updated with protection guarantees
- [ ] Inline comments at all remediated locations explain the rationale
- [ ] This plan document referenced in commit message

---

## Appendix A: File Change Summary

<!-- markdownlint-disable MD013 -->

| File                                | Changes Required                                                                                       |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `src/dependamerge/url_parser.py`    | Add `_host_matches()`, refactor `_is_github_url()`, refactor `_is_gerrit_url()`, add security comments |
| `src/dependamerge/github_client.py` | Refactor `parse_pr_url()` to use `urlparse`, add safe `__repr__`                                       |
| `src/dependamerge/github_async.py`  | Add safe `__repr__`, sanitize httpx exceptions                                                         |
| `src/dependamerge/netrc.py`         | Remove credential values from log messages, break CodeQL taint paths                                   |
| `src/dependamerge/merge_manager.py` | Add safe `__repr__`                                                                                    |
| `src/dependamerge/close_manager.py` | Add safe `__repr__`                                                                                    |
| `src/dependamerge/git_ops.py`       | Expand token patterns, redact `args_vec`                                                               |
| `tests/test_netrc.py`               | Replace substring assertions with exact assertions                                                     |
| `tests/test_gerrit_client.py`       | Replace substring assertions with exact assertions                                                     |
| `tests/test_url_parser.py` (new)    | Add tests for bypass URL rejection                                                                     |
| `README.md`                         | Expand security section                                                                                |

<!-- markdownlint-enable MD013 -->

## Appendix B: Alert Cross-Reference

<!-- markdownlint-disable MD013 -->

| Alert #       | Rule                                       | File                              | Status                     |
| ------------- | ------------------------------------------ | --------------------------------- | -------------------------- |
| Dependabot #2 | CVE-2026-4539 (Pygments ReDoS)             | transitive dep                    | Deferred — no upstream fix |
| CodeQL #33    | `py/incomplete-url-substring-sanitization` | `url_parser.py:128`               | Phase 1 fix                |
| CodeQL #32    | `py/incomplete-url-substring-sanitization` | `tests/test_netrc.py:199`         | Phase 1 fix                |
| CodeQL #31    | `py/incomplete-url-substring-sanitization` | `tests/test_netrc.py:198`         | Phase 1 fix                |
| CodeQL #30    | `py/incomplete-url-substring-sanitization` | `tests/test_netrc.py:119`         | Phase 1 fix                |
| CodeQL #29    | `py/incomplete-url-substring-sanitization` | `tests/test_gerrit_client.py:515` | Phase 1 fix                |
| CodeQL #28    | `py/incomplete-url-substring-sanitization` | `tests/test_gerrit_client.py:190` | Phase 1 fix                |
| CodeQL #27    | `py/clear-text-logging-sensitive-data`     | `netrc.py:791`                    | Phase 2 fix                |
| CodeQL #26    | `py/clear-text-logging-sensitive-data`     | `netrc.py:773`                    | Phase 2 fix                |

<!-- markdownlint-enable MD013 -->
