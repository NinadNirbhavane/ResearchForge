"""ResearchForge AI provider package (RF-011).

Contracts and infrastructure only — no API connections. DeepSeek is the
only approved programmatic integration (adapter lands in RF-012);
Perplexity/Gemini/ChatGPT research results enter via manual imports.
"""
from app.providers.contracts import ProviderRequest, ProviderResponse, Usage
from app.providers.errors import (
    AuthenticationError, InvalidRequestError, NetworkError, ProviderError,
    ProviderRegistryError, ProviderServerError, ProviderTimeoutError,
    RateLimitError, UnsupportedOperationError,
)
from app.providers.interface import Provider
from app.providers.registry import create, has, ids, register, reset

__all__ = [
    "ProviderRequest", "ProviderResponse", "Usage",
    "ProviderError", "AuthenticationError", "RateLimitError",
    "ProviderTimeoutError", "NetworkError", "InvalidRequestError",
    "ProviderServerError", "UnsupportedOperationError", "ProviderRegistryError",
    "Provider", "register", "create", "ids", "has", "reset",
]
