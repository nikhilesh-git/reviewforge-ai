"""Security Review Agent (OWASP-focused).

Reviews the PR diff for security vulnerabilities mapped to the OWASP Top 10 2021:
- A01: Broken Access Control
- A02: Cryptographic Failures
- A03: Injection (SQL, Command, LDAP, XPath, etc.)
- A04: Insecure Design
- A05: Security Misconfiguration
- A06: Vulnerable and Outdated Components
- A07: Identification and Authentication Failures
- A08: Software and Data Integrity Failures
- A09: Security Logging and Monitoring Failures
- A10: Server-Side Request Forgery
"""

from __future__ import annotations

from shared.domain.enums import AgentType
from shared.domain.models import ReviewFinding

from .base import BaseReviewAgent, LLMAgentOutput

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import WorkerSettings
    from ..github_client import PRDiff
    from ..qdrant_client import Convention


class SecurityAgent(BaseReviewAgent):
    """Reviews code for security vulnerabilities using the OWASP Top 10 framework."""

    @property
    def agent_type(self) -> AgentType:
        return AgentType.SECURITY

    @property
    def _system_prompt(self) -> str:
        return """\
You are an expert security review agent specialising in application security.
Your role is to identify security vulnerabilities in GitHub Pull Request changes,
mapped to the OWASP Top 10 2021 categories.

## OWASP Top 10 Focus Areas

**A01 – Broken Access Control**
- Missing authorization checks before sensitive operations
- Insecure direct object references (IDOR) — using user-supplied IDs without ownership checks
- CORS misconfiguration, missing rate limiting on sensitive endpoints

**A02 – Cryptographic Failures**
- Sensitive data transmitted without TLS
- Weak/broken algorithms (MD5, SHA1, DES for passwords)
- Hardcoded secrets, API keys, passwords, or private keys in code
- Insecure random number generation for security-sensitive purposes

**A03 – Injection**
- SQL injection: f-strings/concatenation in SQL, missing parameterization
- Command injection: shell=True, os.system() with user input
- Path traversal: unsanitized file paths from user input
- LDAP, XPath, NoSQL injection patterns

**A04 – Insecure Design**
- Missing threat modelling for new authentication/authorization patterns
- Security-critical logic that's easy to bypass
- Missing input validation on public-facing endpoints

**A05 – Security Misconfiguration**
- Debug mode enabled in production code paths
- Default credentials, permissive CORS (allow-all origins on sensitive APIs)
- Sensitive data exposed in error messages or logs
- Overly permissive file permissions

**A07 – Auth Failures**
- Broken session management
- Missing account lockout or brute force protection
- Insecure "Remember Me" implementations
- JWT implementation flaws (algorithm=none, weak secrets)

**A08 – Integrity Failures**
- Deserialization of untrusted data (pickle, yaml.load without Loader)
- Missing integrity checks on downloaded resources

**A09 – Logging Failures**
- Insufficient logging of security events (auth failures, access control failures)
- Sensitive data (passwords, tokens, PII) logged in plaintext

**A10 – SSRF**
- User-controlled URLs passed to HTTP clients without allowlist validation
- Internal service endpoints accessible via user-provided URLs

## Output Guidelines
- CRITICAL = directly exploitable, no authentication needed
- HIGH = requires some conditions but readily exploitable
- MEDIUM = security weakness with mitigating factors
- LOW = defense-in-depth issue
- Always reference the OWASP category in your findings (use tags)
- Include CWE IDs when applicable (e.g. "CWE-89" for SQL injection)
- Be specific — provide the exact vulnerable code and a concrete remediation
- Only report findings with confidence >= 0.75
- Limit to 8 most critical findings
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

Please perform a security review of the above changes.
Focus on OWASP Top 10 vulnerabilities. For each finding, specify:
- The exact vulnerable code pattern
- The attack vector and impact
- A concrete remediation with code example

Map each finding to its OWASP category in the tags field (e.g. "owasp:A03-injection").
"""

    def _parse_findings(self, output: LLMAgentOutput) -> list[ReviewFinding]:
        """Override to enrich findings with OWASP category metadata."""
        from shared.domain.enums import OWASPCategory, Severity
        from shared.domain.models import CodeLocation

        findings = super()._parse_findings(output)

        # Enrich with OWASP category detection from tags
        owasp_tag_map = {
            "a01": OWASPCategory.A01_BROKEN_ACCESS_CONTROL,
            "a02": OWASPCategory.A02_CRYPTOGRAPHIC_FAILURES,
            "a03": OWASPCategory.A03_INJECTION,
            "a04": OWASPCategory.A04_INSECURE_DESIGN,
            "a05": OWASPCategory.A05_SECURITY_MISCONFIGURATION,
            "a06": OWASPCategory.A06_VULNERABLE_COMPONENTS,
            "a07": OWASPCategory.A07_IDENTIFICATION_FAILURES,
            "a08": OWASPCategory.A08_INTEGRITY_FAILURES,
            "a09": OWASPCategory.A09_LOGGING_FAILURES,
            "a10": OWASPCategory.A10_SSRF,
        }

        enriched: list[ReviewFinding] = []
        for finding in findings:
            owasp_category = None
            cwe_id = None

            for tag in finding.tags:
                tag_lower = tag.lower()
                for key, category in owasp_tag_map.items():
                    if key in tag_lower:
                        owasp_category = category
                        break
                if tag_lower.startswith("cwe-"):
                    cwe_id = tag.upper()

            # Rebuild with enriched OWASP metadata
            enriched.append(
                ReviewFinding(
                    id=finding.id,
                    agent_type=finding.agent_type,
                    severity=finding.severity,
                    location=finding.location,
                    title=finding.title,
                    description=finding.description,
                    suggestion=finding.suggestion,
                    confidence=finding.confidence,
                    tags=finding.tags,
                    owasp_category=owasp_category,
                    cwe_id=cwe_id,
                )
            )

        return enriched
