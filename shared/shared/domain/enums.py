"""Domain enumerations for the GitHub PR Code Reviewer platform.

All enums use ``StrEnum``-compatible pattern so they serialize to/from plain
strings without extra configuration — compatible with Pydantic v2, SQLAlchemy,
and JSON serialization out of the box.

Python 3.11+ has StrEnum in the stdlib. For 3.10 compatibility, we use a
simple str + Enum mixin which behaves identically for our purposes.
The Docker production images use Python 3.13 where StrEnum is native.
"""

import sys
from enum import Enum, auto

# Python 3.11+ has StrEnum natively; 3.10 uses the str+Enum mixin pattern
if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """Backport of StrEnum for Python <3.11."""
        @staticmethod
        def _generate_next_value_(name: str, *args: object) -> str:  # noqa: ANN002
            return name.lower()


class PRAction(StrEnum):
    """GitHub Pull Request event actions we process."""

    OPENED = "opened"
    SYNCHRONIZE = "synchronize"
    REOPENED = "reopened"
    CLOSED = "closed"
    MERGED = "closed"  # merged PRs also come as "closed" with merged=True

    @classmethod
    def reviewable(cls) -> frozenset["PRAction"]:
        """Return actions that should trigger an AI review."""
        return frozenset({cls.OPENED, cls.SYNCHRONIZE, cls.REOPENED})


class AgentType(StrEnum):
    """The four specialized AI review agents."""

    STATIC_ANALYSIS = "static_analysis"
    SECURITY = "security"
    ARCHITECTURE = "architecture"
    STYLE = "style"

    @property
    def display_name(self) -> str:
        """Human-readable name for display in GitHub comments."""
        return {
            AgentType.STATIC_ANALYSIS: "Static Analysis",
            AgentType.SECURITY: "Security Review",
            AgentType.ARCHITECTURE: "Architecture Review",
            AgentType.STYLE: "Code Style",
        }[self]

    @property
    def emoji(self) -> str:
        """Emoji prefix for GitHub comment headers."""
        return {
            AgentType.STATIC_ANALYSIS: "🔍",
            AgentType.SECURITY: "🔒",
            AgentType.ARCHITECTURE: "🏗️",
            AgentType.STYLE: "✨",
        }[self]


class Severity(StrEnum):
    """Finding severity levels, ordered from most to least critical."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def weight(self) -> int:
        """Numeric weight for sorting and scoring (higher = more severe)."""
        return {
            Severity.CRITICAL: 100,
            Severity.HIGH: 75,
            Severity.MEDIUM: 50,
            Severity.LOW: 25,
            Severity.INFO: 5,
        }[self]

    @property
    def emoji(self) -> str:
        """Emoji for GitHub comment severity badges."""
        return {
            Severity.CRITICAL: "🚨",
            Severity.HIGH: "⚠️",
            Severity.MEDIUM: "🔶",
            Severity.LOW: "🔷",
            Severity.INFO: "ℹ️",
        }[self]

    @classmethod
    def blocking(cls) -> frozenset["Severity"]:
        """Severities that should block PR merge (future feature)."""
        return frozenset({cls.CRITICAL, cls.HIGH})


class JobStatus(StrEnum):
    """Lifecycle states of a PR review job."""

    PENDING = auto()
    FETCHING_DIFF = auto()
    LOADING_CONTEXT = auto()
    REVIEWING = auto()
    PUBLISHING = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()

    @property
    def is_terminal(self) -> bool:
        """Whether this status represents a final (non-retryable) state."""
        return self in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


class OWASPCategory(StrEnum):
    """OWASP Top 10 2021 categories for security findings."""

    A01_BROKEN_ACCESS_CONTROL = "A01:2021 – Broken Access Control"
    A02_CRYPTOGRAPHIC_FAILURES = "A02:2021 – Cryptographic Failures"
    A03_INJECTION = "A03:2021 – Injection"
    A04_INSECURE_DESIGN = "A04:2021 – Insecure Design"
    A05_SECURITY_MISCONFIGURATION = "A05:2021 – Security Misconfiguration"
    A06_VULNERABLE_COMPONENTS = "A06:2021 – Vulnerable and Outdated Components"
    A07_IDENTIFICATION_FAILURES = "A07:2021 – Identification and Authentication Failures"
    A08_INTEGRITY_FAILURES = "A08:2021 – Software and Data Integrity Failures"
    A09_LOGGING_FAILURES = "A09:2021 – Security Logging and Monitoring Failures"
    A10_SSRF = "A10:2021 – Server-Side Request Forgery"


class EventProcessingStatus(StrEnum):
    """Status of a raw GitHub webhook event."""

    RECEIVED = auto()
    QUEUED = auto()
    DUPLICATE = auto()
    IGNORED = auto()
    FAILED = auto()
