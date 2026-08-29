"""GitHub review comment formatter.

Converts ``ReviewFinding`` domain objects into rich GitHub review comment
markdown. Each comment is formatted with:
- Severity badge with emoji
- OWASP category label (for security findings)
- Detailed description
- Code suggestion in a ``suggestion`` block (for inline suggestions)
- Agent attribution footer

GitHub review comment rendering reference:
https://docs.github.com/en/rest/pulls/reviews
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.domain.enums import AgentType, Severity
from shared.domain.models import AgentResult, ReviewFinding


# ─── Severity Badge Rendering ─────────────────────────────────────────────────

_SEVERITY_BADGE = {
    Severity.CRITICAL: "🚨 **CRITICAL**",
    Severity.HIGH: "⚠️ **HIGH**",
    Severity.MEDIUM: "🔶 **MEDIUM**",
    Severity.LOW: "🔷 **LOW**",
    Severity.INFO: "ℹ️ **INFO**",
}

_AGENT_EMOJI = {
    AgentType.STATIC_ANALYSIS: "🔍",
    AgentType.SECURITY: "🔒",
    AgentType.ARCHITECTURE: "🏗️",
    AgentType.STYLE: "✨",
}

_AGENT_LABEL = {
    AgentType.STATIC_ANALYSIS: "Static Analysis",
    AgentType.SECURITY: "Security Review",
    AgentType.ARCHITECTURE: "Architecture Review",
    AgentType.STYLE: "Code Style",
}


@dataclass
class FormattedComment:
    """A formatted review comment ready to post to GitHub.

    Attributes:
        body: Markdown body for the comment.
        path: File path for inline comments (None for PR-level comments).
        line: Line number in the diff for inline comments.
        side: Diff side: LEFT or RIGHT.
    """

    body: str
    path: str | None = None
    line: int | None = None
    side: str = "RIGHT"

    @property
    def is_inline(self) -> bool:
        """True if this is an inline file comment (has path + line)."""
        return self.path is not None and self.line is not None


def format_finding_comment(finding: ReviewFinding) -> FormattedComment:
    """Format a single ``ReviewFinding`` as a GitHub review comment.

    Args:
        finding: The review finding to format.

    Returns:
        A ``FormattedComment`` with rendered markdown body.
    """
    severity_badge = _SEVERITY_BADGE.get(finding.severity, f"**{finding.severity.upper()}**")
    agent_emoji = _AGENT_EMOJI.get(finding.agent_type, "🤖")
    agent_label = _AGENT_LABEL.get(finding.agent_type, finding.agent_type.value)

    lines: list[str] = []

    # Header: severity badge + title
    lines.append(f"{severity_badge} — {finding.title}")
    lines.append("")

    # OWASP label for security findings
    if finding.owasp_category:
        lines.append(f"> 🔐 **{finding.owasp_category.value}**")
        if finding.cwe_id:
            lines.append(f"> 📌 {finding.cwe_id}")
        lines.append("")

    # Description
    lines.append(finding.description)

    # Suggestion block
    if finding.suggestion:
        lines.append("")
        lines.append("**💡 Suggestion:**")
        lines.append("")
        lines.append(finding.suggestion)

    # Tags (show cross-agent references)
    cross_refs = [t for t in finding.tags if t.startswith("also:")]
    if cross_refs:
        other_agents = [t.replace("also:", "").replace("_", " ").title() for t in cross_refs]
        lines.append("")
        lines.append(f"*Also flagged by: {', '.join(other_agents)}*")

    # Footer
    confidence_pct = int(finding.confidence * 100)
    lines.append("")
    lines.append(
        f"---\n*{agent_emoji} {agent_label} · Confidence: {confidence_pct}%*"
    )

    body = "\n".join(lines)

    # Build location info
    path = None
    line = None
    side = "RIGHT"

    if finding.location:
        path = finding.location.file_path
        line = finding.location.line_start
        side = finding.location.side

    return FormattedComment(body=body, path=path, line=line, side=side)


def format_review_summary(
    agent_results: list[AgentResult],
    merged_findings: list[ReviewFinding],
    *,
    repo_full_name: str,
    pr_number: int,
) -> tuple[str, str]:
    """Format the overall PR review summary comment.

    This is posted as the main PR Review body (not an inline comment).

    Args:
        agent_results: Results from all four agents.
        merged_findings: Final merged and deduplicated findings.
        repo_full_name: Repository in ``owner/repo`` format.
        pr_number: Pull request number.

    Returns:
        Markdown string for the review body.
    """
    from shared.domain.enums import Severity

    # Count by severity
    severity_counts: dict[Severity, int] = {}
    for finding in merged_findings:
        severity_counts[finding.severity] = severity_counts.get(finding.severity, 0) + 1

    # Determine overall verdict
    has_critical = severity_counts.get(Severity.CRITICAL, 0) > 0
    has_high = severity_counts.get(Severity.HIGH, 0) > 0
    if has_critical:
        verdict = "🚫 **REQUEST CHANGES** — Critical issues found"
        verdict_style = "REQUEST_CHANGES"
    elif has_high:
        verdict = "⚠️ **REQUEST CHANGES** — High severity issues found"
        verdict_style = "REQUEST_CHANGES"
    elif merged_findings:
        verdict = "🔶 **Comment** — Issues found for your consideration"
        verdict_style = "COMMENT"
    else:
        verdict = "✅ **Approved** — No significant issues found"
        verdict_style = "APPROVE"

    lines: list[str] = []
    lines.append("## 🤖 AI Code Review")
    lines.append("")
    lines.append(verdict)
    lines.append("")

    # Summary table
    if merged_findings:
        lines.append("### Finding Summary")
        lines.append("")
        lines.append("| Severity | Count |")
        lines.append("|----------|-------|")
        for sev in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO):
            count = severity_counts.get(sev, 0)
            if count > 0:
                badge = _SEVERITY_BADGE.get(sev, sev.value)
                lines.append(f"| {badge} | {count} |")
        lines.append("")

    # Agent summaries
    lines.append("### Agent Summaries")
    lines.append("")
    for result in agent_results:
        agent_emoji = _AGENT_EMOJI.get(result.agent_type, "🤖")
        agent_label = _AGENT_LABEL.get(result.agent_type, result.agent_type.value)
        finding_count = len(result.findings)
        latency = round(result.latency_ms / 1000, 1)

        lines.append(
            f"**{agent_emoji} {agent_label}** "
            f"— {finding_count} finding{'s' if finding_count != 1 else ''} "
            f"· {latency}s · {result.model_used}"
        )
        lines.append(f"> {result.summary}")
        lines.append("")

    lines.append("---")
    lines.append(
        "*This review was performed automatically by the "
        "[PR Code Reviewer](https://github.com/pr-code-reviewer) AI platform.*"
    )

    return "\n".join(lines), verdict_style
