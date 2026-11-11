"""Tests for `researchforge status` (RF-008)."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.cli import status as status_mod

ROOT = Path(__file__).resolve().parent.parent

SAMPLE_TASKS = """\
# TASKS.md
### RF-001: Establish project documentation
- **Status:** COMPLETE
### RF-002: Create directory structure
- **Status:** COMPLETE *(2026-11-10)*
### RF-003: Configure environment
- **Status:** PENDING
"""
SAMPLE_PROJECT = """\
# PROJECT.md
## Working title
**Smart-Glasses-Based Pedestrian Walkability Assessment and Hazard-Aware Navigation for Urban Pedestrian Networks**
"""
SAMPLE_PROGRESS = """\
# progress.md
## Current phase
Phase 1 - CLI foundation.
"""

SAMPLE_MIXED_TASKS = """\
# TASKS.md
### RF-023: Technical diagram generation
- **Status:** PENDING
### RF-024: Final project validation
- **Status:** PENDING

---

## MVP Track

### MVP-01: CLI integration
- **Status:** COMPLETE *(2026-11-10)*
### MVP-02: Real document ingestion
- **Status:** COMPLETE *(2026-11-10)*
"""


def make_project(**files):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "app" / "cli").mkdir(parents=True)
    defaults = {"TASKS.md": SAMPLE_TASKS, "PROJECT.md": SAMPLE_PROJECT,
                "progress.md": SAMPLE_PROGRESS, "AGENTS.md": "rules"}
    for name, content in {**defaults, **files}.items():
        (root / name).write_text(content, encoding="utf-8")
    return tmp, root


class MixedTaskRecordTests(unittest.TestCase):
    """RF and MVP task records must be parsed independently: an MVP status
    line can never overwrite an RF status or corrupt derived counts."""

    def parse(self, text):
        return status_mod.parse_tasks(text)

    def test_rf_and_mvp_statuses_are_independent(self):
        tasks = self.parse(SAMPLE_MIXED_TASKS)
        by_id = {t["id"]: t["status"] for t in tasks}
        self.assertEqual(by_id["RF-023"], "PENDING")
        self.assertEqual(by_id["RF-024"], "PENDING")
        self.assertEqual(by_id["MVP-01"], "COMPLETE")
        self.assertEqual(by_id["MVP-02"], "COMPLETE")

    def test_counts_include_both_record_families(self):
        tasks = self.parse(SAMPLE_MIXED_TASKS)
        completed = sum(1 for t in tasks if t["status"] == "COMPLETE")
        self.assertEqual(completed, 2)
        self.assertEqual(len(tasks) - completed, 2)

    def test_next_task_is_first_non_complete_in_order(self):
        tasks = self.parse(SAMPLE_MIXED_TASKS)
        nxt = next(t for t in tasks if t["status"] != "COMPLETE")
        self.assertEqual(nxt["id"], "RF-023")
        self.assertEqual(nxt["title"], "Technical diagram generation")

    def test_next_task_can_be_an_mvp_record(self):
        text = ("### RF-001: A\n- **Status:** COMPLETE\n"
                "### MVP-01: B\n- **Status:** PENDING\n")
        nxt = next(t for t in self.parse(text) if t["status"] != "COMPLETE")
        self.assertEqual(nxt["id"], "MVP-01")

    def test_all_complete_has_no_next_task(self):
        text = ("### RF-001: A\n- **Status:** COMPLETE\n"
                "### MVP-01: B\n- **Status:** COMPLETE\n")
        tasks = self.parse(text)
        self.assertFalse([t for t in tasks if t["status"] != "COMPLETE"])
        # render() handles the empty next-task case explicitly
        tmp, root = make_project(**{"TASKS.md": text})
        with tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                facts = status_mod.collect_status(root)
            self.assertIsNone(facts["next"])
            self.assertIn("<none>", status_mod.render(facts))

    def test_status_after_a_non_task_heading_cannot_overwrite_a_task(self):
        text = ("### RF-001: Docs\n- **Status:** COMPLETE\n"
                "## Notes\n- **Status:** PENDING\n")
        tasks = self.parse(text)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "COMPLETE")

    def test_unrecognised_heading_resets_the_current_record(self):
        # A level-4 MVP heading is not a task record; its status must not
        # attach to the preceding RF task.
        text = ("### RF-024: Final validation\n- **Status:** PENDING\n"
                "#### MVP-03: future subheading\n- **Status:** COMPLETE\n")
        tasks = self.parse(text)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "PENDING")

    def test_render_reports_mixed_counts_and_next_task(self):
        tmp, root = make_project(**{"TASKS.md": SAMPLE_MIXED_TASKS})
        with tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                facts = status_mod.collect_status(root)
            out = status_mod.render(facts)
            self.assertIn("Completed: 2", out)
            self.assertIn("Pending: 2", out)
            self.assertIn("RF-023 - Technical diagram generation", out)

    def test_mvp_status_does_not_change_an_rf_status(self):
        tasks = self.parse(SAMPLE_MIXED_TASKS)
        rf = [t for t in tasks if t["id"].startswith("RF-")]
        mvp = [t for t in tasks if t["id"].startswith("MVP-")]
        self.assertTrue(all(t["status"] == "PENDING" for t in rf))
        self.assertTrue(all(t["status"] == "COMPLETE" for t in mvp))


class StatusTests(unittest.TestCase):
    def collect(self, root, env_overrides=None):
        old = {k: os.environ.get(k) for k in ("DEEPSEEK_API_KEY",)}
        try:
            for k, v in (env_overrides or {}).items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            with contextlib.redirect_stdout(io.StringIO()):
                return status_mod.collect_status(root)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def render(self, root, env_overrides=None):
        facts = self.collect(root, env_overrides)
        out = status_mod.render(facts)
        return facts, out

    def test_task_counts_and_next_task(self):
        tmp, root = make_project()
        with tmp:
            facts = self.collect(root)
            self.assertEqual(sum(1 for t in facts["tasks"] if t["status"] == "COMPLETE"), 2)
            self.assertEqual(len(facts["tasks"]) - 2, 1)
            self.assertEqual(facts["next"]["id"], "RF-003")
            self.assertEqual(facts["next"]["title"], "Configure environment")

    def test_status_output_contains_key_facts(self):
        tmp, root = make_project()
        with tmp:
            _, out = self.render(root)
            self.assertIn("ResearchForge v", out)
            self.assertIn("Completed: 2", out)
            self.assertIn("Pending: 1", out)
            self.assertIn("RF-003 - Configure environment", out)
            self.assertIn("Smart-Glasses-Based", out)

    def test_missing_tasks_md_is_error(self):
        tmp, root = make_project()
        (root / "TASKS.md").unlink()
        with tmp:
            facts = self.collect(root)
            self.assertEqual(facts["tasks"], [])
            self.assertIn("TASKS.md is missing", facts["errors"])
            self.assertTrue(facts["errors"])

    def test_malformed_tasks_md_is_handled(self):
        tmp, root = make_project()
        (root / "TASKS.md").write_text("garbage without task records", encoding="utf-8")
        with tmp:
            facts = self.collect(root)
            self.assertEqual(facts["tasks"], [])
            self.assertIn("malformed", " ".join(facts["errors"]).lower())

    def test_missing_required_dirs_is_warning_not_error(self):
        tmp, root = make_project()
        with tmp:
            facts = self.collect(root)
            self.assertTrue(facts["missing_dirs"])
            self.assertFalse(facts["errors"])  # dirs are WARNING-level

    def test_missing_env_and_dotenv_reports_not_configured(self):
        tmp, root = make_project()
        with tmp:
            _, out = self.render(root, {"DEEPSEEK_API_KEY": None})
            self.assertIn("Not configured", out)

    def test_api_key_presence_without_disclosure(self):
        # RF-012 isolation: only the project-local .env counts; a global
        # env key must NOT make credentials appear configured.
        secret = "sk-personal-secret-value-123"
        tmp, root = make_project(**{".env": f"DEEPSEEK_API_KEY={secret}\n"})
        old = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            facts = self.collect(root)
            self.assertTrue(facts["deepseek"])
            out = status_mod.render(facts)
            self.assertIn("Configured", out)
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, repr(facts))
        finally:
            if old is not None:
                os.environ["DEEPSEEK_API_KEY"] = old

    def test_global_env_key_does_not_make_status_configured(self):
        tmp, root = make_project()
        old = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = "sk-company-global-key"
        try:
            facts = self.collect(root)
            self.assertFalse(facts["deepseek"])
        finally:
            if old is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = old

    def test_dotenv_file_counts_but_placeholder_does_not(self):
        tmp, root = make_project(**{".env": "DEEPSEEK_API_KEY=sk-real-from-dotenv\n"})
        with tmp:
            facts = self.collect(root, {"DEEPSEEK_API_KEY": None})
            self.assertTrue(facts["deepseek"])
        tmp, root = make_project(**{".env": "DEEPSEEK_API_KEY=your-deepseek-api-key-here\n"})
        with tmp:
            facts = self.collect(root, {"DEEPSEEK_API_KEY": None})
            self.assertFalse(facts["deepseek"])

    def test_real_workspace_end_to_end(self):
        # Expected values derived independently from TASKS.md so this
        # integration test survives future status updates. Statuses are
        # paired block-by-block with their own heading, so RF and MVP
        # records cannot corrupt each other's counts.
        import re as _re
        tasks_text = (ROOT / "TASKS.md").read_text(encoding="utf-8")
        blocks = _re.split(r"^### ((?:RF|MVP)-\d+):\s*(.+)$", tasks_text,
                           flags=_re.M)
        records = []
        for index in range(1, len(blocks), 3):
            body = blocks[index + 2]
            status = _re.search(r"^-\s*\*\*Status:\*\*\s*(\w+)", body,
                                _re.M)
            records.append((blocks[index], blocks[index + 1],
                            status.group(1) if status else "UNKNOWN"))
        completed = sum(1 for _, _, s in records if s == "COMPLETE")
        next_id, next_title, _ = next(
            (i, t, s) for i, t, s in records if s != "COMPLETE")
        proc = subprocess.run(
            [sys.executable, "-m", "app.main", "status"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ResearchForge v", proc.stdout)
        self.assertIn(f"Completed: {completed}", proc.stdout)
        self.assertIn(f"Pending: {len(records) - completed}", proc.stdout)
        self.assertIn(f"{next_id} - {next_title}", proc.stdout)


if __name__ == "__main__":
    unittest.main()
