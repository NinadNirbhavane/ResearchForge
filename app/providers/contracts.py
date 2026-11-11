"""Provider data contracts (RF-011): request, response, token usage.

Frozen dataclasses; unavailable values are None (unknown), never zero.
"""
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class Usage:
    """Token usage. None means the provider did not report the value."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    @property
    def known(self) -> bool:
        return any(v is not None
                   for v in (self.input_tokens, self.output_tokens,
                             self.total_tokens))


@dataclass(frozen=True)
class ProviderRequest:
    """A generation request. All optional fields are provider-dependent;
    adapters map them onto what their API actually supports."""
    prompt: str
    model: str | None = None
    system: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float | None = None
    structured: Mapping | None = None  # structured-output preference
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if self.temperature is not None and not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be between 0.0 and 2.0, or None")
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer, or None")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive, or None")


@dataclass(frozen=True)
class ProviderResponse:
    """A generation result. usage fields may be unknown (None)."""
    content: str
    provider: str
    model: str
    usage: Usage = field(default_factory=Usage)
    request_id: str | None = None
    finish_reason: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.content, str):
            raise ValueError("content must be a string")
        if not self.provider or not self.model:
            raise ValueError("provider and model must be identified")
