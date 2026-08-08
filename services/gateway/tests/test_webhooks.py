"""Tests for the GitHub webhook receiver endpoint.

Test coverage:
- HMAC signature verification (valid, invalid, missing, malformed)
- Event type filtering (pull_request vs. others)
- PR action filtering (reviewable vs. ignored)
- Successful event processing (DB save + Redis publish + 202 response)
- Deduplication (same event twice returns 200, not 202)
- Merged PR triggers learn event
- Malformed payload handling
- Redis failure handling
- Response schema validation
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from app.repositories.event_repository import DuplicateEventError

pytestmark = pytest.mark.asyncio


# ─── HMAC Signature Tests ─────────────────────────────────────────────────────


class TestHMACVerification:
    """Tests for webhook signature verification."""

    async def test_valid_signature_accepted(
        self,
        client: AsyncClient,
        hmac_signer: Any,
        pr_opened_payload: dict,
        mock_event_repo: MagicMock,
    ) -> None:
        """A webhook with a valid HMAC signature should be accepted (202)."""
        raw_body, signature = hmac_signer.sign_json(pr_opened_payload)

        with patch(
            "app.api.webhooks.EventRepository",
            return_value=mock_event_repo,
        ):
            response = await client.post(
                "/api/v1/webhooks/github",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "test-delivery-001",
                    "X-Hub-Signature-256": signature,
                },
            )

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "accepted"
        assert "job_id" in body
        assert body["delivery_id"] == "test-delivery-001"

    async def test_missing_signature_rejected(
        self,
        client: AsyncClient,
        pr_opened_payload: dict,
    ) -> None:
        """A webhook without X-Hub-Signature-256 header must be rejected (401)."""
        raw_body = json.dumps(pr_opened_payload).encode()

        response = await client.post(
            "/api/v1/webhooks/github",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "test-delivery-002",
                # No X-Hub-Signature-256 header
            },
        )

        assert response.status_code == 401
        body = response.json()
        assert "Signature verification failed" in body["message"]

    async def test_invalid_signature_rejected(
        self,
        client: AsyncClient,
        pr_opened_payload: dict,
    ) -> None:
        """A webhook with an incorrect HMAC signature must be rejected (401)."""
        raw_body = json.dumps(pr_opened_payload).encode()

        response = await client.post(
            "/api/v1/webhooks/github",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "test-delivery-003",
                "X-Hub-Signature-256": "sha256=deadbeefdeadbeefdeadbeefdeadbeef",
            },
        )

        assert response.status_code == 401

    async def test_malformed_signature_prefix_rejected(
        self,
        client: AsyncClient,
        pr_opened_payload: dict,
    ) -> None:
        """A signature without 'sha256=' prefix must be rejected (401)."""
        raw_body = json.dumps(pr_opened_payload).encode()

        response = await client.post(
            "/api/v1/webhooks/github",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "test-delivery-004",
                "X-Hub-Signature-256": "md5=somehashalgorithm",  # Wrong prefix
            },
        )

        assert response.status_code == 401

    async def test_signature_with_wrong_secret_rejected(
        self,
        client: AsyncClient,
        pr_opened_payload: dict,
    ) -> None:
        """A webhook signed with a different secret must be rejected."""
        from tests.conftest import HMACSigner

        wrong_signer = HMACSigner("this-is-the-wrong-secret")
        raw_body, wrong_sig = wrong_signer.sign_json(pr_opened_payload)

        response = await client.post(
            "/api/v1/webhooks/github",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "test-delivery-005",
                "X-Hub-Signature-256": wrong_sig,
            },
        )

        assert response.status_code == 401


# ─── Event Type Filtering Tests ───────────────────────────────────────────────


class TestEventTypeFiltering:
    """Tests for GitHub event type routing."""

    @pytest.mark.parametrize(
        "event_type",
        ["push", "issues", "issue_comment", "create", "delete", "ping", "star"],
    )
    async def test_non_pr_events_ignored(
        self,
        client: AsyncClient,
        hmac_signer: Any,
        event_type: str,
    ) -> None:
        """Non-pull_request events should be accepted (200) but ignored."""
        payload = {"action": "created", "repository": {"full_name": "org/repo"}}
        raw_body, signature = hmac_signer.sign_json(payload)

        response = await client.post(
            "/api/v1/webhooks/github",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": event_type,
                "X-GitHub-Delivery": f"test-{event_type}-001",
                "X-Hub-Signature-256": signature,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ignored"
        assert event_type in body["reason"]

    async def test_ping_event_handled_gracefully(
        self,
        client: AsyncClient,
        hmac_signer: Any,
    ) -> None:
        """GitHub ping event (sent on webhook creation) should return 200."""
        payload = {"zen": "Design for failure.", "hook_id": 12345}
        raw_body, signature = hmac_signer.sign_json(payload)

        response = await client.post(
            "/api/v1/webhooks/github",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "ping-001",
                "X-Hub-Signature-256": signature,
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "ignored"


# ─── PR Action Filtering Tests ────────────────────────────────────────────────


class TestPRActionFiltering:
    """Tests for PR action filtering logic."""

    @pytest.mark.parametrize(
        "action",
        ["opened", "synchronize", "reopened"],
    )
    async def test_reviewable_actions_processed(
        self,
        client: AsyncClient,
        hmac_signer: Any,
        pr_opened_payload: dict,
        mock_event_repo: MagicMock,
        action: str,
    ) -> None:
        """Reviewable PR actions (opened/synchronize/reopened) should enqueue a review job."""
        payload = dict(pr_opened_payload)
        payload["action"] = action
        raw_body, signature = hmac_signer.sign_json(payload)

        with patch("app.api.webhooks.EventRepository", return_value=mock_event_repo):
            response = await client.post(
                "/api/v1/webhooks/github",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": f"test-{action}-001",
                    "X-Hub-Signature-256": signature,
                },
            )

        assert response.status_code == 202
        assert response.json()["status"] == "accepted"

    @pytest.mark.parametrize(
        "action",
        ["assigned", "unassigned", "labeled", "unlabeled", "review_requested",
         "review_request_removed", "ready_for_review", "locked", "unlocked"],
    )
    async def test_non_reviewable_actions_ignored(
        self,
        client: AsyncClient,
        hmac_signer: Any,
        pr_opened_payload: dict,
        mock_event_repo: MagicMock,
        action: str,
    ) -> None:
        """Non-reviewable PR actions should return 200 (ignored)."""
        payload = dict(pr_opened_payload)
        payload["action"] = action
        raw_body, signature = hmac_signer.sign_json(payload)

        with patch("app.api.webhooks.EventRepository", return_value=mock_event_repo):
            response = await client.post(
                "/api/v1/webhooks/github",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": f"test-{action}-001",
                    "X-Hub-Signature-256": signature,
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ignored"


# ─── Success Path Tests ───────────────────────────────────────────────────────


class TestSuccessfulProcessing:
    """Tests for the happy path: event is accepted and enqueued."""

    async def test_accepted_event_has_correct_response_schema(
        self,
        client: AsyncClient,
        hmac_signer: Any,
        pr_opened_payload: dict,
        mock_event_repo: MagicMock,
    ) -> None:
        """Accepted webhook response must include job_id and delivery_id."""
        raw_body, signature = hmac_signer.sign_json(pr_opened_payload)

        with patch("app.api.webhooks.EventRepository", return_value=mock_event_repo):
            response = await client.post(
                "/api/v1/webhooks/github",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "happy-path-001",
                    "X-Hub-Signature-256": signature,
                },
            )

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "accepted"
        assert "job_id" in body
        assert body["delivery_id"] == "happy-path-001"
        assert body["message"] == "PR review job enqueued"

        # Verify job_id is a valid UUID format
        import uuid
        uuid.UUID(body["job_id"])  # Raises ValueError if invalid

    async def test_redis_stream_publish_called(
        self,
        client: AsyncClient,
        hmac_signer: Any,
        pr_opened_payload: dict,
        mock_event_repo: MagicMock,
        mock_redis: AsyncMock,
    ) -> None:
        """Accepted event must trigger a Redis stream XADD call."""
        raw_body, signature = hmac_signer.sign_json(pr_opened_payload)

        with patch("app.api.webhooks.EventRepository", return_value=mock_event_repo):
            await client.post(
                "/api/v1/webhooks/github",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "redis-publish-001",
                    "X-Hub-Signature-256": signature,
                },
            )

        # Verify Redis XADD was called
        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args
        stream_name = call_args.args[0]
        assert "pr:events" in stream_name

    async def test_event_repo_save_called(
        self,
        client: AsyncClient,
        hmac_signer: Any,
        pr_opened_payload: dict,
        mock_event_repo: MagicMock,
    ) -> None:
        """Accepted event must persist a record to the DB."""
        raw_body, signature = hmac_signer.sign_json(pr_opened_payload)

        with patch("app.api.webhooks.EventRepository", return_value=mock_event_repo):
            await client.post(
                "/api/v1/webhooks/github",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "db-save-001",
                    "X-Hub-Signature-256": signature,
                },
            )

        mock_event_repo.save_received.assert_called_once()
        call_kwargs = mock_event_repo.save_received.call_args.kwargs
        assert call_kwargs["delivery_id"] == "db-save-001"
        assert call_kwargs["event_type"] == "pull_request"
        assert call_kwargs["action"] == "opened"
        assert call_kwargs["repository_full_name"] == "test-org/test-repo"
        assert call_kwargs["pr_number"] == 42

    async def test_event_marked_queued_after_publish(
        self,
        client: AsyncClient,
        hmac_signer: Any,
        pr_opened_payload: dict,
        mock_event_repo: MagicMock,
    ) -> None:
        """After Redis publish succeeds, event must be marked as 'queued' in DB."""
        raw_body, signature = hmac_signer.sign_json(pr_opened_payload)

        with patch("app.api.webhooks.EventRepository", return_value=mock_event_repo):
            await client.post(
                "/api/v1/webhooks/github",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "mark-queued-001",
                    "X-Hub-Signature-256": signature,
                },
            )

        mock_event_repo.mark_queued.assert_called_once_with(
            "mark-queued-001",
            stream_entry_id="1704067200000-0",  # From mock_redis fixture
        )


# ─── Deduplication Tests ──────────────────────────────────────────────────────


class TestDeduplication:
    """Tests for event deduplication logic."""

    async def test_duplicate_event_returns_200(
        self,
        client: AsyncClient,
        hmac_signer: Any,
        pr_opened_payload: dict,
        mock_event_repo: MagicMock,
    ) -> None:
        """A duplicate event (already queued) should return 200, not 202."""
        # Configure repo to raise DuplicateEventError
        mock_event_repo.save_received.side_effect = DuplicateEventError(
            delivery_id="dup-delivery-001",
            unique_key="test-org/test-repo#42@" + "a" * 40 + ":opened",
        )

        raw_body, signature = hmac_signer.sign_json(pr_opened_payload)

        with patch("app.api.webhooks.EventRepository", return_value=mock_event_repo):
            response = await client.post(
                "/api/v1/webhooks/github",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "dup-delivery-001",
                    "X-Hub-Signature-256": signature,
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ignored"
        assert "already been queued" in body["reason"]

    async def test_duplicate_does_not_publish_to_redis(
        self,
        client: AsyncClient,
        hmac_signer: Any,
        pr_opened_payload: dict,
        mock_event_repo: MagicMock,
        mock_redis: AsyncMock,
    ) -> None:
        """A duplicate event must NOT publish to Redis."""
        mock_event_repo.save_received.side_effect = DuplicateEventError(
            delivery_id="dup-no-redis-001",
            unique_key="test-key",
        )

        raw_body, signature = hmac_signer.sign_json(pr_opened_payload)

        with patch("app.api.webhooks.EventRepository", return_value=mock_event_repo):
            await client.post(
                "/api/v1/webhooks/github",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "dup-no-redis-001",
                    "X-Hub-Signature-256": signature,
                },
            )

        mock_redis.xadd.assert_not_called()


# ─── Merged PR Tests ──────────────────────────────────────────────────────────


class TestMergedPR:
    """Tests for merged PR handling (triggers learner service)."""

    async def test_merged_pr_triggers_learn_event(
        self,
        client: AsyncClient,
        hmac_signer: Any,
        pr_merged_payload: dict,
        mock_event_repo: MagicMock,
        mock_redis: AsyncMock,
    ) -> None:
        """A merged PR should publish a learn event to the learn stream."""
        raw_body, signature = hmac_signer.sign_json(pr_merged_payload)

        with patch("app.api.webhooks.EventRepository", return_value=mock_event_repo):
            response = await client.post(
                "/api/v1/webhooks/github",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "merged-pr-001",
                    "X-Hub-Signature-256": signature,
                },
            )

        # Merged PR is "closed" action — not reviewable, so 200
        assert response.status_code == 200

        # But it should publish to the learn stream
        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args
        stream_name = call_args.args[0]
        assert "learn" in stream_name


# ─── Error Handling Tests ──────────────────────────────────────────────────────


class TestErrorHandling:
    """Tests for error scenarios."""

    async def test_invalid_json_returns_422(
        self,
        client: AsyncClient,
        hmac_signer: Any,
    ) -> None:
        """Malformed JSON body should return 422."""
        raw_body = b"this is not json {"
        signature = hmac_signer.sign(raw_body)

        response = await client.post(
            "/api/v1/webhooks/github",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "bad-json-001",
                "X-Hub-Signature-256": signature,
            },
        )

        assert response.status_code == 422

    async def test_redis_failure_returns_500(
        self,
        client: AsyncClient,
        hmac_signer: Any,
        pr_opened_payload: dict,
        mock_event_repo: MagicMock,
        mock_redis: AsyncMock,
    ) -> None:
        """Redis publish failure should return 500 Internal Server Error."""
        mock_redis.xadd.side_effect = Exception("Redis connection refused")

        raw_body, signature = hmac_signer.sign_json(pr_opened_payload)

        with patch("app.api.webhooks.EventRepository", return_value=mock_event_repo):
            response = await client.post(
                "/api/v1/webhooks/github",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "redis-fail-001",
                    "X-Hub-Signature-256": signature,
                },
            )

        assert response.status_code == 500

    async def test_redis_failure_marks_event_failed(
        self,
        client: AsyncClient,
        hmac_signer: Any,
        pr_opened_payload: dict,
        mock_event_repo: MagicMock,
        mock_redis: AsyncMock,
    ) -> None:
        """Redis failure should mark the DB event record as failed."""
        mock_redis.xadd.side_effect = Exception("Redis connection refused")

        raw_body, signature = hmac_signer.sign_json(pr_opened_payload)

        with patch("app.api.webhooks.EventRepository", return_value=mock_event_repo):
            await client.post(
                "/api/v1/webhooks/github",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "redis-fail-mark-001",
                    "X-Hub-Signature-256": signature,
                },
            )

        mock_event_repo.mark_failed.assert_called_once()

    async def test_response_includes_request_id_header(
        self,
        client: AsyncClient,
        hmac_signer: Any,
        pr_opened_payload: dict,
        mock_event_repo: MagicMock,
    ) -> None:
        """All responses must include X-Request-ID header for correlation."""
        raw_body, signature = hmac_signer.sign_json(pr_opened_payload)

        with patch("app.api.webhooks.EventRepository", return_value=mock_event_repo):
            response = await client.post(
                "/api/v1/webhooks/github",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "request-id-001",
                    "X-Hub-Signature-256": signature,
                },
            )

        assert "x-request-id" in response.headers
        # Verify it's a valid UUID
        import uuid
        uuid.UUID(response.headers["x-request-id"])
