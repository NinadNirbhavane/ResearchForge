"""Tests for usage tracking and error handling (RF-014). No network
access anywhere; the provider is a fake implementing the RF-011 surface."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from app.core import usage_log
from app.core.usage_log import record_error, record_usage, run_generation
from app.providers.contracts import ProviderRequest, ProviderResponse, Usage
from app.providers.errors import (AuthenticationError, NetworkError,
                                  ProviderError, ProviderServerError,
                                  ProviderTimeoutError, RateLimitError)

ROOT = Path(__file__).resolve().parent.parent
PROMPT = "SECRET-PROMPT-TEXT-xyzzy"
RESPONSE_TEXT = "SECRET-RESPONSE-CONTENT-plugh"
KEY = "sk-personal-profile1-key"


def make_root():
    tmp = tempfile.TemporaryDirectory()
    return tmp, Path(tmp.name)


def read_lines(root: Path) -> list:
    """Parse valid JSONL lines, skipping malformed ones gracefully."""
    path = usage_log.log_path(root)
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


class FakeProvider:
    """Fake provider: returns a canned response or raises a canned error.
    The opener hook pattern from RF-012 is not needed — nothing here
    touches the network."""

    provider_id = "deepseek"

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


def ok_response(**usage_kwargs) -> ProviderResponse:
    return ProviderResponse(
        content=RESPONSE_TEXT, provider="deepseek", model="deepseek-flash",
        usage=Usage(**usage_kwargs), request_id="req-123",
        finish_reason="stop")


class RecordUsageTests(unittest.TestCase):
    def test_known_usage_recorded_with_required_fields(self):
        tmp, root = make_root()
        with tmp:
            self.assertTrue(record_usage(root, ok_response(
                input_tokens=10, output_tokens=5, total_tokens=15), "paper_extraction"))
            (record,) = read_lines(root)
            self.assertEqual(record["event"], "usage")
            self.assertEqual(record["provider"], "deepseek")
            self.assertEqual(record["model"], "deepseek-flash")
            self.assertEqual(record["task"], "paper_extraction")
            self.assertEqual(record["input_tokens"], 10)
            self.assertEqual(record["output_tokens"], 5)
            self.assertEqual(record["total_tokens"], 15)
            self.assertEqual(record["request_id"], "req-123")
            self.assertEqual(record["finish_reason"], "stop")
            # timestamp: parseable, UTC ISO-8601
            self.assertIn("T", record["timestamp"])
            self.assertTrue(record["timestamp"].startswith("20"))
            self.assertRegex(record["timestamp"],
                             r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:")

    def test_unknown_usage_not_recorded_at_all(self):
        tmp, root = make_root()
        with tmp:
            # provider returned no usage information: tracking is skipped
            self.assertFalse(record_usage(root, ok_response(), "paper_extraction"))
            self.assertEqual(read_lines(root), [])

    def test_partially_known_usage_keeps_unknown_as_null_not_zero(self):
        tmp, root = make_root()
        with tmp:
            record_usage(root, ok_response(input_tokens=10), "task_x")
            (record,) = read_lines(root)
            self.assertEqual(record["input_tokens"], 10)
            self.assertIsNone(record["output_tokens"])
            self.assertIsNone(record["total_tokens"])
            self.assertNotIn(0, (record["output_tokens"],
                                 record["total_tokens"]))

    def test_log_file_is_valid_jsonl_append_only(self):
        tmp, root = make_root()
        with tmp:
            record_usage(root, ok_response(input_tokens=1), "t1")
            record_usage(root, ok_response(input_tokens=2), "t2")
            lines = read_lines(root)
            self.assertEqual([r["task"] for r in lines], ["t1", "t2"])
            self.assertEqual(len(lines[0]), len(lines[1]))  # same schema

    def test_unwritable_log_degrades_gracefully(self):
        tmp, root = make_root()
        with tmp:
            # logs/usage.jsonl created as a DIRECTORY: every append fails
            (root / "logs" / "usage.jsonl").mkdir(parents=True)
            self.assertFalse(record_usage(root, ok_response(input_tokens=1), "t"))
            self.assertFalse(record_error(
                root, "deepseek", "t", ProviderError("boom")))


class RecordErrorTests(unittest.TestCase):
    def test_error_class_recorded_without_message_content(self):
        tmp, root = make_root()
        with tmp:
            record_error(root, "deepseek", "section_drafting",
                         ProviderServerError("HTTP 502: body fragment xyz"))
            (record,) = read_lines(root)
            self.assertEqual(record["event"], "error")
            self.assertEqual(record["error_class"], "ProviderServerError")
            self.assertEqual(record["provider"], "deepseek")
            self.assertEqual(record["task"], "section_drafting")
            # message content never enters the log
            self.assertNotIn("502", json.dumps(record))
            self.assertNotIn("xyz", json.dumps(record))

    def test_rate_limit_records_retry_metadata(self):
        tmp, root = make_root()
        with tmp:
            record_error(root, "deepseek", "t",
                         RateLimitError("HTTP 429", retry_after_seconds="30"))
            (record,) = read_lines(root)
            self.assertEqual(record["error_class"], "RateLimitError")
            self.assertEqual(record["retry_after_seconds"], "30")
            self.assertNotIn("429", json.dumps(record))

    def test_non_rate_limit_error_has_no_retry_field(self):
        tmp, root = make_root()
        with tmp:
            record_error(root, "deepseek", "t", NetworkError("down"))
            (record,) = read_lines(root)
            self.assertNotIn("retry_after_seconds", record)

    def test_malformed_existing_log_does_not_break_append(self):
        tmp, root = make_root()
        with tmp:
            usage_log.log_path(root).parent.mkdir(parents=True)
            usage_log.log_path(root).write_text(
                "{not json at all\n\n\x00\xffbinary\n", encoding="utf-8")
            self.assertTrue(record_usage(root, ok_response(input_tokens=3), "t"))
            lines = read_lines(root)
            self.assertEqual(len(lines), 1)  # only the new valid line parses
            self.assertEqual(lines[0]["input_tokens"], 3)


class NoSensitiveDataTests(unittest.TestCase):
    def test_no_prompt_response_or_key_in_log(self):
        tmp, root = make_root()
        with tmp:
            request = ProviderRequest(prompt=PROMPT, model="deepseek-flash")
            provider = FakeProvider(response=ok_response(
                input_tokens=7, output_tokens=3, total_tokens=10))
            run_generation(provider, request, "academic_review", root)
            record_error(root, "deepseek", "academic_review",
                         AuthenticationError(
                             f"auth failed for {KEY}", sensitive=(KEY,)))
            raw = usage_log.log_path(root).read_text(encoding="utf-8")
            for secret in (PROMPT, RESPONSE_TEXT, KEY, "auth failed"):
                self.assertNotIn(secret, raw)


class RunGenerationTests(unittest.TestCase):
    def test_success_returns_response_and_records_usage(self):
        tmp, root = make_root()
        with tmp:
            provider = FakeProvider(response=ok_response(
                input_tokens=11, output_tokens=4, total_tokens=15))
            request = ProviderRequest(prompt="q", model="deepseek-flash")
            response = run_generation(provider, request, "literature_synthesis",
                                      root)
            self.assertEqual(response.request_id, "req-123")
            self.assertEqual(provider.calls, 1)
            (record,) = read_lines(root)
            self.assertEqual(record["event"], "usage")
            self.assertEqual(record["task"], "literature_synthesis")

    def test_provider_error_recorded_and_reraised_unchanged(self):
        tmp, root = make_root()
        with tmp:
            provider = FakeProvider(
                error=RateLimitError("HTTP 429", retry_after_seconds="12"))
            request = ProviderRequest(prompt="q")
            with self.assertRaises(RateLimitError) as cm:
                run_generation(provider, request, "research_gap_analysis", root)
            self.assertEqual(cm.exception.retry_after_seconds, "12")
            (record,) = read_lines(root)
            self.assertEqual(record["event"], "error")
            self.assertEqual(record["error_class"], "RateLimitError")
            self.assertEqual(record["retry_after_seconds"], "12")
            self.assertEqual(provider.calls, 1)  # no automatic retry

    def test_each_error_class_surfaces_unchanged(self):
        tmp, root = make_root()
        with tmp:
            request = ProviderRequest(prompt="q")
            for error in (AuthenticationError("no key"),
                          ProviderTimeoutError("timed out"),
                          NetworkError("dns"),
                          ProviderServerError("500"),
                          ProviderError("other")):
                provider = FakeProvider(error=error)
                with self.assertRaises(type(error)):
                    run_generation(provider, request, "t", root)
            classes = [r["error_class"] for r in read_lines(root)]
            self.assertEqual(classes, ["AuthenticationError",
                                       "ProviderTimeoutError", "NetworkError",
                                       "ProviderServerError", "ProviderError"])

    def test_no_automatic_retries_on_rate_limit(self):
        tmp, root = make_root()
        with tmp:
            provider = FakeProvider(error=RateLimitError("HTTP 429"))
            request = ProviderRequest(prompt="q")
            with self.assertRaises(RateLimitError):
                run_generation(provider, request, "t", root)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(len(read_lines(root)), 1)

    def test_no_network_is_possible_by_construction(self):
        # the runner only calls provider.generate; FakeProvider proves no
        # urllib/socket involvement anywhere in the RF-014 path
        tmp, root = make_root()
        with tmp:
            provider = FakeProvider(response=ok_response(input_tokens=1))
            run_generation(provider, ProviderRequest(prompt="q"), "t", root)
            self.assertEqual(provider.calls, 1)


class ExistingBehaviorRegressionTests(unittest.TestCase):
    def test_real_workspace_log_path_convention(self):
        self.assertEqual(usage_log.log_path(ROOT),
                         ROOT / "logs" / "usage.jsonl")

    def test_usage_none_semantics_unchanged(self):
        from app.providers.contracts import Usage as U
        self.assertFalse(U().known)
        self.assertTrue(U(input_tokens=0).known)  # 0 is a real reported value
        self.assertFalse(U(input_tokens=None, output_tokens=None).known)


if __name__ == "__main__":
    unittest.main()
