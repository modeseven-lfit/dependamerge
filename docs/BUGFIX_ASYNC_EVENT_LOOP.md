<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

# Bug Fix: RuntimeWarning for Unawaited Coroutine in `_analyze_block_reason`

## Problem Description

When running `dependamerge merge`, users encountered the following warning:

```text
/Users/mwatkins/.local/share/uv/tools/dependamerge/lib/python3.11/
site-packages/dependamerge/github_client.py:390: RuntimeWarning:
coroutine 'GitHubClient._analyze_block_reason.<locals>._run'
was never awaited
  return "Blocked"
RuntimeWarning: Enable tracemalloc to get the object
allocation traceback
```

This warning appeared during PR merge operations, specifically when
analyzing why a PR failed to merge.

## Root Cause

The issue occurred due to a **nested event loop conflict**:

1. `AsyncMergeManager._merge_single_pr()` is an **async** function
   running in an event loop
2. When a PR is not mergeable, it created a **synchronous**
   `GitHubClient` instance
3. `GitHubClient.get_pr_status_details()` called `_analyze_block_reason()`
4. `_analyze_block_reason()` attempted to call `asyncio.run(_run())`
5. **`asyncio.run()` fails when called from within an already-running
   event loop**
6. The call failed, but the coroutine `_run()` was already instantiated
7. Python issued a warning about the unawaited coroutine
8. The exception handler returned a generic `"Blocked"` message

### Code Flow

```text
AsyncMergeManager._merge_single_pr() [ASYNC CONTEXT]
  └─> GitHubClient() [SYNC]
      └─> get_pr_status_details()
          └─> _analyze_block_reason()
              └─> asyncio.run(_run())
                  ❌ FAILS - already in event loop!
                  └─> RuntimeWarning: coroutine never awaited
```

## Solution

The fix involved three changes:

### 1. Added Async Method to `GitHubAsync`

Created a new async method `analyze_block_reason()` in `github_async.py`:

```python
async def analyze_block_reason(
    self, owner: str, repo: str, number: int, head_sha: str
) -> str:
    """
    Analyze why a PR has a blocked status and return
    appropriate status message.

    This is the async version for use from async contexts.
    """
    # ... implementation moved from GitHubClient._analyze_block_reason
```

### 2. Updated Sync Method to Detect Event Loop

Modified `GitHubClient._analyze_block_reason()` to detect when the
function runs from an async context:

```python
def _analyze_block_reason(
    self, pr_info: PullRequestInfo
) -> str:
    """
    Analyze why a PR has a blocked status and return
    appropriate status using REST.
    """
    try:
        from .github_async import GitHubAsync

        repo_owner, repo_name = pr_info.repository_full_name.split("/")

        # Check if we're already in an event loop
        try:
            asyncio.get_running_loop()
            # We're in an async context - can't use asyncio.run()
            # Return a basic status message to avoid the coroutine warning
            return "Blocked by branch protection"
        except RuntimeError:
            # No event loop running - safe to use asyncio.run()
            pass

        async def _run():
            async with GitHubAsync(token=self.token) as api:
                return await api.analyze_block_reason(
                    repo_owner, repo_name, pr_info.number, pr_info.head_sha
                )

        return asyncio.run(_run())
    except Exception:
        return "Blocked"
```

### 3. Updated Async Caller to Use Async Method

Modified `merge_manager.py` to use the async client directly:

```python
if not self._is_pr_mergeable(pr_info):
    # Use async method to avoid event loop conflicts
    repo_owner, repo_name = pr_info.repository_full_name.split("/")

    # Check if blocked to get more detailed status
    if pr_info.mergeable_state == "blocked":
        try:
            detailed_status = await self._github_client.analyze_block_reason(
                repo_owner, repo_name, pr_info.number, pr_info.head_sha
            )
        except Exception:
            detailed_status = f"Blocked (state: {pr_info.mergeable_state})"
    else:
        # For non-blocked states, provide basic status
        if pr_info.mergeable_state == "dirty":
            detailed_status = "Merge conflicts"
        elif pr_info.mergeable_state == "behind":
            detailed_status = "Rebase required (out of date)"
        # ... etc
```

## Benefits

1. **No more RuntimeWarnings**: The coroutine is now properly awaited
   in async contexts
2. **Better error messages**: PRs now get detailed blocking reasons
   when analyzed from async code
3. **Proper async/sync separation**: Sync code uses `asyncio.run()`,
   async code uses `await`
4. **Performance improvement**: Avoids creating unnecessary event loops
   in nested contexts

## Testing

Added two new tests in `test_async_integration.py`:

1. `test_analyze_block_reason_in_async_context()` - Verifies the
   async method works as expected
2. `test_analyze_block_reason_sync_context_detection()` - Verifies
   the sync method detects event loops

## Files Modified

- `src/dependamerge/github_async.py` - Added `analyze_block_reason()`
  async method
- `src/dependamerge/github_client.py` - Updated `_analyze_block_reason()`
  to detect event loops
- `src/dependamerge/merge_manager.py` - Updated to use async method directly
- `tests/test_async_integration.py` - Added tests for the fix

## Related Issues

This fix resolves the RuntimeWarning that appeared during bulk PR merges,
most noticeable when merging similar PRs across an organization.
