# GitHub PR Code Reviewer

An enterprise-grade, self-hosted **Agentic AI platform** that automatically reviews GitHub Pull Requests using a multi-agent LangGraph pipeline. Every PR triggers a parallel review by four specialized AI agents — producing structured inline comments for security, architecture, code style, and static analysis findings — all running locally with free LLMs.

---

## Architecture Overview

```
GitHub PR Event
      │
      ▼
 Gateway Service (FastAPI)
  ├── HMAC Signature Verification
  ├── Event Deduplication
  └── Redis Stream Publisher
            │
            ▼
      Redis Stream (pr:events)
            │
            ▼
   Celery Worker (picks up task)
            │
            ▼
  LangGraph Orchestrator
  ├── Fetch GitHub Diff
  ├── Load Repo Context (Qdrant)
  └── Parallel AI Review
       ├── Static Analysis Agent  ─┐
       ├── Security Agent (OWASP) ─┤
       ├── Architecture Agent     ─┤──► Merger & Deduplicator
       └── Style Agent            ─┘          │
                                              ▼
                                   Reviewer Service
                                   (GitHub Comment Poster)
                                              │
                                              ▼
                                   GitHub PR Inline Comments

Learner Service (post-merge)
  ├── Extract Conventions
  └── Qdrant Vector Store (repo memory)

Observability
  ├── Prometheus Metrics
  ├── Grafana Dashboards
  └── Langfuse LLM Tracing
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.13 |
| API Framework | FastAPI 0.115+ |
| Agent Orchestration | LangGraph 0.2+ |
| Task Queue | Celery 5.4+ |
| Message Broker | Redis 7.2 |
| Database | PostgreSQL 16 |
| ORM | SQLAlchemy 2.0 (async) |
| Validation | Pydantic v2 |
| Vector Store | Qdrant 1.9+ |
| LLM Provider | OpenRouter (Qwen3 Coder, DeepSeek V3 — **free**) |
| Tracing | Langfuse (self-hosted) |
| Metrics | Prometheus + Grafana |
| Linting | Ruff + Black |
| Testing | Pytest + pytest-asyncio |
| Deployment | Docker Compose |
| CI/CD | GitHub Actions |

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) 24+
- [Docker Compose](https://docs.docker.com/compose/) V2+
- [Git](https://git-scm.com/)
- An [OpenRouter](https://openrouter.ai) account (free — no credit card needed)
- A GitHub account + repository to test with

---

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/your-org/github-pr-reviewer.git
cd github-pr-reviewer
make env-setup          # Creates .env from .env.example
```

Edit `.env` with your values:
```bash
GITHUB_WEBHOOK_SECRET=your-strong-random-secret
OPENROUTER_API_KEY=your-openrouter-api-key
GITHUB_PAT=ghp_your-personal-access-token
```

### 2. Start infrastructure

```bash
make infra-up           # Starts postgres, redis, qdrant, prometheus, grafana, langfuse
make migrate            # Runs database migrations
```

### 3. Start application services

```bash
make up                 # Starts gateway, worker, reviewer, learner
make logs-all           # Follow logs
```

### 4. Configure GitHub Webhook

1. Go to your repository → **Settings → Webhooks → Add webhook**
2. **Payload URL**: `http://your-server:8000/webhooks/github`
   - For local development, use `make tunnel` (requires [ngrok](https://ngrok.com/))
3. **Content type**: `application/json`
4. **Secret**: Same value as `GITHUB_WEBHOOK_SECRET` in your `.env`
5. **Events**: Select "Pull requests"

### 5. Verify everything works

```bash
make health             # Check all service health endpoints
```

Open a PR in your configured repository and watch the logs!

---

## Service Endpoints

| Service | URL | Description |
|---|---|---|
| Gateway API | `http://localhost:8000` | Webhook receiver |
| Gateway Docs | `http://localhost:8000/docs` | OpenAPI documentation |
| Gateway Health | `http://localhost:8000/health` | Health check |
| Gateway Metrics | `http://localhost:8000/metrics` | Prometheus metrics |
| Grafana | `http://localhost:3000` | Dashboards (admin/admin_changeme) |
| Prometheus | `http://localhost:9090` | Metrics queries |
| Langfuse | `http://localhost:3001` | LLM tracing |
| Qdrant UI | `http://localhost:6333/dashboard` | Vector store browser |

---

## Project Structure

```
github_pr_code_reviewer/
├── .github/workflows/      # CI/CD pipelines
├── infra/
│   ├── prometheus/         # Prometheus config
│   └── grafana/            # Grafana dashboards & provisioning
├── migrations/             # Alembic database migrations
├── services/
│   ├── gateway/            # FastAPI webhook receiver (port 8000)
│   ├── worker/             # Celery + LangGraph orchestrator
│   ├── reviewer/           # GitHub comment publisher
│   └── learner/            # Convention extractor + vector store
├── shared/                 # Shared domain models, infra, utilities
├── docker-compose.yml      # Production Compose config
├── docker-compose.override.yml  # Dev overrides (hot-reload)
├── Makefile                # Convenience commands
├── ruff.toml               # Linter config
└── pyproject.toml          # Root workspace config
```

---

## Development

### Running tests

```bash
make test                   # All tests
make test-service SERVICE=gateway  # Single service
make test-unit              # Unit tests only (no Docker)
make test-cov               # With coverage report
```

### Code quality

```bash
make lint                   # Ruff linting
make format                 # Black formatting
make quality                # Lint + format-check + type-check
```

### Database migrations

```bash
make migrate-new MSG="add review findings table"  # Create migration
make migrate                                        # Apply migrations
make migrate-rollback                               # Roll back last
```

---

## AI Agents

### Static Analysis Agent
Identifies code quality issues: unused variables, complexity hotspots, potential bugs, dead code, and type annotation gaps.

### Security Agent (OWASP-focused)
Reviews against OWASP Top 10: injection flaws, broken authentication, sensitive data exposure, XXE, broken access control, security misconfiguration, XSS, insecure deserialization, known vulnerabilities, and insufficient logging.

### Architecture Agent
Evaluates design patterns, SOLID principles adherence, separation of concerns, dependency direction, coupling/cohesion, and potential scalability issues.

### Style Agent
Enforces code style: naming conventions, docstring completeness, comment quality, function/class length, and consistency with repository conventions (learned from past PRs via Qdrant).

---

## LLM Models (Free Tier)

The platform uses OpenRouter to access free LLMs:

| Model | Use Case | Context |
|---|---|---|
| `qwen/qwen3-coder:free` | Primary code review | 128K |
| `deepseek/deepseek-v3-base:free` | Fallback / architecture review | 64K |

You can also configure `google/gemini-2.5-flash` by setting `GOOGLE_API_KEY`.

---

## Monitoring

### Grafana Dashboards

Pre-provisioned dashboards available at `http://localhost:3000`:
- **PR Review Overview**: Review throughput, latency, finding counts by severity
- **Agent Performance**: Per-agent latency, token usage, error rates
- **System Health**: Queue depth, worker utilization, DB connection pool

### Key Metrics

```
gateway_webhook_requests_total          # Webhook volume
gateway_events_published_total         # Events queued
worker_review_jobs_total               # Jobs processed
worker_agent_duration_seconds          # Per-agent latency
worker_findings_total                  # Findings by type/severity
reviewer_comments_posted_total         # GitHub comments posted
learner_conventions_stored_total       # Repo conventions learned
```

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Run quality checks: `make quality`
4. Run tests: `make test`
5. Commit with conventional commits: `git commit -m "feat: add X"`
6. Open a pull request (this platform will review it! 🤖)

---

## License

MIT License — see [LICENSE](LICENSE) for details.
