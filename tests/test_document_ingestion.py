"""Offline tests for real-document ingestion (MVP-02): DOCX via stdlib
ZIP+XML, PDF via the external pdftotext utility (mocked — never executed)."""
import io
import json
import platform
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from app.cli import workflows as cli
from app.core import sources as core
from app.core.sources import SourceError, import_source, is_valid_docx, load_manifest
from app.workflows import paper_extraction as pe
from app.workflows.paper_extraction import (approve_extraction, extract_source,
                                            load_extraction)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    'content-types"><Default Extension="xml" ContentType="application/'
    'xml"/></Types>')


def p(text):
    return ('<w:p><w:r><w:t xml:space="preserve">'
            + text + '</w:t></w:r></w:p>')


def tbl(rows):
    cells = "".join(
        "<w:tr>" + "".join(f"<w:tc>{p(cell)}</w:tc>" for cell in row) +
        "</w:tr>" for row in rows)
    return f"<w:tbl>{cells}</w:tbl>"


def document_xml(body):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body>'
            '</w:document>')


def write_docx(path: Path, body: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("word/document.xml", document_xml(body))


def make_root():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "in").mkdir()
    (root / "sources" / "papers").mkdir(parents=True)
    (root / "sources" / "extracted_notes").mkdir(parents=True)
    return tmp, root


def import_docx(root: Path, body: str):
    path = root / "in" / "paper.docx"
    write_docx(path, body)
    return import_source(root, path), path


class DocxImportTests(unittest.TestCase):
    def test_docx_is_imported_as_a_binary_paper(self):
        tmp, root = make_root()
        with tmp:
            record, _ = import_docx(root, p("Hello"))
            self.assertEqual(record["source_type"], "docx")
            self.assertTrue(record["stored_location"].startswith(
                "sources/papers/"))
            self.assertTrue(record["stored_location"].endswith(".docx"))

    def test_corrupt_archive_is_rejected_at_import(self):
        tmp, root = make_root()
        with tmp:
            bad = root / "in" / "bad.docx"
            bad.write_bytes(b"this is not a zip archive")
            with self.assertRaises(SourceError) as cm:
                import_source(root, bad)
            self.assertIn("DOCX", str(cm.exception))
            self.assertEqual(load_manifest(root)["sources"], {})

    def test_zip_without_document_part_is_rejected(self):
        tmp, root = make_root()
        with tmp:
            bad = root / "in" / "empty.docx"
            with zipfile.ZipFile(bad, "w") as archive:
                archive.writestr("other.xml", "<x/>")
            self.assertFalse(is_valid_docx(bad))
            with self.assertRaises(SourceError):
                import_source(root, bad)


class DocxExtractionTests(unittest.TestCase):
    def test_paragraphs_extracted_in_document_order(self):
        tmp, root = make_root()
        with tmp:
            source, _ = import_docx(
                root, p("First paragraph.")
                          + p("Claim: Density supports walkability.")
                          + p("Method: Household survey.")
                          + p("Finding: Density predicts walking trips."))
            record = extract_source(root, source["source_id"])
            self.assertEqual(record["outcome"], "COMPLETED")
            self.assertEqual([i["kind"] for i in record["items"]],
                             ["unclassified", "claim", "method", "finding"])
            self.assertEqual(record["items"][0]["text"], "First paragraph.")

    def test_table_cells_preserved_in_reading_order(self):
        tmp, root = make_root()
        with tmp:
            source, _ = import_docx(
                root, p("Before table.")
                          + tbl([["Segment", "Score"], ["A1", "3.2"]])
                          + p("After table."))
            record = extract_source(root, source["source_id"])
            texts = [i["text"] for i in record["items"]]
            self.assertEqual(texts[0], "Before table.")
            self.assertIn("Segment | Score", texts)
            self.assertIn("A1 | 3.2", texts)
            self.assertEqual(texts[-1], "After table.")

    def test_unicode_preserved(self):
        tmp, root = make_root()
        with tmp:
            source, _ = import_docx(
                root, p("Finding: 步行性 é ✔ matters 🚶."))
            record = extract_source(root, source["source_id"])
            self.assertEqual(record["items"][0]["text"],
                             "Finding: 步行性 é ✔ matters 🚶.")

    def test_empty_document_reported_as_empty(self):
        tmp, root = make_root()
        with tmp:
            source, _ = import_docx(root, p("") + p("   "))
            record = extract_source(root, source["source_id"])
            self.assertEqual(record["outcome"], "EMPTY")
            self.assertIn("no extractable text", record["error"])

    def test_invalid_xml_reported_as_failed(self):
        tmp, root = make_root()
        with tmp:
            path = root / "in" / "broken.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("[Content_Types].xml", CONTENT_TYPES)
                archive.writestr("word/document.xml", "<not-valid-xml")
            source = import_source(root, path)
            record = extract_source(root, source["source_id"])
            self.assertEqual(record["outcome"], "FAILED")
            self.assertIn("invalid DOCX document XML", record["error"])
            self.assertEqual(record["items"], [])

    def test_malformed_archive_detected_by_extractor(self):
        tmp, root = make_root()
        with tmp:
            bad = root / "in" / "bad.docx"
            bad.write_bytes(b"PK\x03\x04garbage")
            with self.assertRaises(ValueError):
                pe.docx_text(bad)

    def test_original_document_never_modified(self):
        tmp, root = make_root()
        with tmp:
            source, original = import_docx(root, p("Stable content."))
            stored = root / source["stored_location"]
            before = stored.read_bytes()
            extract_source(root, source["source_id"])
            self.assertEqual(stored.read_bytes(), before)
            self.assertEqual(original.read_bytes(), before)
            self.assertTrue(all(row["level"] == "PASS"
                                for row in core.validate_sources(root)))

    def test_provenance_and_limitations_recorded(self):
        tmp, root = make_root()
        with tmp:
            source, _ = import_docx(root, p("Finding: X."))
            record = extract_source(root, source["source_id"])
            self.assertEqual(record["source_sha256"], source["sha256"])
            self.assertEqual(record["original_filename"], "paper.docx")
            self.assertEqual(record["extraction_method"], "local-deterministic")
            self.assertEqual(record["extraction_tool"], pe.DOCX_TOOL)
            self.assertEqual(record["extraction_tool_version"],
                             platform.python_version())
            self.assertIn("T", record["extracted_at"])
            self.assertTrue(record["limitations"])
            self.assertEqual(record["review"], "PENDING HUMAN REVIEW")
            self.assertIsNone(record["model"])

    def test_evidence_status_not_upgraded_and_approval_required(self):
        tmp, root = make_root()
        with tmp:
            source, _ = import_docx(root, p("Finding: X."))
            record = extract_source(root, source["source_id"])
            self.assertEqual(source["verification_status"],
                             "SEARCH RESULT ONLY")
            self.assertEqual(record["source_evidence_status"],
                             "SEARCH RESULT ONLY")
            approved = approve_extraction(root, source["source_id"],
                                          "N. Researcher")
            self.assertEqual(approved["review"], "APPROVED")
            with self.assertRaises(SourceError):
                extract_source(root, source["source_id"])


class PdfExtractionTests(unittest.TestCase):
    def _pdf_source(self, root):
        path = root / "in" / "paper.pdf"
        path.write_bytes(b"%PDF-1.7\nfake pdf bytes")
        return import_source(root, path)

    def test_missing_tool_is_unsupported_with_conversion_path(self):
        tmp, root = make_root()
        with tmp, mock.patch.object(pe, "find_pdf_tool", return_value=None):
            source = self._pdf_source(root)
            record = extract_source(root, source["source_id"])
            self.assertEqual(record["outcome"], "UNSUPPORTED")
            self.assertIn("pdftotext", record["error"])
            self.assertIn("import", record["error"])
            self.assertEqual(record["items"], [])

    def test_converter_success_records_tool_version_and_pages(self):
        tmp, root = make_root()
        with tmp, mock.patch.object(pe, "find_pdf_tool",
                                    return_value="pdftotext"), \
                mock.patch.object(pe, "pdf_tool_version",
                                  return_value="pdftotext version 23.01.0"), \
                mock.patch.object(pe, "pdf_text",
                                  return_value=("Page one text.\n\nPage two "
                                                "text.", 2)):
            source = self._pdf_source(root)
            record = extract_source(root, source["source_id"])
            self.assertEqual(record["outcome"], "COMPLETED")
            self.assertEqual(record["extraction_tool"], "pdftotext")
            self.assertEqual(record["extraction_tool_version"],
                             "pdftotext version 23.01.0")
            self.assertEqual(record["pages"], 2)
            self.assertTrue(record["limitations"])
            self.assertEqual(len(record["items"]), 2)

    def test_scanned_pdf_is_explicitly_empty(self):
        tmp, root = make_root()
        with tmp, mock.patch.object(pe, "find_pdf_tool",
                                    return_value="pdftotext"), \
                mock.patch.object(pe, "pdf_tool_version",
                                  return_value="v"), \
                mock.patch.object(pe, "pdf_text", return_value=("", 0)):
            source = self._pdf_source(root)
            record = extract_source(root, source["source_id"])
            self.assertEqual(record["outcome"], "EMPTY")
            self.assertIn("scanned", record["error"])
            self.assertIn("OCR", record["error"])

    def test_converter_failure_is_failed_not_empty(self):
        tmp, root = make_root()
        with tmp, mock.patch.object(pe, "find_pdf_tool",
                                    return_value="pdftotext"), \
                mock.patch.object(pe, "pdf_tool_version", return_value="v"), \
                mock.patch.object(
                    pe, "pdf_text",
                    side_effect=ValueError(
                        "pdftotext failed (exit 1): broken file")):
            source = self._pdf_source(root)
            record = extract_source(root, source["source_id"])
            self.assertEqual(record["outcome"], "FAILED")
            self.assertIn("pdftotext", record["error"])

    def test_pdf_text_runs_without_a_shell(self):
        tmp, root = make_root()
        with tmp:
            path = root / "in" / "x.pdf"
            path.write_bytes(b"%PDF-1.4\n")
            completed = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=b"page one\fpage two", stderr=b"")
            with mock.patch.object(pe.subprocess, "run",
                                   return_value=completed) as run:
                text, pages = pe.pdf_text("pdftotext", path)
            argv = run.call_args.args[0]
            self.assertIsInstance(argv, list)
            self.assertIn("-layout", argv)
            self.assertIn("UTF-8", argv)
            self.assertFalse(run.call_args.kwargs.get("shell", False))
            self.assertEqual(pages, 2)
            self.assertIn("page one", text)
            self.assertIn("page two", text)

    def test_tool_version_falls_back_when_unavailable(self):
        with mock.patch.object(pe.subprocess, "run",
                               side_effect=OSError("boom")):
            self.assertEqual(pe.pdf_tool_version("pdftotext"), "unknown")


class CliIntegrationTests(unittest.TestCase):
    def test_extraction_run_shows_tool_and_limitations(self):
        tmp, root = make_root()
        with tmp:
            source, _ = import_docx(root, p("Finding: X."))
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = cli.cmd_extraction_run(
                    type("A", (), {"source_id": source["source_id"]})(),
                    root=root)
            output = buffer.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("COMPLETED", output)
            self.assertIn("tool:", output)
            self.assertIn("limitation:", output)

    def test_sources_help_lists_formats_and_pdf_route(self):
        from app.main import build_parser
        parser = build_parser()
        help_text = parser.format_help()
        self.assertIn("sources", help_text)
        sources_help = parser._subparsers._group_actions[0].choices[
            "sources"].format_help()
        self.assertIn(".docx", sources_help)
        self.assertIn("pdftotext", sources_help)

    def test_text_sources_still_work(self):
        tmp, root = make_root()
        with tmp:
            path = root / "in" / "plain.txt"
            path.write_text("Finding: plain text works.\n", encoding="utf-8")
            source = import_source(root, path)
            record = extract_source(root, source["source_id"])
            self.assertEqual(record["outcome"], "COMPLETED")
            self.assertEqual(record["items"][0]["kind"], "finding")


if __name__ == "__main__":
    unittest.main()
