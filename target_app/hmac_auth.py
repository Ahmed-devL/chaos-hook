"""Local Stripe-style HMAC signing and signature verification."""

import hashlib
import hmac
import time

WEBHOOK_SECRET = "whsec_chaoshook_test_secret_2026"


def sign_stripe_payload(payload_bytes: bytes, timestamp: int | None = None) -> str:
    ts = timestamp or int(time.time())
    signed_payload = f"{ts}.".encode("utf-8") + payload_bytes
    mac = hmac.new(WEBHOOK_SECRET.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify_stripe_signature(
    payload_bytes: bytes, signature_header: str, tolerance: int = 300
) -> bool:
    """Reject malformed, altered, or expired signatures; allow v1 key rotation."""
    if not isinstance(signature_header, str):
        return False
    timestamps = []
    signatures = []
    for part in signature_header.split(","):
        key, separator, value = part.strip().partition("=")
        if not separator:
            return False
        if key == "t":
            timestamps.append(value)
        elif key == "v1":
            signatures.append(value)
    if len(timestamps) != 1 or not signatures:
        return False
    try:
        ts = int(timestamps[0])
    except ValueError:
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    signed_payload = f"{timestamps[0]}.".encode("utf-8") + payload_bytes
    expected = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()
    return any(
        signature.isascii() and hmac.compare_digest(expected, signature)
        for signature in signatures
    )
