"""Tests for section-aware academic extraction (MVP-06). Offline; synthetic
fixtures only — the real-data pilot is reported separately."""
import tempfile
import unittest
from pathlib import Path

from app.core.sources import import_source
from app.workflows import literature_synthesis as ls
from app.workflows import research_gap_analysis as ga
from app.workflows.paper_extraction import (ExtractionError, decide_item,
                                            extract_candidates, extract_source,
                                            load_extraction)

PAPER = """Title of a Synthetic Study

Abstract
This paper studies sidewalk hazards for pedestrians. We propose a vision
system for detecting broken pavement.

1. Introduction
Urban walkability is important. There is a lack of longitudinal evidence
on sidewalk hazards.

2. Methods
We collected 1,200 street-level images in Navi Mumbai.

3. Results
The detector achieved an F1 score of 0.82 on the test set.

4. Discussion
These results suggest that vision models can support audits.

5. Limitations
The dataset covers a single city.

6. Conclusion
Future work should test other cities.

References
[1] A fake reference entry that must not become evidence.
"""


def make_root():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "in").mkdir()
    (root / "sources" / "papers").mkdir(parents=True)
    (root / "sources" / "extracted_notes").mkdir(parents=True)
    return tmp, root


def import_text(root, text=PAPER, name="paper.txt"):
    path = root / "in" / name
    path.write_text(text, encoding="utf-8")
    return import_source(root, path)


class SectionExtractionTests(unittest.TestCase):
    def test_sections_pages_and_locators_recorded(self):
        items = extract_candidates(
            "Abstract\nSome background.\n\n2. Methods\nWe did a survey.",
            "SRC-aaaaaaaaaaaa", "2026-11-10T00:00:00+00:00")
        self.assertEqual(items[0]["section"], "abstract")
        self.assertEqual(items[1]["section"], "methodology")
        self.assertEqual(items[0]["page"], 1)
        self.assertEqual(items[0]["paragraph"], 1)
        self.assertTrue(items[0]["extraction_id"].startswith("EXT-"))
        self.assertEqual(items[0]["review"], "PENDING HUMAN REVIEW")
        self.assertIsNone(items[0]["decision"])
        self.assertEqual(items[0]["extracted_at"],
                         "2026-11-10T00:00:00+00:00")

    def test_page_boundaries_preserved(self):
        text = ("Methods\nPage one text.\n\fResults\nPage two text.")
        items = extract_candidates(text, "SRC-bbbbbbbbbbbb", "t")
        self.assertEqual([i["page"] for i in items], [1, 2])

    def test_classifications_by_section(self):
        items = extract_candidates(PAPER, "SRC-cccccccccccc", "t")
        by_text = {item["text"]: item for item in items}
        self.assertEqual(by_text["We collected 1,200 street-level images "
                                "in Navi Mumbai."]["classification"], "method")
        result = by_text["The detector achieved an F1 score of 0.82 on the "
                         "test set."]
        self.assertEqual(result["classification"], "empirical_result")
        self.assertEqual(result["kind"], "finding")  # candidate, not verified
        self.assertEqual(by_text["The dataset covers a single city."]
                         ["classification"], "limitation")
        self.assertEqual(by_text["Future work should test other cities."]
                         ["classification"], "future_work")

    def test_references_section_produces_no_candidates(self):
        items = extract_candidates(PAPER, "SRC-dddddddddddd", "t")
        texts = " ".join(item["text"] for item in items)
        self.assertNotIn("fake reference entry", texts)

    def test_verbatim_text_preserved(self):
        items = extract_candidates(PAPER, "SRC-eeeeeeeeeeee", "t")
        for item in items:
            self.assertIn(item["text"], PAPER)

    def test_explicit_labels_still_win(self):
        items = extract_candidates(
            "Results\nClaim: explicit label wins over section context.",
            "SRC-ffffffffffff", "t")
        self.assertEqual(items[0]["kind"], "claim")
        self.assertEqual(items[0]["classification"], "claim")
        self.assertEqual(items[0]["text"],
                         "Claim: explicit label wins over section context.")

    def test_no_section_text_stays_unclassified(self):
        items = extract_candidates(
            "A plain paragraph with no structure at all.",
            "SRC-111111111111", "t")
        self.assertEqual(items[0]["classification"], "unclassified")
        self.assertEqual(items[0]["kind"], "unclassified")


class CandidateReviewTests(unittest.TestCase):
    def _extracted(self):
        tmp, root = make_root()
        source = import_text(root)
        record = extract_source(root, source["source_id"])
        return tmp, root, source["source_id"], record

    def test_approve_single_candidate(self):
        tmp, root, sid, record = self._extracted()
        with tmp:
            item = decide_item(root, sid, 1, "APPROVED", "N. Researcher")
            self.assertEqual(item["decision"], "APPROVED")
            self.assertEqual(item["reviewed_by"], "N. Researcher")
            self.assertTrue(item["reviewed_at"])
            reloaded = load_extraction(root, sid)
            self.assertEqual(reloaded["item_decisions"],
                             {"approved": 1, "rejected": 0,
                              "pending": len(record["items"]) - 1})

    def test_reject_single_candidate(self):
        tmp, root, sid, _ = self._extracted()
        with tmp:
            item = decide_item(root, sid, 1, "REJECTED", "R")
            self.assertEqual(item["decision"], "REJECTED")

    def test_reviewer_and_decision_validated(self):
        tmp, root, sid, _ = self._extracted()
        with tmp:
            with self.assertRaises(ExtractionError):
                decide_item(root, sid, 1, "APPROVED", "  ")
            with self.assertRaises(ExtractionError):
                decide_item(root, sid, 1, "MAYBE", "R")
            with self.assertRaises(ExtractionError):
                decide_item(root, sid, 999, "APPROVED", "R")

    def test_decisions_are_final(self):
        tmp, root, sid, _ = self._extracted()
        with tmp:
            decide_item(root, sid, 1, "APPROVED", "R")
            with self.assertRaises(ExtractionError) as cm:
                decide_item(root, sid, 1, "REJECTED", "R")
            self.assertIn("already decided", str(cm.exception))

    def test_whole_record_approval_freezes_candidates(self):
        tmp, root, sid, _ = self._extracted()
        with tmp:
            from app.workflows.paper_extraction import approve_extraction
            approve_extraction(root, sid, "R")
            with self.assertRaises(ExtractionError):
                decide_item(root, sid, 1, "APPROVED", "R")


class SynthesisIntegrationTests(unittest.TestCase):
    def _project(self):
        tmp, root = make_root()
        ids = []
        for index, text in enumerate((PAPER, PAPER.replace(
                "Title of a Synthetic Study", "Second Synthetic Study "
                "(variant)"))):
            path = root / "in" / f"paper{index}.txt"
            path.write_text(text, encoding="utf-8")
            sid = import_source(root, path)["source_id"]
            extract_source(root, sid)
            ids.append(sid)
        return tmp, root, ids

    def test_individually_approved_candidates_feed_synthesis(self):
        tmp, root, ids = self._project()
        with tmp:
            record = load_extraction(root, ids[0])
            finding = next(i for i in record["items"]
                           if i["classification"] == "empirical_result")
            decide_item(root, ids[0], finding["extraction_id"], "APPROVED", "R")
            draft = ls.build_draft(root)
            self.assertEqual(draft["eligibility"]["approved_extractions"],
                             [ids[0]])
            source_ids = {e["source_id"]
                          for e in draft["evidence"]["findings"]}
            self.assertEqual(source_ids, {ids[0]})
            entry = draft["evidence"]["findings"][0]
            self.assertEqual(entry["classification"], "empirical_result")
            self.assertTrue(entry["candidate_id"].startswith("EXT-"))

    def test_rejected_candidates_never_feed_synthesis(self):
        tmp, root, ids = self._project()
        with tmp:
            record = load_extraction(root, ids[0])
            finding = next(i for i in record["items"]
                           if i["classification"] == "empirical_result")
            decide_item(root, ids[0], finding["extraction_id"], "REJECTED", "R")
            draft = ls.build_draft(root)
            self.assertEqual(draft["eligibility"]["approved_extractions"], [])
            self.assertTrue(draft["eligibility"]["insufficient_evidence"])

    def test_gap_analysis_uses_section_classifications(self):
        tmp, root, ids = self._project()
        with tmp:
            record = load_extraction(root, ids[0])
            for item in record["items"]:
                if item["classification"] in ("empirical_result",
                                              "limitation", "future_work"):
                    decide_item(root, ids[0], item["extraction_id"],
                                "APPROVED", "R")
            ls.synthesize(root)
            ls.approve_synthesis(root, "R")
            analysis = ga.build_analysis(root)
            text = " ".join(
                entry["text"] for entry in
                analysis["candidate_gaps"]["directly_evidenced_limitations"])
            self.assertIn("single city", text)
            opportunities = " ".join(
                entry["text"] for entry in
                analysis["candidate_gaps"]["potential_research_opportunities"])
            self.assertIn("Future work", opportunities)


if __name__ == "__main__":
    unittest.main()
