"""Tests for the research-gap analysis workflow (RF-017). Offline only:
real RF-010/015/016 pipeline, fake providers for any AI path."""
import json
import tempfile
import unittest
from pathlib import Path

from app.core.sources import import_source, load_manifest
from app.providers.contracts import ProviderRequest, ProviderResponse, Usage
from app.providers.errors import NetworkError, ProviderServerError
from app.workflows import literature_synthesis as ls
from app.workflows import research_gap_analysis as ga
from app.workflows.paper_extraction import approve_extraction, extract_source

SECRET = "sk-personal-profile1-key"
UNREVIEWED = "UNREVIEWED-GAP-MARKER"

TEXT_A = ("Claim: Density supports walkability.\n\n"
          "Method: Household survey, n=200.\n\n"
          "Finding: Density predicts walking trips.\n\n"
          "Limitation: Single-city sample; no causality.\n\n"
          "Gap: No longitudinal evidence exists in this corpus.\n")
TEXT_B = ("Claim: Density supports walkability.\n\n"
          "Method: Street audit of 40 segments.\n\n"
          "Finding: Density predicts walking trips.\n\n"
          "Limitation: Small sample of segments.\n")
TEXT_C = f"Finding: {UNREVIEWED} should never appear.\n"


def make_approved_synthesis(texts, approve_sources=True):
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
        if approve_sources:
            approve_extraction(root, source_id, "N. Researcher")
        ids.append(source_id)
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
                                usage=Usage(input_tokens=10, output_tokens=5,
                                            total_tokens=15))


class EligibilityTests(unittest.TestCase):
    def test_missing_synthesis_draft_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        with tmp:
            with self.assertRaises(ga.GapAnalysisError) as cm:
                ga.build_analysis(root)
            self.assertIn("synthesis", str(cm.exception))

    def test_unapproved_synthesis_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "in").mkdir()
        (root / "sources" / "papers").mkdir(parents=True)
        (root / "sources" / "extracted_notes").mkdir(parents=True)
        src = root / "in" / "p.txt"
        src.write_text(TEXT_A, encoding="utf-8")
        source_id = import_source(root, src)["source_id"]
        extract_source(root, source_id)
        approve_extraction(root, source_id, "R")
        ls.synthesize(root)  # saved but NOT approved
        with tmp:
            with self.assertRaises(ga.GapAnalysisError) as cm:
                ga.build_analysis(root)
            self.assertIn("PENDING HUMAN REVIEW", str(cm.exception))

    def test_unreviewed_extractions_never_reach_gap_analysis(self):
        tmp2 = tempfile.TemporaryDirectory()
        root2 = Path(tmp2.name)
        (root2 / "in").mkdir()
        (root2 / "sources" / "papers").mkdir(parents=True)
        (root2 / "sources" / "extracted_notes").mkdir(parents=True)
        ids2 = []
        for index, text in enumerate([TEXT_A, TEXT_B]):
            src = root2 / "in" / f"ok{index}.txt"
            src.write_text(text, encoding="utf-8")
            sid = import_source(root2, src)["source_id"]
            extract_source(root2, sid)
            approve_extraction(root2, sid, "R")
            ids2.append(sid)
        src = root2 / "in" / "pending.txt"
        src.write_text(TEXT_C, encoding="utf-8")
        sid = import_source(root2, src)["source_id"]
        extract_source(root2, sid)  # left PENDING
        ls.synthesize(root2)
        ls.approve_synthesis(root2, "R")
        with tmp2:
            analysis = ga.build_analysis(root2)
            raw = json.dumps(analysis, ensure_ascii=False)
            self.assertNotIn(UNREVIEWED, raw)
            self.assertNotIn(sid, raw)
            self.assertEqual(analysis["source_synthesis"]["review"],
                             "APPROVED")


class ClassificationTests(unittest.TestCase):
    def test_directly_evidenced_limitations_with_ids(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            analysis = ga.build_analysis(root)
            limitations = analysis["candidate_gaps"][
                "directly_evidenced_limitations"]
            self.assertEqual(len(limitations), 2)
            for entry in limitations:
                self.assertIn(entry["source_id"], ids)
                self.assertTrue(entry["extraction_id"].startswith("EXT-"))
                self.assertTrue(entry["text"].startswith("Limitation:"))

    def test_cross_source_pattern_requires_two_sources(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            analysis = ga.build_analysis(root)
            patterns = analysis["candidate_gaps"]["inferred_patterns"]
            texts = [p["text"] for p in patterns]
            self.assertIn("Finding: Density predicts walking trips.", texts)
            matching = [p for p in patterns
                        if p["text"] == "Finding: Density predicts walking "
                                           "trips."][0]
            self.assertEqual(sorted(matching["source_ids"]), sorted(ids))
            self.assertEqual(len(matching["extraction_ids"]), 2)
            self.assertIn("cross-source pattern", matching["basis"])
            self.assertIn("not a causal or novelty claim",
                          matching["basis"])

    def test_single_source_text_is_not_a_pattern(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            analysis = ga.build_analysis(root)
            texts = [p["text"] for p in
                     analysis["candidate_gaps"]["inferred_patterns"]]
            # the limitation unique to TEXT_A is not a cross-source pattern
            self.assertNotIn("Limitation: Single-city sample; no causality.",
                             texts)

    def test_explicitly_labelled_gap_is_a_candidate_only(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            analysis = ga.build_analysis(root)
            opportunities = analysis["candidate_gaps"][
                "potential_research_opportunities"]
            self.assertEqual(len(opportunities), 1)
            entry = opportunities[0]
            self.assertTrue(entry["text"].startswith("Gap:"))
            self.assertEqual(entry["source_id"], ids[0])
            self.assertIsNone(analysis["candidate_gaps"]["opportunities_note"])

    def test_no_labelled_gaps_states_so_explicitly(self):
        tmp, root, ids = make_approved_synthesis([TEXT_B, TEXT_C])
        with tmp:
            analysis = ga.build_analysis(root)
            gaps = analysis["candidate_gaps"]
            self.assertEqual(gaps["potential_research_opportunities"], [])
            self.assertIn("none were inferred", gaps["opportunities_note"])

    def test_novelty_hypotheses_always_empty_and_disclaimed(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            analysis = ga.build_analysis(root)
            gaps = analysis["candidate_gaps"]
            self.assertEqual(gaps["unverified_novelty_hypotheses"], [])
            self.assertIn("NOT evidence of global novelty",
                          analysis["novelty_disclaimer"])
            self.assertIn("no novelty claim", analysis["novelty_disclaimer"])
            self.assertIn("Novelty cannot be established",
                          gaps["novelty_note"])
            # requirement 5: no field ever asserts novelty
            raw = json.dumps(gaps).lower()
            self.assertNotIn("novel finding", raw)
            self.assertNotIn("novel gap", raw)


class InsufficientCoverageTests(unittest.TestCase):
    def test_single_source_marks_insufficient_coverage(self):
        tmp2 = tempfile.TemporaryDirectory()
        root2 = Path(tmp2.name)
        (root2 / "in").mkdir()
        (root2 / "sources" / "papers").mkdir(parents=True)
        (root2 / "sources" / "extracted_notes").mkdir(parents=True)
        src = root2 / "in" / "one.txt"
        src.write_text(TEXT_A, encoding="utf-8")
        sid = import_source(root2, src)["source_id"]
        extract_source(root2, sid)
        approve_extraction(root2, sid, "R")
        ls.synthesize(root2)
        ls.approve_synthesis(root2, "R")
        with tmp2:
            analysis = ga.build_analysis(root2)
            self.assertTrue(analysis["eligibility"]["insufficient_coverage"])
            self.assertIn("under-supported",
                          analysis["eligibility"]["coverage_reason"])
            # no pattern is fabricated to fill the gap
            self.assertEqual(analysis["candidate_gaps"]["inferred_patterns"],
                             [])

    def test_two_sources_is_sufficient(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            analysis = ga.build_analysis(root)
            self.assertFalse(analysis["eligibility"]["insufficient_coverage"])
            self.assertIsNone(analysis["eligibility"]["coverage_reason"])
            self.assertEqual(analysis["eligibility"]["evidence_source_count"],
                             2)


class InterpretationSeparationTests(unittest.TestCase):
    def test_deterministic_analysis_has_no_interpretation(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            analysis = ga.build_analysis(root)
            interp = analysis["interpretation"]
            self.assertEqual(interp["status"], "NOT GENERATED")
            self.assertIsNone(interp["origin"])
            for key in ("conflicting_findings", "underexplored_areas",
                        "potential_opportunities", "novelty_hypotheses"):
                self.assertEqual(interp[key], [])

    def test_ai_interpretation_labelled_and_separate(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            analysis = ga.build_analysis(root)
            before = json.dumps(analysis["candidate_gaps"], sort_keys=True)
            provider = FakeProvider(content=json.dumps({
                "conflicting_findings": [],
                "underexplored_areas": ["Longitudinal studies"],
                "potential_opportunities": ["Panel design"],
                "novelty_hypotheses": ["Maybe no longitudinal work exists"],
            }))
            analysis = ga.add_ai_interpretation(root, analysis, provider)
            interp = analysis["interpretation"]
            self.assertEqual(interp["status"], "COMPLETED")
            self.assertEqual(interp["origin"], "AI-GENERATED")
            self.assertEqual(interp["review"], "PENDING HUMAN REVIEW")
            self.assertEqual(interp["underexplored_areas"],
                             ["Longitudinal studies"])
            # AI text never merges into the evidence sections
            self.assertEqual(json.dumps(analysis["candidate_gaps"],
                                        sort_keys=True), before)

    def test_prompt_contains_only_approved_evidence(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            analysis = ga.build_analysis(root)
            provider = FakeProvider(content="{}")
            ga.add_ai_interpretation(root, analysis, provider)
            prompt = provider.requests[0].prompt
            self.assertIn("Density predicts walking trips.", prompt)
            self.assertIn(ids[0], prompt)
            self.assertNotIn(SECRET, prompt)
            self.assertNotIn(UNREVIEWED, prompt)

    def test_provider_failure_marked_failed_without_invention(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            analysis = ga.build_analysis(root)
            provider = FakeProvider(error=NetworkError("simulated outage"))
            analysis = ga.add_ai_interpretation(root, analysis, provider)
            interp = analysis["interpretation"]
            self.assertEqual(interp["status"], "FAILED")
            self.assertEqual(interp["error"], "NetworkError")
            self.assertEqual(interp["underexplored_areas"], [])
            log = (root / "logs" / "usage.jsonl").read_text(encoding="utf-8")
            self.assertIn("NetworkError", log)

    def test_server_error_class_preserved(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            analysis = ga.build_analysis(root)
            provider = FakeProvider(error=ProviderServerError("502"))
            analysis = ga.add_ai_interpretation(root, analysis, provider)
            self.assertEqual(analysis["interpretation"]["error"],
                             "ProviderServerError")

    def test_malformed_ai_output_marked_failed(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            analysis = ga.build_analysis(root)
            provider = FakeProvider(content="[1,2,3]")
            analysis = ga.add_ai_interpretation(root, analysis, provider)
            self.assertEqual(analysis["interpretation"]["status"], "FAILED")
            self.assertIn("unparseable",
                          analysis["interpretation"]["error"])


class ReviewGovernanceTests(unittest.TestCase):
    def test_starts_pending_and_approval_requires_reviewer(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            analysis = ga.analyze_gaps(root)
            self.assertEqual(analysis["review"], "PENDING HUMAN REVIEW")
            with self.assertRaises(ga.GapAnalysisError):
                ga.approve_gap_analysis(root, "  ")

    def test_approval_records_reviewer_and_timestamp(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            ga.analyze_gaps(root)
            approved = ga.approve_gap_analysis(root, "N. Researcher")
            self.assertEqual(approved["review"], "APPROVED")
            self.assertEqual(approved["reviewed_by"], "N. Researcher")
            self.assertTrue(approved["reviewed_at"])

    def test_approved_analysis_cannot_be_overwritten(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            ga.analyze_gaps(root)
            ga.approve_gap_analysis(root, "N. Researcher")
            with self.assertRaises(ga.GapAnalysisError) as cm:
                ga.analyze_gaps(root)
            self.assertIn("APPROVED", str(cm.exception))
            self.assertEqual(ga.load_analysis(root)["review"], "APPROVED")

    def test_approval_without_draft_fails(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            with self.assertRaises(ga.GapAnalysisError):
                ga.approve_gap_analysis(root, "N. Researcher")


class OutputIntegrityTests(unittest.TestCase):
    def test_draft_written_as_valid_json_at_documented_path(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            ga.analyze_gaps(root)
            path = root / "outputs" / "drafts" / \
                "research_gap_analysis_draft.json"
            self.assertTrue(path.is_file())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["document"], "research_gap_analysis")

    def test_inputs_untouched(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            synthesis_path = ls.draft_path(root)
            synthesis_before = synthesis_path.read_bytes()
            stored = root / "sources" / "extracted_notes" / f"{ids[0]}.txt"
            stored_before = stored.read_bytes()
            manifest_before = json.dumps(load_manifest(root), sort_keys=True)
            ga.analyze_gaps(root)
            self.assertEqual(synthesis_path.read_bytes(), synthesis_before)
            self.assertEqual(stored.read_bytes(), stored_before)
            self.assertEqual(json.dumps(load_manifest(root), sort_keys=True),
                             manifest_before)

    def test_every_gap_references_evidence_ids(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            analysis = ga.analyze_gaps(root)
            gaps = analysis["candidate_gaps"]
            for key in ("directly_evidenced_limitations",
                        "potential_research_opportunities"):
                for entry in gaps[key]:
                    self.assertTrue(entry["source_id"].startswith("SRC-"))
                    self.assertTrue(entry["extraction_id"].startswith("EXT-"))
            for pattern in gaps["inferred_patterns"]:
                self.assertTrue(pattern["source_ids"])
                self.assertTrue(pattern["extraction_ids"])

    def test_secret_never_in_output(self):
        tmp, root, ids = make_approved_synthesis([TEXT_A, TEXT_B])
        with tmp:
            ga.analyze_gaps(root)
            raw = ga.analysis_path(root).read_text(encoding="utf-8")
            self.assertNotIn(SECRET, raw)
            self.assertNotIn("Bearer", raw)

    def test_unicode_evidence_preserved(self):
        text = ("Finding: Density predicts walking trips.\n\n"
                "Limitation: 步行性 é ✔ matters 🚶 样本 small.\n")
        tmp, root, ids = make_approved_synthesis([text, TEXT_B])
        with tmp:
            analysis = ga.analyze_gaps(root)
            raw = ga.analysis_path(root).read_text(encoding="utf-8")
            self.assertIn("步行性 é ✔ matters 🚶", raw)


if __name__ == "__main__":
    unittest.main()
