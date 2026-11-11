"""`researchforge status` — read-only project diagnostic (RF-008).

`collect_status()` gathers facts from project files (no state is
modified); `render()` formats them. Missing/malformed files degrade to
WARNING/ERROR markers instead of failing the whole command. API keys are
never included in the report — only their configured/not-configured state.
"""
import platform
import re
import sys
from pathlib import Path

from app import __version__
from app.core import provider_config

CORE_DOCS = ("AGENTS.md", "PROJECT.md", "TASKS.md", "progress.md")
KB_FILES = (
    "research/research_charter.md", "research/research_baseline.md",
    "research/research_questions.md", "research/objectives.md",
    "research/methodology.md", "research/architecture_specification.md",
    "research/decisions_register.md", "research/limitations.md",
    "research/unresolved_questions.md",
)
SOURCE_FILES = (
    "sources/literature_matrix.csv", "sources/evidence_register.csv",
    "sources/references.bib",
)
REQUIRED_DIRS = (
    "config", "research", "sources/papers", "sources/extracted_notes",
    "prompts", "manuscript/sections", "manuscript/figures", "manuscript/tables",
    "diagrams/source", "diagrams/svg", "diagrams/png",
    "outputs/drafts", "outputs/review_reports", "outputs/docx",
    "outputs/pdf", "outputs/final",
    "app/cli", "app/core", "app/providers", "app/workflows",
    "app/exporters", "app/validators", "tests", "logs",
)

_TASK_RE = re.compile(r"^### ((?:RF|MVP)-\d+):\s*(.+)$")
_HEADING_RE = re.compile(r"^#{1,6}\s")
_STATUS_RE = re.compile(r"^-\s*\*\*Status:\*\*\s*(.+)$")


def _read(path: Path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def parse_tasks(text):
    """Conservative TASKS.md parser: returns ordered [{id,title,status}];
    status is UNKNOWN when absent/malformed.

    RF-xxx and MVP-xx records are independent: every task heading starts a
    new record, and any OTHER heading ends the current record. A status
    line can therefore never attach to a task from another section (an
    MVP status update cannot overwrite an RF status)."""
    tasks, current = [], None
    for line in text.splitlines():
        m = _TASK_RE.match(line)
        if m:
            current = {"id": m.group(1), "title": m.group(2).strip(),
                       "status": "UNKNOWN"}
            tasks.append(current)
            continue
        if _HEADING_RE.match(line):
            current = None  # non-task heading: no more status lines for it
            continue
        m = _STATUS_RE.match(line)
        if m and current is not None:
            raw = m.group(1).strip()
            for known in ("IN PROGRESS", "COMPLETE", "PENDING"):
                if raw.startswith(known):
                    current["status"] = known
                    break
    return tasks


def _section_first_line(text, heading):
    """First non-empty line of a '## <heading>' section, or None."""
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.strip().lower().startswith("## " + heading.lower())
            continue
        if in_section and line.strip():
            return line.strip()
    return None


def deepseek_configured(root: Path) -> bool:
    """True when the project-local .env holds a real (non-placeholder)
    DeepSeek key. Delegates to the isolated loader: global environment
    variables are deliberately ignored (RF-012 credential isolation)."""
    return provider_config.credentials_configured("deepseek", root)


def collect_status(root: Path) -> dict:
    """Gather every status fact. Read-only; never writes/repairs."""
    facts = {
        "app": "ResearchForge",
        "version": __version__,
        "workspace": str(root),
        "python": platform.python_version(),
        "in_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
        "errors": [],
        "warnings": [],
    }

    tasks_text = _read(root / "TASKS.md")
    if tasks_text is None:
        facts["tasks"], facts["next"], facts["tasks_error"] = [], None, "TASKS.md missing"
        facts["errors"].append("TASKS.md is missing")
    else:
        facts["tasks"] = parse_tasks(tasks_text)
        if not facts["tasks"]:
            facts["next"], facts["tasks_error"] = None, "no valid task records found"
            facts["errors"].append("TASKS.md malformed: no task records parsed")
        else:
            facts["tasks_error"] = None
            pending = [t for t in facts["tasks"] if t["status"] != "COMPLETE"]
            facts["next"] = pending[0] if pending else None

    project_text = _read(root / "PROJECT.md")
    title = None
    if project_text is None:
        facts["errors"].append("PROJECT.md is missing")
    else:
        title = _section_first_line(project_text, "Working title")
        if title:
            title = title.strip("*").strip()
        else:
            facts["errors"].append("PROJECT.md has no Working title section")
    facts["project_title"] = title

    progress_text = _read(root / "progress.md")
    phase = None
    if progress_text is None:
        facts["errors"].append("progress.md is missing")
    else:
        phase = _section_first_line(progress_text, "Current phase")
        if not phase:
            facts["errors"].append("progress.md has no Current phase section")
    facts["phase"] = phase

    missing = [p for p in CORE_DOCS if not (root / p).is_file()]
    if missing:
        facts["errors"].append("missing core documents: " + ", ".join(missing))
    facts["missing_kb"] = [p for p in KB_FILES if not (root / p).is_file()]
    if facts["missing_kb"]:
        facts["warnings"].append(
            "missing knowledge-base files: " + ", ".join(facts["missing_kb"]))
    facts["missing_sources"] = [p for p in SOURCE_FILES if not (root / p).is_file()]
    if facts["missing_sources"]:
        facts["warnings"].append(
            "missing literature templates: " + ", ".join(facts["missing_sources"]))
    facts["missing_dirs"] = [d for d in REQUIRED_DIRS if not (root / d).is_dir()]
    if facts["missing_dirs"]:
        facts["warnings"].append(
            f"missing required directories ({len(facts['missing_dirs'])}): "
            + ", ".join(facts["missing_dirs"]))
    facts["deepseek"] = deepseek_configured(root)
    return facts


def render(facts: dict) -> str:
    completed = sum(1 for t in facts["tasks"] if t["status"] == "COMPLETE")
    pending = len(facts["tasks"]) - completed
    lines = [
        f"{facts['app']} v{facts['version']}",
        f"Workspace: {facts['workspace']}",
        "",
    ]
    lines.append(f"Project: {facts['project_title'] or '<unavailable>'}")
    lines.append(f"Phase: {facts['phase'] or '<unavailable>'}")
    lines.extend([
        "",
        "Tasks:",
        f"  Completed: {completed}",
        f"  Pending: {pending}",
        "",
        "Next task:",
    ])
    nxt = facts["next"]
    if nxt:
        lines.append(f"  {nxt['id']} - {nxt['title']}")
    else:
        lines.append(f"  <none> ({facts.get('tasks_error') or 'all tasks complete'})")
    venv = "Active" if facts["in_venv"] else "Not active [WARNING]"
    lines.extend([
        "",
        "Environment:",
        f"  Python: {facts['python']}",
        f"  Virtual environment: {venv}",
        "",
        "Project integrity:",
        f"  Core documents: {'PASS' if not facts['errors'] else 'FAIL [ERROR]'}",
        f"  Research knowledge base (9 files): "
        f"{'PASS' if not facts['missing_kb'] else 'FAIL [WARNING]'}",
        f"  Literature templates (3 files): "
        f"{'PASS' if not facts['missing_sources'] else 'FAIL [WARNING]'}",
        f"  Required directories ({len(REQUIRED_DIRS)}): "
        f"{'PASS' if not facts['missing_dirs'] else 'FAIL [WARNING]'}",
        "  DeepSeek API credentials: "
        + ("Configured" if facts["deepseek"] else "Not configured [WARNING]"),
    ])
    return "\n".join(lines)


def cmd_status(args) -> int:
    root = Path(__file__).resolve().parents[2]
    facts = collect_status(root)
    print(render(facts))
    return 1 if facts["errors"] else 0


def setup(subparsers):
    parser = subparsers.add_parser(
        "status", help="show project status (read-only diagnostic)")
    parser.set_defaults(handler=cmd_status)
