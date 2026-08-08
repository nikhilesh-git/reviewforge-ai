"""HMAC signature verification for GitHub webhooks.

GitHub signs every webhook delivery with an HMAC-SHA256 digest of the raw
request body using the webhook secret configured in the repository or app
settings. The digest is sent in the ``X-Hub-Signature-256`` header as:

    ``sha256=<hex_digest>``

Security properties of this implementation:
1. **Constant-time comparison** (``hmac.compare_digest``): Prevents timing
   attacks where an attacker could infer the correct signature by measuring
   response times for different guesses.
2. **Raw body hashing**: The HMAC is computed over the raw bytes of the
   request body, NOT the parsed JSON. We must read the body before FastAPI
   parses it.
3. **Prefix check first**: We reject obviously malformed headers early
   before doing any cryptographic work.
4. **No logging of secrets**: The secret key is never logged, even partially.

Reference: https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
"""

from __future__ import annotations

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)

# GitHub sends this prefix on all HMAC-SHA256 signatures
_GITHUB_SIGNATURE_PREFIX = "sha256="
_GITHUB_SIGNATURE_PREFIX_LEN = len(_GITHUB_SIGNATURE_PREFIX)


def verify_github_signature(
    payload: bytes,
    signature_header: str | None,
    secret: str,
) -> bool:
    """Verify a GitHub webhook HMAC-SHA256 signature.

    Args:
        payload: Raw request body bytes (before JSON parsing).
        signature_header: Value of the ``X-Hub-Signature-256`` header.
                          May be None if the header was not sent.
        secret: The webhook secret shared with GitHub.

    Returns:
        True if the signature is valid, False otherwise.

    Note:
        A return value of False should be treated as a potential attack.
        The caller should return HTTP 401 and log a security warning.
    """
    if not signature_header:
        logger.warning("Missing X-Hub-Signature-256 header — rejecting request")
        return False

    if not signature_header.startswith(_GITHUB_SIGNATURE_PREFIX):
        logger.warning(
            "Malformed signature header — missing sha256= prefix",
            extra={"received_prefix": signature_header[:10]},
        )
        return False

    provided_hex = signature_header[_GITHUB_SIGNATURE_PREFIX_LEN:]

    # Compute expected HMAC-SHA256 digest
    expected_hex = hmac.new(
        key=secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison — prevents timing attacks
    is_valid = hmac.compare_digest(expected_hex, provided_hex)

    if not is_valid:
        logger.warning(
            "HMAC signature mismatch — rejecting webhook",
            extra={
                "provided_sig_prefix": provided_hex[:8] + "...",
                "payload_length": len(payload),
            },
        )

    return is_valid


def compute_signature(payload: bytes, secret: str) -> str:
    """Compute the HMAC-SHA256 signature for a payload.

    Utility function used in tests to generate valid signatures for
    test webhook payloads.

    Args:
        payload: Raw bytes to sign.
        secret: HMAC secret.

    Returns:
        Full signature header value: ``sha256=<hex_digest>``
    """
    hex_digest = hmac.new(
        key=secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"sha256={hex_digest}"
