"""Provider interface (RF-011): the contract every programmatic adapter
must satisfy. A typing.Protocol — no inheritance required; adapters stay
plain classes. Runtime checks verify method presence only."""
from typing import Protocol, runtime_checkable

from app.providers.contracts import ProviderRequest, ProviderResponse


@runtime_checkable
class Provider(Protocol):
    """Common contract for programmatic AI providers.

    Implementations must:
    - identify themselves via `provider_id` (e.g. "deepseek");
    - accept a ProviderRequest and return a ProviderResponse from generate();
    - raise app.providers.errors exceptions on failure;
    - verify their own credentials at construction time (never at import
      or registration time) and never log or embed secret values.
    """

    provider_id: str

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        ...
