"""Provider error hierarchy (RF-011).

ProviderError redacts configured sensitive strings (e.g. API keys) from
the rendered message. Error context is preserved without exposing
credentials or full request payloads.
"""


def redact(text: str, sensitive) -> str:
    for secret in sensitive:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


class ProviderError(Exception):
    """Base class for all provider-related failures."""

    def __init__(self, message: str, *, sensitive=()):
        super().__init__(redact(message, sensitive))
        self.sensitive = tuple(sensitive)


class AuthenticationError(ProviderError):
    """Credentials missing, invalid, or rejected."""


class RateLimitError(ProviderError):
    """Provider rate limit hit. retry_after_seconds when known."""

    def __init__(self, message, *, retry_after_seconds=None, sensitive=()):
        super().__init__(message, sensitive=sensitive)
        self.retry_after_seconds = retry_after_seconds


class ProviderTimeoutError(ProviderError):
    """The provider did not respond within the configured timeout."""


class NetworkError(ProviderError):
    """Connection-level failure (DNS, socket, TLS)."""


class InvalidRequestError(ProviderError):
    """The provider rejected the request as malformed/unsupported."""


class ProviderServerError(ProviderError):
    """Provider-side failure (5xx-class)."""


class UnsupportedOperationError(ProviderError):
    """Operation not supported by this provider/adapter."""


class ProviderRegistryError(ProviderError):
    """Registry misuse: duplicate registration or unknown provider."""
