"""GitHub API client for the worker service.

Fetches PR diffs and file contents needed by the review agents.
Handles authentication via PAT or GitHub App installation tokens.

Design decisions:
- Uses ``httpx.AsyncClient`` for non-blocking HTTP in async contexts.
- Retry logic via ``tenacity`` for transient GitHub API failures.
- Returns structured ``FileDiff`` objects (not raw dicts) to decouple
  agents from the GitHub API response shape.
- Diff size is capped at ``MAX_DIFF_CHARS`` to stay within LLM context windows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# Maximum characters of diff to send to LLM agents (128K token context ≈ ~500K chars)
MAX_DIFF_CHARS = 120_000
# GitHub API base URL
GITHUB_API_BASE = "https://api.github.com"


@dataclass(frozen=True)
class FileDiff:
    """A single file changed in the PR.

    Attributes:
        filename: Relative path within the repository.
        status: GitHub change status: added | modified | removed | renamed | copied.
        additions: Number of lines added.
        deletions: Number of lines removed.
        patch: The raw unified diff patch (may be None for binary files).
        previous_filename: Original filename if the file was renamed.
    """

    filename: str
    status: str
    additions: int
    deletions: int
    patch: str | None = None
    previous_filename: str | None = None

    @property
    def is_binary(self) -> bool:
        """True if this is a binary file with no diff patch."""
        return self.patch is None

    @property
    def change_summary(self) -> str:
        """One-line change summary for LLM context."""
        return f"{self.status}: {self.filename} (+{self.additions}/-{self.deletions})"


@dataclass
class PRDiff:
    """Complete diff for a Pull Request.

    Attributes:
        repo_full_name: Repository in ``owner/repo`` format.
        pr_number: Pull request number.
        head_sha: SHA of the head commit being reviewed.
        files: List of changed files with their patches.
        total_additions: Sum of all line additions.
        total_deletions: Sum of all line deletions.
    """

    repo_full_name: str
    pr_number: int
    head_sha: str
    files: list[FileDiff] = field(default_factory=list)
    total_additions: int = 0
    total_deletions: int = 0

    @property
    def diff_text(self) -> str:
        """Concatenate all file patches into a single diff string.

        Caps the output at ``MAX_DIFF_CHARS`` to fit within LLM context.
        Files are ordered by change size (largest first) so the most impactful
        changes are always included.
        """
        sorted_files = sorted(
            self.files,
            key=lambda f: (f.additions + f.deletions),
            reverse=True,
        )

        parts: list[str] = []
        total_chars = 0

        for file_diff in sorted_files:
            if file_diff.patch is None:
                header = f"### {file_diff.change_summary} [binary file, no patch]\n"
                parts.append(header)
                total_chars += len(header)
                continue

            section = (
                f"### File: {file_diff.filename} ({file_diff.status})\n"
                f"```diff\n{file_diff.patch}\n```\n\n"
            )
            if total_chars + len(section) > MAX_DIFF_CHARS:
                truncation_note = (
                    f"\n[... diff truncated — {len(sorted_files) - len(parts)} "
                    f"files omitted due to size limit ...]\n"
                )
                parts.append(truncation_note)
                break
            parts.append(section)
            total_chars += len(section)

        return "\n".join(parts)

    @property
    def file_list(self) -> str:
        """Compact list of changed files for context."""
        return "\n".join(f.change_summary for f in self.files)


class GitHubClient:
    """Async GitHub REST API client for fetching PR data.

    Args:
        token: GitHub Personal Access Token or App installation token.
        timeout: Request timeout in seconds.
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
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def fetch_pr_diff(
        self,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
    ) -> PRDiff:
        """Fetch the complete diff for a pull request.

        Uses the ``GET /repos/{owner}/{repo}/pulls/{pull_number}/files`` endpoint
        which returns per-file diffs with line-level patch data.

        Args:
            repo_full_name: Repository in ``owner/repo`` format.
            pr_number: Pull request number.
            head_sha: Head commit SHA (used for logging/dedup).

        Returns:
            A ``PRDiff`` object with all changed files and patches.

        Raises:
            httpx.HTTPStatusError: If GitHub returns an error response.
        """
        url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}/files"
        pr_diff = PRDiff(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
        )

        # GitHub paginates at 30 files by default; request max 100 per page
        page = 1
        all_files: list[FileDiff] = []

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while True:
                response = await client.get(
                    url,
                    headers=self._headers,
                    params={"per_page": 100, "page": page},
                )
                response.raise_for_status()

                data: list[dict] = response.json()
                if not data:
                    break

                for file_data in data:
                    file_diff = FileDiff(
                        filename=file_data["filename"],
                        status=file_data["status"],
                        additions=file_data.get("additions", 0),
                        deletions=file_data.get("deletions", 0),
                        patch=file_data.get("patch"),
                        previous_filename=file_data.get("previous_filename"),
                    )
                    all_files.append(file_diff)

                # Check for more pages via Link header
                link_header = response.headers.get("Link", "")
                if 'rel="next"' not in link_header:
                    break
                page += 1

        pr_diff.files = all_files
        pr_diff.total_additions = sum(f.additions for f in all_files)
        pr_diff.total_deletions = sum(f.deletions for f in all_files)

        logger.info(
            "Fetched PR diff",
            extra={
                "repo": repo_full_name,
                "pr_number": pr_number,
                "files": len(all_files),
                "additions": pr_diff.total_additions,
                "deletions": pr_diff.total_deletions,
            },
        )
        return pr_diff

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def get_file_content(
        self,
        repo_full_name: str,
        file_path: str,
        ref: str,
    ) -> str | None:
        """Fetch the raw content of a file at a specific git ref.

        Args:
            repo_full_name: Repository in ``owner/repo`` format.
            file_path: Path to the file relative to repo root.
            ref: Git ref (branch, tag, or SHA).

        Returns:
            Raw file content as a string, or None if the file is binary/too large.
        """
        import base64

        url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/contents/{file_path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                url,
                headers=self._headers,
                params={"ref": ref},
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()

            data = response.json()
            if data.get("encoding") == "base64" and "content" in data:
                content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
                return content[:50_000]  # Cap at 50K chars for LLM safety

            return None


def create_github_client(*, pat: str | None = None, app_id: str | None = None) -> GitHubClient:
    """Factory for creating a GitHubClient with the configured auth method.

    Priority: PAT > GitHub App. In production, prefer App-based auth.

    Args:
        pat: GitHub Personal Access Token.
        app_id: GitHub App ID (requires private key in env).

    Returns:
        A configured ``GitHubClient``.

    Raises:
        ValueError: If neither auth method is configured.
    """
    if pat:
        return GitHubClient(token=pat)

    msg = (
        "No GitHub authentication configured. "
        "Set GITHUB_PAT or GITHUB_APP_ID + private key."
    )
    raise ValueError(msg)
