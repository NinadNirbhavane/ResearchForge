"""Tests for manuscript section drafting (RF-018). Offline only: real
RF-010/015/016 pipeline, fake providers for AI generation."""
import json
import tempfile
import unittest
from pathlib import Path

from app.core.sources import import_source
from app.providers.contracts import ProviderRequest, ProviderResponse, Usage
from app.providers.errors import NetworkError, ProviderServerError
from app.workflows import literature_synthesis as ls
from app.workflows import research_gap_analysis as ga
from app.workflows import section_drafting as sd
from app.workflows.paper_extraction import approve_extraction, extract_source

SECRET = "sk-personal-profile1-key"
UNREVIEWED = "UNREVIEWED-PROSE-MARKER"

TEXT_A = ("Claim: Density supports walkability.\n\n"
          "Finding: Density predicts walking trips.\n\n"
          "Limitation: Single-city sample.\n")
TEXT_B = ("Claim: Density supports walkability.\n\n"
          "Finding: Density predicts walking trips.\n\n"
          "Gap: No longitudinal evidence exists.\n")


def make_approved_project():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "in").mkdir()
    (root / "sources" / "papers").mkdir(parents=True)
    (root / "sources" / "extracted_notes").mkdir(parents=True)
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
                                usage=Usage(input_tokens=50, output_tokens=20,
                                            total_tokens=70))


def prose_response(source_id, extra=""):
    prose = (f"Density predicts walking trips [{source_id}]. "
             f"Causal direction remains unclear [{source_id}].")
    return json.dumps({"prose": prose + extra})


class SectionSelectionTests(unittest.TestCase):
    def test_supported_sections_are_individual(self):
        self.assertEqual(sorted(sd.SECTIONS),
                         ["conclusion", "discussion", "introduction",
                          "literature_review", "methodology"])

    def test_unknown_section_rejected(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            with self.assertRaises(sd.DraftingError) as cm:
                sd.draft_section(root, "results", FakeProvider("{}"))
            self.assertIn("unknown section", str(cm.exception))
            self.assertIn("one section at a time", str(cm.exception))

    def test_sections_are_stored_independently(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            provider = FakeProvider(prose_response(ids[0]))
            sd.draft_section(root, "introduction", provider)
            sd.draft_section(root, "methodology", provider)
            self.assertTrue(sd.section_path(root, "introduction").is_file())
            self.assertTrue(sd.section_path(root, "methodology").is_file())
            self.assertEqual(
                sd.load_section(root, "introduction")["versions"][0]
                ["version"], 1)


class EvidenceEligibilityTests(unittest.TestCase):
    def test_missing_synthesis_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        with tmp:
            with self.assertRaises(sd.DraftingError) as cm:
                sd.draft_section(root, "introduction", FakeProvider("{}"))
            self.assertIn("synthesis", str(cm.exception))

    def test_unapproved_synthesis_is_refused(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            # replace the approved draft with an unapproved one
            draft = ls.build_draft(root)
            (root / "outputs" / "drafts" /
             "literature_review_draft.json").write_text(
                json.dumps(draft), encoding="utf-8")
            with self.assertRaises(sd.DraftingError) as cm:
                sd.draft_section(root, "introduction", FakeProvider("{}"))
            self.assertIn("PENDING HUMAN REVIEW", str(cm.exception))

    def test_evidence_refs_carry_source_and_extraction_ids(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            version = sd.draft_section(
                root, "literature_review",
                FakeProvider(prose_response(ids[0])))
            refs = version["evidence_refs"]
            self.assertTrue(refs)
            for ref in refs:
                self.assertTrue(ref["source_id"].startswith("SRC-"))
                self.assertTrue(ref["extraction_id"].startswith("EXT-"))
                self.assertIn(ref["source_id"], ids)

    def test_unapproved_gap_analysis_is_excluded_with_note(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            ga.analyze_gaps(root)  # saved, NOT approved
            version = sd.draft_section(
                root, "introduction", FakeProvider(prose_response(ids[0])))
            notes = " ".join(version["evidence_notes"])
            self.assertIn("excluded", notes)
            # no gap-analysis entries (multi-source pattern shape) leak in
            self.assertFalse(any("source_ids" in ref
                                 for ref in version["evidence_refs"]))

    def test_approved_gap_analysis_contributes(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            ga.analyze_gaps(root)
            ga.approve_gap_analysis(root, "N. Researcher")
            version = sd.draft_section(
                root, "introduction", FakeProvider(prose_response(ids[0])))
            self.assertTrue(any("longitudinal" in ref["text"].lower()
                                for ref in version["evidence_refs"]))


class GenerationAndAuditTests(unittest.TestCase):
    def test_draft_labelled_ai_generated_and_pending(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            version = sd.draft_section(
                root, "introduction", FakeProvider(prose_response(ids[0])))
            self.assertEqual(version["status"], "DRAFT")
            self.assertEqual(version["origin"], "AI-GENERATED")
            self.assertEqual(version["review"], "PENDING HUMAN REVIEW")
            self.assertEqual(version["model"], "deepseek-flash")

    def test_valid_citations_traced_to_evidence(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            version = sd.draft_section(
                root, "literature_review",
                FakeProvider(prose_response(ids[0])))
            self.assertEqual(version["citations"]["valid"], [ids[0]])
            self.assertEqual(version["citations"]["invalid"], [])

    def test_invented_citation_is_flagged_not_accepted(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            prose = (f"Established fact [SRC-aaaaaaaaaaaa]. "
                     f"Also true [{ids[0]}].")
            version = sd.draft_section(
                root, "discussion", FakeProvider(json.dumps({"prose": prose})))
            self.assertEqual(version["citations"]["invalid"],
                             ["SRC-aaaaaaaaaaaa"])
            self.assertEqual(version["citations"]["valid"], [ids[0]])

    def test_uncited_sentences_explicitly_marked_unsupported(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            prose = (f"Supported statement [{ids[0]}]. "
                     "This claim has no evidence behind it.")
            version = sd.draft_section(
                root, "discussion", FakeProvider(json.dumps({"prose": prose})))
            self.assertIn("This claim has no evidence behind it.",
                          version["unsupported_statements"])
            self.assertEqual(len(version["unsupported_statements"]), 1)
            self.assertIn("NOT evidence-backed",
                          version["unsupported_policy"])

    def test_evidence_stays_separate_from_prose(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            version = sd.draft_section(
                root, "introduction", FakeProvider(prose_response(ids[0])))
            # prose never leaks into the evidence list and vice versa
            for ref in version["evidence_refs"]:
                self.assertNotIn("Also true", ref["text"])
            self.assertIn("Density predicts walking trips", version["prose"])

    def test_researcher_context_is_labelled_and_not_cited(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            provider = FakeProvider(prose_response(ids[0]))
            version = sd.draft_section(
                root, "methodology", provider,
                researcher_context="This study surveys 200 households.")
            self.assertEqual(version["researcher_context"],
                             "This study surveys 200 households.")
            self.assertIn("not evidence",
                          version["researcher_context_label"])
            self.assertIn("RESEARCHER-PROVIDED CONTEXT", provider.requests[0].prompt)

    def test_prompt_contains_only_approved_evidence(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            provider = FakeProvider(prose_response(ids[0]))
            sd.draft_section(root, "introduction", provider)
            prompt = provider.requests[0].prompt
            self.assertIn("Density predicts walking trips", prompt)
            self.assertIn(ids[0], prompt)
            self.assertIn("Never invent", prompt)
            self.assertNotIn(UNREVIEWED, prompt)
            self.assertNotIn(SECRET, prompt)

    def test_usage_recorded_via_run_generation(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            sd.draft_section(root, "introduction",
                             FakeProvider(prose_response(ids[0])))
            log = (root / "logs" / "usage.jsonl").read_text(encoding="utf-8")
            self.assertIn('"task": "section_drafting"', log)

    def test_unicode_prose_preserved(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            prose = f"步行性 matters [{ids[0]}]. ✔"
            version = sd.draft_section(
                root, "discussion", FakeProvider(json.dumps({"prose": prose})))
            self.assertIn("步行性 matters", version["prose"])


class FailureTests(unittest.TestCase):
    def test_provider_failure_records_failed_version_and_raises(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            with self.assertRaises(NetworkError):
                sd.draft_section(root, "introduction",
                                 FakeProvider(error=NetworkError("outage")))
            data = sd.load_section(root, "introduction")
            self.assertEqual(len(data["versions"]), 1)
            failed = data["versions"][0]
            self.assertEqual(failed["status"], "FAILED")
            self.assertEqual(failed["error"], "NetworkError")
            self.assertIsNone(failed["prose"])
            log = (root / "logs" / "usage.jsonl").read_text(encoding="utf-8")
            self.assertIn("NetworkError", log)

    def test_server_error_class_preserved(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            with self.assertRaises(ProviderServerError):
                sd.draft_section(root, "introduction",
                                 FakeProvider(error=ProviderServerError("502")))
            self.assertEqual(
                sd.load_section(root, "introduction")["versions"][0]["error"],
                "ProviderServerError")

    def test_unusable_provider_output_rejected(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            for content in ("not json", json.dumps({"text": "no prose"}),
                            json.dumps({"prose": "   "})):
                with self.assertRaises(sd.DraftingError):
                    sd.draft_section(root, "introduction",
                                     FakeProvider(content))
            self.assertEqual(
                sd.load_section(root, "introduction")["versions"][-1]["status"],
                "FAILED")


class ApprovalAndVersioningTests(unittest.TestCase):
    def test_approval_requires_reviewer(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            sd.draft_section(root, "introduction",
                             FakeProvider(prose_response(ids[0])))
            with self.assertRaises(sd.DraftingError):
                sd.approve_section(root, "introduction", "  ")

    def test_approval_records_reviewer_and_timestamp(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            sd.draft_section(root, "introduction",
                             FakeProvider(prose_response(ids[0])))
            approved = sd.approve_section(root, "introduction", "N. Researcher")
            self.assertEqual(approved["review"], "APPROVED")
            self.assertEqual(approved["reviewed_by"], "N. Researcher")
            self.assertTrue(approved["reviewed_at"])

    def test_approved_version_is_immutable(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            sd.draft_section(root, "introduction",
                             FakeProvider(prose_response(ids[0])))
            sd.approve_section(root, "introduction", "N. Researcher")
            with self.assertRaises(sd.DraftingError) as cm:
                sd.approve_section(root, "introduction", "N. Researcher")
            self.assertIn("immutable", str(cm.exception))

    def test_new_draft_appends_version_and_preserves_approved_one(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            provider = FakeProvider(prose_response(ids[0]))
            sd.draft_section(root, "introduction", provider)
            sd.approve_section(root, "introduction", "N. Researcher")
            second = sd.draft_section(root, "introduction", provider)
            self.assertEqual(second["version"], 2)
            versions = sd.load_section(root, "introduction")["versions"]
            self.assertEqual([v["version"] for v in versions], [1, 2])
            self.assertEqual(versions[0]["review"], "APPROVED")
            self.assertEqual(versions[0]["prose"],
                             versions[0]["prose"])  # unchanged text
            self.assertEqual(versions[1]["review"], "PENDING HUMAN REVIEW")

    def test_approval_of_failed_version_refused(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            with self.assertRaises(NetworkError):
                sd.draft_section(root, "introduction",
                                 FakeProvider(error=NetworkError("x")))
            with self.assertRaises(sd.DraftingError) as cm:
                sd.approve_section(root, "introduction", "N. Researcher")
            self.assertIn("cannot be approved", str(cm.exception))

    def test_approval_without_draft_fails(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            with self.assertRaises(sd.DraftingError):
                sd.approve_section(root, "conclusion", "N. Researcher")


class OutputIntegrityTests(unittest.TestCase):
    def test_section_file_is_valid_json_at_documented_path(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            sd.draft_section(root, "discussion",
                             FakeProvider(prose_response(ids[0])))
            path = root / "outputs" / "sections" / "discussion.json"
            self.assertTrue(path.is_file())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["section"], "discussion")

    def test_final_manuscript_untouched(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            manuscript = root / "manuscript" / "sections"
            manuscript.mkdir(parents=True)
            (manuscript / "introduction.md").write_text("ORIGINAL",
                                                        encoding="utf-8")
            sd.draft_section(root, "introduction",
                             FakeProvider(prose_response(ids[0])))
            sd.approve_section(root, "introduction", "N. Researcher")
            self.assertEqual((manuscript / "introduction.md").read_text(
                encoding="utf-8"), "ORIGINAL")
            self.assertEqual(list(manuscript.iterdir()),
                             [manuscript / "introduction.md"])

    def test_no_secret_or_key_in_section_output(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            sd.draft_section(root, "introduction",
                             FakeProvider(prose_response(ids[0])))
            raw = sd.section_path(root, "introduction").read_text(
                encoding="utf-8")
            self.assertNotIn(SECRET, raw)
            self.assertNotIn("Bearer", raw)

    def test_sources_and_approved_synthesis_untouched(self):
        tmp, root, ids = make_approved_project()
        with tmp:
            synthesis_before = ls.draft_path(root).read_bytes()
            stored = root / "sources" / "extracted_notes" / f"{ids[0]}.txt"
            stored_before = stored.read_bytes()
            sd.draft_section(root, "introduction",
                             FakeProvider(prose_response(ids[0])))
            self.assertEqual(ls.draft_path(root).read_bytes(),
                             synthesis_before)
            self.assertEqual(stored.read_bytes(), stored_before)


if __name__ == "__main__":
    unittest.main()
