"""Worker service — Celery + LangGraph orchestrator.

This service:
- Consumes PR events from the Redis stream via Celery
- Orchestrates four parallel AI agents via a LangGraph DAG
- Merges and deduplicates findings
- Dispatches the reviewer task to post GitHub comments
"""

__version__ = "0.1.0"
