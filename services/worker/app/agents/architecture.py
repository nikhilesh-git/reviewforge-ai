"""Architecture Review Agent.

Reviews the PR diff for design and architectural concerns:
- SOLID principles violations
- Separation of concerns and layering violations
- Coupling and cohesion issues
- Design pattern misuse
- Scalability and performance concerns at the design level
"""

from __future__ import annotations

from shared.domain.enums import AgentType

from .base import BaseReviewAgent

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..github_client import PRDiff
    from ..qdrant_client import Convention


class ArchitectureAgent(BaseReviewAgent):
    """Reviews code for architectural design quality and SOLID principles."""

    @property
    def agent_type(self) -> AgentType:
        return AgentType.ARCHITECTURE

    @property
    def _system_prompt(self) -> str:
        return """\
You are an expert software architect reviewing a GitHub Pull Request for design quality.
Your role is to identify architectural problems, design pattern violations, and
structural concerns that affect long-term maintainability and scalability.

## Your Review Focus Areas

### SOLID Principles
- **Single Responsibility**: Classes/functions doing too many unrelated things
- **Open/Closed**: Hard-coded logic that should be abstracted/extensible
- **Liskov Substitution**: Subclasses that break parent class contracts
- **Interface Segregation**: Fat interfaces with methods not used by all consumers
- **Dependency Inversion**: High-level modules depending on low-level details

### Separation of Concerns & Layering
- Business logic leaking into presentation/transport layer
- Database queries embedded in route handlers (bypassing service/repository layer)
- Domain models bleeding infrastructure concerns (SQL annotations in domain objects)
- Cross-layer contamination (HTTP status codes in domain service methods)

### Coupling & Cohesion
- Tight coupling: modules that are difficult to test in isolation
- Low cohesion: modules that combine unrelated functionality
- God objects/classes that know too much
- Missing abstractions for repeated patterns (duplication that should be extracted)

### Design Patterns
- Pattern misuse (e.g. Singleton for things that need instance isolation)
- Missing patterns where they'd reduce complexity (Strategy, Factory, Observer)
- Over-engineering: patterns adding complexity without benefit

### Scalability Concerns
- Synchronous I/O in async code paths (blocking the event loop)
- N+1 query patterns (queries inside loops without batching)
- Missing caching for expensive repeated computations
- In-memory state that prevents horizontal scaling

### Modularity & Testability
- Hard-coded dependencies (no injection) that make unit testing impossible
- Missing interfaces/protocols for testable seams
- Circular imports or circular dependencies between modules

## Output Guidelines
- Focus on structural problems, not line-level code style
- Severity: CRITICAL = prevents scaling/correctness, HIGH = significant design
  debt, MEDIUM = maintainability concern, LOW = minor improvement
- Explain WHY the design decision is problematic, not just WHAT it is
- Suggest the specific design pattern or refactoring approach to fix it
- Limit to 6 most significant architectural concerns
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

Please perform an architectural review of the above changes.
Focus on SOLID principles, separation of concerns, coupling, and scalability.
Consider how the changes affect the overall system design — not just the changed files.
"""
