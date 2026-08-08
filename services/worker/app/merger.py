"""Finding deduplication and merger for the LangGraph review pipeline.

After the four agents run in parallel, their findings are merged into a single
de-duplicated list. This module handles:

1. **Fingerprint-based deduplication**: Two findings with the same fingerprint
   (agent_type + severity + location + title) are considered duplicates.
   When agents overlap, the finding with the highest confidence is kept.

2. **Cross-agent deduplication**: The same issue found by multiple agents
   (e.g. security AND static analysis both flag an injection risk) is merged
   into a single finding attributed to the agent with the highest confidence,
   while preserving the cross-reference.

3. **Priority sorting**: Findings are returned sorted by severity weight
   (critical first) then by confidence score.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from shared.domain.models import AgentResult, ReviewFinding

logger = logging.getLogger(__name__)


def merge_findings(agent_results: list[AgentResult]) -> list[ReviewFinding]:
    """Merge and deduplicate findings from all four review agents.

    Args:
        agent_results: The results returned by each agent in the parallel review.

    Returns:
        A sorted, deduplicated list of unique ``ReviewFinding`` objects.
        Sorted by severity (critical first), then by confidence (high first).
    """
    if not agent_results:
        return []

    # Step 1: Collect all findings with their fingerprints
    # fingerprint → list of (finding, agent_result)
    fingerprint_groups: dict[str, list[ReviewFinding]] = defaultdict(list)

    total_raw = 0
    for result in agent_results:
        for finding in result.findings:
            fingerprint_groups[finding.fingerprint].append(finding)
            total_raw += 1

    # Step 2: For each fingerprint group, pick the best finding
    merged: list[ReviewFinding] = []
    for fingerprint, duplicates in fingerprint_groups.items():
        if len(duplicates) == 1:
            merged.append(duplicates[0])
        else:
            # Multiple agents found the same issue — keep the one with highest confidence
            best = max(duplicates, key=lambda f: f.confidence)

            # Enrich tags with cross-reference info (which agents also found this)
            cross_ref_agents = sorted(
                {f.agent_type.value for f in duplicates if f.agent_type != best.agent_type}
            )

            enriched_tags = list(best.tags)
            for other_agent in cross_ref_agents:
                tag = f"also:{other_agent}"
                if tag not in enriched_tags:
                    enriched_tags.append(tag)

            # Rebuild with enriched tags (Pydantic frozen model requires recreation)
            merged.append(
                ReviewFinding(
                    id=best.id,
                    agent_type=best.agent_type,
                    severity=best.severity,
                    location=best.location,
                    title=best.title,
                    description=best.description,
                    suggestion=best.suggestion,
                    owasp_category=best.owasp_category,
                    cwe_id=best.cwe_id,
                    confidence=min(1.0, best.confidence + 0.05 * len(cross_ref_agents)),
                    tags=enriched_tags,
                )
            )

    # Step 3: Sort by severity weight (descending) then confidence (descending)
    from shared.domain.enums import Severity

    severity_order = {
        Severity.CRITICAL: 0,
        Severity.HIGH: 1,
        Severity.MEDIUM: 2,
        Severity.LOW: 3,
        Severity.INFO: 4,
    }

    merged.sort(
        key=lambda f: (severity_order.get(f.severity, 5), -f.confidence)
    )

    logger.info(
        "Findings merged",
        extra={
            "raw_total": total_raw,
            "after_dedup": len(merged),
            "duplicates_removed": total_raw - len(merged),
        },
    )

    return merged
