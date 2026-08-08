"""Repository convention extractor for the learner service.

Fetches a merged PR's diff and uses an LLM to extract coding conventions
that are specific to this repository. Extracted conventions are stored
in Qdrant so they can be used by the Style agent in future reviews.

Convention categories extracted:
- naming: Variable, function, class naming patterns
- testing: Test structure, assertion style, test data patterns
- error_handling: Error handling idioms and patterns
- logging: Log levels, message formats, structured field names
- documentation: Docstring style, comment conventions
- imports: Import ordering, aliasing conventions
- patterns: Recurring design patterns in this codebase
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
MAX_DIFF_CHARS = 60_000  # Smaller limit for learning (we need full diff context)


class ExtractedConvention(BaseModel):
    """A single convention extracted by the LLM from a merged PR."""

    convention_type: str = Field(
        ...,
        description="Category: naming|testing|error_handling|logging|documentation|imports|patterns",
    )
    description: str = Field(
        ...,
        min_length=20,
        description="Clear, specific description of the convention. At least 20 chars.",
    )
    example_code: str | None = Field(
        None,
        description="Code snippet from the PR that demonstrates this convention.",
    )
    confidence: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Confidence that this is an intentional pattern (vs. one-off code).",
    )


class ExtractionResult(BaseModel):
    """Complete output from the convention extraction LLM call."""

    conventions: list[ExtractedConvention] = Field(
        default_factory=list,
        description="List of extracted conventions. Empty if no clear patterns found.",
    )
    summary: str = Field(
        ...,
        description="Brief summary of what conventions were or were not found.",
    )


class ConventionExtractor:
    """Uses an LLM to extract coding conventions from merged PR diffs.

    Args:
        openrouter_api_key: API key for OpenRouter.
        openrouter_base_url: OpenRouter API base URL.
        model: LLM model to use for extraction.
        max_tokens: Maximum tokens for the LLM response.
        temperature: Temperature for LLM generation.
        timeout: Request timeout in seconds.
        github_pat: GitHub PAT for fetching PR diffs.
        max_conventions_per_pr: Max conventions to extract per PR.
    """

    def __init__(
        self,
        *,
        openrouter_api_key: str,
        openrouter_base_url: str,
        model: str,
        max_tokens: int,
        temperature: float,
        timeout: int,
        github_pat: str | None,
        max_conventions_per_pr: int = 5,
    ) -> None:
        self._github_pat = github_pat
        self._max_conventions = max_conventions_per_pr

        llm = ChatOpenAI(
            model=model,
            openai_api_key=openrouter_api_key,  # type: ignore[arg-type]
            openai_api_base=openrouter_base_url,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            default_headers={
                "HTTP-Referer": "https://github.com/pr-code-reviewer",
                "X-Title": "PR Code Reviewer Learner",
            },
        )
        self._llm = llm.with_structured_output(ExtractionResult)

    async def extract_from_pr(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
    ) -> list[ExtractedConvention]:
        """Fetch a merged PR's diff and extract conventions from it.

        Args:
            repo_full_name: Repository in ``owner/repo`` format.
            pr_number: Pull request number (must be merged).
            head_sha: Head commit SHA.

        Returns:
            List of extracted conventions (may be empty if no patterns found).
        """
        from shared.infrastructure.metrics import LEARNER_CONVENTIONS_EXTRACTED_TOTAL

        logger.info(
            "Extracting conventions from merged PR",
            extra={"repo": repo_full_name, "pr_number": pr_number},
        )

        # Fetch the diff
        diff_text = await self._fetch_pr_diff(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )

        if not diff_text or len(diff_text) < 100:
            logger.info(
                "PR diff too small to extract conventions",
                extra={"repo": repo_full_name, "pr_number": pr_number},
            )
            return []

        # Run the LLM extraction
        try:
            result = await self._extract_with_llm(
                diff_text=diff_text,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
            )

            # Filter by confidence and cap at max_conventions
            quality_conventions = [
                c for c in result.conventions if c.confidence >= 0.6
            ][:self._max_conventions]

            LEARNER_CONVENTIONS_EXTRACTED_TOTAL.inc(len(quality_conventions))

            logger.info(
                "Convention extraction complete",
                extra={
                    "repo": repo_full_name,
                    "pr_number": pr_number,
                    "total_extracted": len(result.conventions),
                    "after_quality_filter": len(quality_conventions),
                },
            )

            return quality_conventions

        except Exception as exc:
            logger.error(
                "Convention extraction failed",
                extra={"repo": repo_full_name, "pr_number": pr_number, "error": str(exc)},
            )
            return []

    async def _fetch_pr_diff(
        self,
        repo_full_name: str,
        pr_number: int,
    ) -> str:
        """Fetch the PR diff from GitHub API."""
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-reviewer-learner/0.1.0",
        }
        if self._github_pat:
            headers["Authorization"] = f"Bearer {self._github_pat}"

        url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}/files"

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url, headers=headers, params={"per_page": 100})
                response.raise_for_status()

                files = response.json()
                patches: list[str] = []
                total_chars = 0

                for file_data in files:
                    patch = file_data.get("patch")
                    if not patch:
                        continue

                    section = (
                        f"### {file_data['filename']} ({file_data['status']})\n"
                        f"```diff\n{patch}\n```\n\n"
                    )
                    if total_chars + len(section) > MAX_DIFF_CHARS:
                        break

                    patches.append(section)
                    total_chars += len(section)

                return "\n".join(patches)

        except Exception as exc:
            logger.warning(
                "Failed to fetch PR diff for learning",
                extra={"repo": repo_full_name, "pr_number": pr_number, "error": str(exc)},
            )
            return ""

    async def _extract_with_llm(
        self,
        *,
        diff_text: str,
        repo_full_name: str,
        pr_number: int,
    ) -> ExtractionResult:
        """Call the LLM to extract conventions from the diff."""
        from langchain_core.messages import HumanMessage, SystemMessage

        system_prompt = """\
You are a codebase convention extractor. Your job is to analyze merged Pull Request
diffs and identify repeating coding patterns and conventions used in this repository.

Focus on extracting GENUINE patterns that would help a future code reviewer understand
the repository's standards — not one-off implementation details.

Good conventions to extract:
- "All service classes use a `_log` private attribute initialized in __init__"
- "Error handling uses structured logging with .bind() context"
- "Test functions follow the AAA pattern (Arrange, Act, Assert) with comments"
- "All public functions have Google-style docstrings"
- "Imports are grouped: stdlib, third-party, local with blank lines between"

Bad conventions to extract (too specific, not patterns):
- "The login function returns a JWT token" (implementation detail)
- "The database has a users table" (data model fact)
- "PR adds a cache for expensive operations" (one-off optimization)

Be selective — only extract conventions with confidence >= 0.6.
Return an empty list if no clear patterns are visible.
"""

        user_message = f"""\
## Repository: {repo_full_name}
## Merged PR #: {pr_number}

Please analyze this merged PR diff and extract any coding conventions or patterns
that appear to be standards used consistently in this codebase.

## PR Diff:
{diff_text}

Extract up to 5 clear, generalizable conventions from this diff.
"""

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ]

        result = await self._llm.ainvoke(messages)
        return result  # type: ignore[return-value]
