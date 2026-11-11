"""Tests for the paper-extraction workflow (RF-015). Deterministic and
offline: real imports go through the RF-010 pipeline, extraction is local."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.sources import (DuplicateSourceError, SourceError, file_digest,
                              import_source, load_manifest, validate_sources)
from app.workflows import paper_extraction as pe
from app.workflows.paper_extraction import (approve_extraction,
                                            extract_source, load_extraction,
                                            reject_extraction)

ROOT = Path(__file__).resolve().parent.parent

PAPER_TEXT = (
    "Claim: Walkability scores correlate with perceived safety.\n"
    "\n"
    "Method: A survey of 200 residents was conducted in 2025.\n"
    "\n"
    "Finding: Transit access was the strongest predictor.\n"
    "\n"
    "This paragraph has no explicit label, so it stays unclassified.\n"
)

UNICODE_TEXT = "Finding: 兀ä STREET-NAME é 在 GO 一致 ✔ émoji 🚶 ok\n"


def make_project(text=PAPER_TEXT, ext=".txt"):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "in").mkdir()
    # the real workspace has the RF-002 skeleton; a bare temp root needs it
    (root / "sources" / "papers").mkdir(parents=True)
    (root / "sources" / "extracted_notes").mkdir(parents=True)
    src = root / "in" / f"paper{ext}"
    if ext == ".pdf":
        src.write_bytes(b"%PDF-1.7\n" + b"binary-ish payload \x00\x01")
    else:
        src.write_text(text, encoding="utf-8")
    source_id = import_source(root, src)["source_id"]
    return tmp, root, source_id


class SuccessfulExtractionTests(unittest.TestCase):
    def test_text_source_extracts_into_classified_items(self):
        tmp, root, source_id = make_project()
        with tmp:
            record = extract_source(root, source_id)
            self.assertEqual(record["outcome"], "COMPLETED")
            kinds = [i["kind"] for i in record["items"]]
            self.assertEqual(kinds, ["claim", "method", "finding",
                                     "unclassified"])
            self.assertEqual(record["items"][0]["text"],
                             "Claim: Walkability scores correlate with "
                             "perceived safety.")
            self.assertEqual(record["error"], None)

    def test_provenance_fields_present_and_accurate(self):
        tmp, root, source_id = make_project()
        with tmp:
            record = extract_source(root, source_id)
            self.assertEqual(record["source_id"], source_id)
            self.assertEqual(record["source_sha256"],
                             load_manifest(root)["sources"][source_id]["sha256"])
            self.assertEqual(record["original_filename"], "paper.txt")
            self.assertEqual(record["extraction_method"], "local-deterministic")
            self.assertIsNone(record["model"])  # no AI: model stays unknown
            self.assertIn("T", record["extracted_at"])  # ISO timestamp
            self.assertTrue(record["extracted_at"].startswith("20"))

    def test_items_reference_their_source(self):
        tmp, root, source_id = make_project()
        with tmp:
            record = extract_source(root, source_id)
            self.assertTrue(all(i["source_id"] == source_id
                                for i in record["items"]))

    def test_extraction_record_is_stored_as_json_and_separate_from_original(self):
        tmp, root, source_id = make_project()
        with tmp:
            extract_source(root, source_id)
            path = pe.extraction_path(root, source_id)
            self.assertTrue(path.is_file())
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["source_id"], source_id)
            # the original imported file is untouched and still validates
            self.assertTrue((root / "sources" / "papers" /
                             f"{source_id}.txt").exists() or
                            (root / "sources" / "extracted_notes" /
                             f"{source_id}.txt").exists())
            self.assertTrue(all(row["level"] == "PASS"
                                for row in validate_sources(root)))

    def test_human_review_starts_pending_and_requires_explicit_decision(self):
        tmp, root, source_id = make_project()
        with tmp:
            record = extract_source(root, source_id)
            self.assertEqual(record["review"], "PENDING HUMAN REVIEW")
            decided = approve_extraction(root, source_id, "N. Researcher")
            self.assertEqual(decided["review"], "APPROVED")
            self.assertEqual(decided["reviewed_by"], "N. Researcher")
            self.assertIn("T", decided["reviewed_at"])
            reloaded = load_extraction(root, source_id)
            self.assertEqual(reloaded["review"], "APPROVED")

    def test_reject_extraction_records_decision(self):
        tmp, root, source_id = make_project()
        with tmp:
            extract_source(root, source_id)
            record = reject_extraction(root, source_id, "N. Researcher")
            self.assertEqual(record["review"], "REJECTED")

    def test_approval_requires_a_reviewer_name(self):
        tmp, root, source_id = make_project()
        with tmp:
            extract_source(root, source_id)
            with self.assertRaises(pe.ExtractionError):
                approve_extraction(root, source_id, "   ")

    def test_approval_requires_completed_extraction(self):
        tmp, root, source_id = make_project(ext=".pdf")
        with tmp:
            record = extract_source(root, source_id)  # not COMPLETED
            self.assertNotEqual(record["outcome"], "COMPLETED")
            with self.assertRaises(pe.ExtractionError) as cm:
                approve_extraction(root, source_id, "N. Researcher")
            self.assertIn(record["outcome"], str(cm.exception))

    def test_approved_extraction_cannot_be_overwritten(self):
        tmp, root, source_id = make_project()
        with tmp:
            extract_source(root, source_id)
            approve_extraction(root, source_id, "N. Researcher")
            with self.assertRaises(pe.ExtractionError) as cm:
                extract_source(root, source_id)
            self.assertIn("APPROVED", str(cm.exception))
            # the approved record survives untouched
            self.assertEqual(load_extraction(root, source_id)["review"],
                             "APPROVED")

    def test_pending_extraction_can_be_rerun(self):
        tmp, root, source_id = make_project()
        with tmp:
            first = extract_source(root, source_id)
            second = extract_source(root, source_id)
            self.assertEqual(first["outcome"], second["outcome"])
            self.assertGreaterEqual(second["extracted_at"],
                                    first["extracted_at"])


class ExplicitFailureTests(unittest.TestCase):
    def test_unknown_source_rejected(self):
        tmp, root, _ = make_project()
        with tmp:
            with self.assertRaises(SourceError) as cm:
                extract_source(root, "SRC-doesnotexist")
            self.assertIn("unknown source", str(cm.exception))

    def test_pdf_recorded_as_unsupported_not_successful(self):
        tmp, root, source_id = make_project(ext=".pdf")
        with tmp, mock.patch.object(pe, "find_pdf_tool",
                                    return_value=None):
            record = extract_source(root, source_id)
            self.assertEqual(record["outcome"], "UNSUPPORTED")
            self.assertEqual(record["items"], [])
            self.assertIn("pdftotext", record["error"])
            self.assertIn("import", record["error"])
            self.assertEqual(record["review"], "PENDING HUMAN REVIEW")

    def test_non_utf8_file_recorded_as_failed(self):
        # invalid UTF-8 bytes imported verbatim (txt has no content check):
        # extraction must report FAILED, not silently succeed
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "in").mkdir()
        (root / "sources" / "papers").mkdir(parents=True)
        (root / "sources" / "extracted_notes").mkdir(parents=True)
        src = root / "in" / "broken.txt"
        src.write_bytes(b"\xff\xfe not utf8 \x00\x80")
        source_id = import_source(root, src)["source_id"]
        with tmp:
            record = extract_source(root, source_id)
            self.assertEqual(record["outcome"], "FAILED")
            self.assertIn("UTF-8", record["error"])
            self.assertEqual(record["items"], [])

    def test_empty_source_recorded_as_empty_not_completed(self):
        tmp, root, source_id = make_project(text="")
        with tmp:
            record = extract_source(root, source_id)
            self.assertEqual(record["outcome"], "EMPTY")
            self.assertEqual(record["items"], [])
            self.assertIn("no text content", record["error"])

    def test_tampered_stored_file_fails_integrity_check(self):
        tmp, root, source_id = make_project()
        with tmp:
            stored = root / "sources" / "extracted_notes" / f"{source_id}.txt"
            stored.write_text("TAMPERED CONTENT\n", encoding="utf-8")
            record = extract_source(root, source_id)
            self.assertEqual(record["outcome"], "FAILED")
            self.assertIn("integrity", record["error"])

    def test_missing_stored_file_fails_explicitly(self):
        tmp, root, source_id = make_project()
        with tmp:
            (root / "sources" / "extracted_notes" /
             f"{source_id}.txt").unlink()
            record = extract_source(root, source_id)
            self.assertEqual(record["outcome"], "FAILED")
            self.assertIn("missing", record["error"])

    def test_original_imported_file_is_never_modified(self):
        tmp, root, source_id = make_project()
        with tmp:
            stored = root / "sources" / "extracted_notes" / f"{source_id}.txt"
            before = stored.read_bytes()
            manifest = load_manifest(root)
            extract_source(root, source_id)
            self.assertEqual(stored.read_bytes(), before)
            self.assertEqual(load_manifest(root), manifest)  # manifest intact


class ContentFidelityTests(unittest.TestCase):
    def test_unicode_content_preserved_verbatim(self):
        tmp, root, source_id = make_project(text=UNICODE_TEXT)
        with tmp:
            record = extract_source(root, source_id)
            self.assertEqual(record["outcome"], "COMPLETED")
            self.assertEqual(record["items"][0]["text"],
                             UNICODE_TEXT.strip())
            self.assertEqual(record["items"][0]["kind"], "finding")

    def test_no_invented_content(self):
        tmp, root, source_id = make_project()
        with tmp:
            record = extract_source(root, source_id)
            raw = json.dumps(record, ensure_ascii=False)
            # every item's text appears verbatim in the original file
            original = (root / "sources" / "extracted_notes" /
                        f"{source_id}.txt").read_text(encoding="utf-8")
            self.assertTrue(all(i["text"] in original
                                for i in record["items"]))
            # and nothing beyond the original text plus bookkeeping fields
            self.assertNotIn("AI", raw)
            self.assertNotIn("summary", raw.lower())

    def test_evidence_status_copied_not_upgraded(self):
        tmp, root, source_id = make_project()
        with tmp:
            record = extract_source(root, source_id)
            source = load_manifest(root)["sources"][source_id]
            self.assertEqual(record["source_evidence_status"],
                             source["verification_status"])
            self.assertEqual(source["verification_status"],
                             "SEARCH RESULT ONLY")  # untouched


class ExistingBehaviorRegressionTests(unittest.TestCase):
    def test_real_workspace_extractable_sources_exist_or_absent_cleanly(self):
        # real manifest may be empty; unknown ids must raise, known import
        # flow must keep working end-to-end
        from app.core.sources import get_source
        self.assertIsNone(get_source(ROOT, "SRC-nothing"))

    def test_duplicate_import_still_detected(self):
        tmp, root, source_id = make_project()
        with tmp:
            with tempfile.TemporaryDirectory() as other:
                copy = Path(other) / "copy.txt"
                copy.write_text(PAPER_TEXT, encoding="utf-8")
                with self.assertRaises(DuplicateSourceError):
                    import_source(root, copy)

    def test_extraction_path_never_collides_with_imported_files(self):
        self.assertTrue(pe.extraction_path(
            ROOT, "SRC-abc").name.endswith(".extraction.json"))


if __name__ == "__main__":
    unittest.main()
