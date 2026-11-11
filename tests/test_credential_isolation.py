"""Credential isolation tests (RF-012 security requirement).

The DeepSeek key must come EXCLUSIVELY from the project-root .env file;
global environment variables are deliberately ignored.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import provider_config
from app.providers import AuthenticationError, create, reset
from app.providers import deepseek

COMPANY_KEY = "sk-company-global-machine-key"
PERSONAL_KEY = "sk-personal-profile1-key"

ENV_KEY = "DEEPSEEK_API_KEY"


class IsolationEnvGuard(unittest.TestCase):
    """Base: guarantees a company-style global key exists during tests so
    we prove it is ignored, and restores the environment afterwards."""

    def setUp(self):
        self._old = os.environ.get(ENV_KEY)
        os.environ[ENV_KEY] = COMPANY_KEY
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop(ENV_KEY, None)
        else:
            os.environ[ENV_KEY] = self._old

    def make_root(self, env_content=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        if env_content is not None:
            (root / ".env").write_text(env_content, encoding="utf-8")
        return root


class LoaderIsolationTests(IsolationEnvGuard):
    def test_project_local_key_selected(self):
        root = self.make_root(f"DEEPSEEK_API_KEY={PERSONAL_KEY}\n")
        self.assertEqual(provider_config.load_credential("deepseek", root),
                         PERSONAL_KEY)
        self.assertTrue(provider_config.credentials_configured("deepseek", root))

    def test_global_company_key_ignored(self):
        root = self.make_root(None)  # no local .env at all
        self.assertIsNone(provider_config.load_credential("deepseek", root))
        self.assertFalse(provider_config.credentials_configured("deepseek", root))

    def test_other_env_vars_do_not_override_local_key(self):
        root = self.make_root(f"DEEPSEEK_API_KEY={PERSONAL_KEY}\n")
        # company key sits in the global environment; local key must win
        self.assertEqual(provider_config.load_credential("deepseek", root),
                         PERSONAL_KEY)

    def test_missing_local_key_fails(self):
        root = self.make_root("# only a comment\nOTHER=value\n")
        self.assertIsNone(provider_config.load_credential("deepseek", root))

    def test_placeholder_local_key_fails(self):
        root = self.make_root("DEEPSEEK_API_KEY=your-deepseek-api-key-here\n")
        self.assertIsNone(provider_config.load_credential("deepseek", root))
        self.assertFalse(provider_config.credentials_configured("deepseek", root))

    def test_malformed_local_env_fails_safely(self):
        garbage = "this line has no equals sign\n\x00\x01binary\n=novalue\n"
        root = self.make_root(garbage)
        # no crash, no execution, no credential
        self.assertIsNone(provider_config.load_credential("deepseek", root))

    def test_no_credential_access_outside_project_root(self):
        # loader must only ever open <root>/.env — verified by patching the
        # one function that touches the filesystem
        root = self.make_root(f"DEEPSEEK_API_KEY={PERSONAL_KEY}\n")
        opened = []
        real_read = provider_config._read_env_file
        def spy(path):
            opened.append(Path(path))  # the only location the loader may pass
            return real_read(path)
        with mock.patch.object(provider_config, "_read_env_file", spy):
            provider_config.load_credential("deepseek", root)
        self.assertEqual(opened, [root])  # exactly one location: project root

    def test_without_root_no_credential_resolves(self):
        # root=None means no permitted location: nothing is read anywhere
        self.assertIsNone(provider_config.load_credential("deepseek", None))

    def test_key_never_in_adapter_error_output(self):
        root = self.make_root(f"DEEPSEEK_API_KEY={PERSONAL_KEY}\n")
        # Simulate a wrong-key rejection AFTER the key was loaded: the
        # adapter redacts the key from any error message.
        from app.providers.deepseek import DeepSeekProvider
        provider = DeepSeekProvider(root=root, _opener=_NeverOpener())
        exc = AuthenticationError(
            f"auth failed for {provider.api_key}", sensitive=(provider.api_key,))
        self.assertNotIn(PERSONAL_KEY, str(exc))
        self.assertIn("[REDACTED]", str(exc))


class _NeverOpener:
    def open(self, req, timeout=None):  # pragma: no cover - must never run
        raise AssertionError("no network access is permitted in these tests")


class AdapterIsolationTests(IsolationEnvGuard):
    def test_adapter_uses_local_key_not_global(self):
        root = self.make_root(f"DEEPSEEK_API_KEY={PERSONAL_KEY}\n")
        provider = deepseek.DeepSeekProvider(root=root)
        self.assertEqual(provider.api_key, PERSONAL_KEY)  # personal selected
        self.assertNotEqual(provider.api_key, COMPANY_KEY)  # company ignored

    def test_adapter_fails_clearly_without_local_key(self):
        root = self.make_root(None)  # global company key exists, ignored
        with self.assertRaises(AuthenticationError) as cm:
            deepseek.DeepSeekProvider(root=root)
        self.assertIn("not configured", str(cm.exception))
        self.assertNotIn(COMPANY_KEY, str(cm.exception))
        self.assertNotIn(PERSONAL_KEY, str(cm.exception))

    def test_registry_create_respects_isolation(self):
        reset()
        self.addCleanup(reset)
        deepseek.register_self()
        # real factory resolves against the real project root; patch the
        # file reader so this test never depends on (or exposes) the
        # user's actual .env contents
        with mock.patch.object(provider_config, "_read_env_file",
                               return_value={ENV_KEY: PERSONAL_KEY}):
            provider = create("deepseek")
            self.assertEqual(provider.api_key, PERSONAL_KEY)
        with mock.patch.object(provider_config, "_read_env_file",
                               return_value={}):
            with self.assertRaises(AuthenticationError):
                create("deepseek")

    def test_status_reports_local_only_configuration(self):
        from app.cli import status as status_mod
        root = self.make_root(None)
        self.assertFalse(status_mod.deepseek_configured(root))
        root = self.make_root(f"DEEPSEEK_API_KEY={PERSONAL_KEY}\n")
        self.assertTrue(status_mod.deepseek_configured(root))


if __name__ == "__main__":
    unittest.main()
