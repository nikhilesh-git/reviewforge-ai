"""Static Analysis Review Agent.

Reviews the PR diff for code quality issues such as:
- Unused variables, imports, and dead code
- Complexity hotspots (deeply nested functions, long methods)
- Potential bugs (off-by-one errors, null dereferences, unhandled exceptions)
- Type annotation gaps and inconsistencies
- Resource leaks (unclosed files, connections without context managers)
"""

from __future__ import annotations

from shared.domain.enums import AgentType

from .base import BaseReviewAgent

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import WorkerSettings
    from ..github_client import PRDiff
    from ..qdrant_client import Convention


class StaticAnalysisAgent(BaseReviewAgent):
    """Reviews code for static analysis issues — bugs, complexity, dead code."""

    @property
    def agent_type(self) -> AgentType:
        return AgentType.STATIC_ANALYSIS

    @property
    def _system_prompt(self) -> str:
        return """\
You are an expert static code analysis agent reviewing a GitHub Pull Request.
Your role is to identify genuine code quality issues — not style preferences,
but actual bugs, complexity problems, and maintainability risks.

## Your Review Focus Areas

1. **Potential Bugs**
   - Off-by-one errors in loops and array indexing
   - Null/None dereferences without guards
   - Unhandled exceptions (bare except, swallowed errors)
   - Race conditions in concurrent code
   - Integer overflow / type coercion issues

2. **Dead Code & Complexity**
   - Unused variables, parameters, and imports
   - Functions/methods exceeding 50 lines (complexity risk)
   - Deeply nested control flow (> 4 levels)
   - Duplicate code blocks that should be extracted
   - Unreachable code after return/raise statements

3. **Resource Management**
   - File handles, DB connections, network sockets not closed
   - Missing context managers for resource cleanup
   - Memory leaks in long-running loops

4. **Type Safety**
   - Functions without type annotations (in typed codebases)
   - Type mismatches detectable without runtime info
   - Missing return type annotations

## Output Guidelines
- Only report genuine issues — not minor style nitpicks
- Be specific: quote the problematic code and explain exactly what will go wrong
- Severity: CRITICAL = data corruption/crash, HIGH = likely bug, MEDIUM = likely
  bug in edge cases, LOW = maintainability risk, INFO = suggestion
- Limit findings to the 10 most important issues
- Skip findings with confidence < 0.7
- Return an empty findings list if the diff looks clean
"""

    def _build_user_prompt(
        self,
        pr_diff: PRDiff,
        conventions: list[Convention],
    ) -> str:
        return f"""\
## Pull Request Information
- Repository: {pr_diff.repo_full_name}
- PR #: {pr_diff.pr_number}
- Changed Files: {len(pr_diff.files)} ({pr_diff.total_additions} additions, {pr_diff.total_deletions} deletions)

## File Change Summary
{pr_diff.file_list}

## Diff to Review
{pr_diff.diff_text}

Please perform a static analysis review of the above changes.
Focus on bugs, complexity, dead code, and resource management issues.
Be precise about file paths and line numbers in your findings.
"""
