"""Tests for the ResearchForge CLI entry point (RF-007)."""
import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path

from app.main import __version__, main

ROOT = Path(__file__).resolve().parent.parent


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as exc:  # argparse help/version/error exits
                code = exc.code
        self.assertIsInstance(code, int)
        return code, out.getvalue(), err.getvalue()

    def test_cli_starts_and_showshelp_with_no_command(self):
        code, out, _ = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)
        self.assertIn("ResearchForge", out)

    def test_help_output(self):
        code, out, _ = self.run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)
        self.assertIn("--version", out)

    def test_version_output(self):
        code, out, err = self.run_cli(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(f"researchforge {__version__}", out)

    def test_invalid_argument_exits_nonzero(self):
        code, _, err = self.run_cli(["--definitely-not-a-flag"])
        self.assertNotEqual(code, 0)
        self.assertIn("unrecognized arguments", err)

    def test_importing_entry_point_does_not_execute_cli(self):
        proc = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_version_is_a_string_constant(self):
        self.assertIsInstance(__version__, str)
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
