"""Offline tests for the DeepSeek adapter (RF-012). All HTTP behavior is
mocked; no real API call is ever made. Credential resolution is project-
local .env only (RF-012 isolation); global env vars are ignored."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from app.core import provider_config
from app.providers import (AuthenticationError, InvalidRequestError,
                           NetworkError, ProviderError, ProviderRegistryError,
                           ProviderRequest, ProviderResponse,
                           ProviderServerError, ProviderTimeoutError,
                           RateLimitError, UnsupportedOperationError, Usage,
                           create, ids, reset)
from app.providers import deepseek
from app.providers.deepseek import DeepSeekProvider

SECRET = "sk-test-secret-key-000"
SUCCESS_BODY = {
    "id": "chatcmpl-abc123",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "Hello!"}}],
    "model": "deepseek-flash",
    "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    "system_fingerprint": "fp_1",
}


class FakeResponse:
    def __init__(self, body, headers=None):
        self._body = json.dumps(body).encode("utf-8") \
            if isinstance(body, dict) else body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records the request and replays a canned result/exception."""

    def __init__(self, result):
        self.result = result      # FakeResponse or Exception instance
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append({"url": req.full_url, "headers": dict(req.header_items()),
                              "data": req.data, "timeout": timeout,
                              "method": req.get_method()})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def http_error(status, body=None, headers=None):
    hdrs = headers or {}
    return urllib.error.HTTPError(
        "https://api.deepseek.com/chat/completions", status, "err",
        hdrs, io.BytesIO(json.dumps(body).encode() if body else b"{}"))


def make_provider(opener, **kw):
    return DeepSeekProvider(api_key=SECRET, _opener=opener, **kw)


class SerializationTests(unittest.TestCase):
    def setUp(self):
        reset()
        import app.providers.deepseek  # registers deepseek (no network)
        self.opener = FakeOpener(FakeResponse(SUCCESS_BODY))
        self.provider = make_provider(self.opener)
        self.opener.requests.clear()  # constructor makes no requests

    def test_system_and_user_serialization(self):
        self.provider.generate(ProviderRequest(prompt="Hello!", system="Be brief."))
        payload = json.loads(self.opener.requests[0]["data"])
        self.assertEqual(payload["messages"], [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Hello!"}])
        self.assertEqual(payload["stream"], False)

    def test_model_selection_and_default(self):
        self.provider.generate(ProviderRequest(prompt="p", model="deepseek-v4-pro"))
        self.assertEqual(json.loads(self.opener.requests[0]["data"])["model"],
                         "deepseek-v4-pro")
        self.provider.generate(ProviderRequest(prompt="p"))
        self.assertEqual(json.loads(self.opener.requests[1]["data"])["model"],
                         "deepseek-flash")  # documented default

    def test_temperature_and_max_tokens_mapping(self):
        self.provider.generate(ProviderRequest(prompt="p", temperature=0.3,
                                               max_output_tokens=256))
        payload = json.loads(self.opener.requests[0]["data"])
        self.assertEqual(payload["temperature"], 0.3)
        self.assertEqual(payload["max_tokens"], 256)

    def test_structured_json_maps_to_response_format(self):
        self.provider.generate(ProviderRequest(prompt="p",
                                               structured={"format": "json"}))
        self.assertEqual(json.loads(self.opener.requests[0]["data"])["response_format"],
                         {"type": "json_object"})

    def test_unsupported_structured_format(self):
        with self.assertRaises(UnsupportedOperationError):
            self.provider.generate(ProviderRequest(prompt="p",
                                                   structured={"format": "csv"}))

    def test_url_and_auth_header(self):
        self.provider.generate(ProviderRequest(prompt="p"))
        req = self.opener.requests[0]
        self.assertEqual(req["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(req["method"], "POST")
        auth = dict(req["headers"])["Authorization"]
        self.assertEqual(auth, f"Bearer {SECRET}")


class ResponseParsingTests(unittest.TestCase):
    def setUp(self):
        self.opener = FakeOpener(FakeResponse(SUCCESS_BODY))
        self.provider = make_provider(self.opener)

    def test_successful_response_parsing(self):
        resp = self.provider.generate(ProviderRequest(prompt="p"))
        self.assertEqual(resp.content, "Hello!")
        self.assertEqual(resp.provider, "deepseek")
        self.assertEqual(resp.model, "deepseek-flash")
        self.assertEqual(resp.request_id, "chatcmpl-abc123")
        self.assertEqual(resp.finish_reason, "stop")
        self.assertEqual(resp.metadata["system_fingerprint"], "fp_1")

    def test_usage_parsing(self):
        resp = self.provider.generate(ProviderRequest(prompt="p"))
        self.assertEqual(resp.usage.input_tokens, 12)
        self.assertEqual(resp.usage.output_tokens, 3)
        self.assertEqual(resp.usage.total_tokens, 15)
        self.assertTrue(resp.usage.known)

    def test_missing_usage_is_unknown_not_zero(self):
        body = {k: v for k, v in SUCCESS_BODY.items() if k != "usage"}
        self.opener.result = FakeResponse(body)
        resp = self.provider.generate(ProviderRequest(prompt="p"))
        self.assertFalse(resp.usage.known)
        self.assertIsNone(resp.usage.input_tokens)
        self.assertIsNone(resp.usage.total_tokens)

    def test_null_content_handled_gracefully(self):
        body = json.loads(json.dumps(SUCCESS_BODY))
        body["choices"][0]["message"]["content"] = None
        self.opener.result = FakeResponse(body)
        resp = self.provider.generate(ProviderRequest(prompt="p"))
        self.assertEqual(resp.content, "")   # no invented text
        self.assertEqual(resp.finish_reason, "stop")

    def test_invalid_json_rejected(self):
        self.opener.result = FakeResponse(b"<html>not json</html>")
        with self.assertRaises(ProviderError) as cm:
            self.provider.generate(ProviderRequest(prompt="p"))
        self.assertIn("invalid JSON", str(cm.exception))

    def test_unexpected_structure_rejected(self):
        self.opener.result = FakeResponse({"object": "chat.completion"})
        with self.assertRaises(ProviderError) as cm:
            self.provider.generate(ProviderRequest(prompt="p"))
        self.assertIn("missing choices", str(cm.exception))

    def test_timeout_handling(self):
        self.opener.result = TimeoutError("timed out")
        with self.assertRaises(ProviderTimeoutError):
            self.provider.generate(ProviderRequest(prompt="p", timeout_seconds=5))

    def test_network_failure(self):
        self.opener.result = urllib.error.URLError(
            OSError("name resolution failed"))
        with self.assertRaises(NetworkError):
            self.provider.generate(ProviderRequest(prompt="p"))


class ErrorMappingTests(unittest.TestCase):
    def setUp(self):
        self.provider = make_provider(FakeOpener(None))

    def expect(self, exception_class, status, body=None, headers=None):
        self.provider._opener.result = http_error(status, body, headers)
        with self.assertRaises(exception_class) as cm:
            self.provider.generate(ProviderRequest(prompt="p"))
        return cm.exception

    def test_auth_failure(self):
        exc = self.expect(AuthenticationError, 401,
                          {"error": {"message": "auth failed"}})
        self.assertIn("401", str(exc))

    def test_rate_limit_preserves_retry_after(self):
        exc = self.expect(RateLimitError, 429, {"error": {"message": "slow down"}},
                          headers={"Retry-After": "17"})
        self.assertEqual(exc.retry_after_seconds, "17")

    def test_rate_limit_without_retry_after(self):
        exc = self.expect(RateLimitError, 429)
        self.assertIsNone(exc.retry_after_seconds)

    def test_server_error(self):
        self.expect(ProviderServerError, 500)
        self.expect(ProviderServerError, 503)

    def test_invalid_request(self):
        self.expect(InvalidRequestError, 400)
        self.expect(InvalidRequestError, 422)

    def test_secret_never_in_error_messages(self):
        body = {"error": {"message": f"bad key {SECRET} for account"}}
        exc = self.expect(AuthenticationError, 401, body)
        self.assertNotIn(SECRET, str(exc))
        self.assertIn("[REDACTED]", str(exc))

    def test_prompt_not_in_error_messages(self):
        self.provider._opener.result = http_error(500, {"error": {"message": "boom"}})
        marker = "XYZZY-UNIQUE-PROMPT-MARKER-42"
        with self.assertRaises(ProviderServerError) as cm:
            self.provider.generate(ProviderRequest(prompt=marker))
        self.assertNotIn(marker, str(cm.exception))  # body not echoed


class CredentialAndRegistrationTests(unittest.TestCase):
    def setUp(self):
        reset()
        self.addCleanup(reset)
        deepseek.register_self()  # re-register after reset (no network)

    def test_missing_api_key_rejected_without_network(self):
        # Isolation: even a global env key is ignored; only project .env counts
        old = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = "sk-company-global-key"
        try:
            with tempfile.TemporaryDirectory() as name:
                with self.assertRaises(AuthenticationError) as cm:
                    DeepSeekProvider(root=Path(name))
                self.assertIn("not configured", str(cm.exception))
                self.assertNotIn("sk-company-global-key", str(cm.exception))
        finally:
            if old is not None:
                os.environ["DEEPSEEK_API_KEY"] = old

    def test_placeholder_api_key_rejected(self):
        with self.assertRaises(AuthenticationError):
            DeepSeekProvider(api_key="your-deepseek-api-key-here")

    def test_no_http_request_on_import_or_registration(self):
        # Registration only stores a factory; construction performs no
        # request. Credential resolution goes through provider_config's
        # project-local .env reader — patched here so the test never
        # depends on or exposes the user's real .env contents.
        self.assertIn("deepseek", ids())
        with mock.patch.object(provider_config, "_read_env_file",
                               return_value={"DEEPSEEK_API_KEY": SECRET}):
            provider = create("deepseek")  # factories take no arguments
            self.assertIsInstance(provider, DeepSeekProvider)
            self.assertEqual(provider.default_model, "deepseek-flash")

    def test_registry_compatibility_end_to_end(self):
        with mock.patch.object(provider_config, "_read_env_file",
                               return_value={"DEEPSEEK_API_KEY": SECRET}):
            provider = create("deepseek")
            self.assertEqual(provider.provider_id, "deepseek")

    def test_env_file_fallback_via_load_credential(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / ".env").write_text(f"DEEPSEEK_API_KEY={SECRET}\n",
                                       encoding="utf-8")
            self.assertEqual(provider_config.load_credential("deepseek", root),
                             SECRET)

    def test_smoke_test_refuses_without_opt_in(self):
        old = os.environ.pop("RESEARCHFORGE_LIVE_SMOKE", None)
        try:
            self.assertEqual(deepseek.run_smoke_test(), 2)  # refuses
        finally:
            if old is not None:
                os.environ["RESEARCHFORGE_LIVE_SMOKE"] = old


if __name__ == "__main__":
    unittest.main()
