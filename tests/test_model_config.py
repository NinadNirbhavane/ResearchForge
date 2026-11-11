"""Tests for model configuration (RF-013). No network access anywhere."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import model_config, provider_config
from app.core.model_config import (ConfigError, DisabledProviderError,
                                   apply_generation_defaults, build_provider,
                                   default_config, load_config, resolve_task)
from app.providers import deepseek, registry
from app.providers.deepseek import DeepSeekProvider
from app.providers.errors import AuthenticationError

PERSONAL_KEY = "sk-personal-profile1-key"
COMPANY_KEY = "sk-company-global-machine-key"

ENV_KEY = "DEEPSEEK_API_KEY"
ROOT = Path(__file__).resolve().parent.parent


def write_config(root: Path, data) -> Path:
    path = root / "config/models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (dict, list)):
        data = json.dumps(data)
    path.write_text(data, encoding="utf-8")
    return path


def make_root(config_data=None, env_content=None):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "app").mkdir(parents=True)
    if config_data is not None:
        write_config(root, config_data)
    if env_content is not None:
        (root / ".env").write_text(env_content, encoding="utf-8")
    return tmp, root


class EnvGuardMixin:
    """Plants a company-style global key during tests to prove it is ignored."""

    def setUp(self):
        # other test modules call registry.reset(); re-register the adapter
        # only when missing (registration itself must stay unique)
        if not registry.has("deepseek"):
            deepseek.register_self()
        self._old = os.environ.get(ENV_KEY)
        os.environ[ENV_KEY] = COMPANY_KEY
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop(ENV_KEY, None)
        else:
            os.environ[ENV_KEY] = self._old


class LoadingTests(EnvGuardMixin, unittest.TestCase):
    def test_missing_configuration_file_yields_defaults(self):
        tmp, root = make_root()
        with tmp:
            config = load_config(root)
            self.assertEqual(config["default_provider"], "deepseek")
            self.assertEqual(config["providers"]["deepseek"]["default_model"],
                             "deepseek-flash")
            self.assertIn("deepseek-v4-pro",
                          config["providers"]["deepseek"]["known_models"])

    def test_custom_configuration_loads(self):
        cfg = default_config()
        cfg["providers"]["deepseek"]["timeout_seconds"] = 30
        tmp, root = make_root(cfg)
        with tmp:
            config = load_config(root)
            self.assertEqual(
                config["providers"]["deepseek"]["timeout_seconds"], 30)

    def test_real_workspace_config_loads(self):
        config = load_config(ROOT)
        self.assertEqual(config["version"], 1)
        self.assertTrue(config["providers"]["deepseek"]["enabled"])

    def test_malformed_configuration_rejected(self):
        tmp, root = make_root("{not valid json")
        with tmp:
            with self.assertRaises(ConfigError) as cm:
                load_config(root)
            self.assertIn("invalid", str(cm.exception).lower())

    def test_no_network_during_loading_and_resolution(self):
        tmp, root = make_root(default_config())
        with tmp, mock.patch.object(
                DeepSeekProvider, "_send",
                side_effect=AssertionError("no network permitted")):
            config = load_config(root)
            resolve_task(config, "paper_extraction")  # never constructs


class TaskResolutionTests(EnvGuardMixin, unittest.TestCase):
    def test_task_specific_override_selected(self):
        cfg = default_config()
        cfg["providers"]["deepseek"]["tasks"]["paper_extraction"] = {
            "model": "deepseek-v4-pro"}
        tmp, root = make_root(cfg)
        with tmp:
            config = load_config(root)
            resolved = resolve_task(config, "paper_extraction")
            self.assertEqual(resolved, {"provider": "deepseek",
                                        "model": "deepseek-v4-pro"})

    def test_task_without_override_inherits_default_model(self):
        tmp, root = make_root(default_config())
        with tmp:
            config = load_config(root)
            for task in ("paper_extraction", "section_drafting",
                         "academic_review"):
                resolved = resolve_task(config, task)
                self.assertEqual(resolved["model"], "deepseek-flash")

    def test_unlisted_task_inherits_default_provider_default(self):
        tmp, root = make_root(default_config())
        with tmp:
            config = load_config(root)
            resolved = resolve_task(config, "some_future_task")
            self.assertEqual(resolved, {"provider": "deepseek",
                                        "model": "deepseek-flash"})


class ValidationTests(EnvGuardMixin, unittest.TestCase):
    def assert_config_error(self, mutator, fragment):
        cfg = default_config()
        mutator(cfg)
        tmp, root = make_root(cfg)
        with tmp:
            with self.assertRaises(ConfigError) as cm:
                load_config(root)
            if fragment:
                self.assertIn(fragment, str(cm.exception))

    def test_unknown_provider_rejected(self):
        self.assert_config_error(
            lambda c: c.__setitem__("default_provider", "openai"),
            "default_provider")

    def test_unregistered_provider_rejected(self):
        def mutate(cfg):
            cfg["providers"]["gemini-api"] = {
                "enabled": True, "default_model": "x",
                "base_url": "https://x.example", "timeout_seconds": 10,
                "generation_defaults": {}, "known_models": ["x"], "tasks": {}}
        self.assert_config_error(mutate, "no registered adapter")

    def test_unknown_model_rejected(self):
        self.assert_config_error(
            lambda c: c["providers"]["deepseek"].__setitem__(
                "default_model", "gpt-4o"),
            "not in known_models")

    def test_task_override_unknown_model_rejected(self):
        def mutate(cfg):
            cfg["providers"]["deepseek"]["tasks"]["paper_extraction"] = {
                "model": "mystery-model"}
        self.assert_config_error(mutate, "not in known_models")

    def test_invalid_timeout_rejected(self):
        self.assert_config_error(
            lambda c: c["providers"]["deepseek"].__setitem__(
                "timeout_seconds", 0), "timeout_seconds")
        self.assert_config_error(
            lambda c: c["providers"]["deepseek"].__setitem__(
                "timeout_seconds", "sixty"), "timeout_seconds")

    def test_invalid_generation_parameters_rejected(self):
        def temp_too_high(cfg):
            cfg["providers"]["deepseek"]["generation_defaults"]["temperature"] = 3
        self.assert_config_error(temp_too_high, "temperature")

        def negative_tokens(cfg):
            cfg["providers"]["deepseek"]["generation_defaults"][
                "max_output_tokens"] = -5
        self.assert_config_error(negative_tokens, "max_output_tokens")

        def unknown_key(cfg):
            cfg["providers"]["deepseek"]["generation_defaults"]["top_k"] = 5
        self.assert_config_error(unknown_key, "not a recognised parameter")

    def test_missing_default_model_rejected(self):
        self.assert_config_error(
            lambda c: c["providers"]["deepseek"].__setitem__(
                "default_model", None), "default_model is missing")

    def test_malformed_task_override_rejected(self):
        def mutate(cfg):
            cfg["providers"]["deepseek"]["tasks"]["paper_extraction"] = {
                "model": "deepseek-flash", "tools": True}
        self.assert_config_error(mutate, "unknown keys")

    def test_disabled_provider_loads_but_resolution_rejected(self):
        cfg = default_config()
        cfg["providers"]["deepseek"]["enabled"] = False
        tmp, root = make_root(cfg)
        with tmp:
            config = load_config(root)  # disabled is valid configuration
            with self.assertRaises(DisabledProviderError):
                resolve_task(config, "paper_extraction")

    def test_task_on_disabled_provider_rejected_not_skipped(self):
        cfg = default_config()
        cfg["providers"]["deepseek"]["tasks"]["paper_extraction"] = {
            "model": "deepseek-v4-pro"}
        cfg["providers"]["deepseek"]["enabled"] = False
        tmp, root = make_root(cfg)
        with tmp:
            config = load_config(root)
            with self.assertRaises(DisabledProviderError):
                resolve_task(config, "paper_extraction")


class ProviderConstructionTests(EnvGuardMixin, unittest.TestCase):
    def test_build_provider_uses_configuration(self):
        tmp, root = make_root(default_config(),
                              env_content=f"DEEPSEEK_API_KEY={PERSONAL_KEY}\n")
        with tmp, mock.patch.object(
                DeepSeekProvider, "_send",
                side_effect=AssertionError("no network")):
            config = load_config(root)
            provider = build_provider(config, "deepseek", root)
            self.assertEqual(provider.provider_id, "deepseek")
            self.assertEqual(provider.default_model, "deepseek-flash")
            self.assertEqual(provider.base_url, "https://api.deepseek.com")
            self.assertEqual(provider.timeout, 60.0)

    def test_build_provider_without_local_key_fails(self):
        tmp, root = make_root(default_config(), env_content=None)
        with tmp:
            config = load_config(root)
            with self.assertRaises(AuthenticationError) as cm:
                build_provider(config, "deepseek", root)
            self.assertIn("not configured", str(cm.exception))
            self.assertNotIn(COMPANY_KEY, str(cm.exception))

    def test_generation_defaults_merge_with_overrides(self):
        cfg = default_config()
        merged = apply_generation_defaults(cfg, "deepseek",
                                           {"temperature": 0.9})
        self.assertEqual(merged["temperature"], 0.9)  # override wins
        self.assertEqual(merged["max_output_tokens"], 2048)  # default kept

    def test_project_local_credential_selected_not_global(self):
        tmp, root = make_root(default_config(),
                              env_content=f"DEEPSEEK_API_KEY={PERSONAL_KEY}\n")
        with tmp, mock.patch.object(
                DeepSeekProvider, "_send",
                side_effect=AssertionError("no network")):
            config = load_config(root)
            provider = build_provider(config, "deepseek", root)
            self.assertEqual(provider.api_key, PERSONAL_KEY)
            self.assertNotEqual(provider.api_key, COMPANY_KEY)


class CliIntegrationTests(EnvGuardMixin, unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "app.main", "config", *args],
            capture_output=True, text=True, cwd=str(ROOT))

    def test_config_show_read_only(self):
        proc = self.run_cli("show")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Provider 'deepseek'", proc.stdout)
        self.assertIn("deepseek-flash", proc.stdout)
        self.assertIn("credentials=configured", proc.stdout.lower())
        self.assertIn("inherit", proc.stdout)
        # the actual key value must never appear in output
        real_key = provider_config.load_credential("deepseek", ROOT)
        if real_key:
            self.assertNotIn(real_key, proc.stdout)

    def test_config_validate_passes(self):
        proc = self.run_cli("validate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Validation result: PASS", proc.stdout)
        self.assertIn("No network calls were made", proc.stdout)

    def test_existing_commands_still_functional(self):
        for args in (["status"], ["research", "list"], ["sources", "list"]):
            proc = subprocess.run([sys.executable, "-m", "app.main", *args],
                                  capture_output=True, text=True,
                                  cwd=str(ROOT))
            self.assertEqual(proc.returncode, 0, f"{args}: {proc.stderr}")


if __name__ == "__main__":
    unittest.main()
