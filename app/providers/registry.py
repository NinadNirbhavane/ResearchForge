"""Provider registry (RF-011): register factories by identifier; nothing
is instantiated (and no credential check or network call happens) until
create() is invoked. Discovery is separated from execution."""
from typing import Callable

from app.providers.errors import ProviderRegistryError
from app.providers.interface import Provider

# provider_id -> zero-argument factory returning a Provider instance.
# Populated explicitly by later tasks (RF-012 registers DeepSeek);
# never by scanning or dynamic import.
_FACTORIES: dict[str, Callable[[], Provider]] = {}


def register(provider_id: str, factory: Callable[[], Provider]) -> None:
    if not provider_id or not provider_id.strip():
        raise ProviderRegistryError("provider id must be a non-empty string")
    if not callable(factory):
        raise ProviderRegistryError("factory must be callable")
    if provider_id in _FACTORIES:
        raise ProviderRegistryError(
            f"provider already registered: '{provider_id}'")
    _FACTORIES[provider_id] = factory


def create(provider_id: str, **kwargs) -> Provider:
    """Instantiate a provider. The factory performs its own credential
    check and may raise AuthenticationError; no network call is made here."""
    if provider_id not in _FACTORIES:
        raise ProviderRegistryError(
            f"unknown provider: '{provider_id}' "
            f"(available: {', '.join(ids()) or '<none>'})")
    return _FACTORIES[provider_id](**kwargs)


def ids() -> tuple:
    return tuple(sorted(_FACTORIES))


def has(provider_id: str) -> bool:
    return provider_id in _FACTORIES


def reset() -> None:
    """Test helper: clear all registrations."""
    _FACTORIES.clear()
