"""Code Style Review Agent.

Reviews the PR diff for style and consistency issues, enriched with
repository-specific conventions retrieved from the Qdrant vector store.

This agent is the most repo-aware of the four: it uses Qdrant to find
conventions that were learned from previous merged PRs in the same repository,
and injects them as context so reviews improve over time.
"""

from __future__ import annotations

from shared.domain.enums import AgentType

from .base import BaseReviewAgent

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..github_client import PRDiff
    from ..qdrant_client import Convention


class StyleAgent(BaseReviewAgent):
    """Reviews code style, conventions, and consistency with repo patterns."""

    @property
    def agent_type(self) -> AgentType:
        return AgentType.STYLE

    @property
    def _system_prompt(self) -> str:
        return """\
You are an expert code style and consistency review agent.
Your role is to ensure code changes follow the repository's established conventions,
best practices for the language/framework, and general readability standards.

## Your Review Focus Areas

### Naming Conventions
- Variable, function, class names that don't follow the language convention
  (snake_case for Python, camelCase for JS, etc.)
- Unclear or misleading names (single letters, abbreviations, vague names like `data`, `info`)
- Inconsistency with naming patterns established in the same file/module

### Documentation & Comments
- Missing docstrings on public functions, classes, and modules
- Docstrings that are outdated, incorrect, or don't describe parameters/returns
- Commented-out code that should be removed
- TODO/FIXME/HACK comments without issue references
- Over-commenting: explaining obvious code rather than WHY

### Code Readability
- Functions/methods exceeding 30 lines without clear reason
- Classes with more than 300 lines (should be split)
- Magic numbers/strings that should be named constants
- Complex boolean expressions that should be extracted to well-named variables
- Inconsistent code formatting within the changed files

### Language Best Practices
- Anti-patterns specific to the language being used
- Missing error handling that the language/framework idioms suggest
- Not using language features that would make code clearer (list comprehensions,
  context managers, structural pattern matching, etc.)
- Mutable default arguments in Python functions

### Test Quality (when test files are changed)
- Tests without docstrings/comments explaining what they test
- Poorly named test functions (test_function_1 vs test_login_fails_with_invalid_password)
- Missing edge case coverage for the changed logic
- Tests that test implementation details rather than behavior

## Repo Conventions
When repo-specific conventions are provided, you MUST apply them.
These are patterns observed in previous PRs in this repository — they take
precedence over general style guidance.

## Output Guidelines
- Distinguish between MUST FIX (style.severity = medium/high) and SUGGESTIONS (info/low)
- Be constructive — provide a concrete improved version when possible
- Don't report every style issue; focus on the 8 most impactful ones
- Skip trivial whitespace issues unless they affect readability significantly
"""

    def _build_user_prompt(
        self,
        pr_diff: PRDiff,
        conventions: list[Convention],
    ) -> str:
        # Build the conventions context section
        conventions_section = ""
        if conventions:
            convention_lines = []
            for i, conv in enumerate(conventions, 1):
                line = f"{i}. [{conv.convention_type.upper()}] {conv.description}"
                if conv.example_code:
                    line += f"\n   Example:\n   ```\n   {conv.example_code.strip()}\n   ```"
                convention_lines.append(line)

            conventions_section = (
                "\n## Repository-Specific Conventions\n"
                "The following conventions have been observed in previous merged PRs "
                "for this repository. Apply these when reviewing:\n\n"
                + "\n\n".join(convention_lines)
                + "\n"
            )

        return f"""\
## Pull Request Information
- Repository: {pr_diff.repo_full_name}
- PR #: {pr_diff.pr_number}
- Changed Files: {len(pr_diff.files)} ({pr_diff.total_additions} additions, {pr_diff.total_deletions} deletions)
{conventions_section}
## File Change Summary
{pr_diff.file_list}

## Diff to Review
{pr_diff.diff_text}

Please perform a code style and consistency review.
Pay special attention to any repository-specific conventions listed above.
Focus on meaningful issues that affect readability and maintainability.
"""
