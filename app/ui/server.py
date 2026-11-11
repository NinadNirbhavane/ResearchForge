"""Local browser workspace server (UI-01).

A thin, standard-library HTTP adapter over the existing engine. No
research logic lives here: reads call the existing loaders, and the one
write path calls `paper_extraction.decide_item` (which now serializes
decisions across threads and processes through `app.core.locking`).

Security model (documented in the UI-01 report):
- binds to 127.0.0.1 only;
- Host header must be 127.0.0.1/localhost with the server port;
- POST requires a per-run random token header, a same-origin Origin, and
  application/json; bodies are capped at 64 KB;
- source documents are served only for manifest source IDs, with
  canonical path containment inside sources/;
- all source-derived text is returned as JSON data (the UI renders it
  with textContent, never innerHTML);
- restrictive CSP and no-store responses; no provider/credential code is
  imported beyond the boolean credential check used by status.
"""
import csv
import html
import json
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import __version__
from app.core import locking, model_config
from app.core.sources import (DuplicateSourceError, EVIDENCE_STATUSES,
                              PROVENANCES, SourceError, get_source,
                              import_source, load_manifest,
                              validate_sources)
from app.exporters import docx as docx_exporter
from app.providers.errors import ProviderError
from app.workflows import (independent_review, literature_synthesis,
                           paper_extraction, research_gap_analysis,
                           section_drafting)

DEFAULT_PORT = 8765
MAX_BODY_BYTES = 64 * 1024
MAX_IMPORT_BYTES = 50 * 1024 * 1024
IMPORT_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv", ".bib"}
IMPORT_STAGING = "outputs/imports"
TEXT_PREVIEW_EXTENSIONS = {".txt", ".md", ".csv", ".bib"}
MAX_PREVIEW_CHARS = 200_000
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"app.js": "text/javascript; charset=utf-8",
                "styles.css": "text/css; charset=utf-8"}
SOURCE_ID_RE = re.compile(r"^SRC-[0-9a-f]{12}$")
CANDIDATE_ID_RE = re.compile(r"^EXT-[0-9a-f]{12}-\d{3}$")
DOC_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument."
             "wordprocessingml.document",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".bib": "text/plain; charset=utf-8",
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

DECISION_MAP = {"approve": "APPROVED", "reject": "REJECTED"}

SECTION_LABELS = {
    "introduction": "Introduction",
    "literature_review": "Literature Review",
    "methodology": "Methodology",
    "discussion": "Discussion",
    "conclusion": "Conclusion",
}


def sd_label(name: str) -> str:
    return SECTION_LABELS.get(name, name.replace("_", " ").title())


def build_provider(task: str, root: Path):
    """Construct a provider only when an explicit AI action is requested."""
    config = model_config.load_config(root)
    resolved = model_config.resolve_task(config, task)
    provider = model_config.build_provider(config, resolved["provider"], root)
    if resolved.get("model") and resolved["model"] != getattr(
            provider, "default_model", None):
        provider.default_model = resolved["model"]
    return provider


def workflow_lock(root: Path):
    """Serialize UI workflow writes (synthesis/gaps/drafting/reviews/export)
    across requests and processes. Decisions have their own lock."""
    return locking.exclusive_lock(root / "outputs" / ".workflow.lock")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _project_title(root: Path) -> str:
    try:
        text = (root / "PROJECT.md").read_text(encoding="utf-8")
    except OSError:
        return "ResearchForge"
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.strip().lower() == "## working title"
            continue
        if in_section and line.strip():
            return line.strip().strip("*").strip()
    return "ResearchForge"


def _literature_index(root: Path) -> dict:
    """Map source ids to verified literature-matrix rows (title/authors)."""
    index = {}
    path = root / "sources" / "literature_matrix.csv"
    if not path.is_file():
        return index
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                note = row.get("verification_notes", "") or ""
                match = re.search(r"SRC-[0-9a-f]{12}", note)
                if match:
                    index[match.group(0)] = row
    except OSError:
        return {}
    return index


def _literature_rows(root: Path) -> int:
    path = root / "sources" / "literature_matrix.csv"
    if not path.is_file():
        return 0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return sum(1 for _row in csv.DictReader(handle))
    except OSError:
        return 0


def _metadata_only_rows(root: Path) -> int:
    """Literature records whose recorded evidence status is not full text."""
    path = root / "sources" / "literature_matrix.csv"
    if not path.is_file():
        return 0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return sum(1 for row in csv.DictReader(handle)
                       if (row.get("evidence_status") or "").strip()
                       not in ("", "FULL TEXT READ"))
    except OSError:
        return 0


def _section_first_line(text: str, heading: str) -> str | None:
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.strip().lower() == ("## " + heading).lower()
            continue
        if in_section and line.strip():
            return line.strip()
    return None


def _progress_phase(root: Path) -> str | None:
    try:
        text = (root / "progress.md").read_text(encoding="utf-8")
    except OSError:
        return None
    return _section_first_line(text, "Current phase")


def _research_problem(root: Path) -> str:
    """The accepted research problem statement, verbatim (no invention)."""
    try:
        text = (root / "research" / "research_charter.md").read_text(
            encoding="utf-8")
    except OSError:
        return ""
    lines, in_section, collected = text.splitlines(), False, []
    for line in lines:
        if line.startswith("## "):
            if in_section:
                break
            in_section = line.strip().lower() == "## research problem"
            continue
        if in_section:
            if line.strip().startswith("**Status:**"):
                break
            if line.strip():
                collected.append(line.strip())
    return " ".join(collected)[:900]


class Workspace:
    """Read-oriented view over the existing files and workflows."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.metadata = _literature_index(self.root)
        self.literature_rows = _literature_rows(self.root)
        self.project_title = _project_title(self.root)
        self.phase = _progress_phase(self.root)
        self.research_problem = _research_problem(self.root)
        self.metadata_only_rows = _metadata_only_rows(self.root)

    # ------------------------------------------------------------- reads
    def sources(self, validate: bool = False) -> list:
        manifest = load_manifest(self.root)
        validation = {}
        if validate:
            try:
                for row in validate_sources(self.root):
                    validation[row["source_id"]] = {
                        "level": row["level"],
                        "messages": row["messages"],
                    }
            except SourceError:
                validation = {}
        rows = []
        for source_id in sorted(manifest["sources"]):
            record = manifest["sources"][source_id]
            try:
                extraction = paper_extraction.load_extraction(self.root,
                                                              source_id)
            except SourceError:
                extraction = None
            meta = self.metadata.get(source_id, {})
            items = extraction.get("items", []) if extraction else []
            if record.get("provenance") == "researcher" and \
                    record.get("verification_status") == "GREY LITERATURE":
                category = "research_brief"
            elif meta:
                category = "external_literature"
            else:
                category = "other"
            rows.append({
                "source_id": source_id,
                "filename": record.get("original_filename"),
                "title": (meta.get("title") or record.get("original_filename")
                          or source_id),
                "authors": meta.get("authors") or "",
                "year": meta.get("publication_year") or "",
                "doi": meta.get("doi") or "",
                "source_type": record.get("source_type"),
                "provenance": record.get("provenance"),
                "verification_status": record.get("verification_status"),
                "imported_at": record.get("imported_at"),
                "outcome": (extraction or {}).get("outcome"),
                "review": (extraction or {}).get("review"),
                "pages": (extraction or {}).get("pages"),
                "category": category,
                "validation": validation.get(source_id),
                "full_text_available":
                    record.get("verification_status") == "FULL TEXT READ",
                "extracted": (extraction or {}).get("outcome") == "COMPLETED",
                "document_kind": Path(record.get("stored_location") or "")
                                 .suffix.lstrip(".").lower(),
                "candidates": {
                    "total": len(items),
                    "pending": sum(1 for i in items
                                   if i.get("decision") is None),
                    "approved": sum(1 for i in items
                                    if i.get("decision") == "APPROVED"),
                    "rejected": sum(1 for i in items
                                    if i.get("decision") == "REJECTED"),
                },
                "has_document": (self.root / record.get("stored_location", ""))
                                .is_file(),
            })
        return rows

    def _all_candidates(self):
        manifest = load_manifest(self.root)
        for source_id in sorted(manifest["sources"]):
            try:
                extraction = paper_extraction.load_extraction(self.root,
                                                              source_id)
            except SourceError:
                continue
            if not extraction:
                continue
            for index, item in enumerate(extraction.get("items", []), start=1):
                yield source_id, index, item

    def counts(self) -> dict:
        totals = {"pending": 0, "approved": 0, "rejected": 0, "total": 0}
        classifications = {}
        for _sid, _index, item in self._all_candidates():
            totals["total"] += 1
            decision = item.get("decision")
            key = ("pending" if decision is None
                   else "approved" if decision == "APPROVED" else "rejected")
            totals[key] += 1
            name = item.get("classification") or "unclassified"
            classifications[name] = classifications.get(name, 0) + 1
        return {"candidates": totals,
                "classifications": dict(sorted(classifications.items()))}

    def status(self) -> dict:
        sources = self.sources()
        counts = self.counts()
        return {
            "app": "ResearchForge",
            "version": __version__,
            "workspace": self.root.name,
            "project_title": self.project_title,
            "sources": len(sources),
            **counts,
        }

    def candidates(self, source_id=None, decision=None, classification=None,
                   query=None, page=1, page_size=20) -> dict:
        manifest = load_manifest(self.root)
        if source_id and source_id not in manifest["sources"]:
            raise ApiError(404, "unknown_source", "source not found")
        query_lower = (query or "").strip().lower()
        rows = []
        for sid, index, item in self._all_candidates():
            if source_id and sid != source_id:
                continue
            state = item.get("decision")
            label = ("pending" if state is None
                     else "approved" if state == "APPROVED" else "rejected")
            if decision and decision != "all" and label != decision:
                continue
            if classification and (item.get("classification")
                                   != classification):
                continue
            if query_lower:
                haystack = " ".join(str(item.get(key, "")) for key in
                                    ("text", "section", "classification",
                                     "extraction_id")).lower()
                if query_lower not in haystack:
                    continue
            rows.append((sid, index, item))
        total = len(rows)
        page_size = max(1, min(int(page_size), 100))
        page = max(1, int(page))
        start = (page - 1) * page_size
        page_rows = rows[start:start + page_size]
        titles = {row["source_id"]: row["title"] for row in self.sources()}
        items = []
        for sid, index, item in page_rows:
            items.append({
                "candidate_id": item.get("extraction_id"),
                "index": index,
                "source_id": sid,
                "source_title": titles.get(sid, sid),
                "section": item.get("section"),
                "page": item.get("page"),
                "paragraph": item.get("paragraph"),
                "classification": item.get("classification"),
                "kind": item.get("kind"),
                "decision": item.get("decision"),
                "reviewed_by": item.get("reviewed_by"),
                "reviewed_at": item.get("reviewed_at"),
                "preview": (item.get("text") or "")[:240],
            })
        return {"items": items, "total": total, "page": page,
                "page_size": page_size,
                "pages": max(1, -(-total // page_size))}

    def candidate(self, candidate_id: str, source_id=None) -> dict:
        if not CANDIDATE_ID_RE.match(candidate_id or ""):
            raise ApiError(404, "unknown_candidate", "candidate not found")
        for sid, index, item in self._all_candidates():
            if item.get("extraction_id") != candidate_id:
                continue
            if source_id and sid != source_id:
                raise ApiError(404, "unknown_candidate",
                               "candidate does not belong to this source")
            source = next((row for row in self.sources()
                           if row["source_id"] == sid), None)
            return {"candidate_id": candidate_id, "index": index,
                    "source": source, "item": {
                        "candidate_id": candidate_id,
                        "section": item.get("section"),
                        "page": item.get("page"),
                        "paragraph": item.get("paragraph"),
                        "classification": item.get("classification"),
                        "kind": item.get("kind"),
                        "text": item.get("text"),
                        "paragraph_text": item.get("paragraph_text"),
                        "text_kind": item.get("text_kind"),
                        "extraction_method": item.get("extraction_method"),
                        "extracted_at": item.get("extracted_at"),
                        "review": item.get("review"),
                        "decision": item.get("decision"),
                        "reviewed_by": item.get("reviewed_by"),
                        "reviewed_at": item.get("reviewed_at"),
                    }}
        raise ApiError(404, "unknown_candidate", "candidate not found")

    def document(self, source_id: str):
        if not SOURCE_ID_RE.match(source_id or ""):
            raise ApiError(404, "unknown_source", "source not found")
        record = get_source(self.root, source_id)
        if record is None:
            raise ApiError(404, "unknown_source", "source not found")
        relative = record.get("stored_location") or ""
        path = (self.root / relative).resolve()
        allowed = [(self.root / "sources" / "papers").resolve(),
                   (self.root / "sources" / "extracted_notes").resolve()]
        if not any(path.is_relative_to(directory) for directory in allowed):
            raise ApiError(403, "forbidden_path",
                           "source path is outside the permitted area")
        if path.suffix.lower() not in DOC_TYPES or not path.is_file():
            raise ApiError(404, "document_unavailable",
                           "source document is not available")
        return path, DOC_TYPES[path.suffix.lower()]

    def source_detail(self, source_id: str) -> dict:
        row = next((item for item in self.sources(validate=True)
                    if item["source_id"] == source_id), None)
        if row is None:
            raise ApiError(404, "unknown_source", "source not found")
        manifest_record = get_source(self.root, source_id) or {}
        try:
            record = paper_extraction.load_extraction(self.root, source_id)
        except SourceError:
            record = None
        extraction = None
        if record:
            items = record.get("items", [])
            extraction = {
                "outcome": record.get("outcome"),
                "review": record.get("review"),
                "reviewed_by": record.get("reviewed_by"),
                "reviewed_at": record.get("reviewed_at"),
                "extracted_at": record.get("extracted_at"),
                "tool": record.get("extraction_tool"),
                "tool_version": record.get("extraction_tool_version"),
                "pages": record.get("pages"),
                "max_page": max((item.get("page") or 0 for item in items),
                                default=None),
                "item_count": len(items),
                "characters": sum(len(item.get("text") or "")
                                  for item in items),
                "limitations": record.get("limitations", []),
                "error": record.get("error"),
                "item_decisions": record.get("item_decisions"),
            }
        return {
            "source": row,
            "checksum": manifest_record.get("sha256"),
            "stored_size_bytes": manifest_record.get("size_bytes"),
            "extraction": extraction,
        }

    def source_preview(self, source_id: str) -> dict:
        record = get_source(self.root, source_id)
        if record is None:
            raise ApiError(404, "unknown_source", "source not found")
        path, _content_type = self.document(source_id)
        if path.suffix.lower() not in TEXT_PREVIEW_EXTENSIONS:
            raise ApiError(415, "preview_unavailable",
                           "this format has no safe text preview; open or "
                           "download the original document")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ApiError(415, "preview_unavailable",
                           f"document is not readable UTF-8 text: {exc}"
                           ) from exc
        return {"kind": path.suffix.lstrip(".").lower(),
                "characters": len(text),
                "truncated": len(text) > MAX_PREVIEW_CHARS,
                "text": text[:MAX_PREVIEW_CHARS]}

    def import_upload(self, filename: str, data: bytes,
                      provenance: str = "researcher",
                      verification_status: str = "SEARCH RESULT ONLY"
                      ) -> dict:
        """Import one uploaded document through the existing engine. The
        browser supplies bytes and a base filename only; no filesystem path
        is ever accepted. A controlled staging directory is always cleaned."""
        name = str(filename or "")
        if (not name or "/" in name or "\\" in name or ".." in name
                or "\x00" in name or len(name) > 200
                or Path(name).name != name):
            raise ApiError(400, "invalid_filename",
                           "filename must be a simple file name")
        extension = Path(name).suffix.lower()
        if extension not in IMPORT_EXTENSIONS:
            raise ApiError(415, "unsupported_format",
                           f"unsupported format {extension or '<none>'}; "
                           f"supported: {', '.join(sorted(IMPORT_EXTENSIONS))}")
        if provenance not in PROVENANCES:
            raise ApiError(400, "invalid_provenance", "unknown provenance")
        if verification_status not in EVIDENCE_STATUSES:
            raise ApiError(400, "invalid_status",
                           "unknown evidence status")
        staging_root = self.root / IMPORT_STAGING
        staging_root.mkdir(parents=True, exist_ok=True)
        try:
            with workflow_lock(self.root):
                temp_dir = Path(tempfile.mkdtemp(prefix="upload-",
                                                 dir=staging_root))
                try:
                    staged = temp_dir / name
                    staged.write_bytes(data)
                    try:
                        record = import_source(
                            self.root, staged, provenance=provenance,
                            verification_status=verification_status)
                    except DuplicateSourceError as exc:
                        return {"status": "duplicate", "filename": name,
                                "message": str(exc)}
                    except SourceError as exc:
                        return {"status": "rejected", "filename": name,
                                "message": str(exc)}
                    extraction = paper_extraction.extract_source(
                        self.root, record["source_id"])
                    return {
                        "status": "imported",
                        "filename": name,
                        "source": record,
                        "extraction": {
                            "outcome": extraction.get("outcome"),
                            "review": extraction.get("review"),
                            "items": len(extraction.get("items", [])),
                            "pages": extraction.get("pages"),
                            "error": extraction.get("error"),
                            "limitations": extraction.get("limitations", []),
                        },
                    }
                except OSError as exc:
                    return {"status": "failed", "filename": name,
                            "message": f"import failed: {exc}"}
                finally:
                    shutil.rmtree(temp_dir, ignore_errors=True)
        except locking.LockTimeout as exc:
            raise ApiError(409, "write_conflict", str(exc)) from exc

    def overview(self) -> dict:
        sources = self.sources()
        counts = self.counts()
        candidates = counts["candidates"]
        full_text = sum(1 for row in sources
                        if row["verification_status"] == "FULL TEXT READ")
        briefs = sum(1 for row in sources
                     if row["category"] == "research_brief")
        metadata_only = self.metadata_only_rows

        synthesis = None
        try:
            synthesis = literature_synthesis.load_draft(self.root)
        except SourceError:
            synthesis = None
        analysis = None
        try:
            analysis = research_gap_analysis.load_analysis(self.root)
        except SourceError:
            analysis = None
        sections = []
        for name in sorted(section_drafting.SECTIONS):
            try:
                data = section_drafting.load_section(self.root, name)
            except SourceError:
                continue
            if data and data.get("versions"):
                latest = data["versions"][-1]
                sections.append({"section": name,
                                 "versions": len(data["versions"]),
                                 "review": latest.get("review"),
                                 "created_at": latest.get("created_at")})
        reviews = []
        reports_dir = self.root / independent_review.REPORTS_DIR
        if reports_dir.is_dir():
            for path in sorted(reports_dir.glob("RVW-*.json")):
                try:
                    review = independent_review.load_review(
                        self.root, path.stem)
                except SourceError:
                    continue
                if review:
                    reviews.append(review)
        exports = list((self.root / "outputs" / "docx").glob("*.docx"))

        approved_evidence = 0
        synthesis_review = None
        if synthesis:
            approved_evidence = len(
                synthesis.get("eligibility", {}).get("approved_extractions",
                                                     []))
            synthesis_review = synthesis.get("review")

        pipeline = [
            {"key": "ingestion", "label": "Source ingestion",
             "state": "complete" if sources else "not_started",
             "detail": f"{len(sources)} sources · {full_text} full text · "
                       f"{briefs} research brief"},
        ]
        if candidates["total"] == 0:
            evidence_state, evidence_detail = (
                "not_started", "No evidence candidates extracted yet")
        elif candidates["pending"] > 0:
            evidence_state = "in_progress"
            evidence_detail = (f"{candidates['pending']} pending · "
                               f"{candidates['approved']} approved")
        else:
            evidence_state = "complete"
            evidence_detail = (f"All {candidates['total']} candidates "
                               "decided")
        pipeline.append({"key": "evidence", "label": "Evidence review",
                         "state": evidence_state, "detail": evidence_detail})

        if synthesis is None:
            synthesis_state, synthesis_detail = (
                "not_started", "No synthesis draft")
        elif approved_evidence == 0:
            synthesis_state, synthesis_detail = (
                "blocked", "Draft needs approved evidence")
        elif synthesis_review == "APPROVED":
            synthesis_state, synthesis_detail = (
                "complete", "Synthesis approved")
        else:
            synthesis_state, synthesis_detail = (
                "in_progress", "Draft awaiting approval")
        pipeline.append({"key": "synthesis", "label": "Literature synthesis",
                         "state": synthesis_state,
                         "detail": synthesis_detail})

        if analysis is None:
            if synthesis_review != "APPROVED":
                gap_state, gap_detail = (
                    "blocked", "Requires an approved synthesis")
            else:
                gap_state, gap_detail = ("not_started", "No gap analysis")
        elif analysis.get("review") == "APPROVED":
            gap_state, gap_detail = ("complete", "Gap analysis approved")
        else:
            gap_state, gap_detail = (
                "in_progress", "Draft awaiting approval")
        pipeline.append({"key": "gaps", "label": "Research gaps",
                         "state": gap_state, "detail": gap_detail})

        approved_sections = sum(1 for section in sections
                                if section["review"] == "APPROVED")
        if sections:
            drafting_state = ("complete" if approved_sections
                              else "in_progress")
            drafting_detail = (f"{len(sections)} section draft(s) · "
                               f"{approved_sections} approved")
        elif synthesis_review == "APPROVED":
            drafting_state, drafting_detail = (
                "not_started", "No section drafted")
        else:
            drafting_state, drafting_detail = (
                "blocked", "Requires an approved synthesis")
        pipeline.append({"key": "drafting", "label": "Manuscript drafting",
                         "state": drafting_state, "detail": drafting_detail})
        pipeline.append({
            "key": "review", "label": "Independent review",
            "state": "in_progress" if reviews else "not_started",
            "detail": (f"{len(reviews)} review report(s)" if reviews
                       else "No review reports")})
        pipeline.append({
            "key": "export", "label": "DOCX export",
            "state": "complete" if exports else "not_started",
            "detail": (f"{len(exports)} exported document(s)" if exports
                       else "No exports yet")})

        blockers = []
        if candidates["pending"] > 0:
            blockers.append({
                "severity": "blocking" if candidates["approved"] == 0
                else "attention",
                "title": f"{candidates['pending']} evidence candidates "
                         "await human review",
                "detail": "Approve or reject passages in Evidence Review. "
                          "Synthesis and drafting stay blocked until at "
                          "least one candidate is approved.",
            })
        if candidates["approved"] == 0:
            blockers.append({
                "severity": "blocking",
                "title": "No approved evidence",
                "detail": "No synthesis statement can be supported yet.",
            })
        if synthesis and synthesis_review != "APPROVED" and approved_evidence:
            blockers.append({
                "severity": "attention",
                "title": "Synthesis draft waiting for approval",
                "detail": "Review the generated literature synthesis and "
                          "record an explicit approval when it is defensible.",
            })

        activity = []
        manifest = load_manifest(self.root)
        titles = {row["source_id"]: row["title"] for row in sources}
        for source_id, record in manifest["sources"].items():
            if record.get("imported_at"):
                activity.append({
                    "at": record["imported_at"], "kind": "source",
                    "title": "Source imported",
                    "detail": titles.get(source_id, source_id)})
        for source_id in manifest["sources"]:
            try:
                extraction = paper_extraction.load_extraction(self.root,
                                                              source_id)
            except SourceError:
                continue
            if extraction and extraction.get("extracted_at"):
                activity.append({
                    "at": extraction["extracted_at"], "kind": "extraction",
                    "title": "Evidence extracted",
                    "detail": f"{titles.get(source_id, source_id)} · "
                              f"{len(extraction.get('items', []))} "
                              "candidates"})
        if synthesis and synthesis.get("generated_at"):
            activity.append({"at": synthesis["generated_at"],
                             "kind": "synthesis",
                             "title": "Synthesis draft generated",
                             "detail": synthesis.get("review", "")})
        if analysis and analysis.get("generated_at"):
            activity.append({"at": analysis["generated_at"],
                             "kind": "gaps",
                             "title": "Gap analysis generated",
                             "detail": analysis.get("review", "")})
        for section in sections:
            if section.get("created_at"):
                activity.append({"at": section["created_at"],
                                 "kind": "section",
                                 "title": "Section draft created",
                                 "detail": section["section"]})
        for review in reviews:
            if review.get("created_at"):
                activity.append({"at": review["created_at"],
                                 "kind": "review",
                                 "title": "Independent review created",
                                 "detail": review.get("reviewed_artifact", {})
                                 .get("section", "")})
        activity.sort(key=lambda item: item["at"], reverse=True)
        return {
            "project": {"title": self.project_title,
                        "description": self.research_problem,
                        "phase": self.phase,
                        "workspace": self.root.name,
                        "version": __version__},
            "sources": {"total": len(sources), "full_text": full_text,
                        "research_brief": briefs,
                        "metadata_only": metadata_only,
                        "verified_records": self.literature_rows},
            "candidates": candidates,
            "classifications": counts["classifications"],
            "pipeline": pipeline,
            "blockers": blockers[:4],
            "activity": activity[:8],
        }

    # ---------------------------------------------------------- workflows
    def synthesis_state(self) -> dict:
        draft = None
        try:
            draft = literature_synthesis.load_draft(self.root)
        except SourceError as exc:
            return {"exists": False, "error": str(exc), "evidence": {},
                    "eligibility": {}, "review": None,
                    "blocked_reason": "Synthesis draft is unreadable."}
        if draft is None:
            return {"exists": False, "review": None, "evidence": {},
                    "eligibility": {},
                    "blocked_reason": "No synthesis draft exists yet. "
                                      "Run deterministic synthesis to create one."}
        eligible = draft.get("eligibility", {}).get("approved_extractions", [])
        evidence = draft.get("evidence", {})
        total = sum(len(evidence.get(key, [])) for key in
                    ("findings", "claims", "methods", "other_evidence"))
        return {
            "exists": True,
            "review": draft.get("review"),
            "generated_at": draft.get("generated_at"),
            "reviewed_by": draft.get("reviewed_by"),
            "reviewed_at": draft.get("reviewed_at"),
            "eligibility": draft.get("eligibility", {}),
            "evidence": evidence,
            "evidence_count": total,
            "approved_evidence_count": len(eligible),
            "interpretation": draft.get("interpretation", {}),
            "blocked_reason": (None if eligible else
                               "No approved evidence yet; synthesis cannot "
                               "be evidence-supported."),
        }

    def run_synthesis(self, ai: bool = False) -> dict:
        try:
            with workflow_lock(self.root):
                draft = literature_synthesis.synthesize(self.root)
                if ai:
                    provider = build_provider("literature_synthesis",
                                              self.root)
                    draft = literature_synthesis.add_ai_interpretation(
                        self.root, draft, provider)
                    draft = literature_synthesis.save_draft(self.root, draft)
        except locking.LockTimeout as exc:
            raise ApiError(409, "write_conflict", str(exc)) from exc
        except ProviderError as exc:
            raise ApiError(503, "provider_unavailable", str(exc)) from exc
        except SourceError as exc:
            raise ApiError(400, "workflow_error", str(exc)) from exc
        state = self.synthesis_state()
        interpretation = state.get("interpretation") or {}
        if interpretation.get("status") == "FAILED":
            state["ai_error"] = interpretation.get("error")
        return state

    def approve_synthesis(self, reviewer: str) -> dict:
        if not reviewer or not reviewer.strip():
            raise ApiError(400, "reviewer_required",
                           "reviewer attribution is required")
        try:
            with workflow_lock(self.root):
                draft = literature_synthesis.load_draft(self.root)
                if draft is None:
                    raise ApiError(404, "missing_draft",
                                   "no synthesis draft exists")
                if draft.get("review") == "APPROVED":
                    raise ApiError(409, "already_approved",
                                   "synthesis is already approved")
                return literature_synthesis.approve_synthesis(
                    self.root, reviewer.strip())
        except locking.LockTimeout as exc:
            raise ApiError(409, "write_conflict", str(exc)) from exc
        except SourceError as exc:
            raise ApiError(400, "workflow_error", str(exc)) from exc

    def gaps_state(self) -> dict:
        synthesis = None
        try:
            synthesis = literature_synthesis.load_draft(self.root)
        except SourceError:
            synthesis = None
        synthesis_ready = bool(synthesis
                               and synthesis.get("review") == "APPROVED")
        analysis = None
        try:
            analysis = research_gap_analysis.load_analysis(self.root)
        except SourceError as exc:
            return {"exists": False, "synthesis_ready": synthesis_ready,
                    "error": str(exc), "candidate_gaps": {},
                    "blocked_reason": "Gap analysis is unreadable."}
        if analysis is None:
            return {"exists": False, "synthesis_ready": synthesis_ready,
                    "candidate_gaps": {},
                    "blocked_reason": (None if synthesis_ready else
                                       "Requires an APPROVED synthesis "
                                       "before gap analysis can run.")}
        return {
            "exists": True,
            "synthesis_ready": synthesis_ready,
            "review": analysis.get("review"),
            "generated_at": analysis.get("generated_at"),
            "reviewed_by": analysis.get("reviewed_by"),
            "candidate_gaps": analysis.get("candidate_gaps", {}),
            "eligibility": analysis.get("eligibility", {}),
            "interpretation": analysis.get("interpretation", {}),
            "novelty_disclaimer": analysis.get("novelty_disclaimer"),
            "blocked_reason": (None if synthesis_ready else
                               "Synthesis must be approved before gaps."),
        }

    def run_gaps(self, ai: bool = False) -> dict:
        try:
            with workflow_lock(self.root):
                analysis = research_gap_analysis.analyze_gaps(self.root)
                if ai:
                    provider = build_provider("research_gap_analysis",
                                              self.root)
                    analysis = research_gap_analysis.add_ai_interpretation(
                        self.root, analysis, provider)
                    analysis = research_gap_analysis.save_analysis(
                        self.root, analysis)
        except locking.LockTimeout as exc:
            raise ApiError(409, "write_conflict", str(exc)) from exc
        except ProviderError as exc:
            raise ApiError(503, "provider_unavailable", str(exc)) from exc
        except SourceError as exc:
            raise ApiError(400, "workflow_error", str(exc)) from exc
        state = self.gaps_state()
        interpretation = state.get("interpretation") or {}
        if interpretation.get("status") == "FAILED":
            state["ai_error"] = interpretation.get("error")
        return state

    def approve_gaps(self, reviewer: str) -> dict:
        if not reviewer or not reviewer.strip():
            raise ApiError(400, "reviewer_required",
                           "reviewer attribution is required")
        try:
            with workflow_lock(self.root):
                analysis = research_gap_analysis.load_analysis(self.root)
                if analysis is None:
                    raise ApiError(404, "missing_draft",
                                   "no gap analysis exists")
                if analysis.get("review") == "APPROVED":
                    raise ApiError(409, "already_approved",
                                   "gap analysis is already approved")
                return research_gap_analysis.approve_gap_analysis(
                    self.root, reviewer.strip())
        except locking.LockTimeout as exc:
            raise ApiError(409, "write_conflict", str(exc)) from exc
        except SourceError as exc:
            raise ApiError(400, "workflow_error", str(exc)) from exc

    MANUSCRIPT_STRUCTURE = (
        ("Objectives", None,
         "No draftable section exists for objectives; use the supported "
         "sections below."),
        ("Introduction", "introduction", None),
        ("Literature Review", "literature_review", None),
        ("Methodology", "methodology", None),
        ("Architecture", None,
         "Not supported by the current section engine."),
        ("Outcome", None,
         "No outcome section exists; only Discussion can be drafted."),
        ("Conclusion", "conclusion", None),
        ("References", None,
         "References are produced by DOCX export, not drafted here."),
    )

    def manuscript_state(self) -> dict:
        synthesis = None
        try:
            synthesis = literature_synthesis.load_draft(self.root)
        except SourceError:
            synthesis = None
        synthesis_approved = bool(synthesis
                                  and synthesis.get("review") == "APPROVED")
        sections = []
        reviews_by_section = {}
        reports_dir = self.root / independent_review.REPORTS_DIR
        if reports_dir.is_dir():
            for path in sorted(reports_dir.glob("RVW-*.json")):
                try:
                    review = independent_review.load_review(self.root,
                                                            path.stem)
                except SourceError:
                    continue
                if not review:
                    continue
                section = (review.get("reviewed_artifact") or {}).get(
                    "section")
                reviews_by_section.setdefault(section, []).append({
                    "review_id": review.get("review_id"),
                    "version": (review.get("reviewed_artifact") or {}).get(
                        "version"),
                    "review": review.get("review"),
                    "findings": len(review.get("deterministic_findings",
                                               [])),
                    "ai_status": (review.get("ai_review") or {}).get(
                        "status"),
                })
        for name in sorted(section_drafting.SECTIONS):
            try:
                data = section_drafting.load_section(self.root, name)
            except SourceError:
                data = None
            versions = []
            if data:
                for version in data.get("versions", []):
                    versions.append({
                        "version": version.get("version"),
                        "review": version.get("review"),
                        "created_at": version.get("created_at"),
                        "origin": version.get("origin"),
                        "model": version.get("model"),
                        "status": version.get("status"),
                        "prose": version.get("prose"),
                        "citations": version.get("citations", {}),
                        "unsupported": version.get(
                            "unsupported_statements", []),
                        "evidence_refs": version.get("evidence_refs", []),
                        "researcher_context": version.get(
                            "researcher_context"),
                        "error": version.get("error"),
                    })
            sections.append({
                "section": name,
                "label": sd_label(name),
                "versions": versions,
                "latest": versions[-1] if versions else None,
                "reviews": reviews_by_section.get(name, []),
            })
        return {
            "synthesis_approved": synthesis_approved,
            "blocked_reason": (None if synthesis_approved else
                               "Drafting requires an APPROVED synthesis."),
            "structure": [{"label": label, "section": section,
                           "note": note}
                          for label, section, note
                          in self.MANUSCRIPT_STRUCTURE],
            "sections": sections,
        }

    def draft_manuscript(self, section: str, context) -> dict:
        if section not in section_drafting.SECTIONS:
            raise ApiError(400, "unknown_section",
                           "unsupported section")
        try:
            provider = build_provider("section_drafting", self.root)
        except ProviderError as exc:
            raise ApiError(503, "provider_unavailable", str(exc)) from exc
        try:
            with workflow_lock(self.root):
                version = section_drafting.draft_section(
                    self.root, section, provider,
                    researcher_context=(context or None))
        except locking.LockTimeout as exc:
            raise ApiError(409, "write_conflict", str(exc)) from exc
        except ProviderError as exc:
            raise ApiError(503, "provider_unavailable", str(exc)) from exc
        except SourceError as exc:
            raise ApiError(400, "workflow_error", str(exc)) from exc
        return {"section": section, "version": version.get("version"),
                "review": version.get("review"),
                "prose": version.get("prose"),
                "citations": version.get("citations"),
                "unsupported": version.get("unsupported_statements")}

    def approve_manuscript(self, section: str, version: int,
                           reviewer: str) -> dict:
        if not reviewer or not reviewer.strip():
            raise ApiError(400, "reviewer_required",
                           "reviewer attribution is required")
        if section not in section_drafting.SECTIONS:
            raise ApiError(400, "unknown_section", "unsupported section")
        try:
            with workflow_lock(self.root):
                data = section_drafting.load_section(self.root, section)
                if data is None:
                    raise ApiError(404, "missing_draft",
                                   "no draft exists for this section")
                match = next((item for item in data["versions"]
                              if item.get("version") == version), None)
                if match is None:
                    raise ApiError(404, "unknown_version",
                                   "draft version not found")
                if match.get("review") == "APPROVED":
                    raise ApiError(409, "already_approved",
                                   "this version is already approved")
                return section_drafting.approve_section(
                    self.root, section, reviewer.strip(), version=version)
        except locking.LockTimeout as exc:
            raise ApiError(409, "write_conflict", str(exc)) from exc
        except SourceError as exc:
            raise ApiError(400, "workflow_error", str(exc)) from exc

    def reviews_state(self) -> dict:
        reports = []
        directory = self.root / independent_review.REPORTS_DIR
        if directory.is_dir():
            for path in sorted(directory.glob("RVW-*.json")):
                try:
                    review = independent_review.load_review(
                        self.root, path.stem)
                except SourceError:
                    continue
                if not review:
                    continue
                artifact = review.get("reviewed_artifact") or {}
                findings = review.get("deterministic_findings", [])
                reports.append({
                    "review_id": review.get("review_id"),
                    "created_at": review.get("created_at"),
                    "section": artifact.get("section"),
                    "version": artifact.get("version"),
                    "review": review.get("review"),
                    "ai_status": (review.get("ai_review") or {}).get(
                        "status"),
                    "findings": findings,
                    "external_reviews": review.get("external_reviews", []),
                    "contradiction_references": review.get(
                        "contradiction_references", []),
                })
        reports.sort(key=lambda item: item.get("created_at") or "",
                     reverse=True)
        return {"reviews": reports,
                "sections": sorted(section_drafting.SECTIONS)}

    def run_review(self, section: str, version, ai: bool = False) -> dict:
        if section not in section_drafting.SECTIONS:
            raise ApiError(400, "unknown_section", "unsupported section")
        provider = None
        if ai:
            try:
                provider = build_provider("academic_review", self.root)
            except ProviderError as exc:
                raise ApiError(503, "provider_unavailable",
                               str(exc)) from exc
        try:
            with workflow_lock(self.root):
                review = independent_review.review_section(
                    self.root, section,
                    version=version, provider=provider)
        except locking.LockTimeout as exc:
            raise ApiError(409, "write_conflict", str(exc)) from exc
        except SourceError as exc:
            raise ApiError(400, "workflow_error", str(exc)) from exc
        return self._review_summary(review)

    def _review_summary(self, review: dict) -> dict:
        artifact = review.get("reviewed_artifact") or {}
        return {
            "review_id": review.get("review_id"),
            "section": artifact.get("section"),
            "version": artifact.get("version"),
            "review": review.get("review"),
            "findings": review.get("deterministic_findings", []),
            "ai_status": (review.get("ai_review") or {}).get("status"),
            "external_reviews": review.get("external_reviews", []),
        }

    def add_external_review(self, review_id: str, platform: str,
                            text: str, reviewer: str) -> dict:
        if not reviewer or not reviewer.strip():
            raise ApiError(400, "reviewer_required",
                           "reviewer attribution is required")
        try:
            with workflow_lock(self.root):
                review = independent_review.add_external_review(
                    self.root, review_id, text, platform)
        except locking.LockTimeout as exc:
            raise ApiError(409, "write_conflict", str(exc)) from exc
        except SourceError as exc:
            raise ApiError(400, "workflow_error", str(exc)) from exc
        return self._review_summary(review)

    def export_state(self) -> dict:
        exports = []
        directory = self.root / "outputs" / "docx"
        if directory.is_dir():
            for path in sorted(directory.glob("*.docx")):
                exports.append({"name": path.name,
                                "size_bytes": path.stat().st_size,
                                "modified_at": path.stat().st_mtime})
        try:
            plan = docx_exporter.plan_export(self.root)
        except SourceError as exc:
            return {"ready": False, "error": str(exc), "sections": [],
                    "excluded": [], "citations": [], "unresolved": [],
                    "exports": exports}
        citations = sorted({cid for section in plan["sections"]
                            for cid in section.get("citation_identifiers",
                                                   [])})
        unresolved = [{"citation_id": cid, "status": "UNRESOLVED",
                       "reason": "no verified bibliographic metadata"}
                      for cid in citations]
        return {
            "ready": bool(plan["sections"]),
            "sections": [{
                "section": item["section"],
                "heading": item["heading"],
                "version": item["version"],
                "fingerprint": item["fingerprint"],
                "approved_by": item["approved_by"],
                "approved_at": item["approved_at"],
                "citations": item.get("citation_identifiers", []),
                "evidence_count": len(item.get("evidence_refs", [])),
            } for item in plan["sections"]],
            "excluded": plan["excluded"],
            "citations": citations,
            "unresolved": unresolved,
            "exports": exports,
        }

    def run_export(self, output_name, title, sections, force) -> dict:
        name = Path(str(output_name or "manuscript.docx")).name
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.docx$", name):
            raise ApiError(400, "invalid_filename",
                           "output must be a simple .docx filename")
        output = self.root / "outputs" / "docx" / name
        chosen = sections or None
        if chosen:
            invalid = [item for item in chosen
                       if item not in section_drafting.SECTIONS]
            if invalid:
                raise ApiError(400, "unknown_section",
                               "unknown section selection")
        try:
            with workflow_lock(self.root):
                manifest = docx_exporter.export_docx(
                    self.root, output, title=(title or "Manuscript"),
                    sections=chosen, force=bool(force))
        except locking.LockTimeout as exc:
            raise ApiError(409, "write_conflict", str(exc)) from exc
        except SourceError as exc:
            raise ApiError(400, "workflow_error", str(exc)) from exc
        return {"manifest": manifest, "download": name}

    def export_file(self, name: str):
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.docx$", name or ""):
            raise ApiError(404, "not_found", "export not found")
        directory = (self.root / "outputs" / "docx").resolve()
        path = (directory / name).resolve()
        if path.parent != directory or not path.is_file():
            raise ApiError(404, "not_found", "export not found")
        return path

    # -------------------------------------------------------------- write
    def decide(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_request", "JSON object required")
        source_id = str(payload.get("source_id") or "")
        candidate_id = str(payload.get("candidate_id") or "")
        decision = payload.get("decision")
        reviewer = str(payload.get("reviewer") or "").strip()
        confirm = payload.get("confirm")
        if decision not in DECISION_MAP:
            raise ApiError(400, "invalid_decision",
                           "decision must be 'approve' or 'reject'")
        if not SOURCE_ID_RE.match(source_id):
            raise ApiError(404, "unknown_source", "source not found")
        if not CANDIDATE_ID_RE.match(candidate_id):
            raise ApiError(404, "unknown_candidate", "candidate not found")
        if not reviewer:
            raise ApiError(400, "reviewer_required",
                           "reviewer attribution is required")
        if confirm is not True:
            raise ApiError(400, "confirmation_required",
                           "explicit confirmation is required")
        try:
            item = paper_extraction.decide_item(
                self.root, source_id, candidate_id,
                DECISION_MAP[decision], reviewer)
        except SourceError as exc:
            message = str(exc)
            if "already decided" in message:
                raise ApiError(409, "already_decided", message) from exc
            if "another process" in message or "another request" in message:
                raise ApiError(409, "write_conflict", message) from exc
            if "candidate not found" in message:
                raise ApiError(404, "unknown_candidate", message) from exc
            raise ApiError(400, "decision_refused", message) from exc
        return {"candidate": {
                    "candidate_id": item.get("extraction_id"),
                    "decision": item.get("decision"),
                    "reviewed_by": item.get("reviewed_by"),
                    "reviewed_at": item.get("reviewed_at")},
                **{key: value for key, value in self.counts().items()}}


def _make_handler(state: Workspace, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ResearchForgeUI/1.0"

        # ------------------------------------------------------ helpers
        def _security_headers(self):
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")

        def _json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _error(self, error: ApiError):
            self._json(error.status, {"error": {"code": error.code,
                                                "message": error.message}})

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            port = self.server.server_address[1]
            return host in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _origin_ok(self) -> bool:
            origin = (self.headers.get("Origin") or "").strip().lower()
            port = self.server.server_address[1]
            return origin in {f"http://127.0.0.1:{port}",
                              f"http://localhost:{port}"}

        def _guard(self) -> bool:
            if not self._host_ok():
                self._error(ApiError(403, "invalid_host",
                                     "unexpected Host header"))
                return False
            return True

        # ---------------------------------------------------------- GET
        def do_GET(self):
            if not self._guard():
                return
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if path in ("/", "/index.html"):
                    return self._index()
                match = re.match(r"^/static/([A-Za-z0-9_.-]+)$", path)
                if match:
                    return self._static(match.group(1))
                if path == "/api/status":
                    return self._json(200, state.status())
                if path == "/api/overview":
                    return self._json(200, state.overview())
                if path == "/api/sources":
                    validate = (query.get("validate") or [""])[0] == "1"
                    return self._json(200, {"sources": state.sources(
                        validate=validate)})
                match = re.match(r"^/api/sources/([^/]+)$", path)
                if match:
                    return self._source(match.group(1))
                match = re.match(r"^/api/sources/([^/]+)/preview$", path)
                if match:
                    return self._preview(match.group(1))
                match = re.match(r"^/api/sources/([^/]+)/document$", path)
                if match:
                    return self._document(match.group(1))
                if path == "/api/candidates":
                    return self._candidates(query)
                match = re.match(r"^/api/candidates/([^/]+)$", path)
                if match:
                    source = (query.get("source") or [None])[0]
                    return self._candidate(match.group(1), source)
                if path == "/api/synthesis":
                    return self._json(200, state.synthesis_state())
                if path == "/api/gaps":
                    return self._json(200, state.gaps_state())
                if path == "/api/manuscript":
                    return self._json(200, state.manuscript_state())
                if path == "/api/reviews":
                    return self._json(200, state.reviews_state())
                match = re.match(r"^/api/reviews/([^/]+)$", path)
                if match:
                    return self._review_detail(match.group(1))
                if path == "/api/export":
                    return self._json(200, state.export_state())
                if path == "/api/export/download":
                    name = (query.get("file") or [""])[0]
                    return self._export_download(name)
                raise ApiError(404, "not_found", "unknown route")
            except ApiError as error:
                self._error(error)
            except SourceError as error:
                self._error(ApiError(400, "engine_error", str(error)))
            except Exception:  # noqa: BLE001 - never leak internals
                traceback.print_exc()
                self._error(ApiError(500, "internal_error",
                                     "unexpected server error"))

        # --------------------------------------------------------- POST
        def do_POST(self):
            if not self._guard():
                return
            parsed = urllib.parse.urlsplit(self.path)
            try:
                if not self._origin_ok():
                    raise ApiError(403, "invalid_origin",
                                   "same-origin requests only")
                token_header = self.headers.get("X-ResearchForge-Token") or ""
                if not secrets.compare_digest(token_header, token):
                    raise ApiError(403, "invalid_token",
                                   "missing or invalid session token")
                if parsed.path == "/api/sources/import":
                    return self._post_import(parsed)
                content_type = (self.headers.get("Content-Type") or "").split(
                    ";")[0].strip().lower()
                if content_type != "application/json":
                    raise ApiError(415, "unsupported_media_type",
                                   "application/json required")
                length = self.headers.get("Content-Length")
                if length is None or not length.isdigit():
                    raise ApiError(411, "length_required",
                                   "Content-Length required")
                if int(length) > MAX_BODY_BYTES:
                    self.close_connection = True
                    raise ApiError(413, "payload_too_large",
                                   "request body is too large")
                raw = self.rfile.read(int(length))
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ApiError(400, "invalid_json",
                                   "malformed JSON body") from exc
                if not isinstance(payload, dict):
                    raise ApiError(400, "invalid_json",
                                   "JSON object required")
                return self._post_workflow(parsed.path, payload)
            except ApiError as error:
                self._error(error)
            except SourceError as error:
                self._error(ApiError(400, "engine_error", str(error)))
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self._error(ApiError(500, "internal_error",
                                     "unexpected server error"))

        # ------------------------------------------------------- actions
        def _post_workflow(self, path, payload):
            if path == "/api/decisions":
                return self._json(200, state.decide(payload))
            if payload.get("confirm") is not True:
                raise ApiError(400, "confirmation_required",
                               "explicit confirmation is required")
            if path == "/api/synthesis/run":
                return self._json(200, state.run_synthesis(
                    payload.get("ai") is True))
            if path == "/api/synthesis/approve":
                return self._json(200, state.approve_synthesis(
                    str(payload.get("reviewer") or "")))
            if path == "/api/gaps/run":
                return self._json(200, state.run_gaps(
                    payload.get("ai") is True))
            if path == "/api/gaps/approve":
                return self._json(200, state.approve_gaps(
                    str(payload.get("reviewer") or "")))
            if path == "/api/manuscript/draft":
                section = str(payload.get("section") or "")
                context = payload.get("context")
                if context is not None and not isinstance(context, str):
                    raise ApiError(400, "invalid_context",
                                   "context must be text")
                return self._json(200, state.draft_manuscript(
                    section, context))
            if path == "/api/manuscript/approve":
                version = payload.get("version")
                if not isinstance(version, int):
                    raise ApiError(400, "invalid_version",
                                   "version must be an integer")
                return self._json(200, state.approve_manuscript(
                    str(payload.get("section") or ""), version,
                    str(payload.get("reviewer") or "")))
            if path == "/api/reviews/run":
                version = payload.get("version")
                if version is not None and not isinstance(version, int):
                    raise ApiError(400, "invalid_version",
                                   "version must be an integer")
                return self._json(200, state.run_review(
                    str(payload.get("section") or ""), version,
                    payload.get("ai") is True))
            if path == "/api/reviews/external":
                text = payload.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise ApiError(400, "invalid_text",
                                   "review text is required")
                return self._json(200, state.add_external_review(
                    str(payload.get("review_id") or ""),
                    str(payload.get("platform") or ""), text,
                    str(payload.get("reviewer") or "")))
            if path == "/api/export/docx":
                sections = payload.get("sections")
                if sections is not None and not isinstance(sections, list):
                    raise ApiError(400, "invalid_sections",
                                   "sections must be a list")
                return self._json(200, state.run_export(
                    payload.get("output"), payload.get("title"),
                    [str(item) for item in sections] if sections else None,
                    payload.get("force") is True))
            raise ApiError(404, "not_found", "unknown route")

        def _review_detail(self, review_id):
            try:
                review = independent_review.load_review(state.root, review_id)
            except SourceError as exc:
                raise ApiError(400, "engine_error", str(exc)) from exc
            if review is None:
                raise ApiError(404, "not_found", "review not found")
            return self._json(200, review)

        def _export_download(self, name):
            path = state.export_file(name)
            body = path.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type", "application/vnd.openxmlformats-"
                "officedocument.wordprocessingml.document")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{path.name}"')
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _index(self):
            template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            body = (template
                    .replace("__RF_TOKEN__", token)
                    .replace("__RF_WORKSPACE__", html.escape(
                        state.root.name))
                    .replace("__RF_PROJECT__", html.escape(
                        state.project_title))).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _static(self, name):
            if name not in STATIC_FILES:
                raise ApiError(404, "not_found", "unknown asset")
            body = (STATIC_DIR / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", STATIC_FILES[name])
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _source(self, source_id):
            if not SOURCE_ID_RE.match(source_id):
                raise ApiError(404, "unknown_source", "source not found")
            return self._json(200, state.source_detail(source_id))

        def _document(self, source_id):
            path, content_type = state.document(source_id)
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            disposition = ("attachment" if path.suffix.lower() == ".docx"
                           else "inline")
            self.send_header("Content-Disposition",
                             f'{disposition}; filename="{source_id}{path.suffix.lower()}"')
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _preview(self, source_id):
            return self._json(200, state.source_preview(source_id))

        def _post_import(self, parsed):
            query = urllib.parse.parse_qs(parsed.query)

            def arg(name, default=None):
                return (query.get(name) or [default])[0]

            if arg("confirm") != "true":
                raise ApiError(400, "confirmation_required",
                               "explicit confirmation is required")
            length = self.headers.get("Content-Length")
            if length is None or not length.isdigit():
                raise ApiError(411, "length_required",
                               "Content-Length required")
            if int(length) > MAX_IMPORT_BYTES:
                self.close_connection = True
                raise ApiError(413, "payload_too_large",
                               "upload exceeds the allowed size")
            data = self.rfile.read(int(length))
            if not data:
                raise ApiError(400, "empty_file", "uploaded file is empty")
            result = state.import_upload(
                arg("name") or "", data,
                arg("provenance") or "researcher",
                arg("status") or "SEARCH RESULT ONLY")
            return self._json(200, result)

        def _candidates(self, query):
            def arg(name, default=None):
                return (query.get(name) or [default])[0]

            try:
                page = int(arg("page", "1"))
                page_size = int(arg("page_size", "20"))
            except (TypeError, ValueError) as exc:
                raise ApiError(400, "invalid_pagination",
                               "page and page_size must be integers") from exc
            result = state.candidates(
                source_id=arg("source"), decision=arg("decision"),
                classification=arg("classification"), query=arg("q"),
                page=page, page_size=page_size)
            return self._json(200, result)

        def _candidate(self, candidate_id, source_id):
            return self._json(200, state.candidate(candidate_id, source_id))

        def log_message(self, fmt, *args):  # concise, no secrets
            print(f"[ui] {self.address_string()} {fmt % args}")

    return Handler


class ResearchForgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(root: Path, host: str = "127.0.0.1", port: int = 0,
                  token: str | None = None) -> ResearchForgeServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("the UI server must bind to loopback")
    state = Workspace(root)
    token = token or secrets.token_urlsafe(32)
    handler = _make_handler(state, token)
    server = ResearchForgeServer((host, port), handler)
    server.workspace = state
    server.token = token
    server.url = f"http://127.0.0.1:{server.server_address[1]}/"
    return server


def _edge_executable() -> str | None:
    for candidate in ("msedge", r"C:\Program Files (x86)\Microsoft\Edge"
                             r"\Application\msedge.exe",
                      r"C:\Program Files\Microsoft\Edge\Application"
                      r"\msedge.exe"):
        found = shutil.which(candidate) if "\\" not in candidate else candidate
        if found and Path(found).is_file():
            return found
    return None


def serve(root: Path, port: int = DEFAULT_PORT, open_browser: bool = True,
          host: str = "127.0.0.1", app_window: bool = False) -> None:
    server = create_server(root, host=host, port=port)
    print(f"ResearchForge workspace: {root}")
    print(f"Local URL: {server.url}")
    print("Press Ctrl+C to stop the server.")

    def _open():
        if app_window:
            edge = _edge_executable()
            if edge:
                try:
                    subprocess.Popen(
                        [edge, f"--app={server.url}"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
                    return
                except OSError:
                    pass
        webbrowser.open(server.url)

    if open_browser or app_window:
        timer = threading.Timer(0.4, _open)
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping ResearchForge UI.")
    finally:
        server.shutdown()
        server.server_close()
