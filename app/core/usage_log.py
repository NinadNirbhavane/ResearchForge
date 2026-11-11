"""Usage tracking and uniform error handling (RF-014).

Appends one JSON object per line to logs/usage.jsonl (JSONL: append-only,
each line independently readable, no database needed — SQLite stays
deferred per PROJECT.md).

Records carry ONLY: timestamp (UTC ISO-8601), event type, provider id,
model, task label, token counts (None = unknown, never 0), request id,
finish reason, error class name and retry_after_seconds. Never the API
key, prompt text, or response content — sensitive data is excluded by
construction, not filtered afterwards.

Retry policy (RF-014): NO automatic retries. RateLimitError carries
retry_after_seconds for the caller to act on explicitly (human control
over API calls is a standing project constraint).

Failures degrade gracefully: an unwritable log never masks the caller's
real outcome — record_* return False instead of raising, and the
provider's own result/error is always delivered unchanged.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from app.providers.contracts import ProviderRequest, ProviderResponse
from app.providers.errors import ProviderError, RateLimitError

USAGE_LOG_RELPATH = "logs/usage.jsonl"


def log_path(root: Path) -> Path:
    return root / USAGE_LOG_RELPATH


def _append(root: Path, record: dict) -> bool:
    line = json.dumps(record, ensure_ascii=False)
    try:
        log_path(root).parent.mkdir(parents=True, exist_ok=True)
        with log_path(root).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        return False  # graceful degradation: logging must not mask the outcome
    return True


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token(value: int | None):
    return value if isinstance(value, int) else None  # unknown stays None


def record_usage(root: Path, response: ProviderResponse, task: str) -> bool:
    """Record one successful generation. Skipped entirely (False) when the
    provider returned no usage information at all (RF-014: track usage only
    when the provider actually reports it); partially known fields are
    written as null, never 0."""
    usage = response.usage
    if not usage.known:
        return False
    return _append(root, {
        "event": "usage",
        "timestamp": _timestamp(),
        "provider": response.provider,
        "model": response.model,
        "task": task,
        "input_tokens": _token(usage.input_tokens),
        "output_tokens": _token(usage.output_tokens),
        "total_tokens": _token(usage.total_tokens),
        "request_id": response.request_id,
        "finish_reason": response.finish_reason,
    })


def record_error(root: Path, provider_id: str, task: str,
                 error: ProviderError, model: str | None = None) -> bool:
    """Record one failed generation. Only the error CLASS name is logged —
    error messages may carry provider-body fragments, so they never enter
    the log."""
    record = {
        "event": "error",
        "timestamp": _timestamp(),
        "provider": provider_id,
        "model": model,
        "task": task,
        "error_class": type(error).__name__,
    }
    if isinstance(error, RateLimitError) and \
            error.retry_after_seconds is not None:
        record["retry_after_seconds"] = error.retry_after_seconds
    return _append(root, record)


def run_generation(provider, request: ProviderRequest, task: str, root: Path
                   ) -> ProviderResponse:
    """Uniform workflow-facing entry point: calls provider.generate(),
    records usage on success, records + re-raises ProviderError on
    failure. Logging failures never alter the outcome (graceful
    degradation). No retries; RateLimitError surfaces its
    retry_after_seconds to the caller."""
    try:
        response = provider.generate(request)
    except ProviderError as error:
        record_error(root, provider.provider_id, task, error,
                     model=request.model)
        raise
    record_usage(root, response, task)
    return response
