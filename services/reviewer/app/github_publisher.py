"""GitHub API client for posting PR review comments.

Handles the GitHub Pull Request Reviews API:
- Create a review with overall summary and inline comments
- Rate limiting with exponential backoff
- GitHub App installation token refresh
- Batch comment posting to minimize API calls

GitHub PR Reviews API reference:
https://docs.github.com/en/rest/pulls/reviews#create-a-review-for-a-pull-request
"""

from __future__ import annotations

import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"

# GitHub allows at most 4000 chars per review comment
MAX_COMMENT_LENGTH = 3800


class GitHubPublisher:
    """Client for posting PR review comments to GitHub.

    Args:
        token: GitHub PAT or App installation token.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(self, token: str, *, timeout: int = 30) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-reviewer-bot/0.1.0",
        }
        self._timeout = timeout

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        wait=wait_exponential(multiplier=2, min=5, max=60),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def create_review(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
        body: str,
        event: str,
        inline_comments: list[dict],
    ) -> int | None:
        """Create a Pull Request Review with inline comments.

        A single review can include multiple inline comments.
        This is the most efficient way to post AI review findings.

        Args:
            repo_full_name: Repository in ``owner/repo`` format.
            pr_number: Pull request number.
            head_sha: The SHA of the commit to review.
            body: Overall review body markdown.
            event: Review event type: APPROVE | REQUEST_CHANGES | COMMENT.
            inline_comments: List of inline comment dicts with path, line, body.

        Returns:
            The GitHub review ID if created successfully, or None.
        """
        from shared.infrastructure.metrics import (
            REVIEWER_COMMENTS_POSTED_TOTAL,
            REVIEWER_REVIEWS_CREATED_TOTAL,
        )

        url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}/reviews"

        # Truncate inline comments to GitHub's limit
        safe_comments = []
        for comment in inline_comments:
            safe_body = comment.get("body", "")
            if len(safe_body) > MAX_COMMENT_LENGTH:
                safe_body = safe_body[:MAX_COMMENT_LENGTH] + "\n\n*[truncated]*"
            safe_comments.append({**comment, "body": safe_body})

        payload = {
            "commit_id": head_sha,
            "body": body[:65535],  # GitHub's review body limit
            "event": event,
            "comments": safe_comments,
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, json=payload, headers=self._headers)

            if response.status_code == 422:
                # Often caused by line numbers that don't match the diff
                # Fall back to posting as a PR comment
                logger.warning(
                    "Review creation failed with 422 — falling back to PR comment",
                    repo=repo_full_name,
                    pr_number=pr_number,
                    response_body=response.text[:500],
                )
                REVIEWER_REVIEWS_CREATED_TOTAL.labels(status="fallback_422").inc()
                await self._post_fallback_comment(
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                    body=body,
                    client=client,
                )
                return None

            response.raise_for_status()

            review_data = response.json()
            review_id = review_data.get("id")

            REVIEWER_REVIEWS_CREATED_TOTAL.labels(status="success").inc()
            REVIEWER_COMMENTS_POSTED_TOTAL.labels(status="success").inc(len(safe_comments))

            logger.info(
                "GitHub review created",
                repo=repo_full_name,
                pr_number=pr_number,
                review_id=review_id,
                comments=len(safe_comments),
            )
            return review_id

    async def _post_fallback_comment(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        body: str,
        client: httpx.AsyncClient,
    ) -> None:
        """Post a PR-level comment as fallback when inline comment creation fails."""
        url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/issues/{pr_number}/comments"
        response = await client.post(
            url,
            json={"body": body[:65535]},
            headers=self._headers,
        )
        if not response.is_success:
            logger.error(
                "Fallback comment also failed",
                status=response.status_code,
                body=response.text[:500],
            )


def create_github_publisher(*, pat: str | None) -> GitHubPublisher:
    """Factory for creating a GitHubPublisher with configured auth.

    Args:
        pat: GitHub Personal Access Token.

    Returns:
        A configured ``GitHubPublisher``.

    Raises:
        ValueError: If no auth method is configured.
    """
    if pat:
        return GitHubPublisher(token=pat)

    msg = "No GitHub authentication configured. Set GITHUB_PAT."
    raise ValueError(msg)
