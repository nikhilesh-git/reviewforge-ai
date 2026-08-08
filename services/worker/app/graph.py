"""LangGraph orchestration DAG for the PR review pipeline.

This module defines the multi-agent review graph using LangGraph.
The graph implements a fan-out/fan-in pattern:

    fetch_diff ─► load_context ─┬─► static_analysis ─┐
                                ├─► security         ─┤─► merge_findings
                                ├─► architecture     ─┤
                                └─► style            ─┘

All four agents run in parallel after the context is loaded, dramatically
reducing end-to-end latency vs sequential execution.

State flows through the graph as a typed dict. Each node reads from
and writes to this shared state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph

from shared.domain.enums import AgentType
from shared.domain.models import AgentResult, ReviewFinding

from .agents.architecture import ArchitectureAgent
from .agents.security import SecurityAgent
from .agents.static_analysis import StaticAnalysisAgent
from .agents.style import StyleAgent
from .github_client import GitHubClient, PRDiff, create_github_client
from .merger import merge_findings
from .qdrant_client import Convention, QdrantConventionStore

logger = structlog.get_logger(__name__)


# ─── Graph State ─────────────────────────────────────────────────────────────


class ReviewState(TypedDict, total=False):
    """Shared state that flows through the LangGraph review pipeline.

    All fields are optional (``total=False``) to allow partial updates
    as nodes add data incrementally.
    """

    # Input fields (set before graph run)
    repo_full_name: str
    pr_number: int
    head_sha: str
    base_sha: str
    author_login: str
    installation_id: int | None

    # Intermediate state (populated by nodes)
    pr_diff: PRDiff | None
    conventions: list[Convention]

    # Agent results (one per agent, populated in parallel)
    static_analysis_result: AgentResult | None
    security_result: AgentResult | None
    architecture_result: AgentResult | None
    style_result: AgentResult | None

    # Final output
    agent_results: list[AgentResult]
    merged_findings: list[ReviewFinding]

    # Error tracking
    fetch_error: str | None
    context_error: str | None


# ─── Graph Nodes ──────────────────────────────────────────────────────────────


async def fetch_diff_node(
    state: ReviewState,
    *,
    github_client: GitHubClient,
) -> dict[str, Any]:
    """Fetch the PR diff from the GitHub API.

    This is the first node in the graph. It fetches all changed files
    and their patches and stores the result in the state.
    """
    log = logger.bind(
        repo=state["repo_full_name"],
        pr_number=state["pr_number"],
    )
    log.info("Fetching PR diff from GitHub")

    try:
        pr_diff = await github_client.fetch_pr_diff(
            repo_full_name=state["repo_full_name"],
            pr_number=state["pr_number"],
            head_sha=state["head_sha"],
        )
        log.info(
            "PR diff fetched",
            files=len(pr_diff.files),
            additions=pr_diff.total_additions,
            deletions=pr_diff.total_deletions,
        )
        return {"pr_diff": pr_diff, "fetch_error": None}
    except Exception as exc:
        log.error("Failed to fetch PR diff", error=str(exc))
        return {"pr_diff": None, "fetch_error": str(exc)}


async def load_context_node(
    state: ReviewState,
    *,
    qdrant_store: QdrantConventionStore,
    embedding_client: Any,
) -> dict[str, Any]:
    """Load repository conventions from Qdrant vector store.

    Uses the PR metadata (repo name, file types changed) to build
    a context query and retrieves relevant conventions.

    If Qdrant is unavailable, gracefully degrades to an empty list.
    """
    log = logger.bind(repo=state["repo_full_name"])

    try:
        pr_diff = state.get("pr_diff")
        if not pr_diff:
            log.warning("No PR diff available — skipping context loading")
            return {"conventions": [], "context_error": "No diff available"}

        # Build a query from the file types and a sample of changed code
        file_extensions = sorted(
            {f.filename.rsplit(".", 1)[-1] for f in pr_diff.files if "." in f.filename}
        )
        query_text = (
            f"Code review conventions for {state['repo_full_name']} "
            f"in files: {', '.join(file_extensions[:5])}"
        )

        # Generate embedding for the query
        query_vector = await _embed_text(embedding_client, query_text)
        if query_vector is None:
            return {"conventions": [], "context_error": "Embedding failed"}

        conventions = await qdrant_store.search_conventions(
            query_vector=query_vector,
            repo_full_name=state["repo_full_name"],
            top_k=5,
            score_threshold=0.65,
        )

        log.info("Loaded repository conventions", count=len(conventions))
        return {"conventions": conventions, "context_error": None}

    except Exception as exc:
        log.warning("Failed to load repo context (non-fatal)", error=str(exc))
        return {"conventions": [], "context_error": str(exc)}


async def static_analysis_node(
    state: ReviewState,
    *,
    agent: StaticAnalysisAgent,
) -> dict[str, Any]:
    """Run the Static Analysis agent."""
    pr_diff = state.get("pr_diff")
    if not pr_diff:
        return {
            "static_analysis_result": AgentResult(
                agent_type=AgentType.STATIC_ANALYSIS,
                findings=[],
                summary="Skipped — no diff available",
                tokens_used=0,
                latency_ms=0.0,
                model_used="n/a",
                error="No diff",
            )
        }

    result = await agent.review(pr_diff, state.get("conventions", []))
    return {"static_analysis_result": result}


async def security_node(
    state: ReviewState,
    *,
    agent: SecurityAgent,
) -> dict[str, Any]:
    """Run the Security agent."""
    pr_diff = state.get("pr_diff")
    if not pr_diff:
        return {
            "security_result": AgentResult(
                agent_type=AgentType.SECURITY,
                findings=[],
                summary="Skipped — no diff available",
                tokens_used=0,
                latency_ms=0.0,
                model_used="n/a",
                error="No diff",
            )
        }

    result = await agent.review(pr_diff, state.get("conventions", []))
    return {"security_result": result}


async def architecture_node(
    state: ReviewState,
    *,
    agent: ArchitectureAgent,
) -> dict[str, Any]:
    """Run the Architecture agent."""
    pr_diff = state.get("pr_diff")
    if not pr_diff:
        return {
            "architecture_result": AgentResult(
                agent_type=AgentType.ARCHITECTURE,
                findings=[],
                summary="Skipped — no diff available",
                tokens_used=0,
                latency_ms=0.0,
                model_used="n/a",
                error="No diff",
            )
        }

    result = await agent.review(pr_diff, state.get("conventions", []))
    return {"architecture_result": result}


async def style_node(
    state: ReviewState,
    *,
    agent: StyleAgent,
) -> dict[str, Any]:
    """Run the Style agent."""
    pr_diff = state.get("pr_diff")
    if not pr_diff:
        return {
            "style_result": AgentResult(
                agent_type=AgentType.STYLE,
                findings=[],
                summary="Skipped — no diff available",
                tokens_used=0,
                latency_ms=0.0,
                model_used="n/a",
                error="No diff",
            )
        }

    result = await agent.review(pr_diff, state.get("conventions", []))
    return {"style_result": result}


async def merge_findings_node(state: ReviewState) -> dict[str, Any]:
    """Collect all agent results and merge/deduplicate findings."""
    all_results: list[AgentResult] = []

    for key in (
        "static_analysis_result",
        "security_result",
        "architecture_result",
        "style_result",
    ):
        result = state.get(key)
        if result is not None:
            all_results.append(result)

    merged = merge_findings(all_results)

    logger.info(
        "Review complete",
        total_agent_results=len(all_results),
        total_merged_findings=len(merged),
    )

    return {
        "agent_results": all_results,
        "merged_findings": merged,
    }


# ─── Graph Factory ────────────────────────────────────────────────────────────


async def _embed_text(embedding_client: Any, text: str) -> list[float] | None:
    """Generate a text embedding using the provided client.

    The embedding_client is now the free local ``aembed_text`` async function
    from ``app.embeddings`` (sentence-transformers, no API cost).
    """
    try:
        # embedding_client is a plain async callable: async def aembed_text(text) -> list[float]
        result = await embedding_client(text)
        return result
    except Exception as exc:
        logger.warning("Embedding failed", error=str(exc))
        return None



def build_review_graph(
    github_client: GitHubClient,
    qdrant_store: QdrantConventionStore,
    embedding_client: Any,
    static_agent: StaticAnalysisAgent,
    security_agent: SecurityAgent,
    architecture_agent: ArchitectureAgent,
    style_agent: StyleAgent,
) -> Any:
    """Build and compile the LangGraph review DAG.

    The graph implements a sequential fetch → parallel review → merge pattern.

    Args:
        github_client: Authenticated GitHub API client.
        qdrant_store: Qdrant client for convention retrieval.
        embedding_client: LangChain embeddings client.
        static_agent: Static analysis agent instance.
        security_agent: Security agent instance.
        architecture_agent: Architecture agent instance.
        style_agent: Style agent instance.

    Returns:
        A compiled LangGraph ``CompiledGraph`` ready to run with ``.ainvoke()``.
    """
    from functools import partial

    graph = StateGraph(ReviewState)

    # Add nodes with bound dependencies (partial application)
    graph.add_node(
        "fetch_diff",
        partial(fetch_diff_node, github_client=github_client),
    )
    graph.add_node(
        "load_context",
        partial(load_context_node, qdrant_store=qdrant_store, embedding_client=embedding_client),
    )
    graph.add_node(
        "static_analysis",
        partial(static_analysis_node, agent=static_agent),
    )
    graph.add_node(
        "security",
        partial(security_node, agent=security_agent),
    )
    graph.add_node(
        "architecture",
        partial(architecture_node, agent=architecture_agent),
    )
    graph.add_node(
        "style",
        partial(style_node, agent=style_agent),
    )
    graph.add_node("merge_findings", merge_findings_node)

    # Define edges: sequential → parallel fan-out → merge
    graph.add_edge(START, "fetch_diff")
    graph.add_edge("fetch_diff", "load_context")

    # Fan-out: all 4 agents run after context is loaded
    graph.add_edge("load_context", "static_analysis")
    graph.add_edge("load_context", "security")
    graph.add_edge("load_context", "architecture")
    graph.add_edge("load_context", "style")

    # Fan-in: all agents must complete before merge
    graph.add_edge("static_analysis", "merge_findings")
    graph.add_edge("security", "merge_findings")
    graph.add_edge("architecture", "merge_findings")
    graph.add_edge("style", "merge_findings")

    graph.add_edge("merge_findings", END)

    return graph.compile()


class ReviewOrchestrator:
    """High-level orchestrator that builds and runs the review graph.

    This is the main entry point for the Celery task.
    """

    def __init__(self, settings: Any) -> None:

        self._settings = settings

        # Shared GitHub client
        self._github_client = create_github_client(pat=settings.github_pat)

        # Qdrant store
        self._qdrant_store = QdrantConventionStore(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            collection_name=settings.qdrant_collection_name,
            embedding_dim=settings.qdrant_embedding_dim,
        )

        # Free local embeddings — uses sentence-transformers (all-MiniLM-L6-v2)
        # Runs on CPU, no API key needed, no cost.
        from .embeddings import aembed_text as _local_embed
        self._embedding_client = _local_embed

        # Agents
        self._static_agent = StaticAnalysisAgent(settings)
        self._security_agent = SecurityAgent(settings)
        self._architecture_agent = ArchitectureAgent(settings)
        self._style_agent = StyleAgent(settings)

        # Compiled graph
        self._graph = build_review_graph(
            github_client=self._github_client,
            qdrant_store=self._qdrant_store,
            embedding_client=self._embedding_client,
            static_agent=self._static_agent,
            security_agent=self._security_agent,
            architecture_agent=self._architecture_agent,
            style_agent=self._style_agent,
        )

    async def run(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
        base_sha: str,
        author_login: str,
        installation_id: int | None = None,
    ) -> tuple[list[AgentResult], list[ReviewFinding]]:
        """Run the full review pipeline for a PR.

        Args:
            repo_full_name: Repository in ``owner/repo`` format.
            pr_number: Pull request number.
            head_sha: Head commit SHA to review.
            base_sha: Base commit SHA.
            author_login: GitHub login of PR author.
            installation_id: GitHub App installation ID if using App auth.

        Returns:
            Tuple of (agent_results, merged_findings).
        """
        # Ensure Qdrant collection exists
        try:
            await self._qdrant_store.ensure_collection()
        except Exception as exc:
            logger.warning("Could not ensure Qdrant collection", error=str(exc))

        initial_state: ReviewState = {
            "repo_full_name": repo_full_name,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "author_login": author_login,
            "installation_id": installation_id,
            "pr_diff": None,
            "conventions": [],
            "static_analysis_result": None,
            "security_result": None,
            "architecture_result": None,
            "style_result": None,
            "agent_results": [],
            "merged_findings": [],
            "fetch_error": None,
            "context_error": None,
        }

        final_state: ReviewState = await self._graph.ainvoke(initial_state)

        return final_state.get("agent_results", []), final_state.get("merged_findings", [])
