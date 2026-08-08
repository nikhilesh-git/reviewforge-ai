"""Prometheus metrics definitions shared across services.

Metrics naming convention follows Prometheus best practices:
- ``<service>_<noun>_<unit>_<suffix>``
- Suffixes: _total (counter), _seconds (duration), _bytes (size)
- Labels: keep low cardinality (< 20 label values per metric)

All metrics are registered once at module import time.
Services import the specific metrics they need.

Reference: https://prometheus.io/docs/practices/naming/
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info

# ─── System Info ─────────────────────────────────────────────────────────────

BUILD_INFO = Info(
    "pr_reviewer_build",
    "Build and version information for the PR reviewer platform",
)

# ─── Gateway Metrics ──────────────────────────────────────────────────────────

GATEWAY_WEBHOOK_REQUESTS_TOTAL = Counter(
    "gateway_webhook_requests_total",
    "Total number of GitHub webhook requests received",
    ["event_type", "action", "status"],
    # status: accepted | rejected_hmac | rejected_event_type | error
)

GATEWAY_WEBHOOK_DURATION_SECONDS = Histogram(
    "gateway_webhook_duration_seconds",
    "End-to-end webhook processing duration in seconds",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

GATEWAY_EVENTS_PUBLISHED_TOTAL = Counter(
    "gateway_events_published_total",
    "Total events successfully published to Redis stream",
    ["repo"],  # owner/repo (keep low by hashing unknown repos)
)

GATEWAY_EVENTS_DEDUPLICATED_TOTAL = Counter(
    "gateway_events_deduplicated_total",
    "Total events rejected as duplicates",
)

GATEWAY_HMAC_FAILURES_TOTAL = Counter(
    "gateway_hmac_failures_total",
    "Total webhook signature verification failures",
)

GATEWAY_ACTIVE_CONNECTIONS = Gauge(
    "gateway_active_connections",
    "Number of active HTTP connections to the gateway",
)

# ─── Worker / Orchestrator Metrics ────────────────────────────────────────────

WORKER_REVIEW_JOBS_TOTAL = Counter(
    "worker_review_jobs_total",
    "Total PR review jobs started",
    ["action", "status"],
    # action: opened|synchronize|reopened
    # status: completed|failed|cancelled
)

WORKER_AGENT_DURATION_SECONDS = Histogram(
    "worker_agent_duration_seconds",
    "Duration of each AI agent review in seconds",
    ["agent_type"],
    buckets=[1.0, 5.0, 10.0, 15.0, 30.0, 45.0, 60.0, 90.0, 120.0, 180.0, 300.0],
)

WORKER_FINDINGS_TOTAL = Counter(
    "worker_findings_total",
    "Total review findings produced by agents",
    ["agent_type", "severity"],
)

WORKER_LLM_TOKENS_TOTAL = Counter(
    "worker_llm_tokens_total",
    "Total LLM tokens consumed",
    ["agent_type", "model"],
)

WORKER_LLM_REQUESTS_TOTAL = Counter(
    "worker_llm_requests_total",
    "Total LLM API requests made",
    ["agent_type", "model", "status"],
    # status: success | error | timeout
)

WORKER_QUEUE_DEPTH = Gauge(
    "worker_queue_depth",
    "Current number of review jobs in the Redis queue",
)

WORKER_ACTIVE_JOBS = Gauge(
    "worker_active_jobs",
    "Number of PR review jobs currently being processed",
)

WORKER_JOB_DURATION_SECONDS = Histogram(
    "worker_job_duration_seconds",
    "Total end-to-end review job duration in seconds",
    buckets=[10.0, 30.0, 60.0, 90.0, 120.0, 180.0, 300.0, 600.0],
)

# ─── Reviewer Service Metrics ─────────────────────────────────────────────────

REVIEWER_COMMENTS_POSTED_TOTAL = Counter(
    "reviewer_comments_posted_total",
    "Total GitHub PR review comments posted",
    ["status"],
    # status: success | github_api_error | rate_limited
)

REVIEWER_REVIEWS_CREATED_TOTAL = Counter(
    "reviewer_reviews_created_total",
    "Total GitHub Pull Request Reviews created",
    ["status"],
)

REVIEWER_GITHUB_API_DURATION_SECONDS = Histogram(
    "reviewer_github_api_duration_seconds",
    "GitHub API call duration in seconds",
    ["endpoint"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)

# ─── Learner Service Metrics ──────────────────────────────────────────────────

LEARNER_CONVENTIONS_EXTRACTED_TOTAL = Counter(
    "learner_conventions_extracted_total",
    "Total repository conventions extracted from merged PRs",
)

LEARNER_CONVENTIONS_STORED_TOTAL = Counter(
    "learner_conventions_stored_total",
    "Total conventions stored in Qdrant vector database",
    ["status"],
)

LEARNER_VECTOR_SEARCH_DURATION_SECONDS = Histogram(
    "learner_vector_search_duration_seconds",
    "Qdrant vector similarity search duration in seconds",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0],
)

# ─── Database Metrics ─────────────────────────────────────────────────────────

DB_QUERY_DURATION_SECONDS = Histogram(
    "db_query_duration_seconds",
    "Database query duration in seconds",
    ["operation", "table"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
)

DB_POOL_CONNECTIONS = Gauge(
    "db_pool_connections",
    "Current database connection pool statistics",
    ["state"],
    # state: active | idle | overflow
)
