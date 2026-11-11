"""Tests for the central approval ledger and versioning (RF-020). Offline
only: real RF-010/015/016/017/018 pipeline, no providers needed except a
fake one to create a draft."""
import json
import tempfile
import unittest
from pathlib import Path

from app.core import approvals
from app.core.sources import import_source
from app.providers.contracts import ProviderResponse, Usage
from app.workflows import literature_synthesis as ls
from app.workflows import research_gap_analysis as ga
from app.workflows import section_drafting as sd
from app.workflows.paper_extraction import (ExtractionError,
                                            approve_extraction, extract_source,
                                            reject_extraction)

SECRET = "sk-personal-profile1-key"
TEXT_A = ("Claim: Density supports walkability.\n\n"
          "Finding: Density predicts walking trips.\n")
TEXT_B = ("Finding: Density predicts walking trips.\n")


class FakeProvider:
    provider_id = "deepseek"

    def generate(self, request):
        return ProviderResponse(
            content=json.dumps({"prose": "Draft prose [SRC-xxxx]."}),
            provider="deepseek", model="deepseek-flash",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2))


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


def make_section_draft(root, ids):
    return sd.draft_section(
        root, "discussion", FakeProvider())


class LedgerRecordingTests(unittest.TestCase):
    def test_ledger_records_each_artifact_kind(self):
        tmp, root, ids = make_project()
        with tmp:
            ga.analyze_gaps(root)
            ga.approve_gap_analysis(root, "G. Reviewer")
            make_section_draft(root, ids)
            sd.approve_section(root, "discussion", "S. Reviewer")
            entries = approvals.load_ledger(root)
            kinds = {e["artifact_kind"] for e in entries}
            self.assertEqual(kinds, {"extraction", "synthesis",
                                     "gap_analysis", "section"})
            for entry in entries:
                self.assertEqual(entry["decision"], "APPROVED")
                self.assertTrue(entry["reviewer"])
                self.assertIn("T", entry["timestamp"])
                self.assertEqual(len(entry["sha256"]), 64)

    def test_ledger_is_append_only_jsonl(self):
        tmp, root, ids = make_project()
        with tmp:
            # two extraction approvals + one synthesis approval were made
            # by the fixture
            before = approvals.ledger_path(root).read_text(
                encoding="utf-8").splitlines()
            self.assertEqual(len(before), 3)
            make_section_draft(root, ids)
            sd.approve_section(root, "discussion", "S. Reviewer")
            after = approvals.ledger_path(root).read_text(
                encoding="utf-8").splitlines()
            self.assertEqual(len(after), 4)
            # existing lines untouched
            self.assertEqual(after[:3], before)
            for line in after:
                json.loads(line)  # every line is valid JSON

    def test_rejection_is_recorded_too(self):
        tmp, root, ids = make_project()
        with tmp:
            reject_extraction(root, ids[0], "R. Reviewer")
            entries = approvals.load_ledger(root)
            rejected = [e for e in entries if e["decision"] == "REJECTED"]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["artifact_id"], ids[0])

    def test_no_ledger_entry_for_refused_approval(self):
        tmp, root, ids = make_project()
        with tmp:
            with self.assertRaises(sd.DraftingError):
                sd.approve_section(root, "conclusion", "R")
            entries = approvals.load_ledger(root)
            self.assertFalse(any(e["artifact_kind"] == "section"
                                 for e in entries))

    def test_secret_never_in_ledger(self):
        tmp, root, ids = make_project()
        with tmp:
            raw = approvals.ledger_path(root).read_text(encoding="utf-8")
            self.assertNotIn(SECRET, raw)


class FingerprintAndTamperTests(unittest.TestCase):
    def test_intact_artifacts_verify_ok(self):
        tmp, root, ids = make_project()
        with tmp:
            ga.analyze_gaps(root)
            ga.approve_gap_analysis(root, "R")
            make_section_draft(root, ids)
            sd.approve_section(root, "discussion", "R")
            rows = approvals.verify_ledger(root)
            # 2 extractions + synthesis + gap analysis + section
            self.assertEqual(len(rows), 5)
            self.assertTrue(all(row["status"] == "OK" for row in rows))

    def test_tampered_synthesis_detected(self):
        tmp, root, ids = make_project()
        with tmp:
            path = ls.draft_path(root)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["evidence"]["findings"][0]["text"] = "silently altered"
            path.write_text(json.dumps(data), encoding="utf-8")
            rows = approvals.verify_ledger(root)
            synthesis = [r for r in rows
                         if r["entry"]["artifact_kind"] == "synthesis"]
            self.assertEqual(synthesis[0]["status"], "TAMPERED")

    def test_tampered_extraction_detected(self):
        tmp, root, ids = make_project()
        with tmp:
            path = root / "sources" / "extracted_notes" / \
                f"{ids[0]}.extraction.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["items"] = []
            path.write_text(json.dumps(data), encoding="utf-8")
            rows = approvals.verify_ledger(root)
            tampered = [r for r in rows if r["status"] == "TAMPERED"]
            self.assertEqual(len(tampered), 1)
            self.assertEqual(tampered[0]["entry"]["artifact_id"], ids[0])

    def test_tampered_approved_section_version_detected(self):
        tmp, root, ids = make_project()
        with tmp:
            make_section_draft(root, ids)
            sd.approve_section(root, "discussion", "R")
            path = sd.section_path(root, "discussion")
            data = json.loads(path.read_text(encoding="utf-8"))
            data["versions"][0]["prose"] = "rewritten after approval"
            path.write_text(json.dumps(data), encoding="utf-8")
            rows = approvals.verify_ledger(root)
            section = [r for r in rows
                       if r["entry"]["artifact_kind"] == "section"]
            self.assertEqual(section[0]["status"], "TAMPERED")

    def test_later_version_does_not_invalidate_earlier_approval(self):
        tmp, root, ids = make_project()
        with tmp:
            make_section_draft(root, ids)
            sd.approve_section(root, "discussion", "R")
            sd.draft_section(root, "discussion", FakeProvider())  # v2
            rows = approvals.verify_ledger(root)
            section = [r for r in rows
                       if r["entry"]["artifact_kind"] == "section"]
            self.assertEqual(section[0]["status"], "OK")

    def test_missing_artifact_reported(self):
        tmp, root, ids = make_project()
        with tmp:
            ls.draft_path(root).unlink()
            rows = approvals.verify_ledger(root)
            synthesis = [r for r in rows
                         if r["entry"]["artifact_kind"] == "synthesis"]
            self.assertEqual(synthesis[0]["status"], "MISSING")


class PromotionGuardTests(unittest.TestCase):
    def test_rejected_extraction_cannot_be_promoted(self):
        tmp, root, ids = make_project()
        with tmp:
            reject_extraction(root, ids[0], "R")
            with self.assertRaises(ExtractionError) as cm:
                approve_extraction(root, ids[0], "R")
            self.assertIn("cannot be promoted", str(cm.exception))

    def test_failed_section_version_cannot_be_approved(self):
        tmp, root, ids = make_project()
        with tmp:
            from app.providers.errors import NetworkError
            from app.workflows.section_drafting import draft_section

            class Failing:
                provider_id = "deepseek"

                def generate(self, request):
                    raise NetworkError("outage")

            with self.assertRaises(NetworkError):
                draft_section(root, "discussion", Failing())
            with self.assertRaises(sd.DraftingError):
                sd.approve_section(root, "discussion", "R")
            self.assertFalse(any(
                e["artifact_kind"] == "section"
                for e in approvals.load_ledger(root)))

    def test_approval_requires_reviewer_identity(self):
        tmp, root, ids = make_project()
        with tmp:
            make_section_draft(root, ids)
            with self.assertRaises(sd.DraftingError):
                sd.approve_section(root, "discussion", "   ")

    def test_approved_section_is_immutable(self):
        tmp, root, ids = make_project()
        with tmp:
            make_section_draft(root, ids)
            sd.approve_section(root, "discussion", "R")
            with self.assertRaises(sd.DraftingError):
                sd.approve_section(root, "discussion", "R")


class VersionHistoryTests(unittest.TestCase):
    def test_version_history_is_append_only(self):
        tmp, root, ids = make_project()
        with tmp:
            make_section_draft(root, ids)
            sd.approve_section(root, "discussion", "R")
            first = sd.load_section(root, "discussion")["versions"][0]
            sd.draft_section(root, "discussion", FakeProvider())
            versions = sd.load_section(root, "discussion")["versions"]
            self.assertEqual([v["version"] for v in versions], [1, 2])
            self.assertEqual(versions[0]["prose"], first["prose"])
            self.assertEqual(versions[0]["review"], "APPROVED")
            self.assertEqual(versions[1]["review"], "PENDING HUMAN REVIEW")

    def test_approval_does_not_change_content_origin(self):
        tmp, root, ids = make_project()
        with tmp:
            make_section_draft(root, ids)
            approved = sd.approve_section(root, "discussion", "R")
            self.assertEqual(approved["origin"], "AI-GENERATED")
            self.assertIsNone(approved["researcher_context"])
            self.assertTrue(approved["evidence_refs"])
            self.assertEqual(approved["review"], "APPROVED")


class CliApprovalTests(unittest.TestCase):
    def _args(self, **kwargs):
        class Args:
            pass
        args = Args()
        for key, value in kwargs.items():
            setattr(args, key, value)
        return args

    def test_approve_section_command(self):
        from app.cli import approvals as cli
        tmp, root, ids = make_project()
        with tmp:
            make_section_draft(root, ids)
            code = cli.cmd_approve(self._args(
                artifact="section", target="discussion", reviewer="N. R.",
                version=None), root=root)
            self.assertEqual(code, 0)
            self.assertEqual(sd.load_section(root, "discussion")
                             ["versions"][0]["review"], "APPROVED")
            self.assertTrue(approvals.load_ledger(root))

    def test_approve_synthesis_command(self):
        from app.cli import approvals as cli
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "in").mkdir()
        (root / "sources" / "papers").mkdir(parents=True)
        (root / "sources" / "extracted_notes").mkdir(parents=True)
        src = root / "in" / "p.txt"
        src.write_text(TEXT_A, encoding="utf-8")
        sid = import_source(root, src)["source_id"]
        extract_source(root, sid)
        approve_extraction(root, sid, "R")
        ls.synthesize(root)
        with tmp:
            code = cli.cmd_approve(self._args(
                artifact="synthesis", target=None, reviewer="N. R.",
                version=None), root=root)
            self.assertEqual(code, 0)
            self.assertEqual(ls.load_draft(root)["review"], "APPROVED")

    def test_approve_failure_returns_error_code(self):
        from app.cli import approvals as cli
        tmp, root, ids = make_project()
        with tmp:
            code = cli.cmd_approve(self._args(
                artifact="section", target="conclusion", reviewer="R",
                version=None), root=root)
            self.assertNotEqual(code, 0)

    def test_list_and_verify_commands(self):
        from app.cli import approvals as cli
        tmp, root, ids = make_project()
        with tmp:
            make_section_draft(root, ids)
            sd.approve_section(root, "discussion", "R")
            self.assertEqual(cli.cmd_list(self._args(), root=root), 0)
            self.assertEqual(cli.cmd_verify(self._args(), root=root), 0)
            # tampering makes verify fail with a non-zero exit
            path = ls.draft_path(root)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["evidence"] = {}
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(cli.cmd_verify(self._args(), root=root), 1)

    def test_empty_ledger_commands_succeed(self):
        from app.cli import approvals as cli
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        with tmp:
            self.assertEqual(cli.cmd_list(self._args(), root=root), 0)
            self.assertEqual(cli.cmd_verify(self._args(), root=root), 0)


class ExistingApprovalRegressionTests(unittest.TestCase):
    def test_all_existing_approval_paths_still_work(self):
        tmp, root, ids = make_project()
        with tmp:
            self.assertTrue(Path(root / "sources" / "extracted_notes" /
                                 f"{ids[0]}.extraction.json").is_file())
            self.assertEqual(ls.load_draft(root)["review"], "APPROVED")
            ga.analyze_gaps(root)
            self.assertEqual(ga.approve_gap_analysis(
                root, "R")["review"], "APPROVED")
            make_section_draft(root, ids)
            self.assertEqual(sd.approve_section(
                root, "discussion", "R")["review"], "APPROVED")

    def test_ledger_unwritable_does_not_block_approval(self):
        tmp, root, ids = make_project()
        with tmp:
            ledger = approvals.ledger_path(root)
            ledger.unlink()
            ledger.mkdir()  # a directory where the ledger file should be
            make_section_draft(root, ids)
            approved = sd.approve_section(root, "discussion", "R")
            self.assertEqual(approved["review"], "APPROVED")


if __name__ == "__main__":
    unittest.main()
