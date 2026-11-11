"""Offline tests for DOCX manuscript export (MVP-03). Real section
pipeline fixtures; no network, no LibreOffice requirement."""
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from xml.etree import ElementTree

from app.cli import export as export_cli
from app.core import approvals
from app.core.sources import import_source
from app.exporters import docx as exporter
from app.exporters.docx import ExportError, export_docx, plan_export, validate_export
from app.main import build_parser
from app.providers.contracts import ProviderResponse, Usage
from app.providers.errors import NetworkError
from app.workflows import literature_synthesis as ls
from app.workflows import section_drafting as sd
from app.workflows.paper_extraction import approve_extraction, extract_source

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

TEXT_A = ("Claim: Density supports walkability.\n\n"
          "Finding: Density predicts walking trips.\n")
TEXT_B = ("Finding: Density predicts walking trips.\n\n"
          "Limitation: Single-city sample.\n")


class FakeProvider:
    provider_id = "deepseek"

    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error

    def generate(self, request):
        if self.error is not None:
            raise self.error
        return ProviderResponse(content=self.content, provider="deepseek",
                                model="deepseek-flash",
                                usage=Usage(input_tokens=1, output_tokens=1,
                                            total_tokens=2))


def make_project():
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


def approve_section(root, section, prose, reviewer="N. Researcher"):
    version = sd.draft_section(
        root, section, FakeProvider(json.dumps({"prose": prose})))
    sd.approve_section(root, section, reviewer, version=version["version"])
    return version


def make_ready_project():
    tmp, root, ids = make_project()
    approve_section(root, "introduction",
                    f"Walking matters [{ids[0]}]. 步行性 é 🚶 survives.\n\n"
                    "A second paragraph without markup.")
    approve_section(root, "discussion",
                    f"Density predicts walking [{ids[0]}], and again "
                    f"[{ids[1]}].")
    return tmp, root, ids


def document_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    return "".join(node.text or "" for node in root.iter(f"{W_NS}t"))


def headings(path: Path) -> list:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    result = []
    for paragraph in root.iter(f"{W_NS}p"):
        style = paragraph.find(f"{W_NS}pPr/{W_NS}pStyle")
        if style is not None and style.get(f"{W_NS}val") == "Heading1":
            result.append("".join(
                node.text or "" for node in paragraph.iter(f"{W_NS}t")))
    return result


class ExportStructureTests(unittest.TestCase):
    def test_valid_docx_with_required_parts_and_xml(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            manifest = export_docx(root, "outputs/docx/test.docx")
            path = root / "outputs/docx/test.docx"
            self.assertTrue(path.is_file())
            self.assertEqual(manifest["validation"]["status"], "PASS")
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                for part in exporter.DOCX_REQUIRED_PARTS:
                    self.assertIn(part, names)
                for part in exporter.DOCX_REQUIRED_PARTS:
                    ElementTree.fromstring(archive.read(part))

    def test_title_and_headings_present(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            export_docx(root, "outputs/docx/test.docx", title="Walkability Study")
            path = root / "outputs/docx/test.docx"
            self.assertEqual(headings(path),
                             ["Introduction", "Discussion",
                              exporter.REFERENCES_HEADING])
            with zipfile.ZipFile(path) as archive:
                core = archive.read("docProps/core.xml").decode("utf-8")
            self.assertIn("Walkability Study", core)

    def test_page_setup_and_metadata(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            export_docx(root, "outputs/docx/test.docx")
            with zipfile.ZipFile(root / "outputs/docx/test.docx") as archive:
                document = archive.read("word/document.xml").decode("utf-8")
                core = archive.read("docProps/core.xml").decode("utf-8")
                styles = archive.read("word/styles.xml").decode("utf-8")
            self.assertIn('w:pgSz w:w="11906" w:h="16838"', document)
            self.assertIn("w:pgMar", document)
            self.assertIn("<dc:creator>ResearchForge", core)
            self.assertIn("Heading1", styles)

    def test_unicode_and_paragraph_boundaries_preserved(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            export_docx(root, "outputs/docx/test.docx")
            text = document_text(root / "outputs/docx/test.docx")
            self.assertIn("步行性 é 🚶 survives.", text)
            self.assertIn("A second paragraph without markup.", text)
            self.assertIn("Density predicts walking", text)

    def test_section_order_and_selection(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            manifest = export_docx(
                root, "outputs/docx/selected.docx",
                sections=["discussion", "introduction"])
            self.assertEqual([s["section"] for s in manifest["sections"]],
                             ["introduction", "discussion"])
            self.assertEqual(headings(root / "outputs/docx/selected.docx"),
                             ["Introduction", "Discussion",
                              exporter.REFERENCES_HEADING])


class ApprovalEnforcementTests(unittest.TestCase):
    def test_multiple_approved_versions_use_latest(self):
        tmp, root, ids = make_project()
        with tmp:
            approve_section(root, "introduction", "First approved text.")
            approve_section(root, "introduction", "Second approved text.")
            manifest = export_docx(root, "outputs/docx/test.docx")
            self.assertEqual(manifest["sections"][0]["version"], 2)
            text = document_text(root / "outputs/docx/test.docx")
            self.assertIn("Second approved text.", text)
            self.assertNotIn("First approved text.", text)

    def test_unapproved_section_excluded_by_default(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            sd.draft_section(root, "methodology",
                             FakeProvider(json.dumps(
                                 {"prose": "Pending method text."})))
            manifest = export_docx(root, "outputs/docx/test.docx")
            names = [s["section"] for s in manifest["sections"]]
            self.assertEqual(names, ["introduction", "discussion"])
            excluded = {e["section"]: e["reason"]
                        for e in manifest["excluded_sections"]}
            self.assertIn("methodology", excluded)
            self.assertNotIn("Pending method text.",
                             document_text(root / "outputs/docx/test.docx"))

    def test_explicitly_requesting_unapproved_section_is_refused(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            sd.draft_section(root, "methodology",
                             FakeProvider(json.dumps({"prose": "Pending."})))
            with self.assertRaises(ExportError) as cm:
                export_docx(root, "outputs/docx/test.docx",
                            sections=["methodology"])
            self.assertIn("no APPROVED", str(cm.exception))

    def test_failed_section_version_is_never_exported(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            with self.assertRaises(NetworkError):
                sd.draft_section(root, "methodology",
                                 FakeProvider(error=NetworkError("outage")))
            manifest = export_docx(root, "outputs/docx/test.docx")
            self.assertNotIn("methodology",
                             [s["section"] for s in manifest["sections"]])
            with self.assertRaises(ExportError):
                export_docx(root, "outputs/docx/x.docx",
                            sections=["methodology"])

    def test_tampered_approved_section_refused(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            path = sd.section_path(root, "introduction")
            data = json.loads(path.read_text(encoding="utf-8"))
            data["versions"][0]["prose"] = "silently rewritten after approval"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ExportError) as cm:
                export_docx(root, "outputs/docx/test.docx")
            self.assertIn("fingerprint mismatch", str(cm.exception))
            self.assertFalse((root / "outputs/docx/test.docx").exists())

    def test_missing_ledger_entry_refused(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            approvals.ledger_path(root).unlink()
            with self.assertRaises(ExportError) as cm:
                export_docx(root, "outputs/docx/test.docx")
            self.assertIn("no approval ledger entry", str(cm.exception))

    def test_no_approved_sections_refused(self):
        tmp, root, ids = make_project()
        with tmp:
            with self.assertRaises(ExportError) as cm:
                export_docx(root, "outputs/docx/test.docx")
            self.assertIn("no approved sections", str(cm.exception))
            self.assertFalse((root / "outputs/docx/test.docx").exists())


class CitationTests(unittest.TestCase):
    def test_citation_identifiers_preserved_exactly(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            manifest = export_docx(root, "outputs/docx/test.docx")
            text = document_text(root / "outputs/docx/test.docx")
            for cid in ids:
                self.assertIn(f"[{cid}]", text)
            self.assertEqual(sorted(manifest["citation_identifiers"]),
                             sorted(ids))

    def test_unresolved_references_section_labeled(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            export_docx(root, "outputs/docx/test.docx")
            path = root / "outputs/docx/test.docx"
            self.assertIn(exporter.REFERENCES_HEADING, headings(path))
            text = document_text(path)
            self.assertIn("NOT complete academic references", text)
            self.assertIn("unresolved reference", text)

    def test_no_fabricated_bibliographic_details(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            export_docx(root, "outputs/docx/test.docx")
            text = document_text(root / "outputs/docx/test.docx").lower()
            for forbidden in ("doi:", "et al", "http://", "https://",
                              "journal of"):
                self.assertNotIn(forbidden, text)

    def test_manifest_records_unresolved_references(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            manifest = export_docx(root, "outputs/docx/test.docx")
            unresolved = {item["citation_id"]
                          for item in manifest["unresolved_references"]}
            self.assertEqual(unresolved, set(ids))
            self.assertTrue(all(item["status"] == "UNRESOLVED"
                                for item in manifest["unresolved_references"]))


class OutputSafetyTests(unittest.TestCase):
    def test_existing_output_is_protected(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            out = root / "outputs/docx/test.docx"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"existing document")
            with self.assertRaises(ExportError) as cm:
                export_docx(root, "outputs/docx/test.docx")
            self.assertIn("already exists", str(cm.exception))
            self.assertEqual(out.read_bytes(), b"existing document")

    def test_force_overwrites(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            out = root / "outputs/docx/test.docx"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"old")
            manifest = export_docx(root, "outputs/docx/test.docx", force=True)
            self.assertEqual(manifest["validation"]["status"], "PASS")
            with zipfile.ZipFile(out) as archive:
                self.assertIn("word/document.xml", archive.namelist())

    def test_manifest_matches_written_document(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            manifest = export_docx(root, "outputs/docx/test.docx")
            path = root / "outputs/docx/test.docx"
            manifest_path = root / "outputs/docx/test.manifest.json"
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["output"]["sha256"],
                             manifest["output"]["sha256"])
            self.assertEqual(saved["sections"][0]["version"],
                             manifest["sections"][0]["version"])
            self.assertTrue(saved["created_at"])
            for section in saved["sections"]:
                self.assertEqual(len(section["fingerprint"]), 64)
                self.assertTrue(section["evidence_refs"])


class ValidationTests(unittest.TestCase):
    def test_validate_pass_and_checks_listed(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            manifest = export_docx(root, "outputs/docx/test.docx")
            result = validate_export(root / "outputs/docx/test.docx",
                                     manifest)
            self.assertEqual(result["status"], "PASS")
            names = {check["check"] for check in result["checks"]}
            for expected in ("zip_integrity", "required_parts",
                             "xml_well_formed", "section_order",
                             "citations_preserved", "output_checksum"):
                self.assertIn(expected, names)

    def test_missing_part_detected(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            path = root / "outputs/docx/test.docx"
            export_docx(root, "outputs/docx/test.docx")
            with zipfile.ZipFile(path) as archive:
                parts = {name: archive.read(name)
                         for name in archive.namelist()}
            del parts["word/styles.xml"]
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in parts.items():
                    archive.writestr(name, data)
            result = validate_export(path)
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(any("required_parts" in p
                                for p in result["problems"]))

    def test_non_docx_detected(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            path = root / "outputs/docx/bogus.docx"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"not a zip")
            self.assertEqual(validate_export(path)["status"], "FAIL")

    def test_checksum_mismatch_detected(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            manifest = export_docx(root, "outputs/docx/test.docx")
            manifest["output"]["sha256"] = "0" * 64
            result = validate_export(root / "outputs/docx/test.docx", manifest)
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(any("output_checksum" in p
                                for p in result["problems"]))

    def test_libreoffice_check_is_skipped_when_absent(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            export_docx(root, "outputs/docx/test.docx")
            result = exporter.libreoffice_check(
                root / "outputs/docx/test.docx")
            self.assertIn(result["status"], ("SKIPPED", "PASS"))


class CliTests(unittest.TestCase):
    def _args(self, **kwargs):
        class Args:
            pass
        args = Args()
        defaults = {"output": "outputs/docx/cli.docx", "title": None,
                    "sections": None, "check": False, "force": False,
                    "libreoffice": False}
        defaults.update(kwargs)
        for key, value in defaults.items():
            setattr(args, key, value)
        return args

    def test_cli_export_success(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = export_cli.cmd_export_docx(self._args(), root=root)
            output = buffer.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("validation: PASS", output)
            self.assertIn("manifest:", output)
            self.assertTrue((root / "outputs/docx/cli.docx").is_file())

    def test_cli_check_writes_nothing(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = export_cli.cmd_export_docx(
                    self._args(check=True), root=root)
            self.assertEqual(code, 0)
            self.assertIn("nothing was written", buffer.getvalue())
            self.assertFalse((root / "outputs/docx/cli.docx").exists())

    def test_cli_refuses_existing_output(self):
        tmp, root, ids = make_ready_project()
        with tmp:
            out = root / "outputs/docx/cli.docx"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"existing")
            with redirect_stdout(io.StringIO()):
                code = export_cli.cmd_export_docx(self._args(), root=root)
            self.assertEqual(code, 2)

    def test_cli_reports_missing_approval(self):
        tmp, root, ids = make_project()
        with tmp:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = export_cli.cmd_export_docx(self._args(), root=root)
            self.assertEqual(code, 2)
            self.assertIn("[ERROR]", buffer.getvalue())

    def test_parser_accepts_export_and_rejects_bad_section(self):
        args = build_parser().parse_args(["export", "docx"])
        self.assertEqual(args.output, exporter.DEFAULT_OUTPUT)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["export", "docx", "--sections", "results"])

    def test_export_is_registered_in_help(self):
        self.assertIn("export", build_parser().format_help())


if __name__ == "__main__":
    unittest.main()
