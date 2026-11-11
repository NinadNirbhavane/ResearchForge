"""Tests for the independent review workflow (RF-019). Offline only:
real RF-010/015/016/018 pipeline, fake providers for the AI review path."""
import json
import tempfile
import unittest
from pathlib import Path

from app.core.sources import import_source
from app.providers.contracts import ProviderRequest, ProviderResponse, Usage
from app.providers.errors import NetworkError, ProviderServerError
from app.workflows import independent_review as ir
from app.workflows import literature_synthesis as ls
from app.workflows import section_drafting as sd
from app.workflows.paper_extraction import approve_extraction, extract_source

SECRET = "sk-personal-profile1-key"

TEXT_A = ("Claim: Density supports walkability.\n\n"
          "Finding: Density predicts walking trips.\n\n"
          "Limitation: Single-city sample.\n")
TEXT_B = ("Claim: Density supports walkability.\n\n"
          "Finding: Density predicts walking trips.\n")


def make_project():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "in").mkdir()
    (root / "sources" / "papers").mkdir(parents=True)
    (root / "sources" / "extracted_notes").mkdir(parents=True)
    (root / "research").mkdir()
    for name in ("research_questions.md", "objectives.md", "methodology.md"):
        (root / "research" / name).write_text(
            f"# {name}\nApproved research context.\n", encoding="utf-8")
    ids = []
    for index, text in enumerate([TEXT_A, TEXT_B]):
        src = root / "in" / f"paper{index}.txt"
        src.write_text(text, encoding="utf-8")
        sid = import_source(root, src)["source_id"]
        extract_source(root, sid)
        approve_extraction(root, sid, "N. Researcher")
        ids.append(sid)
    ls.synthesize(root)
    ls.approve_synthesis(root, "N. Researcher")
    return tmp, root, ids


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
                                usage=Usage(input_tokens=40, output_tokens=20,
                                            total_tokens=60))


def draft(root, ids, prose=None):
    prose = prose or (f"Density predicts walking trips [{ids[0]}]. "
                      f"Uncited opinion sentence here.")
    provider = FakeProvider(json.dumps({"prose": prose}))
    return sd.draft_section(root, "discussion", provider)


class DeterministicCheckTests(unittest.TestCase):
    def test_review_reports_valid_and_invalid_citations(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids, f"Fact [{ids[0]}]. Invented [SRC-aaaaaaaaaaaa].")
            review = ir.review_section(root, "discussion")
            highs = [f for f in review["deterministic_findings"]
                     if f["severity"] == "HIGH"]
            self.assertTrue(any("SRC-aaaaaaaaaaaa" in f["message"]
                                for f in highs))
            self.assertTrue(any("not in the approved evidence set"
                                in f["message"] for f in highs))

    def test_uncited_sentence_flagged_medium(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids, f"Supported [{ids[0]}]. Unsupported claim text.")
            review = ir.review_section(root, "discussion")
            medium = [f for f in review["deterministic_findings"]
                      if f["severity"] == "MEDIUM"]
            self.assertTrue(any("Unsupported claim text." in f["message"]
                                for f in medium))
            self.assertTrue(all(f["category"] == "factual/evidence"
                                for f in medium))

    def test_prose_with_no_citations_at_all_is_high(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids, "A confident paragraph with no citations.")
            review = ir.review_section(root, "discussion")
            self.assertTrue(any("no citations at all" in f["message"]
                                for f in review["deterministic_findings"]))

    def test_provenance_mismatch_detected(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            path = sd.section_path(root, "discussion")
            data = json.loads(path.read_text(encoding="utf-8"))
            data["versions"][0]["evidence_refs"][0]["extraction_id"] = \
                "EXT-tampered0000"
            path.write_text(json.dumps(data), encoding="utf-8")
            review = ir.review_section(root, "discussion")
            self.assertTrue(any("extraction id mismatch" in f["message"]
                                for f in review["deterministic_findings"]))

    def test_unknown_source_reference_detected(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            path = sd.section_path(root, "discussion")
            data = json.loads(path.read_text(encoding="utf-8"))
            data["versions"][0]["evidence_refs"][0]["source_id"] = \
                "SRC-deadbeef0000"
            path.write_text(json.dumps(data), encoding="utf-8")
            review = ir.review_section(root, "discussion")
            self.assertTrue(any("unknown source" in f["message"]
                                for f in review["deterministic_findings"]))

    def test_failed_draft_version_is_high_finding(self):
        tmp, root, ids = make_project()
        with tmp:
            provider = FakeProvider(error=NetworkError("outage"))
            with self.assertRaises(NetworkError):
                sd.draft_section(root, "discussion", provider)
            review = ir.review_section(root, "discussion")
            self.assertTrue(any("not a completed draft" in f["message"]
                                for f in review["deterministic_findings"]))

    def test_missing_research_context_docs_reported(self):
        tmp, root, ids = make_project()
        with tmp:
            (root / "research" / "methodology.md").unlink()
            draft(root, ids)
            review = ir.review_section(root, "discussion")
            self.assertTrue(any("methodology.md" in f["message"]
                                for f in review["deterministic_findings"]))
            self.assertIn("research_questions.md", review["context_docs"])

    def test_severities_and_categories_are_from_documented_sets(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            review = ir.review_section(root, "discussion")
            for finding in review["deterministic_findings"]:
                self.assertIn(finding["severity"], ir.SEVERITIES)
                self.assertIn(finding["category"], ir.CATEGORIES)
                self.assertTrue(finding["rationale"])
                self.assertTrue(finding["verified"])


class AISeparationTests(unittest.TestCase):
    def test_ai_findings_kept_separate_and_unverified(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            before = ir.review_section(root, "discussion")
            provider = FakeProvider(json.dumps({"findings": [
                {"category": "methodological",
                 "severity": "HIGH",
                 "message": "Sample size not justified",
                 "rationale": "Methodology doc requires justification"},
                {"category": "writing-quality", "severity": "LOW",
                 "message": "Long sentences", "rationale": "Readability"},
            ]}))
            review = ir.review_section(root, "discussion", provider=provider)
            self.assertEqual(review["ai_review"]["status"], "COMPLETED")
            self.assertEqual(review["ai_review"]["origin"], "AI-GENERATED")
            ai = review["ai_review"]["findings"]
            self.assertEqual(len(ai), 2)
            self.assertTrue(all(not f["verified"] for f in ai))
            self.assertTrue(all(f["origin"] == "AI-GENERATED" for f in ai))
            # deterministic findings survive unchanged and stay separate
            self.assertEqual(review["deterministic_findings"],
                             before["deterministic_findings"])

    def test_invalid_ai_severity_is_not_invented(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            provider = FakeProvider(json.dumps({"findings": [
                {"category": "nonsense-category", "severity": "CRITICAL",
                 "message": "m", "rationale": "r"}]}))
            review = ir.review_section(root, "discussion", provider=provider)
            finding = review["ai_review"]["findings"][0]
            self.assertEqual(finding["severity"], "UNSPECIFIED")
            self.assertEqual(finding["category"], "writing-quality")

    def test_provider_failure_preserves_deterministic_review(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            provider = FakeProvider(error=ProviderServerError("502"))
            review = ir.review_section(root, "discussion", provider=provider)
            self.assertEqual(review["ai_review"]["status"], "FAILED")
            self.assertEqual(review["ai_review"]["error"],
                             "ProviderServerError")
            self.assertTrue(review["deterministic_findings"])
            log = (root / "logs" / "usage.jsonl").read_text(encoding="utf-8")
            self.assertIn("ProviderServerError", log)

    def test_malformed_ai_output_marked_failed(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            provider = FakeProvider("not json")
            review = ir.review_section(root, "discussion", provider=provider)
            self.assertEqual(review["ai_review"]["status"], "FAILED")
            self.assertIn("unparseable", review["ai_review"]["error"])

    def test_prompt_contains_context_and_evidence_but_no_secret(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            provider = FakeProvider(json.dumps({"findings": []}))
            ir.review_section(root, "discussion", provider=provider)
            prompt = provider.requests[0].prompt
            self.assertIn("research_questions.md", prompt)
            self.assertIn("Approved research context.", prompt)
            self.assertIn(ids[0], prompt)
            self.assertIn("Do not invent evidence", prompt)
            self.assertNotIn(SECRET, prompt)


class ExternalReviewTests(unittest.TestCase):
    def test_manual_paste_stored_separately_not_auto_accepted(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            review = ir.review_section(root, "discussion")
            review = ir.add_external_review(
                root, review["review_id"],
                "Gemini: the discussion overstates causality.", "gemini")
            external = review["external_reviews"][0]
            self.assertEqual(external["origin"], "EXTERNAL MANUAL IMPORT")
            self.assertEqual(external["platform"], "gemini")
            self.assertIsNone(external["decision"])
            self.assertEqual(review["external_reviews"][0]["text"],
                             "Gemini: the discussion overstates causality.")

    def test_unknown_platform_rejected(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            review = ir.review_section(root, "discussion")
            with self.assertRaises(ir.ReviewError):
                ir.add_external_review(root, review["review_id"], "x", "bing")

    def test_empty_external_text_rejected(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            review = ir.review_section(root, "discussion")
            with self.assertRaises(ir.ReviewError):
                ir.add_external_review(root, review["review_id"], "   ",
                                       "gemini")


class HumanDecisionTests(unittest.TestCase):
    def test_findings_start_undecided(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            review = ir.review_section(root, "discussion")
            self.assertTrue(all(f["decision"] is None
                                for f in review["deterministic_findings"]))
            self.assertEqual(review["review"], "PENDING HUMAN REVIEW")

    def test_decision_requires_reviewer_and_valid_decision(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            review = ir.review_section(root, "discussion")
            fid = review["deterministic_findings"][0]["finding_id"]
            with self.assertRaises(ir.ReviewError):
                ir.decide_finding(root, review["review_id"], fid, "ACCEPTED",
                                  "  ")
            with self.assertRaises(ir.ReviewError):
                ir.decide_finding(root, review["review_id"], fid, "MAYBE",
                                  "N. Researcher")

    def test_decision_recorded_and_final(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            review = ir.review_section(root, "discussion")
            fid = review["deterministic_findings"][0]["finding_id"]
            decided = ir.decide_finding(root, review["review_id"], fid,
                                        "ACCEPTED", "N. Researcher")
            self.assertEqual(decided["decision"], "ACCEPTED")
            self.assertEqual(decided["decided_by"], "N. Researcher")
            self.assertTrue(decided["decided_at"])
            with self.assertRaises(ir.ReviewError) as cm:
                ir.decide_finding(root, review["review_id"], fid, "REJECTED",
                                  "N. Researcher")
            self.assertIn("already decided", str(cm.exception))

    def test_unknown_finding_rejected(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            review = ir.review_section(root, "discussion")
            with self.assertRaises(ir.ReviewError):
                ir.decide_finding(root, review["review_id"], "F-999",
                                  "ACCEPTED", "N. Researcher")

    def test_review_itself_is_never_auto_approved(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            review = ir.review_section(root, "discussion")
            self.assertEqual(review["review"], "PENDING HUMAN REVIEW")
            draft_data = sd.load_section(root, "discussion")
            self.assertEqual(draft_data["versions"][-1]["review"],
                             "PENDING HUMAN REVIEW")


class ArtifactImmutabilityTests(unittest.TestCase):
    def test_draft_and_synthesis_bytes_unchanged_by_review(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            section_before = sd.section_path(root, "discussion").read_bytes()
            synthesis_before = ls.draft_path(root).read_bytes()
            ir.review_section(root, "discussion")
            self.assertEqual(sd.section_path(root, "discussion").read_bytes(),
                             section_before)
            self.assertEqual(ls.draft_path(root).read_bytes(),
                             synthesis_before)

    def test_review_stored_in_separate_directory(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            review = ir.review_section(root, "discussion")
            path = ir.review_path(root, review["review_id"])
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent.name, "review_reports")
            self.assertEqual(path.parent.parent.name, "outputs")
            self.assertNotEqual(path.parent,
                                sd.section_path(root, "discussion").parent)

    def test_review_records_artifact_fingerprint(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            review = ir.review_section(root, "discussion")
            artifact = review["reviewed_artifact"]
            self.assertEqual(artifact["section"], "discussion")
            self.assertEqual(artifact["version"], 1)
            self.assertEqual(len(artifact["sha256"]), 64)
            self.assertTrue(review["review_id"].startswith("RVW-"))

    def test_output_is_valid_json_without_secrets(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            review = ir.review_section(root, "discussion")
            path = ir.review_path(root, review["review_id"])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["review_id"], review["review_id"])
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn(SECRET, raw)
            self.assertNotIn("Bearer", raw)

    def test_unknown_section_and_version_rejected(self):
        tmp, root, ids = make_project()
        with tmp:
            draft(root, ids)
            with self.assertRaises(ir.ReviewError):
                ir.review_section(root, "introduction")  # no draft there
            with self.assertRaises(ir.ReviewError):
                ir.review_section(root, "discussion", version=99)

    def test_contradiction_references_surfaced_with_origin(self):
        tmp, root, ids = make_project()
        with tmp:
            # an approved synthesis AI interpretation that recorded conflicts
            synthesis = json.loads(ls.draft_path(root).read_text(
                encoding="utf-8"))
            synthesis["interpretation"].update({
                "status": "COMPLETED", "origin": "AI-GENERATED",
                "contradictions": ["A says X, B says not-X."]})
            ls.draft_path(root).write_text(json.dumps(synthesis),
                                           encoding="utf-8")
            draft(root, ids)
            review = ir.review_section(root, "discussion")
            refs = review["contradiction_references"]
            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0]["origin"], "AI-GENERATED")
            self.assertEqual(refs[0]["text"], "A says X, B says not-X.")


if __name__ == "__main__":
    unittest.main()
