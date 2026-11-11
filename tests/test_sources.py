"""Tests for the source import workflow (RF-010)."""
import os
import tempfile
import unittest
from pathlib import Path

from app.core import sources as core

MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"

CANONICAL_LIT = "paper_id,title\n"
CANONICAL_EV = "evidence_id,claim\n"
CANONICAL_BIB = "% references.bib\n"

LIT_HEADER = ("paper_id,title,authors,publication_year,publication_type,"
              "journal_or_conference,publisher,doi,publisher_url,research_problem,"
              "research_objectives,methodology,dataset,study_location,key_findings,"
              "limitations,relevance_to_research,relevant_research_question,"
              "evidence_status,full_text_available,verification_notes,date_added")


def make_workspace():
    """Temp project root with canonical record files present."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    for d in ("sources/papers", "sources/extracted_notes"):
        (root / d).mkdir(parents=True)
    (root / "sources/literature_matrix.csv").write_text(CANONICAL_LIT, encoding="utf-8")
    (root / "sources/evidence_register.csv").write_text(CANONICAL_EV, encoding="utf-8")
    (root / "sources/references.bib").write_text(CANONICAL_BIB, encoding="utf-8")
    return tmp, root


def write_input(root_or_tmp, name, data):
    """Write an import input file (outside the workspace storage dirs)."""
    p = Path(root_or_tmp) / name
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data, encoding="utf-8")
    return p


def canonical_hashes(root: Path):
    return {name: (root / name).read_bytes()
            for name in ("sources/literature_matrix.csv",
                         "sources/evidence_register.csv",
                         "sources/references.bib")}


class ImportTypeTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root = make_workspace()
        self.addCleanup(self.tmp.cleanup)

    def import_ok(self, name, data, **kw):
        src = write_input(self.tmp.name, name, data)
        return core.import_source(self.root, src, **kw)

    def test_pdf_import(self):
        rec = self.import_ok("paper.pdf", MINIMAL_PDF)
        self.assertEqual(rec["source_type"], "pdf")
        self.assertTrue((self.root / rec["stored_location"]).is_file())
        self.assertEqual(rec["stored_location"].startswith("sources/papers/"), True)

    def test_markdown_import(self):
        rec = self.import_ok("notes.md", "# Perplexity findings\nsome text\n")
        self.assertEqual(rec["source_type"], "markdown")
        self.assertIn("extracted_notes", rec["stored_location"])

    def test_text_import(self):
        rec = self.import_ok("notes.txt", "plain observations\n")
        self.assertEqual(rec["source_type"], "text")

    def test_bibtex_import(self):
        bib = "@article{key1,\n  title = {Something},\n  year = {2024}\n}\n"
        rec = self.import_ok("refs.bib", bib)
        self.assertEqual(rec["source_type"], "bibtex")
        self.assertEqual(rec["validation_warnings"], [])

    def test_csv_import(self):
        rec = self.import_ok("lit.csv", LIT_HEADER + "\n")
        self.assertEqual(rec["source_type"], "csv")

    def test_unsupported_extension_rejected(self):
        with self.assertRaises(core.SourceError) as cm:
            self.import_ok("slides.pptx", b"PK\x03\x04")
        self.assertIn("unsupported", str(cm.exception))
        self.assertIn("pdftotext", str(cm.exception))  # PDF conversion hint

    def test_corrupt_docx_rejected(self):
        with self.assertRaises(core.SourceError) as cm:
            self.import_ok("doc.docx", b"PK\x03\x04")
        self.assertIn("DOCX", str(cm.exception))

    def test_invalid_pdf_rejected(self):
        with self.assertRaises(core.SourceError) as cm:
            self.import_ok("fake.pdf", b"this is not a pdf at all")
        self.assertIn("not a readable PDF", str(cm.exception))

    def test_malformed_bibtex_imports_with_warning(self):
        rec = self.import_ok("broken.bib", "@article{key1, title = {open brace...")
        self.assertTrue(any("unbalanced braces" in w
                            for w in rec["validation_warnings"]))
        self.assertTrue((self.root / rec["stored_location"]).is_file())

    def test_malformed_csv_rejected(self):
        ragged = LIT_HEADER + "\nonly,one,field\n"
        with self.assertRaises(core.SourceError) as cm:
            self.import_ok("bad.csv", ragged)
        self.assertIn("malformed", str(cm.exception).lower())


class DuplicateAndCollisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root = make_workspace()
        self.addCleanup(self.tmp.cleanup)

    def test_duplicate_checksum_detected(self):
        src = write_input(self.tmp.name, "a.md", "same content\n")
        first = core.import_source(self.root, src)
        with self.assertRaises(core.DuplicateSourceError) as cm:
            core.import_source(self.root, src)
        self.assertIn(first["source_id"], str(cm.exception))
        # still only one record and one stored file
        manifest = core.load_manifest(self.root)
        self.assertEqual(len(manifest["sources"]), 1)

    def test_same_name_different_content_is_stored_separately(self):
        a = write_input(self.tmp.name, "notes_a.md", "version one\n")
        b = write_input(self.tmp.name, "notes_b.md", "version two\n")
        r1 = core.import_source(self.root, a)
        r2 = core.import_source(self.root, b)
        self.assertNotEqual(r1["source_id"], r2["source_id"])
        self.assertNotEqual(r1["stored_location"], r2["stored_location"])
        self.assertEqual(r2["original_filename"], "notes_b.md")  # both preserved

    def test_existing_storage_collision_refuses_overwrite(self):
        src = write_input(self.tmp.name, "a.txt", "hello world\n")
        # Orphan scenario: a file sits at the deterministic storage path
        # (content-derived) without any manifest record.
        digest, _ = core.file_digest(src)
        stored = self.root / f"sources/extracted_notes/SRC-{digest[:12]}.txt"
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_text("orphan", encoding="utf-8")
        with self.assertRaises(core.DuplicateSourceError) as cm:
            core.import_source(self.root, src)
        self.assertIn("collision", str(cm.exception))
        self.assertEqual(stored.read_text(encoding="utf-8"), "orphan")  # not overwritten


class MetadataAndProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root = make_workspace()
        self.addCleanup(self.tmp.cleanup)

    def test_metadata_preserved(self):
        src = write_input(self.tmp.name, "gemini_findings.md", "findings\n")
        rec = core.import_source(self.root, src, provenance="gemini",
                                 verification_status="GREY LITERATURE")
        self.assertEqual(rec["original_filename"], "gemini_findings.md")
        self.assertEqual(rec["provenance"], "gemini")
        self.assertEqual(rec["verification_status"], "GREY LITERATURE")
        self.assertEqual(rec["import_status"], "IMPORTED")
        self.assertTrue(rec["imported_at"].endswith("Z") or "+" in rec["imported_at"])
        self.assertTrue(rec["sha256"])  # 64-hex
        self.assertEqual(len(rec["sha256"]), 64)
        stored = self.root / rec["stored_location"]
        self.assertEqual(stored.read_text(encoding="utf-8"), "findings\n")  # verbatim

    def test_pdf_not_marked_full_text_read(self):
        src = write_input(self.tmp.name, "paper.pdf", MINIMAL_PDF)
        rec = core.import_source(self.root, src)
        self.assertNotEqual(rec["verification_status"], "FULL TEXT READ")

    def test_invalid_status_rejected(self):
        src = write_input(self.tmp.name, "a.txt", "x")
        with self.assertRaises(core.SourceError):
            core.import_source(self.root, src, verification_status="TOTALLY FINE")

    def test_invalid_provenance_rejected(self):
        src = write_input(self.tmp.name, "a.txt", "x")
        with self.assertRaises(core.SourceError):
            core.import_source(self.root, src, provenance="twitter-dm")


class SecurityAndReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root = make_workspace()
        self.addCleanup(self.tmp.cleanup)

    def test_missing_input_file(self):
        with self.assertRaises(FileNotFoundError):
            core.import_source(self.root, self.tmp.name + "/nope.md")

    def test_show_by_id_never_treats_id_as_path(self):
        # traversal-like ids resolve to nothing in the manifest, not to paths
        self.assertIsNone(core.get_source(self.root, "../../PROJECT.md"))
        self.assertIsNone(core.get_source(self.root, "C:\\Windows\\system32"))

    def test_large_file_import(self):
        big = ("line of research observation text\n" * 60000)  # ~2 MB
        src = write_input(self.tmp.name, "big.txt", big)
        rec = core.import_source(self.root, src)
        self.assertEqual(rec["size_bytes"], src.stat().st_size)  # on-disk size
        rows = core.validate_sources(self.root)
        self.assertEqual(rows[0]["level"], "PASS")

    def test_interrupted_import_leaves_no_record(self):
        src = write_input(self.tmp.name, "a.md", "content\n")
        original = core.write_manifest
        def boom(root, manifest):
            raise OSError("simulated crash")
        core.write_manifest = boom
        try:
            with self.assertRaises(OSError):
                core.import_source(self.root, src)
        finally:
            core.write_manifest = original
        manifest = core.load_manifest(self.root)
        self.assertEqual(manifest["sources"], {})  # no misleading record
        leftovers = list((self.root / "sources").rglob("*.part"))
        self.assertEqual(leftovers, [])
        self.assertEqual(list((self.root / "sources/extracted_notes").iterdir()), [])

    def test_validate_detects_tampered_stored_file(self):
        src = write_input(self.tmp.name, "a.txt", "original\n")
        rec = core.import_source(self.root, src)
        (self.root / rec["stored_location"]).write_text("tampered\n", encoding="utf-8")
        rows = core.validate_sources(self.root)
        self.assertEqual(rows[0]["level"], "ERROR")
        self.assertTrue(any("checksum" in m for m in rows[0]["messages"]))


class CanonicalImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root = make_workspace()
        self.addCleanup(self.tmp.cleanup)

    def test_imports_never_touch_canonical_records(self):
        before = canonical_hashes(self.root)
        for name, data in (("p.pdf", MINIMAL_PDF), ("n.md", "md\n"),
                           ("n.txt", "txt\n"), ("r.bib", "@x{y,z}\n"),
                           ("l.csv", LIT_HEADER + "\n")):
            src = write_input(self.tmp.name, name, data)
            core.import_source(self.root, src)
        self.assertEqual(canonical_hashes(self.root), before)
