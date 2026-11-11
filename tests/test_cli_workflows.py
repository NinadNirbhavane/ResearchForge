"""Offline CLI tests for the MVP-01 workflow commands. No network: the
provider builder is patched with fake providers wherever AI is involved."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.cli import workflows as cli
from app.core.sources import import_source
from app.main import build_parser
from app.providers.contracts import ProviderRequest, ProviderResponse, Usage
from app.providers.errors import AuthenticationError, NetworkError
from app.workflows import independent_review as ir
from app.workflows import literature_synthesis as ls
from app.workflows import paper_extraction as pe
from app.workflows import section_drafting as sd
from app.workflows.paper_extraction import approve_extraction, extract_source

SECRET = "sk-personal-profile1-key"

TEXT_A = ("Claim: Density supports walkability.\n\n"
          "Method: Household survey, n=200.\n\n"
          "Finding: Density predicts walking trips.\n\n"
          "Limitation: Single-city sample.\n")
TEXT_B = ("Claim: Density supports walkability.\n\n"
          "Method: Street audit of 40 segments.\n\n"
          "Finding: Density predicts walking trips.\n")


class FakeProvider:
    provider_id = "deepseek"

    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.requests = []

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ProviderResponse(content=self.content, provider="deepseek",
                                model="deepseek-flash",
                                usage=Usage(input_tokens=10, output_tokens=5,
                                            total_tokens=15))


def prose_response(source_id):
    return FakeProvider(json.dumps({
        "prose": f"Density predicts walking trips [{source_id}]. "
                 f"It remains unclear why."}))


def make_project(*texts, approve=True):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "in").mkdir()
    (root / "sources" / "papers").mkdir(parents=True)
    (root / "sources" / "extracted_notes").mkdir(parents=True)
    ids = []
    for index, text in enumerate(texts or (TEXT_A, TEXT_B)):
        src = root / "in" / f"paper{index}.txt"
        src.write_text(text, encoding="utf-8")
        sid = import_source(root, src)["source_id"]
        extract_source(root, sid)
        if approve:
            approve_extraction(root, sid, "N. Researcher")
        ids.append(sid)
    return tmp, root, ids


class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def extraction_args(source_id):
    return Args(source_id=source_id)


class ExtractionCliTests(unittest.TestCase):
    def test_run_extraction_success(self):
        tmp, root, ids = make_project(approve=False)
        with tmp:
            code = cli.cmd_extraction_run(extraction_args(ids[0]), root=root)
            self.assertEqual(code, 0)
            self.assertTrue((root / "sources" / "extracted_notes" /
                             f"{ids[0]}.extraction.json").is_file())

    def test_run_unknown_source_fails_with_2(self):
        tmp, root, ids = make_project()
        with tmp:
            code = cli.cmd_extraction_run(extraction_args("SRC-nope"), root=root)
            self.assertEqual(code, 2)

    def test_run_pdf_reports_unsupported_with_1(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "in").mkdir()
        (root / "sources" / "papers").mkdir(parents=True)
        (root / "sources" / "extracted_notes").mkdir(parents=True)
        src = root / "in" / "paper.pdf"
        src.write_bytes(b"%PDF-1.7\nbinary")
        sid = import_source(root, src)["source_id"]
        with tmp, mock.patch.object(pe, "find_pdf_tool", return_value=None):
            code = cli.cmd_extraction_run(extraction_args(sid), root=root)
            self.assertEqual(code, 1)

    def test_list_and_show(self):
        tmp, root, ids = make_project()
        with tmp:
            self.assertEqual(cli.cmd_extractions_list(Args(), root=root), 0)
            self.assertEqual(cli.cmd_extractions_show(
                extraction_args(ids[0]), root=root), 0)

    def test_show_missing_record_returns_1(self):
        tmp, root, ids = make_project()
        with tmp:
            (root / "sources" / "extracted_notes" /
             f"{ids[0]}.extraction.json").unlink()
            self.assertEqual(cli.cmd_extractions_show(
                extraction_args(ids[0]), root=root), 1)

    def test_show_unknown_source_returns_1(self):
        tmp, root, ids = make_project()
        with tmp:
            self.assertEqual(cli.cmd_extractions_show(
                extraction_args("SRC-000000000000"), root=root), 1)

    def test_list_never_builds_a_provider(self):
        tmp, root, ids = make_project()
        with tmp, mock.patch.object(cli, "_build_provider",
                                    side_effect=AssertionError("no AI")):
            self.assertEqual(cli.cmd_extractions_list(Args(), root=root), 0)


class SynthesisCliTests(unittest.TestCase):
    def test_run_without_ai_is_deterministic(self):
        tmp, root, ids = make_project()
        with tmp:
            with mock.patch.object(cli, "_build_provider",
                                   side_effect=AssertionError("no AI")):
                code = cli.cmd_synthesis_run(Args(ai=False), root=root)
            self.assertEqual(code, 0)
            self.assertTrue(ls.draft_path(root).is_file())

    def test_run_with_unapproved_evidence_warns_but_succeeds(self):
        tmp, root, ids = make_project(approve=False)
        with tmp:
            code = cli.cmd_synthesis_run(Args(ai=False), root=root)
            self.assertEqual(code, 0)
            draft = ls.load_draft(root)
            self.assertTrue(draft["eligibility"]["insufficient_evidence"])
            self.assertEqual(draft["evidence"]["findings"], [])

    def test_run_ai_success(self):
        tmp, root, ids = make_project()
        with tmp:
            provider = FakeProvider(json.dumps({
                "agreements": ["Both agree."], "contradictions": [],
                "limitations": [], "unanswered_questions": []}))
            with mock.patch.object(cli, "_build_provider",
                                   return_value=provider):
                code = cli.cmd_synthesis_run(Args(ai=True), root=root)
            self.assertEqual(code, 0)
            draft = ls.load_draft(root)
            self.assertEqual(draft["interpretation"]["status"], "COMPLETED")

    def test_run_ai_failure_returns_1_and_keeps_draft(self):
        tmp, root, ids = make_project()
        with tmp:
            provider = FakeProvider(error=NetworkError("outage"))
            with mock.patch.object(cli, "_build_provider",
                                   return_value=provider):
                code = cli.cmd_synthesis_run(Args(ai=True), root=root)
            self.assertEqual(code, 1)
            draft = ls.load_draft(root)
            self.assertEqual(draft["interpretation"]["status"], "FAILED")

    def test_run_ai_missing_credentials_returns_1(self):
        tmp, root, ids = make_project()
        with tmp:
            with mock.patch.object(cli, "_build_provider",
                                   side_effect=AuthenticationError("no key")):
                code = cli.cmd_synthesis_run(Args(ai=True), root=root)
            self.assertEqual(code, 1)

    def test_show(self):
        tmp, root, ids = make_project()
        with tmp:
            cli.cmd_synthesis_run(Args(ai=False), root=root)
            self.assertEqual(cli.cmd_synthesis_show(Args(), root=root), 0)

    def test_show_missing_returns_1_and_no_ai(self):
        tmp, root, ids = make_project()
        with tmp, mock.patch.object(cli, "_build_provider",
                                    side_effect=AssertionError("no AI")):
            self.assertEqual(cli.cmd_synthesis_show(Args(), root=root), 1)


class GapsCliTests(unittest.TestCase):
    def test_requires_approved_synthesis(self):
        tmp, root, ids = make_project()
        with tmp:
            code = cli.cmd_gaps_run(Args(ai=False), root=root)
            self.assertEqual(code, 2)

    def test_run_after_approval(self):
        tmp, root, ids = make_project()
        with tmp:
            ls.synthesize(root)
            ls.approve_synthesis(root, "N. Researcher")
            self.assertEqual(cli.cmd_gaps_run(Args(ai=False), root=root), 0)

    def test_run_ai_success(self):
        tmp, root, ids = make_project()
        with tmp:
            ls.synthesize(root)
            ls.approve_synthesis(root, "N. Researcher")
            provider = FakeProvider(json.dumps({
                "conflicting_findings": [], "underexplored_areas": [],
                "potential_opportunities": [], "novelty_hypotheses": []}))
            with mock.patch.object(cli, "_build_provider",
                                   return_value=provider):
                self.assertEqual(cli.cmd_gaps_run(Args(ai=True), root=root), 0)

    def test_run_ai_failure_returns_1(self):
        tmp, root, ids = make_project()
        with tmp:
            ls.synthesize(root)
            ls.approve_synthesis(root, "N. Researcher")
            with mock.patch.object(
                    cli, "_build_provider",
                    return_value=FakeProvider(error=NetworkError("x"))):
                self.assertEqual(cli.cmd_gaps_run(Args(ai=True), root=root), 1)

    def test_show(self):
        tmp, root, ids = make_project()
        with tmp:
            self.assertEqual(cli.cmd_gaps_show(Args(), root=root), 1)
            ls.synthesize(root)
            ls.approve_synthesis(root, "N. Researcher")
            cli.cmd_gaps_run(Args(ai=False), root=root)
            self.assertEqual(cli.cmd_gaps_show(Args(), root=root), 0)


class DraftsCliTests(unittest.TestCase):
    def _draft(self, root, ids, section="discussion", context=None,
               context_file=None, provider=None):
        args = Args(section=section, context=context,
                    context_file=context_file)
        with mock.patch.object(cli, "_build_provider",
                               return_value=provider or prose_response(ids[0])):
            return cli.cmd_drafts_run(args, root=root)

    def _ready_project(self):
        tmp, root, ids = make_project()
        ls.synthesize(root)
        ls.approve_synthesis(root, "N. Researcher")
        return tmp, root, ids

    def test_draft_success(self):
        tmp, root, ids = self._ready_project()
        with tmp:
            self.assertEqual(self._draft(root, ids), 0)
            data = sd.load_section(root, "discussion")
            self.assertEqual(data["versions"][0]["version"], 1)
            self.assertEqual(data["versions"][0]["origin"], "AI-GENERATED")

    def test_draft_with_inline_context(self):
        tmp, root, ids = self._ready_project()
        with tmp:
            self.assertEqual(self._draft(root, ids,
                                         context="200 households"), 0)
            version = sd.load_section(root, "discussion")["versions"][0]
            self.assertEqual(version["researcher_context"], "200 households")

    def test_draft_with_context_file(self):
        tmp, root, ids = self._ready_project()
        with tmp:
            path = root / "context.txt"
            path.write_text("Corridor survey context.", encoding="utf-8")
            self.assertEqual(self._draft(root, ids,
                                         context_file=str(path)), 0)
            version = sd.load_section(root, "discussion")["versions"][0]
            self.assertEqual(version["researcher_context"],
                             "Corridor survey context.")

    def test_draft_invalid_section_rejected_by_parser(self):
        with self.assertRaises(SystemExit) as cm:
            build_parser().parse_args(["drafts", "run", "results"])
        self.assertEqual(cm.exception.code, 2)

    def test_draft_provider_failure_returns_2(self):
        tmp, root, ids = self._ready_project()
        with tmp:
            code = self._draft(root, ids,
                               provider=FakeProvider(error=NetworkError("x")))
            self.assertEqual(code, 2)
            data = sd.load_section(root, "discussion")
            self.assertEqual(data["versions"][0]["status"], "FAILED")

    def test_draft_missing_synthesis_approval_returns_2(self):
        tmp, root, ids = make_project()  # synthesis never approved
        with tmp:
            self.assertEqual(self._draft(root, ids), 2)

    def test_draft_missing_context_file_returns_2(self):
        tmp, root, ids = self._ready_project()
        with tmp:
            self.assertEqual(self._draft(root, ids,
                                         context_file=str(root / "nope")), 2)

    def test_list_and_show(self):
        tmp, root, ids = self._ready_project()
        with tmp:
            self.assertEqual(cli.cmd_drafts_list(Args(), root=root), 0)
            self._draft(root, ids)
            self.assertEqual(cli.cmd_drafts_list(Args(), root=root), 0)
            self.assertEqual(cli.cmd_drafts_show(
                Args(section="discussion", version=None), root=root), 0)
            self.assertEqual(cli.cmd_drafts_show(
                Args(section="discussion", version=1), root=root), 0)

    def test_show_missing_version_and_section_return_1(self):
        tmp, root, ids = self._ready_project()
        with tmp:
            self._draft(root, ids)
            self.assertEqual(cli.cmd_drafts_show(
                Args(section="discussion", version=99), root=root), 1)
            self.assertEqual(cli.cmd_drafts_show(
                Args(section="conclusion", version=None), root=root), 1)

    def test_read_only_commands_never_build_a_provider(self):
        tmp, root, ids = self._ready_project()
        with tmp:
            self._draft(root, ids)
            with mock.patch.object(cli, "_build_provider",
                                   side_effect=AssertionError("no AI")):
                self.assertEqual(cli.cmd_drafts_list(Args(), root=root), 0)
                self.assertEqual(cli.cmd_drafts_show(
                    Args(section="discussion", version=None), root=root), 0)


class ReviewsCliTests(unittest.TestCase):
    def _ready(self):
        tmp, root, ids = make_project()
        ls.synthesize(root)
        ls.approve_synthesis(root, "N. Researcher")
        with mock.patch.object(cli, "_build_provider",
                               return_value=prose_response(ids[0])):
            cli.cmd_drafts_run(Args(section="discussion", context=None,
                                    context_file=None), root=root)
        return tmp, root, ids

    def test_review_deterministic(self):
        tmp, root, ids = self._ready()
        with tmp, mock.patch.object(cli, "_build_provider",
                                    side_effect=AssertionError("no AI")):
            code = cli.cmd_reviews_run(
                Args(section="discussion", version=None, ai=False), root=root)
            self.assertEqual(code, 0)
            reports = list((root / ir.REPORTS_DIR).glob("*.json"))
            self.assertEqual(len(reports), 1)

    def test_review_with_ai(self):
        tmp, root, ids = self._ready()
        with tmp:
            provider = FakeProvider(json.dumps({"findings": [
                {"category": "methodological", "severity": "HIGH",
                 "message": "Sample size", "rationale": "Needs justification"}]}))
            with mock.patch.object(cli, "_build_provider",
                                   return_value=provider):
                code = cli.cmd_reviews_run(
                    Args(section="discussion", version=None, ai=True),
                    root=root)
            self.assertEqual(code, 0)

    def test_review_ai_failure_returns_1(self):
        tmp, root, ids = self._ready()
        with tmp, mock.patch.object(
                cli, "_build_provider",
                return_value=FakeProvider(error=NetworkError("x"))):
            code = cli.cmd_reviews_run(
                Args(section="discussion", version=None, ai=True), root=root)
        self.assertEqual(code, 1)

    def test_review_unknown_version_returns_2(self):
        tmp, root, ids = self._ready()
        with tmp:
            code = cli.cmd_reviews_run(
                Args(section="discussion", version=99, ai=False), root=root)
            self.assertEqual(code, 2)

    def test_list_show_and_external_critique(self):
        tmp, root, ids = self._ready()
        with tmp:
            cli.cmd_reviews_run(
                Args(section="discussion", version=None, ai=False), root=root)
            self.assertEqual(cli.cmd_reviews_list(Args(), root=root), 0)
            review_id = next((root / ir.REPORTS_DIR).glob("*.json")).stem
            self.assertEqual(cli.cmd_reviews_show(
                Args(review_id=review_id), root=root), 0)
            critique = root / "critique.txt"
            critique.write_text("Gemini: overstates causality.",
                                encoding="utf-8")
            code = cli.cmd_reviews_add_external(
                Args(review_id=review_id, platform="gemini",
                     file=str(critique)), root=root)
            self.assertEqual(code, 0)
            review = ir.load_review(root, review_id)
            self.assertEqual(review["external_reviews"][0]["text"],
                             "Gemini: overstates causality.")

    def test_show_missing_review_returns_1(self):
        tmp, root, ids = self._ready()
        with tmp:
            self.assertEqual(cli.cmd_reviews_show(
                Args(review_id="RVW-missing"), root=root), 1)

    def test_add_external_unknown_review_returns_2(self):
        tmp, root, ids = self._ready()
        with tmp:
            critique = root / "c.txt"
            critique.write_text("text", encoding="utf-8")
            code = cli.cmd_reviews_add_external(
                Args(review_id="RVW-nope", platform="gemini",
                     file=str(critique)), root=root)
            self.assertEqual(code, 2)

    def test_list_never_builds_provider(self):
        tmp, root, ids = self._ready()
        with tmp, mock.patch.object(cli, "_build_provider",
                                    side_effect=AssertionError("no AI")):
            self.assertEqual(cli.cmd_reviews_list(Args(), root=root), 0)


class ParserAndSecrecyTests(unittest.TestCase):
    def test_new_commands_registered_in_help(self):
        parser = build_parser()
        text = parser.format_help()
        for command in ("extraction", "synthesis", "gaps", "drafts",
                        "reviews"):
            self.assertIn(command, text)

    def test_ai_flags_default_to_off(self):
        parser = build_parser()
        args = parser.parse_args(["synthesis", "run"])
        self.assertFalse(args.ai)
        args = parser.parse_args(["gaps", "run"])
        self.assertFalse(args.ai)
        args = parser.parse_args(["reviews", "run", "introduction"])
        self.assertFalse(args.ai)

    def test_context_options_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["drafts", "run", "introduction", "--context", "a",
                 "--context-file", "b.txt"])

    def test_provider_failure_message_never_contains_credentials(self):
        tmp, root, ids = make_project()
        with tmp:
            error = AuthenticationError(f"bad key {SECRET}", sensitive=(SECRET,))
            with mock.patch.object(cli, "_build_provider", side_effect=error):
                code = cli.cmd_synthesis_run(Args(ai=True), root=root)
            self.assertEqual(code, 1)
            text = str(error)
            self.assertNotIn(SECRET, text)

    def test_console_safe_text_never_raises(self):
        # Real papers contain Greek symbols; cp1252 consoles must not crash.
        from app.cli.workflows import _safe
        self.assertIsInstance(_safe("εικόνα 🚶"), str)

    def test_existing_commands_still_parse(self):
        parser = build_parser()
        for argv in (["status"], ["sources", "list"], ["research", "list"],
                     ["config", "show"], ["approvals", "list"]):
            args = parser.parse_args(argv)
            self.assertTrue(callable(getattr(args, "handler", None)))


if __name__ == "__main__":
    unittest.main()
