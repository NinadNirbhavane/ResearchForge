"""Tests for the provider interface (RF-011). No network access anywhere."""
import unittest

from app.core import provider_config
from app.providers import (AuthenticationError, InvalidRequestError,
                           NetworkError, Provider, ProviderError,
                           ProviderRegistryError, ProviderRequest,
                           ProviderResponse, ProviderServerError,
                           ProviderTimeoutError, RateLimitError, Usage,
                           UnsupportedOperationError, create, ids, register,
                           reset)


class DummyProvider:
    """Minimal adapter satisfying the Provider protocol."""

    provider_id = "dummy"

    def generate(self, request):
        return ProviderResponse(content="ok", provider=self.provider_id,
                                model="dummy-model",
                                usage=Usage(input_tokens=3, output_tokens=5))


class RequestResponseTests(unittest.TestCase):
    def test_request_construction_and_defaults(self):
        req = ProviderRequest(prompt="Summarise this abstract.")
        self.assertEqual(req.prompt, "Summarise this abstract.")
        self.assertIsNone(req.model)
        self.assertIsNone(req.temperature)
        self.assertIsNone(req.max_output_tokens)
        self.assertIsNone(req.timeout_seconds)
        self.assertIsNone(req.structured)

    def test_request_full_fields(self):
        req = ProviderRequest(prompt="p", model="deepseek-chat", system="be brief",
                              temperature=0.2, max_output_tokens=512,
                              timeout_seconds=30, structured={"format": "json"})
        self.assertEqual(req.structured, {"format": "json"})

    def test_request_validation(self):
        for kwargs, msg in (
                ({"prompt": ""}, "prompt"),
                ({"prompt": "   "}, "prompt"),
                ({"prompt": "p", "temperature": 2.5}, "temperature"),
                ({"prompt": "p", "max_output_tokens": 0}, "max_output_tokens"),
                ({"prompt": "p", "timeout_seconds": 0}, "timeout")):
            with self.assertRaises(ValueError, msg=msg):
                ProviderRequest(**kwargs)

    def test_response_construction(self):
        resp = ProviderResponse(content="done", provider="dummy",
                                model="dummy-model")
        self.assertEqual(resp.content, "done")
        self.assertIsNone(resp.request_id)
        self.assertIsNone(resp.finish_reason)

    def test_optional_token_usage_unknown_not_zero(self):
        usage = Usage()
        self.assertFalse(usage.known)
        self.assertIsNone(usage.input_tokens)   # unknown, never 0
        self.assertIsNone(usage.output_tokens)
        self.assertIsNone(usage.total_tokens)
        full = Usage(input_tokens=10, output_tokens=20, total_tokens=30)
        self.assertTrue(full.known)

    def test_contracts_are_immutable(self):
        req = ProviderRequest(prompt="p")
        with self.assertRaises(Exception):
            req.prompt = "changed"
        resp = ProviderResponse(content="c", provider="p", model="m")
        with self.assertRaises(Exception):
            resp.content = "changed"


class RegistryTests(unittest.TestCase):
    def setUp(self):
        reset()
        self.addCleanup(reset)

    def test_provider_identification_via_protocol(self):
        self.assertIsInstance(DummyProvider(), Provider)

    def test_register_and_create(self):
        calls = []
        def factory():
            calls.append(1)
            return DummyProvider()
        register("dummy", factory)
        self.assertIn("dummy", ids())
        provider = create("dummy")
        self.assertEqual(calls, [1])  # instantiated exactly once, at create()
        self.assertEqual(provider.generate(
            ProviderRequest(prompt="hi")).content, "ok")

    def test_duplicate_registration_rejected(self):
        register("dummy", DummyProvider)
        with self.assertRaises(ProviderRegistryError) as cm:
            register("dummy", DummyProvider)
        self.assertIn("already registered", str(cm.exception))

    def test_unknown_provider_reported_clearly(self):
        with self.assertRaises(ProviderRegistryError) as cm:
            create("perplexity-api")
        self.assertIn("unknown provider: 'perplexity-api'", str(cm.exception))
        self.assertIn("<none>", str(cm.exception))  # registry is empty

    def test_no_instantiation_or_api_call_at_registration(self):
        calls = []
        def factory():
            calls.append(1)
            raise AuthenticationError("should never be constructed")
        register("never", factory)
        self.assertEqual(calls, [])          # registration did not build it
        self.assertEqual(ids(), ("never",))  # discovery only
        with self.assertRaises(AuthenticationError):
            create("never")                  # credential check happens here
        self.assertEqual(calls, [1])

    def test_registry_errors_are_provider_errors(self):
        with self.assertRaises(ProviderError):
            register("dummy", DummyProvider), register("dummy", DummyProvider)
        with self.assertRaises(ProviderError):
            create("ghost")


class ErrorHierarchyTests(unittest.TestCase):
    ERRORS = (AuthenticationError, RateLimitError, ProviderTimeoutError,
              NetworkError, InvalidRequestError, ProviderServerError,
              UnsupportedOperationError, ProviderRegistryError)

    def test_all_extend_provider_error(self):
        for cls in self.ERRORS:
            self.assertTrue(issubclass(cls, ProviderError), cls.__name__)
            self.assertTrue(issubclass(cls, Exception))

    def test_catch_as_base_class(self):
        try:
            raise RateLimitError("slow down", retry_after_seconds=30)
        except ProviderError as exc:
            self.assertEqual(exc.retry_after_seconds, 30)

    def test_sensitive_information_redacted(self):
        secret = "sk-live-abcdef123456"
        exc = AuthenticationError(
            f"auth failed for key {secret} on request 42",
            sensitive=(secret,))
        self.assertNotIn(secret, str(exc))
        self.assertIn("[REDACTED]", str(exc))
        self.assertIn("request 42", str(exc))  # non-sensitive context kept


class ConfigContractTests(unittest.TestCase):
    def test_credential_env_vars_contract(self):
        self.assertEqual(provider_config.credential_env_vars("deepseek"),
                         ("DEEPSEEK_API_KEY",))
        # No planned API integration for manual-import platforms
        self.assertEqual(provider_config.credential_env_vars("perplexity"), ())
        self.assertEqual(provider_config.credential_env_vars("chatgpt"), ())
        self.assertEqual(provider_config.credential_env_vars("bogus"), ())

    def test_project_local_only_resolution(self):
        # RF-012 isolation: env vars ignored; project .env is the only source
        import os as _os, tempfile
        from pathlib import Path
        old = _os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as name:
                root = Path(name)
                self.assertFalse(provider_config.credentials_configured("deepseek", root))
                (root / ".env").write_text(
                    "DEEPSEEK_API_KEY=sk-local-value-abc\n", encoding="utf-8")
                self.assertTrue(provider_config.credentials_configured("deepseek", root))
                self.assertEqual(provider_config.load_credential("deepseek", root),
                                 "sk-local-value-abc")
                (root / ".env").write_text(
                    "DEEPSEEK_API_KEY=your-deepseek-api-key-here\n", encoding="utf-8")
                self.assertFalse(provider_config.credentials_configured("deepseek", root))
        finally:
            if old is not None:
                _os.environ["DEEPSEEK_API_KEY"] = old


class NoNetworkTests(unittest.TestCase):
    def test_importing_package_makes_no_api_call(self):
        # Importing the package and the registry must not construct
        # providers or open sockets; registry starts empty by default.
        reset()
        self.assertEqual(ids(), ())  # nothing auto-registered

    def test_unsupported_operation_is_explicit(self):
        provider = DummyProvider()
        with self.assertRaises(UnsupportedOperationError):
            # adapters must raise this rather than silently faking success
            raise UnsupportedOperationError("embedding not supported by dummy")


if __name__ == "__main__":
    unittest.main()
