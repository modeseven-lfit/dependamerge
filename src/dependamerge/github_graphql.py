# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
GraphQL query strings for retrieving repositories in an organization and their
open pull requests, including status check rollups and basic file/comment data.

These queries are designed to batch-read as much as possible to reduce the
number of HTTP round-trips compared to multiple REST calls per PR.

Notes:
- The mergeable field is an enum: MERGEABLE | CONFLICTING | UNKNOWN
- The mergeStateStatus field includes states like CLEAN, DIRTY, BLOCKED, BEHIND, DRAFT, UNKNOWN
- statusCheckRollup provides both CheckRun and StatusContext results for the latest commit
"""

__all__ = [
    "ORG_REPOS_ONLY",
    "ORG_REPOS_WITH_OPEN_PRS",
    "REPO_OPEN_PRS_PAGE",
]

# Lightweight query to list repositories without PR nodes for accurate counting
ORG_REPOS_ONLY = """
query($org: String!, $reposCursor: String) {
  organization(login: $org) {
    repositories(first: 100, after: $reposCursor, orderBy: { field: NAME, direction: ASC }) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        nameWithOwner
        isArchived
      }
    }
  }
}
"""

# Fetch organization repositories with a first page of their open PRs.
# Use the returned pageInfo to continue paging repositories.
# Each repository node also includes pageInfo for its pull requests; for repos
# with more than 50 open PRs, use REPO_OPEN_PRS_PAGE to paginate further.
ORG_REPOS_WITH_OPEN_PRS = """
query($org: String!, $reposCursor: String) {
  organization(login: $org) {
    repositories(first: 30, after: $reposCursor, orderBy: { field: NAME, direction: ASC }) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        nameWithOwner
        isArchived
        pullRequests(
          states: OPEN
          first: 30
          orderBy: { field: CREATED_AT, direction: DESC }
        ) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            number
            title
            body
            url
            isDraft
            author { login }
            mergeable
            mergeStateStatus
            baseRefName
            headRefName
            headRefOid
            createdAt
            updatedAt
            files(first: 50) {
              nodes {
                path
                additions
                deletions
              }
            }
            comments(first: 10, orderBy: { field: UPDATED_AT, direction: DESC }) {
              nodes {
                author { login }
                body
                createdAt
              }
            }
            commits(last: 1) {
              nodes {
                commit {
                  oid
                  statusCheckRollup {
                    state
                    contexts(first: 20) {
                      nodes {
                        __typename
                        ... on CheckRun {
                          name
                          status
                          conclusion
                        }
                        ... on StatusContext {
                          context
                          state
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

# Paginate open PRs for a specific repository when there are more than 50.
# Provide the repository owner/name and the PR cursor returned by previous pages.
REPO_OPEN_PRS_PAGE = """
query($owner: String!, $name: String!, $prsCursor: String, $prsPageSize: Int!, $filesPageSize: Int!, $commentsPageSize: Int!, $contextsPageSize: Int!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequests(
      states: OPEN
      first: $prsPageSize
      after: $prsCursor
      orderBy: { field: CREATED_AT, direction: DESC }
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        number
        title
        body
        url
        isDraft
        author { login }
        mergeable
        mergeStateStatus
        baseRefName
        headRefName
        headRefOid
        createdAt
        updatedAt
        files(first: $filesPageSize) {
          nodes {
            path
            additions
            deletions
          }
        }
        comments(first: $commentsPageSize, orderBy: { field: UPDATED_AT, direction: DESC }) {
          nodes {
            author { login }
            body
            createdAt
          }
        }
        commits(last: 1) {
          nodes {
            commit {
              oid
              statusCheckRollup {
                state
                contexts(first: $contextsPageSize) {
                  nodes {
                    __typename
                    ... on CheckRun {
                      name
                      status
                      conclusion
                    }
                    ... on StatusContext {
                      context
                      state
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""
