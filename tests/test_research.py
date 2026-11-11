"""Tests for `researchforge research` list/show/validate (RF-009)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.cli import research as research_mod

ROOT = Path(__file__).resolve().parent.parent

MARKER = "[DECISION REQUIRED]"


def minimal_doc(doc_id: str) -> str:
    headings = "\n".join(research_mod.REQUIRED_HEADINGS[doc_id])
    return f"# {doc_id}\n{headings}\n\ncontent {MARKER}\n"


def make_project(**overrides):
    """Temp project with all nine valid docs; overrides replace content
    keyed by relative path (value None removes the file)."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "app" / "cli").mkdir(parents=True)
    (root / "research").mkdir()
    contents = {rel: minimal_doc(rel[len("research/"):-len(".md")])
                for rel in research_mod.DOCUMENTS.values()}
    contents.update(overrides)
    for name, content in contents.items():
        if content is None:
            continue
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp, root


class ResearchListTests(unittest.TestCase):
    def test_lists_all_nine_documents(self):
        tmp, root = make_project()
        with tmp:
            rows = research_mod.list_documents(root)
            self.assertEqual(len(rows), 9)
            self.assertTrue(all(r["exists"] for r in rows))
            self.assertTrue(all(r["size"] and r["size"] > 0 for r in rows))

    def test_missing_document_is_reported_not_omitted(self):
        tmp, root = make_project(**{"research/limitations.md": None})
        with tmp:
            rows = research_mod.list_documents(root)
            self.assertEqual(len(rows), 9)
            missing = [r for r in rows if not r["exists"]]
            self.assertEqual([r["id"] for r in missing], ["limitations"])
            self.assertIsNone(missing[0]["size"])


class ResearchShowTests(unittest.TestCase):
    def test_reads_valid_document_by_id(self):
        tmp, root = make_project()
        with tmp:
            rel, text = research_mod.read_document(root, "methodology")
            self.assertEqual(rel, "research/methodology.md")
            self.assertIn("## Design type", text)

    def test_reads_by_approved_relative_path(self):
        tmp, root = make_project()
        with tmp:
            rel, text = research_mod.read_document(root, "research\\objectives.md")
            self.assertEqual(rel, "research/objectives.md")

    def test_unknown_document_rejected(self):
        tmp, root = make_project()
        with tmp:
            with self.assertRaises(ValueError):
                research_mod.read_document(root, "not_a_doc")

    def test_path_traversal_rejected(self):
        tmp, root = make_project()
        with tmp:
            for ref in ("../PROJECT.md", "research/../AGENTS.md",
                        "..\\..\\secrets.txt"):
                with self.assertRaises(ValueError, msg=ref):
                    research_mod.read_document(root, ref)

    def test_absolute_path_outside_workspace_rejected(self):
        tmp, root = make_project()
        with tmp:
            outside = str(Path(tmp.name).parent / "elsewhere.md")
            with self.assertRaises(ValueError):
                research_mod.read_document(root, outside)

    def test_missing_file_raises_file_not_found(self):
        tmp, root = make_project(**{"research/limitations.md": None})
        with tmp:
            with self.assertRaises(FileNotFoundError):
                research_mod.read_document(root, "limitations")

    def test_utf8_content_preserved(self):
        body = "# charter\n## Working title\n\nunicode: héllo — naïve ✅\n" + MARKER
        tmp, root = make_project(**{"research/research_charter.md": body})
        with tmp:
            _, text = research_mod.read_document(root, "research_charter")
            self.assertIn("héllo — naïve ✅", text)

    def test_show_is_read_only(self):
        tmp, root = make_project()
        with tmp:
            before = {p: p.read_bytes()
                      for p in root.rglob("*.md")}
            research_mod.read_document(root, "research_charter")
            research_mod.validate_documents(root)
            research_mod.list_documents(root)
            after = {p: p.read_bytes() for p in root.rglob("*.md")}
            self.assertEqual(before, after)


class ResearchValidateTests(unittest.TestCase):
    def test_valid_project_passes_clean(self):
        tmp, root = make_project()
        with tmp:
            results = research_mod.validate_documents(root)
            self.assertTrue(all(r["level"] == "PASS" for r in results["rows"]))
            self.assertEqual(results["unexpected"], [])

    def test_missing_file_is_error(self):
        tmp, root = make_project(**{"research/limitations.md": None})
        with tmp:
            results = research_mod.validate_documents(root)
            row = next(r for r in results["rows"] if r["id"] == "limitations")
            self.assertEqual(row["level"], "ERROR")

    def test_empty_document_is_warning(self):
        tmp, root = make_project(**{"research/limitations.md": ""})
        with tmp:
            results = research_mod.validate_documents(root)
            row = next(r for r in results["rows"] if r["id"] == "limitations")
            self.assertEqual(row["level"], "WARNING")
            self.assertIn("empty", " ".join(row["messages"]))

    def test_missing_required_section_is_warning(self):
        body = "# baseline\n## Glossary\n\n" + MARKER  # lacks other headings
        tmp, root = make_project(**{"research/research_baseline.md": body})
        with tmp:
            results = research_mod.validate_documents(root)
            row = next(r for r in results["rows"] if r["id"] == "research_baseline")
            self.assertEqual(row["level"], "WARNING")
            self.assertTrue(any("missing required sections" in m
                                for m in row["messages"]))

    def test_document_without_markers_is_warning(self):
        body = ("# charter\n"
                + "\n".join(research_mod.REQUIRED_HEADINGS["research_charter"])
                + "\n\nplain content\n")
        tmp, root = make_project(**{"research/research_charter.md": body})
        with tmp:
            results = research_mod.validate_documents(root)
            row = next(r for r in results["rows"] if r["id"] == "research_charter")
            self.assertTrue(any("markers" in m for m in row["messages"]))

    def test_unexpected_research_file_detected(self):
        tmp, root = make_project(**{"research/stray_notes.md": "extra"})
        with tmp:
            results = research_mod.validate_documents(root)
            self.assertEqual(results["unexpected"], ["research/stray_notes.md"])


class ResearchCliEndToEnd(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "app.main", "research", *args],
                              capture_output=True, text=True, cwd=str(ROOT))

    def test_real_workspace_commands(self):
        proc = self.run_cli("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("9 of 9 documents present", proc.stdout)
        proc = self.run_cli("show", "research_charter")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[research/research_charter.md]", proc.stdout)
        self.assertIn("## Working title", proc.stdout)
        proc = self.run_cli("show", "../PROJECT.md")
        self.assertEqual(proc.returncode, 2)
        proc = self.run_cli("validate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Validation result:", proc.stdout)

    def test_main_help_still_lists_commands(self):
        proc = subprocess.run([sys.executable, "-m", "app.main", "--help"],
                              capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("research", proc.stdout)
        self.assertIn("status", proc.stdout)


if __name__ == "__main__":
    unittest.main()
