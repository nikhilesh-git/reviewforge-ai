"""Abstract base class for all PR review agents.

Each agent is responsible for reviewing the PR diff from a specific angle
(security, architecture, style, static analysis) and returning structured
findings as ``AgentResult`` domain objects.

Design decisions:
- All agents share the same LangChain OpenAI client (pointing at OpenRouter).
- Structured output is achieved via ``model.with_structured_output()`` using
  Pydantic schemas — this uses the model's JSON mode / function calling.
- Retry logic uses ``tenacity`` with exponential backoff.
- Langfuse tracing is conditionally enabled based on config.
- Each agent has its own system prompt that specialises its review focus.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

import structlog
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from shared.domain.enums import AgentType, Severity
from shared.domain.models import AgentResult, CodeLocation, ReviewFinding

if TYPE_CHECKING:
    from ..config import WorkerSettings
    from ..github_client import PRDiff
    from ..qdrant_client import Convention

logger = structlog.get_logger(__name__)


# ─── Structured Output Schemas ───────────────────────────────────────────────
# These are the Pydantic schemas that the LLM must fill in.
# They are separate from domain models to allow flexible LLM schema iteration.


class LLMCodeLocation(BaseModel):
    """Location of a finding within the diff."""

    file_path: str = Field(..., description="Relative file path (e.g. src/auth/login.py)")
    line_start: int = Field(..., gt=0, description="1-indexed start line in the file")
    line_end: int | None = Field(None, description="1-indexed end line (None for single line)")
    side: str = Field(default="RIGHT", description="LEFT or RIGHT diff side")


class LLMFinding(BaseModel):
    """A single code review finding from an agent."""

    title: str = Field(..., max_length=200, description="Short, specific finding title")
    description: str = Field(
        ...,
        min_length=20,
        description=(
            "Detailed explanation of the issue. Include WHY it's a problem, "
            "not just WHAT the issue is."
        ),
    )
    severity: str = Field(
        ...,
        description="One of: critical, high, medium, low, info",
    )
    location: LLMCodeLocation | None = Field(
        None,
        description=(
            "Precise code location. Required for line-level findings. "
            "Null only for PR-level findings (e.g. missing test coverage overall)."
        ),
    )
    suggestion: str | None = Field(
        None,
        description=(
            "Concrete code fix or improvement. Use markdown code blocks for code examples. "
            "Be specific — don't just say 'add error handling'."
        ),
    )
    confidence: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="Your confidence in this finding (0.0–1.0)",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Optional categorization tags (e.g. ['sql-injection', 'input-validation'])",
    )


class LLMAgentOutput(BaseModel):
    """Complete structured output from an agent review pass."""

    findings: list[LLMFinding] = Field(
        default_factory=list,
        description=(
            "List of review findings. Be selective — only report genuine issues. "
            "Empty list is valid if the diff looks clean."
        ),
    )
    summary: str = Field(
        ...,
        min_length=10,
        description=(
            "Overall summary of your review of this area. "
            "1-3 sentences covering the overall assessment."
        ),
    )


# ─── Base Agent ───────────────────────────────────────────────────────────────


class BaseReviewAgent(ABC):
    """Abstract base class for all PR review agents.

    Subclasses must implement:
    - ``agent_type``: The ``AgentType`` enum value for this agent.
    - ``_system_prompt``: The system prompt defining the agent's review focus.
    - ``_build_user_prompt()``: Method that constructs the per-PR user message.

    The base class provides:
    - LLM client initialization
    - Structured output parsing
    - Retry logic
    - Metrics recording
    - Langfuse tracing
    """

    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._llm = self._create_llm(settings.primary_model)
        self._fallback_llm = self._create_llm(settings.fallback_model)
        self._structured_llm = self._llm.with_structured_output(LLMAgentOutput)
        self._structured_fallback = self._fallback_llm.with_structured_output(LLMAgentOutput)

    def _create_llm(self, model: str) -> ChatOpenAI:
        """Create a LangChain ChatOpenAI client pointing at OpenRouter."""
        return ChatOpenAI(
            model=model,
            openai_api_key=self._settings.openrouter_api_key,  # type: ignore[arg-type]
            openai_api_base=self._settings.openrouter_base_url,
            max_tokens=self._settings.llm_max_tokens,
            temperature=self._settings.llm_temperature,
            timeout=self._settings.llm_request_timeout,
            default_headers={
                "HTTP-Referer": "https://github.com/pr-code-reviewer",
                "X-Title": "PR Code Reviewer",
            },
        )

    @property
    @abstractmethod
    def agent_type(self) -> AgentType:
        """The type of this review agent."""

    @property
    @abstractmethod
    def _system_prompt(self) -> str:
        """System prompt that defines this agent's review persona and focus."""

    @abstractmethod
    def _build_user_prompt(
        self,
        pr_diff: PRDiff,
        conventions: list[Convention],
    ) -> str:
        """Build the user message for this agent given a PR diff and conventions.

        Args:
            pr_diff: The complete PR diff to review.
            conventions: Relevant repository conventions from Qdrant.

        Returns:
            The user message string to send to the LLM.
        """

    async def review(
        self,
        pr_diff: PRDiff,
        conventions: list[Convention],
    ) -> AgentResult:
        """Run this agent's review on a PR diff.

        Args:
            pr_diff: The complete PR diff to review.
            conventions: Repo conventions retrieved from Qdrant (may be empty).

        Returns:
            An ``AgentResult`` with all findings from this agent.
        """
        from shared.infrastructure.metrics import (
            WORKER_AGENT_DURATION_SECONDS,
            WORKER_FINDINGS_TOTAL,
            WORKER_LLM_REQUESTS_TOTAL,
            WORKER_LLM_TOKENS_TOTAL,
        )

        log = logger.bind(
            agent=self.agent_type.value,
            repo=pr_diff.repo_full_name,
            pr_number=pr_diff.pr_number,
        )

        start_time = time.perf_counter()
        tokens_used = 0
        model_used = self._settings.primary_model

        try:
            log.info("Starting agent review")
            llm_output = await self._invoke_with_retry(pr_diff, conventions)
            model_used = self._settings.primary_model

            WORKER_LLM_REQUESTS_TOTAL.labels(
                agent_type=self.agent_type.value,
                model=model_used,
                status="success",
            ).inc()

        except Exception as exc:
            log.warning(
                "Primary model failed, trying fallback",
                error=str(exc),
                primary_model=self._settings.primary_model,
                fallback_model=self._settings.fallback_model,
            )
            WORKER_LLM_REQUESTS_TOTAL.labels(
                agent_type=self.agent_type.value,
                model=self._settings.primary_model,
                status="error",
            ).inc()

            try:
                llm_output = await self._invoke_fallback_with_retry(pr_diff, conventions)
                model_used = self._settings.fallback_model
                WORKER_LLM_REQUESTS_TOTAL.labels(
                    agent_type=self.agent_type.value,
                    model=model_used,
                    status="success",
                ).inc()
            except Exception as fallback_exc:
                log.error(
                    "Both primary and fallback models failed",
                    primary_error=str(exc),
                    fallback_error=str(fallback_exc),
                )
                WORKER_LLM_REQUESTS_TOTAL.labels(
                    agent_type=self.agent_type.value,
                    model=self._settings.fallback_model,
                    status="error",
                ).inc()
                latency = (time.perf_counter() - start_time) * 1000
                WORKER_AGENT_DURATION_SECONDS.labels(
                    agent_type=self.agent_type.value
                ).observe(latency / 1000)
                return AgentResult(
                    agent_type=self.agent_type,
                    findings=[],
                    summary=f"Agent failed: {fallback_exc!s}",
                    tokens_used=0,
                    latency_ms=latency,
                    model_used=model_used,
                    error=str(fallback_exc),
                )

        # Convert LLM output to domain models
        findings = self._parse_findings(llm_output)

        latency_ms = (time.perf_counter() - start_time) * 1000

        # Record per-finding metrics
        for finding in findings:
            WORKER_FINDINGS_TOTAL.labels(
                agent_type=self.agent_type.value,
                severity=finding.severity.value,
            ).inc()

        WORKER_AGENT_DURATION_SECONDS.labels(
            agent_type=self.agent_type.value
        ).observe(latency_ms / 1000)

        log.info(
            "Agent review complete",
            findings=len(findings),
            latency_ms=round(latency_ms, 1),
            model=model_used,
        )

        return AgentResult(
            agent_type=self.agent_type,
            findings=findings,
            summary=llm_output.summary,
            tokens_used=tokens_used,
            latency_ms=latency_ms,
            model_used=model_used,
        )

    @retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_exponential(multiplier=1, min=3, max=30),
        stop=stop_after_attempt(2),
        reraise=True,
    )
    async def _invoke_with_retry(
        self,
        pr_diff: PRDiff,
        conventions: list[Convention],
    ) -> LLMAgentOutput:
        """Invoke the primary LLM with retry logic."""
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [
            SystemMessage(content=self._system_prompt),
            HumanMessage(content=self._build_user_prompt(pr_diff, conventions)),
        ]
        result = await self._structured_llm.ainvoke(messages)
        return result  # type: ignore[return-value]

    @retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_exponential(multiplier=1, min=3, max=30),
        stop=stop_after_attempt(2),
        reraise=True,
    )
    async def _invoke_fallback_with_retry(
        self,
        pr_diff: PRDiff,
        conventions: list[Convention],
    ) -> LLMAgentOutput:
        """Invoke the fallback LLM with retry logic."""
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [
            SystemMessage(content=self._system_prompt),
            HumanMessage(content=self._build_user_prompt(pr_diff, conventions)),
        ]
        result = await self._structured_fallback.ainvoke(messages)
        return result  # type: ignore[return-value]

    def _parse_findings(self, output: LLMAgentOutput) -> list[ReviewFinding]:
        """Convert LLM output findings to domain model objects.

        Filters out invalid findings and normalises severity values.
        """
        findings: list[ReviewFinding] = []
        valid_severities = {s.value for s in Severity}

        for llm_finding in output.findings:
            severity_val = llm_finding.severity.lower()
            if severity_val not in valid_severities:
                logger.warning(
                    "Ignoring finding with invalid severity",
                    severity=llm_finding.severity,
                    title=llm_finding.title,
                )
                severity_val = "medium"  # Safe fallback

            location: CodeLocation | None = None
            if llm_finding.location:
                try:
                    location = CodeLocation(
                        file_path=llm_finding.location.file_path,
                        line_start=llm_finding.location.line_start,
                        line_end=llm_finding.location.line_end,
                        side=llm_finding.location.side,
                    )
                except Exception as exc:
                    logger.debug(
                        "Skipping invalid code location",
                        error=str(exc),
                        title=llm_finding.title,
                    )

            try:
                finding = ReviewFinding(
                    agent_type=self.agent_type,
                    severity=Severity(severity_val),
                    location=location,
                    title=llm_finding.title,
                    description=llm_finding.description,
                    suggestion=llm_finding.suggestion,
                    confidence=llm_finding.confidence,
                    tags=llm_finding.tags,
                )
                findings.append(finding)
            except Exception as exc:
                logger.warning(
                    "Failed to parse finding",
                    error=str(exc),
                    title=llm_finding.title,
                )

        return findings
