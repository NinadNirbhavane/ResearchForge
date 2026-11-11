"""Tests for the literature-synthesis workflow (RF-016). Offline only:
real RF-010 imports + RF-015 extraction, fake providers for any AI path."""
import json
import tempfile
import unittest
from pathlib import Path

from app.core.sources import SourceError, import_source, load_manifest
from app.providers.contracts import ProviderRequest, ProviderResponse, Usage
from app.providers.errors import NetworkError, RateLimitError
from app.workflows import literature_synthesis as ls
from app.workflows.paper_extraction import (approve_extraction, extract_source,
                                            reject_extraction)

ROOT = Path(__file__).resolve().parent.parent
SECRET = "sk-personal-profile1-key"

TEXT_A = ("Claim: Density supports walkability.\n\n"
          "Method: Household survey, n=200.\n\n"
          "Finding: Density predicts walking trips.\n")
TEXT_B = ("Claim: Density supports walkability.\n\n"
          "Method: Street audit of 40 segments.\n\n"
          "Finding: Density predicts walking trips.\n")
TEXT_C = ("Finding: UNREVIEWED-FINDING-MARKER should never leak.\n")


def make_project(texts):
    """Import each text as a source, extract it, return (tmp, root, ids)."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "in").mkdir()
    (root / "sources" / "papers").mkdir(parents=True)
    (root / "sources" / "extracted_notes").mkdir(parents=True)
    ids = []
    for index, text in enumerate(texts):
        src = root / "in" / f"paper{index}.txt"
        src.write_text(text, encoding="utf-8")
        source_id = import_source(root, src)["source_id"]
        extract_source(root, source_id)
        ids.append(source_id)
    return tmp, root, ids


def approve_all(root, ids, reviewer="N. Researcher"):
    for source_id in ids:
        approve_extraction(root, source_id, reviewer)


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


class EligibilityTests(unittest.TestCase):
    def test_pending_extraction_never_contributes_evidence(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B, TEXT_C])
        with tmp:
            # only the first two are approved
            approve_all(root, ids[:2])
            draft = ls.build_draft(root)
            self.assertEqual(draft["eligibility"]["approved_extractions"],
                             sorted(ids[:2]))
            raw = json.dumps(draft, ensure_ascii=False)
            self.assertNotIn("UNREVIEWED-FINDING-MARKER", raw)
            excluded = {e["source_id"]: e["reason"]
                        for e in draft["eligibility"]["excluded"]}
            self.assertIn(ids[2], excluded)
            self.assertIn("PENDING HUMAN REVIEW", excluded[ids[2]])

    def test_rejected_extraction_excluded(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_extraction(root, ids[0], "R")
            reject_extraction(root, ids[1], "R")
            draft = ls.build_draft(root)
            self.assertEqual(draft["eligibility"]["approved_extractions"],
                             [ids[0]])
            rejected = [e for e in draft["eligibility"]["excluded"]
                        if e["source_id"] == ids[1]]
            self.assertIn("REJECTED", rejected[0]["reason"])

    def test_unapproved_findings_do_not_appear(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_extraction(root, ids[0], "R")
            draft = ls.build_draft(root)
            sources = {e["source_id"] for e in draft["evidence"]["findings"]}
            self.assertEqual(sources, {ids[0]})

    def test_no_extraction_record_excluded_with_reason(self):
        tmp, root, ids = make_project([TEXT_A])
        with tmp:
            (root / "sources" / "extracted_notes" /
             f"{ids[0]}.extraction.json").unlink()
            draft = ls.build_draft(root)
            excluded = draft["eligibility"]["excluded"]
            self.assertIn("not extracted", excluded[0]["reason"])

    def test_corrupt_extraction_record_excluded_not_crash(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            (root / "sources" / "extracted_notes" /
             f"{ids[0]}.extraction.json").write_text("{broken",
                                                     encoding="utf-8")
            draft = ls.build_draft(root)
            self.assertEqual(draft["eligibility"]["approved_extractions"],
                             [ids[1]])
            self.assertTrue(any("unreadable" in e["reason"]
                                for e in draft["eligibility"]["excluded"]))


class TraceabilityTests(unittest.TestCase):
    def test_findings_carry_source_and_extraction_ids(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            draft = ls.build_draft(root)
            findings = draft["evidence"]["findings"]
            self.assertEqual(len(findings), 2)
            for entry in findings:
                self.assertIn(entry["source_id"], ids)
                self.assertTrue(entry["extraction_id"].startswith("EXT-"))
                self.assertEqual(entry["extraction_id"],
                                 "EXT-" + entry["source_id"][4:])
                self.assertEqual(entry["text"],
                                 "Finding: Density predicts walking trips.")

    def test_evidence_text_is_verbatim_from_extraction_records(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            draft = ls.build_draft(root)
            for kind in ("findings", "claims", "methods"):
                for entry in draft["evidence"][kind]:
                    record = json.loads((root / "sources" / "extracted_notes" /
                                         f"{entry['source_id']}.extraction.json")
                                        .read_text(encoding="utf-8"))
                    self.assertIn(entry["text"],
                                  [i["text"] for i in record["items"]])

    def test_no_invented_references_or_metadata(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            draft = ls.build_draft(root)
            raw = json.dumps(draft)
            self.assertNotIn("doi", raw.lower())
            self.assertNotIn("http", raw.lower())
            self.assertNotIn("et al", raw.lower())
            # citations are source IDs only
            for entry in draft["evidence"]["findings"]:
                self.assertRegex(entry["source_id"], r"^SRC-[0-9a-f]{12}$")


class InsufficientEvidenceTests(unittest.TestCase):
    def test_single_approved_source_is_marked_insufficient(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_extraction(root, ids[0], "R")
            draft = ls.build_draft(root)
            eligibility = draft["eligibility"]
            self.assertTrue(eligibility["insufficient_evidence"])
            self.assertIn("at least 2", eligibility["insufficient_reason"])
            # evidence is not padded or fabricated to fill the gap
            self.assertEqual(len(draft["evidence"]["findings"]), 1)

    def test_zero_approved_sources_is_insufficient_with_empty_sections(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            draft = ls.build_draft(root)
            self.assertTrue(draft["eligibility"]["insufficient_evidence"])
            for kind in ("findings", "claims", "methods", "other_evidence"):
                self.assertEqual(draft["evidence"][kind], [])

    def test_two_independent_sources_is_sufficient(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            draft = ls.build_draft(root)
            self.assertFalse(draft["eligibility"]["insufficient_evidence"])
            self.assertIsNone(draft["eligibility"]["insufficient_reason"])


class InterpretationSeparationTests(unittest.TestCase):
    def test_deterministic_draft_contains_no_interpretation(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            draft = ls.build_draft(root)
            interp = draft["interpretation"]
            self.assertEqual(interp["status"], "NOT GENERATED")
            self.assertIsNone(interp["origin"])
            for key in ("agreements", "contradictions", "limitations",
                        "unanswered_questions"):
                self.assertEqual(interp[key], [])

    def test_ai_interpretation_is_labelled_and_separate(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            draft = ls.build_draft(root)
            evidence_before = json.dumps(draft["evidence"], sort_keys=True)
            provider = FakeProvider(content=json.dumps({
                "agreements": ["Both studies report density predicts walking."],
                "contradictions": [],
                "limitations": ["Small samples."],
                "unanswered_questions": ["Causality?"],
            }))
            draft = ls.add_ai_interpretation(root, draft, provider)
            interp = draft["interpretation"]
            self.assertEqual(interp["status"], "COMPLETED")
            self.assertEqual(interp["origin"], "AI-GENERATED")
            self.assertEqual(interp["review"], "PENDING HUMAN REVIEW")
            self.assertEqual(interp["model"], "deepseek-flash")
            self.assertEqual(len(interp["agreements"]), 1)
            # evidence untouched: AI text never merges into source evidence
            self.assertEqual(json.dumps(draft["evidence"], sort_keys=True),
                             evidence_before)

    def test_provider_prompt_contains_only_approved_evidence(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B, TEXT_C])
        with tmp:
            approve_all(root, ids[:2])  # TEXT_C stays PENDING
            draft = ls.build_draft(root)
            provider = FakeProvider(content="{}")
            ls.add_ai_interpretation(root, draft, provider)
            prompt = provider.requests[0].prompt
            self.assertIn("Density predicts walking trips.", prompt)
            self.assertIn(ids[0], prompt)  # citations by source id
            self.assertNotIn("UNREVIEWED-FINDING-MARKER", prompt)
            self.assertNotIn(SECRET, prompt)

    def test_provider_failure_marks_failed_without_inventing_text(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            draft = ls.build_draft(root)
            provider = FakeProvider(error=NetworkError("simulated outage"))
            draft = ls.add_ai_interpretation(root, draft, provider)
            interp = draft["interpretation"]
            self.assertEqual(interp["status"], "FAILED")
            self.assertEqual(interp["error"], "NetworkError")
            self.assertEqual(interp["agreements"], [])
            # failure was recorded by RF-014's run_generation usage log
            log = (root / "logs" / "usage.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "error"', log)
            self.assertIn("NetworkError", log)

    def test_rate_limit_failure_is_recorded(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            draft = ls.build_draft(root)
            provider = FakeProvider(
                error=RateLimitError("429", retry_after_seconds="20"))
            draft = ls.add_ai_interpretation(root, draft, provider)
            self.assertEqual(draft["interpretation"]["error"], "RateLimitError")

    def test_malformed_provider_output_marked_failed(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            draft = ls.build_draft(root)
            provider = FakeProvider(content="not json at all")
            draft = ls.add_ai_interpretation(root, draft, provider)
            self.assertEqual(draft["interpretation"]["status"], "FAILED")
            self.assertIn("unparseable", draft["interpretation"]["error"])


class ReviewGovernanceTests(unittest.TestCase):
    def test_saved_draft_starts_pending_and_approval_requires_reviewer(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            draft = ls.synthesize(root)
            self.assertEqual(draft["review"], "PENDING HUMAN REVIEW")
            saved = ls.load_draft(root)
            self.assertEqual(saved["review"], "PENDING HUMAN REVIEW")
            with self.assertRaises(ls.SynthesisError):
                ls.approve_synthesis(root, "  ")

    def test_approval_records_reviewer_and_timestamp(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            ls.synthesize(root)
            approved = ls.approve_synthesis(root, "N. Researcher")
            self.assertEqual(approved["review"], "APPROVED")
            self.assertEqual(approved["reviewed_by"], "N. Researcher")
            self.assertTrue(approved["reviewed_at"])

    def test_approved_draft_cannot_be_overwritten(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            ls.synthesize(root)
            ls.approve_synthesis(root, "N. Researcher")
            with self.assertRaises(ls.SynthesisError) as cm:
                ls.synthesize(root)
            self.assertIn("APPROVED", str(cm.exception))
            self.assertEqual(ls.load_draft(root)["review"], "APPROVED")

    def test_approval_without_draft_fails(self):
        tmp, root, _ = make_project([TEXT_A])
        with tmp:
            with self.assertRaises(ls.SynthesisError):
                ls.approve_synthesis(root, "N. Researcher")


class OutputIntegrityTests(unittest.TestCase):
    def test_draft_is_written_as_valid_json_at_documented_path(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            ls.synthesize(root)
            path = root / "outputs" / "drafts" / "literature_review_draft.json"
            self.assertTrue(path.is_file())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["document"], "literature_review_draft")

    def test_source_files_and_manifest_untouched_by_synthesis(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            stored = root / "sources" / "extracted_notes" / f"{ids[0]}.txt"
            before = stored.read_bytes()
            manifest_before = json.dumps(load_manifest(root), sort_keys=True)
            ls.synthesize(root)
            self.assertEqual(stored.read_bytes(), before)
            self.assertEqual(json.dumps(load_manifest(root), sort_keys=True),
                             manifest_before)

    def test_unicode_evidence_preserved(self):
        text = "Finding: 步行性 é ✔ matters 🚶 here.\n"
        tmp, root, ids = make_project([text, TEXT_B])
        with tmp:
            approve_all(root, ids)
            draft = ls.synthesize(root)
            self.assertIn("步行性 é ✔ matters 🚶", json.dumps(
                draft, ensure_ascii=False))

    def test_secret_never_appears_in_draft(self):
        tmp, root, ids = make_project([TEXT_A, TEXT_B])
        with tmp:
            approve_all(root, ids)
            draft = ls.build_draft(root)
            draft = ls.add_ai_interpretation(
                root, draft, FakeProvider(content=json.dumps(
                    {"agreements": [], "contradictions": [],
                     "limitations": [], "unanswered_questions": []})))
            raw = ls.draft_path(root).read_text(encoding="utf-8") \
                if ls.draft_path(root).exists() else json.dumps(draft)
            self.assertNotIn(SECRET, raw)
            self.assertNotIn("Bearer", raw)


if __name__ == "__main__":
    unittest.main()
