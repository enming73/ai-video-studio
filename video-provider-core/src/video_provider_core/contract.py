"""Stable provider contract shared by Studio, Worker and VBench."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MICRO_PER_USD = 1_000_000
ERROR_KINDS = frozenset({
    'auth', 'invalid_request', 'rate_limited', 'transient',
    'unknown_remote', 'content_rejected',
})
SUBMISSION_STATES = frozenset({'not_sent', 'maybe_sent', 'confirmed'})


@dataclass(frozen=True)
class Capability:
    durations: tuple[int, ...]
    resolutions: tuple[str, ...]
    supports_first_frame: bool
    supports_last_frame: bool
    supports_cancel: bool
    supports_idempotency: bool
    source: str


@dataclass(frozen=True)
class VideoRequest:
    prompt: str
    duration_sec: int
    width: int
    height: int
    model: str = ''
    first_frame: bytes | None = None
    first_frame_mime: str | None = None
    last_frame: bytes | None = None
    last_frame_mime: str | None = None


@dataclass(frozen=True)
class RemoteJob:
    provider: str
    remote_id: str


@dataclass(frozen=True)
class RemoteStatus:
    state: str
    url: str | None = None
    error_class: str | None = None
    error_detail: str | None = None


@dataclass(frozen=True)
class CostEstimate:
    amount_microusd: int
    pricing_version: str
    source: str


class ProviderError(RuntimeError):
    """A classified provider failure, safe for workflow decisions."""

    def __init__(self, message: str, *, kind: str,
                 retry_after_sec: int | None = None,
                 submission_state: str | None = None):
        if kind not in ERROR_KINDS:
            raise ValueError(f'Unknown provider error kind: {kind}')
        if submission_state is None:
            submission_state = 'maybe_sent' if kind == 'unknown_remote' else 'not_sent'
        if submission_state not in SUBMISSION_STATES:
            raise ValueError(f'Invalid submission state: {submission_state}')
        self.kind = kind
        self.retry_after_sec = retry_after_sec
        self.submission_state = submission_state
        super().__init__(message)


RETRYABLE_KINDS = frozenset({'rate_limited', 'transient'})


def is_retryable(error: ProviderError) -> bool:
    """Only a failure proven not to have been sent may be retried."""
    return error.kind in RETRYABLE_KINDS and error.submission_state == 'not_sent'


def to_micro(usd) -> int:
    if isinstance(usd, bool):
        raise ValueError('Money cannot be a boolean')
    try:
        value = Decimal(str(usd))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('Invalid money value') from exc
    if not value.is_finite() or value < 0:
        raise ValueError('Money must be finite and nonnegative')
    return int((value * MICRO_PER_USD).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
