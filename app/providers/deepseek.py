"""DeepSeek API adapter (RF-012) — the only approved programmatic provider.

Source of truth: https://api-docs.deepseek.com/  (verified 2026-11-10)
- Endpoint:  POST {base_url}/chat/completions   (base_url default
  https://api.deepseek.com, configurable via --base-url / DEEPSEEK_BASE_URL)
- Auth:      header  Authorization: Bearer <DEEPSEEK_API_KEY>
- Models:    deepseek-flash (default), deepseek-v4-pro  (configurable;
             RF-013 owns model configuration)
- Request:   {model, messages[{role, content}], temperature (0-2),
             max_tokens, response_format {type: json_object|text}, stream:false}
- Response:  {id, choices[{message:{content (nullable), role},
             finish_reason}], model, usage{prompt_tokens, completion_tokens,
             total_tokens, ...details}, system_fingerprint}
- Errors:    JSON body {"error":{"message"}}; 401/403 auth, 429 rate limit
             (+ Retry-After), 400/422 invalid request, 5xx server error.

Standard-library HTTP only (urllib). Credential checks happen at
construction; no network call occurs at import or registration.
"""
import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path

from app import __version__
from app.core import provider_config
from app.providers.contracts import ProviderRequest, ProviderResponse, Usage
from app.providers.errors import (AuthenticationError, InvalidRequestError,
                                  NetworkError, ProviderError,
                                  ProviderServerError, ProviderTimeoutError,
                                  RateLimitError, UnsupportedOperationError)
from app.providers.registry import register

PROVIDER_ID = "deepseek"
DEFAULT_MODEL = "deepseek-flash"       # per docs, configurable (RF-013)
DEFAULT_BASE_URL = "https://api.deepseek.com"
CHAT_PATH = "/chat/completions"
DEFAULT_TIMEOUT_SECONDS = 60.0
_MAX_ERROR_MESSAGE = 300  # keep provider error bodies short; never echo prompts


def _build_messages(request: ProviderRequest) -> list:
    messages = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    messages.append({"role": "user", "content": request.prompt})
    return messages


class DeepSeekProvider:
    """Adapter implementing the RF-011 Provider protocol."""

    provider_id = PROVIDER_ID

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 default_model: str | None = None, root: Path | None = None,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS, _opener=None):
        # Credential resolution: explicit argument > environment > .env.
        value = api_key if api_key is not None else \
            provider_config.load_credential(PROVIDER_ID, root)
        if not value or value.lower().startswith(("your-", "example", "changeme")):
            raise AuthenticationError(
                "DeepSeek API key is not configured; set DEEPSEEK_API_KEY "
                "(see .env.example). No request was sent.")
        self.api_key = value  # used only for the auth header; never logged
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.default_model = default_model or os.environ.get("DEEPSEEK_MODEL") \
            or DEFAULT_MODEL
        self.timeout = timeout
        self._opener = _opener  # dependency hook for offline tests

    # ------------------------------------------------------------ request

    def _build_payload(self, request: ProviderRequest, model: str) -> dict:
        payload = {
            "model": model,
            "messages": _build_messages(request),
            "stream": False,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.structured:
            fmt = request.structured.get("format")
            if fmt == "json":
                payload["response_format"] = {"type": "json_object"}
            else:
                raise UnsupportedOperationError(
                    f"structured format not supported by this adapter: {fmt!r}")
        return payload

    def _send(self, payload: dict, timeout: float):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + CHAT_PATH, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",  # never logged
                "User-Agent": f"ResearchForge/{__version__}",
            })
        opener = self._opener or urllib.request.build_opener()
        return opener.open(req, timeout=timeout)

    # ------------------------------------------------------------- errors

    def _map_http_error(self, exc: urllib.error.HTTPError) -> ProviderError:
        status = exc.code
        message = f"DeepSeek API returned HTTP {status}"
        try:
            body = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            detail = parsed.get("error", {}).get("message") or body
            if detail:
                message += f": {str(detail)[:_MAX_ERROR_MESSAGE]}"
        except (OSError, ValueError, AttributeError):
            pass
        kwargs = {"sensitive": (self.api_key,)}
        if status in (401, 403):
            return AuthenticationError(message, **kwargs)
        if status == 402:
            return AuthenticationError(
                message + " (account/balance issue)", **kwargs)
        if status == 429:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            return RateLimitError(message, retry_after_seconds=retry_after,
                                  **kwargs)
        if status in (400, 404, 422):
            return InvalidRequestError(message, **kwargs)
        if status >= 500:
            return ProviderServerError(message, **kwargs)
        return ProviderError(message, **kwargs)

    def _map_network_error(self, exc: Exception) -> ProviderError:
        if isinstance(exc, TimeoutError) or isinstance(
                getattr(exc, "reason", None), TimeoutError):
            return ProviderTimeoutError(
                f"DeepSeek request timed out after "
                f"{getattr(exc, 'timeout', 'unknown')}s",
                sensitive=(self.api_key,))
        return NetworkError(
            f"network failure contacting DeepSeek: "
            f"{getattr(exc, 'reason', exc)!s:.200}",
            sensitive=(self.api_key,))

    # ----------------------------------------------------------- response

    def _parse_response(self, raw: bytes, requested_model: str) -> ProviderResponse:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(
                f"invalid JSON in DeepSeek response: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(
                data.get("choices"), list) or not data["choices"]:
            raise ProviderError("unexpected DeepSeek response structure: "
                                "missing choices")
        choice = data["choices"][0]
        message = choice.get("message") or {}
        # content is nullable per the API spec; missing/None -> empty string
        content = message.get("content") or ""
        usage_raw = data.get("usage") or {}

        def _int(value):
            return value if isinstance(value, int) else None

        usage = Usage(
            input_tokens=_int(usage_raw.get("prompt_tokens")),
            output_tokens=_int(usage_raw.get("completion_tokens")),
            total_tokens=_int(usage_raw.get("total_tokens")),
        )
        metadata = {}
        if data.get("system_fingerprint"):
            metadata["system_fingerprint"] = data["system_fingerprint"]
        return ProviderResponse(
            content=content,
            provider=PROVIDER_ID,
            model=data.get("model") or requested_model,
            usage=usage,
            request_id=data.get("id"),
            finish_reason=choice.get("finish_reason"),
            metadata=metadata,
        )

    # ------------------------------------------------------------ public

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        model = request.model or self.default_model
        payload = self._build_payload(request, model)
        timeout = request.timeout_seconds or self.timeout
        try:
            with self._send(payload, timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._map_http_error(exc) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise self._map_network_error(exc) from exc
        return self._parse_response(raw, model)


def _factory() -> DeepSeekProvider:
    # Credential check happens here (at construction), never at import.
    return DeepSeekProvider(root=Path(__file__).resolve().parents[2])


def register_self() -> None:
    """(Re-)register this provider with the global registry. Called at
    module import; tests may call it again after registry.reset()."""
    register(PROVIDER_ID, _factory)


register_self()


# ------------------------------------------------- opt-in live smoke test

def run_smoke_test() -> int:
    """Explicitly opt-in live call. Requires BOTH:
      1. env RESEARCHFORGE_LIVE_SMOKE=1, and
      2. explicit invocation:  python -m app.providers.deepseek --smoke
    Never runs during tests. Uses one short, inexpensive request.
    """
    if os.environ.get("RESEARCHFORGE_LIVE_SMOKE") != "1":
        print("Live smoke test disabled. To run it explicitly:")
        print("  set RESEARCHFORGE_LIVE_SMOKE=1")
        print("  python -m app.providers.deepseek --smoke")
        return 2
    try:
        provider = DeepSeekProvider(root=Path(__file__).resolve().parents[2])
    except AuthenticationError as exc:
        print(f"[ERROR] {exc}")
        return 1
    print(f"provider={PROVIDER_ID} model={provider.default_model} "
          f"base_url={provider.base_url}")
    try:
        response = provider.generate(ProviderRequest(
            prompt="Reply with the single word: ready.",
            max_output_tokens=16))
    except ProviderError as exc:
        print(f"[ERROR] smoke test failed (no state changed): {exc}")
        return 1
    print(f"request_id={response.request_id} "
          f"finish_reason={response.finish_reason}")
    print(f"content={response.content[:100]!r}")
    print(f"usage: input={response.usage.input_tokens} "
          f"output={response.usage.output_tokens} "
          f"total={response.usage.total_tokens}")
    return 0


if __name__ == "__main__":
    import sys
    if "--smoke" in sys.argv:
        sys.exit(run_smoke_test())
    else:
        print(__doc__)
