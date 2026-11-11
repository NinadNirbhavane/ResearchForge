"""Paper extraction workflow (RF-015).

Turns an imported source (RF-010) into a structured evidence record with
claims, methods and findings — deterministically and locally. No AI/API
call is made: RF-015's specification does not require one, and every
extraction must be reproducible from the original file bytes.

Design:
- Records: one JSON per source at
  sources/extracted_notes/<source_id>.extraction.json (atomic write;
  `.json` is not an importable extension, so it can never collide with
  an imported source file).
- Classification is deterministic and structure-aware: explicit labels
  ("Claim:", "Finding:", "Method:", "Limitation:", "Gap:") always win;
  otherwise the enclosing document section (Abstract, Introduction,
  Methods, Results, Discussion, Limitations, Conclusion, …) and simple
  linguistic cues determine a candidate classification (research_problem,
  objective, method, dataset, empirical_result, author_interpretation,
  limitation, future_work, background, unclassified). Section context is
  a CANDIDATE signal only — results-section paragraphs are not verified
  findings until human review approves that specific candidate. Text is
  taken verbatim — nothing is invented, corrected or summarized.
  References/bibliography sections produce no evidence candidates.
- Per-candidate governance: every candidate starts
  review="PENDING HUMAN REVIEW"; decide_item() approves/rejects ONE
  candidate with a reviewer + timestamp (final decision). Whole-record
  approve_extraction()/reject_extraction() remain supported and freeze
  the record. Per-candidate decisions live in the extraction record
  (auditable); the central approval ledger stays artifact-level.
- Provenance: source_id, source_sha256 (re-verified against the stored
  file BEFORE reading — original-file integrity), original filename,
  evidence status copied from the source record, timestamp,
  extraction_method="local-deterministic", model=None (honest: no model
  was involved).
- Failure is explicit: statuses COMPLETED / EMPTY / UNSUPPORTED / FAILED
  with an `error` reason. Nothing is silently treated as successful.
- Governance: every record starts with review="PENDING HUMAN REVIEW";
  approve_extraction()/reject_extraction() are the only paths to a
  decided state, and APPROVED records cannot be overwritten by a
  re-extraction. Extraction never upgrades a source's evidence status
  (RF-010 rule: never auto-assign FULL TEXT READ).
- PDFs and DOCX files are handled deterministically: DOCX text is read
  from word/document.xml with the standard library (ZIP + ElementTree);
  PDF text uses the external `pdftotext` utility when installed, and is
  otherwise recorded as UNSUPPORTED with an explicit external-conversion
  path (convert to UTF-8 text and import that). No OCR.
"""
import json
import os
import platform
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from app.core import approvals, locking
from app.core.sources import (EVIDENCE_STATUSES, SourceError, file_digest,
                              get_source, load_manifest)

EXTRACTION_SUFFIX = ".extraction.json"
METHOD = "local-deterministic"
DOCX_TOOL = "python-stdlib (zipfile + ElementTree)"
DOCX_DOCUMENT_PART = "word/document.xml"
PDF_TOOL = "pdftotext"
PDF_TIMEOUT_SECONDS = 120
TEXT_TOOL = "utf-8 text reader"
PDF_MISSING_TOOL_MESSAGE = (
    "PDF text extraction requires the external 'pdftotext' utility "
    "(Poppler), which is not available. Convert the PDF to UTF-8 text "
    "externally (e.g. `pdftotext file.pdf out.txt`) and import the .txt or "
    ".md file instead.")
PDF_SCANNED_MESSAGE = (
    "no extractable text found; the PDF may be scanned/image-only. OCR is "
    "not supported — provide externally extracted or OCR'd UTF-8 text as a "
    "separate .txt/.md source instead.")
DOCX_LIMITATIONS = (
    "text-only extraction: formatting, styles, headers/footers, "
    "footnotes/endnotes, comments, tracked changes and text boxes are not "
    "preserved",
    "tables are flattened to 'cell | cell' rows",
    "macros, images and embedded objects are never executed or extracted",
    "footnote/endnote and comment parts are not read",
)
PDF_LIMITATIONS = (
    "text-only extraction via pdftotext: visual layout may differ",
    "figures, tables and captions are flattened into text",
    "no OCR: scanned/image-only PDFs produce no text",
    "page boundaries are approximated by blank-line separation",
)
LABEL_PREFIXES = {"claim": "claim", "claims": "claim", "finding": "finding",
                  "findings": "finding", "method": "method",
                  "methods": "method", "methodology": "method"}
ITEM_KINDS = ("claim", "method", "finding", "unclassified")
REVIEW_PENDING = "PENDING HUMAN REVIEW"
REVIEW_APPROVED = "APPROVED"
REVIEW_REJECTED = "REJECTED"

OUTCOME_COMPLETED = "COMPLETED"
OUTCOME_EMPTY = "EMPTY"
OUTCOME_UNSUPPORTED = "UNSUPPORTED"
OUTCOME_FAILED = "FAILED"


class ExtractionError(SourceError):
    """Extraction refused/invalid (reuses the RF-010 error hierarchy)."""


def extraction_path(root: Path, source_id: str) -> Path:
    return root / "sources" / "extracted_notes" / f"{source_id}{EXTRACTION_SUFFIX}"


def load_extraction(root: Path, source_id: str) -> dict | None:
    path = extraction_path(root, source_id)
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractionError(
            f"extraction record for {source_id} is unreadable: {exc}") from exc
    if not isinstance(record, dict) or record.get("source_id") != source_id:
        raise ExtractionError(
            f"extraction record for {source_id} is malformed")
    return record


def _write_extraction(root: Path, record: dict) -> None:
    path = extraction_path(root, record["source_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def _classify_paragraph(paragraph: str) -> str:
    head = paragraph.strip().split(":", 1)[0].strip().lower()
    return LABEL_PREFIXES.get(head, "unclassified")


W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraph_text(paragraph) -> str:
    return "".join(node.text or "" for node in paragraph.iter(f"{W_NS}t")).strip()


def docx_text(path: Path) -> str:
    """Deterministic text extraction from a DOCX (ZIP + XML, stdlib only).

    Paragraphs are emitted in document order; tables become one line per
    row with ' | ' between cells. Raises ValueError for malformed
    archives/XML or a missing main document part. Macros/embedded objects
    are never opened or executed."""
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read(DOCX_DOCUMENT_PART)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ValueError(f"not a readable DOCX archive: {exc}") from exc
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError(f"invalid DOCX document XML: {exc}") from exc
    body = root.find(f"{W_NS}body")
    if body is None:
        raise ValueError("DOCX document body is missing")
    blocks = []
    for child in body:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = _paragraph_text(child)
            if text:
                blocks.append(text)
        elif tag == "tbl":
            for row in child.findall(f"{W_NS}tr"):
                cells = []
                for cell in row.findall(f"{W_NS}tc"):
                    paragraphs = [_paragraph_text(p)
                                  for p in cell.findall(f"{W_NS}p")]
                    cells.append(" ".join(p for p in paragraphs if p))
                line = " | ".join(cells).strip()
                if line.strip(" |"):
                    blocks.append(line)
    return "\n\n".join(blocks)


def find_pdf_tool() -> str | None:
    """Path to the external pdftotext utility, or None when unavailable."""
    return shutil.which("pdftotext")


def pdf_tool_version(executable: str) -> str:
    try:
        result = subprocess.run([executable, "-v"], capture_output=True,
                                text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    output = (result.stdout or "") + (result.stderr or "")
    first = output.strip().splitlines()[0] if output.strip() else ""
    return first[:120] or "unknown"


def pdf_text(executable: str, path: Path) -> tuple:
    """Run pdftotext without a shell (no injection). Returns (text, pages).
    Page boundaries from the form-feed separators are preserved as
    blank-line separation. Raises ValueError on tool failure/timeout."""
    try:
        result = subprocess.run(
            [executable, "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True, timeout=PDF_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"pdftotext timed out after {PDF_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise ValueError(f"cannot run pdftotext: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace")
        raise ValueError(f"pdftotext failed (exit {result.returncode}): "
                         f"{detail.strip()[:300]}")
    text = (result.stdout or b"").decode("utf-8", "replace")
    pages = [chunk.strip() for chunk in text.split("\f")]
    pages = [page for page in pages if page]
    return "\n\n".join(pages), len(pages)


_SECTION_PATTERNS = (
    (r"abstract\b", "abstract"),
    (r"(related works?|literature review|background)\b", "related_work"),
    (r"(study area and data|data and study area|dataset|study area|"
     r"data collection|data preparation)\b", "dataset"),
    (r"(system design|system architecture|proposed (?:system|method|"
     r"framework)|implementation|materials and methods|methodology|"
     r"methods?)\b", "methodology"),
    (r"(experiments?|experimental (?:setup|protocol|design)|"
     r"numerical evaluation|evaluation)\b", "results"),
    (r"(results?|findings?)\b", "results"),
    (r"discussion\b", "discussion"),
    (r"limitations?\b", "limitations"),
    (r"(conclusions?|concluding remarks)\b", "conclusion"),
    (r"(references|bibliography)\b", "references"),
    (r"introduction\b", "introduction"),
)
_FUTURE_RE = re.compile(
    r"\b(future (?:work|research|studies)|further (?:work|research)|"
    r"should be (?:investigated|explored|studied))\b", re.I)
_OBJECTIVE_RE = re.compile(
    r"\b(this (?:study|paper|work|proposal) (?:aims?|objectives?|presents?|"
    r"proposes?|introduces?)|we (?:aim|propose|present|introduce|develop))\b",
    re.I)
_PROBLEM_RE = re.compile(
    r"\b(the problem|challenges?|lack of|limited (?:evidence|research)|"
    r"remains? (?:unclear|unknown))\b", re.I)

_SECTION_CLASSIFICATION = {
    "methodology": ("method", "method"),
    "dataset": ("method", "dataset"),
    "results": ("finding", "empirical_result"),
    "discussion": ("claim", "author_interpretation"),
    "conclusion": ("claim", "author_interpretation"),
    "limitations": ("unclassified", "limitation"),
    "abstract": ("unclassified", "background"),
    "introduction": ("unclassified", "background"),
    "related_work": ("unclassified", "background"),
}


def _heading_parts(line: str):
    """Return (specific_section, level, is_heading) for one line.

    Recognises '3.', '3.2.', roman-numeral and unnumbered headings;
    returns level None for unnumbered headings."""
    stripped = line.strip()
    if not stripped or len(stripped) > 70 or len(stripped.split()) > 9:
        return None, None, False
    if stripped.endswith((".", ",", ";", ":")):
        return None, None, False
    level = None
    text = stripped
    numbered = re.match(r"^(\d+(?:\.\d+)*)\.\s+(.*)$", stripped)
    roman = re.match(r"^([IVXLC]+)\.\s+(.*)$", stripped)
    if numbered:
        level = numbered.group(1).count(".") + 1
        text = numbered.group(2)
    elif roman:
        level = 1
        text = roman.group(2)
    lowered = text.lower().strip()
    for pattern, section in _SECTION_PATTERNS:
        if re.match(pattern, lowered):
            return section, level, True
    if numbered or roman:
        return None, level, True
    if (len(stripped) >= 4 and stripped.isupper()
            and sum(character.isalpha() for character in stripped) >= 3):
        return None, None, True
    return None, None, False


def _classify_candidate(text: str, section: str | None):
    """Return (kind, classification) for candidate evidence."""
    explicit = _classify_paragraph(text)
    if explicit != "unclassified":
        return explicit, explicit
    if _FUTURE_RE.search(text):
        return "unclassified", "future_work"
    if _OBJECTIVE_RE.search(text):
        return "claim", "objective"
    if section in ("introduction", "related_work") and _PROBLEM_RE.search(text):
        return "claim", "research_problem"
    return _SECTION_CLASSIFICATION.get(section,
                                        ("unclassified", "unclassified"))


def _candidate_texts(paragraph: str, section: str | None) -> list:
    """Sentence-level candidates for structured academic sections (exact
    substrings of the source), paragraph-level otherwise."""
    if section is None:
        return [paragraph]
    pieces = [part.strip() for part in
              re.split(r"(?<=[.!?])\s+", paragraph) if part.strip()]
    substantial = [part for part in pieces if len(part) >= 25]
    return substantial or [paragraph]


def extract_candidates(text: str, source_id: str, extracted_at: str,
                       method: str = METHOD) -> list:
    """Deterministic, document-structure-aware candidate evidence.

    PDF-style text wraps lines without blank lines, so paragraphs are
    rebuilt line-by-line: blank lines and section headings end a
    paragraph, and headings are recognised mid-page. Structured sections
    yield sentence-level candidates (exact source substrings, enclosing
    paragraph retained); unstructured text stays paragraph-level. Pages
    (form-feed separated), sections and locators are recorded; text is
    always verbatim."""
    items, section = [], None
    section_by_level = {}
    serial = 0

    for page_number, page in enumerate(text.split("\f"), start=1):
        paragraph_number = 0

        def flush():
            nonlocal serial, paragraph_number
            raw = "\n".join(current).strip()
            current.clear()
            if not raw or (raw.isdigit() and len(raw) <= 4):
                return
            paragraph_number += 1
            for candidate in _candidate_texts(raw, section):
                serial += 1
                kind, classification = _classify_candidate(candidate, section)
                items.append({
                    "source_id": source_id,
                    "extraction_id": f"EXT-{source_id[4:]}-{serial:03d}",
                    "kind": kind,
                    "classification": classification,
                    "section": section,
                    "page": page_number,
                    "paragraph": paragraph_number,
                    "text": candidate,
                    "paragraph_text": raw,
                    "text_kind": "verbatim_quote",
                    "extraction_method": method,
                    "extracted_at": extracted_at,
                    "review": REVIEW_PENDING,
                    "decision": None,
                    "reviewed_by": None,
                    "reviewed_at": None,
                })

        current = []
        for line in page.split("\n"):
            stripped = line.strip()
            if not stripped:
                flush()
                continue
            specific, level, is_heading = _heading_parts(stripped)
            if is_heading:
                flush()
                if specific is not None:
                    section = specific
                    section_by_level[level or 1] = specific
                    for deeper in [key for key in section_by_level
                                   if key > (level or 1)]:
                        del section_by_level[deeper]
                elif level and level > 1 and (level - 1) in section_by_level:
                    section = section_by_level[level - 1]
                elif level == 1:
                    section = None
                continue
            if section == "references":
                continue
            current.append(line)
        flush()
    return items


def extract_source(root: Path, source_id: str) -> dict:
    """Extract one imported source into an evidence record. Raises
    SourceError for unknown sources or protected records; every other
    outcome (including failure) becomes an explicit extraction record."""
    source = get_source(root, source_id)
    if source is None:
        raise SourceError(
            f"unknown source: '{source_id}' (run `researchforge sources "
            "list` to see imported sources)")

    existing = load_extraction(root, source_id)
    if existing is not None and existing.get("review") == REVIEW_APPROVED:
        raise ExtractionError(
            f"{source_id} already has an APPROVED extraction; it cannot "
            "be overwritten (human decision stands)")

    base = {
        "source_id": source_id,
        "original_filename": source["original_filename"],
        "source_sha256": source["sha256"],
        "source_evidence_status": source["verification_status"],
        "extracted_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "extraction_method": METHOD,
        "extraction_tool": None,
        "extraction_tool_version": None,
        "limitations": [],
        "pages": None,
        "model": None,  # no AI involved; never a fabricated model name
        "review": REVIEW_PENDING,
        "reviewed_by": None,
        "reviewed_at": None,
        "items": [],
        "error": None,
    }

    stored = root / source["stored_location"]
    if not stored.is_file():
        record = {**base, "outcome": OUTCOME_FAILED,
                  "error": "stored source file is missing"}
        _write_extraction(root, record)
        return record

    # integrity first: the record binds to verified original bytes
    digest, _size = file_digest(stored)
    if digest != source["sha256"]:
        record = {**base, "outcome": OUTCOME_FAILED,
                  "error": "stored file no longer matches the imported "
                           "checksum; original-file integrity violated"}
        _write_extraction(root, record)
        return record

    tool, version, limitations, pages = TEXT_TOOL, None, (), None
    if source["source_type"] == "pdf":
        executable = find_pdf_tool()
        if executable is None:
            record = {**base, "outcome": OUTCOME_UNSUPPORTED,
                      "error": PDF_MISSING_TOOL_MESSAGE}
            _write_extraction(root, record)
            return record
        tool, limitations = PDF_TOOL, PDF_LIMITATIONS
        try:
            version = pdf_tool_version(executable)
            text, pages = pdf_text(executable, stored)
        except ValueError as exc:
            record = {**base, "outcome": OUTCOME_FAILED,
                      "error": f"PDF text extraction failed: {exc}",
                      "extraction_tool": tool,
                      "extraction_tool_version": version,
                      "limitations": list(limitations)}
            _write_extraction(root, record)
            return record
        if not text.strip():
            record = {**base, "outcome": OUTCOME_EMPTY,
                      "error": PDF_SCANNED_MESSAGE,
                      "extraction_tool": tool,
                      "extraction_tool_version": version,
                      "limitations": list(limitations),
                      "pages": pages}
            _write_extraction(root, record)
            return record
    elif source["source_type"] == "docx":
        tool, version = DOCX_TOOL, platform.python_version()
        limitations = DOCX_LIMITATIONS
        try:
            text = docx_text(stored)
        except ValueError as exc:
            record = {**base, "outcome": OUTCOME_FAILED,
                      "error": str(exc),
                      "extraction_tool": tool,
                      "extraction_tool_version": version,
                      "limitations": list(limitations)}
            _write_extraction(root, record)
            return record
        if not text.strip():
            record = {**base, "outcome": OUTCOME_EMPTY,
                      "error": "DOCX contains no extractable text",
                      "extraction_tool": tool,
                      "extraction_tool_version": version,
                      "limitations": list(limitations)}
            _write_extraction(root, record)
            return record
    else:
        try:
            text = stored.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            record = {**base, "outcome": OUTCOME_FAILED,
                      "error": f"source is not readable UTF-8 text: {exc}"}
            _write_extraction(root, record)
            return record

    items = extract_candidates(text, source_id, base["extracted_at"])
    outcome = OUTCOME_COMPLETED if items else OUTCOME_EMPTY
    record = {**base, "outcome": outcome,
              "candidate_method": "local-deterministic-academic",
              "item_decisions": {"approved": 0, "rejected": 0,
                                 "pending": len(items)},
              "extraction_tool": tool,
              "extraction_tool_version": version,
              "limitations": list(limitations),
              "pages": pages,
              "error": None if items else
              "no text content found in source"}
    if items:
        record["items"] = items
    _write_extraction(root, record)
    return record


def _decide(root: Path, source_id: str, decision: str, reviewer: str) -> dict:
    if not reviewer or not reviewer.strip():
        raise ExtractionError("a reviewer name is required for the "
                              "human-review decision")
    try:
        with locking.exclusive_lock(locking.lock_path(root)):
            record = load_extraction(root, source_id)
            if record is None:
                raise ExtractionError(
                    f"no extraction record exists for {source_id}; run "
                    "extraction first")
            if record["outcome"] != OUTCOME_COMPLETED:
                raise ExtractionError(
                    f"only COMPLETED extractions can be decided; {source_id} "
                    f"is {record['outcome']}")
            if decision == REVIEW_APPROVED and \
                    record["review"] == REVIEW_REJECTED:
                raise ExtractionError(
                    f"{source_id} was REJECTED by a human reviewer; a "
                    "rejected extraction cannot be promoted to approved "
                    "(re-extract instead)")
            record["review"] = decision
            record["reviewed_by"] = reviewer.strip()
            record["reviewed_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds")
            _write_extraction(root, record)
            approvals.record_decision(root, "extraction", source_id, decision,
                                      reviewer.strip())
            return record
    except locking.LockTimeout as exc:
        raise ExtractionError(str(exc)) from exc


def approve_extraction(root: Path, source_id: str, reviewer: str) -> dict:
    """Explicit human approval. Never called automatically; evidence
    stays PENDING HUMAN REVIEW until this is invoked."""
    return _decide(root, source_id, REVIEW_APPROVED, reviewer)


def _item_summary(record: dict) -> dict:
    items = record.get("items", [])
    approved = sum(1 for item in items
                   if item.get("decision") == REVIEW_APPROVED)
    rejected = sum(1 for item in items
                   if item.get("decision") == REVIEW_REJECTED)
    return {"approved": approved, "rejected": rejected,
            "pending": len(items) - approved - rejected}


def decide_item(root: Path, source_id: str, reference, decision: str,
                reviewer: str) -> dict:
    """Approve or reject ONE evidence candidate. `reference` is the
    1-based candidate position or its extraction_id. Decisions are final;
    a whole-record APPROVED extraction is frozen. Serialized across
    threads and processes through the shared decision lock, and the
    record is re-read while holding it."""
    if not reviewer or not reviewer.strip():
        raise ExtractionError("a reviewer name is required for the "
                              "candidate decision")
    if decision not in (REVIEW_APPROVED, REVIEW_REJECTED):
        raise ExtractionError(
            f"decision must be {REVIEW_APPROVED} or {REVIEW_REJECTED}")
    try:
        with locking.exclusive_lock(locking.lock_path(root)):
            record = load_extraction(root, source_id)
            if record is None:
                raise ExtractionError(
                    f"no extraction record exists for {source_id}; run "
                    "extraction first")
            if record.get("review") == REVIEW_APPROVED:
                raise ExtractionError(
                    f"{source_id} is APPROVED as a whole record; "
                    "per-candidate decisions are frozen")
            items = record.get("items", [])
            item = None
            if isinstance(reference, int):
                if 1 <= reference <= len(items):
                    item = items[reference - 1]
            else:
                item = next((candidate for candidate in items
                             if candidate.get("extraction_id") == reference),
                            None)
            if item is None:
                raise ExtractionError(
                    f"candidate not found in {source_id}: {reference!r}")
            if item.get("decision") is not None:
                raise ExtractionError(
                    f"candidate {item.get('extraction_id')} was already "
                    f"decided ({item['decision']}); decisions are final")
            item["decision"] = decision
            item["reviewed_by"] = reviewer.strip()
            item["reviewed_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds")
            record["item_decisions"] = _item_summary(record)
            _write_extraction(root, record)
            return item
    except locking.LockTimeout as exc:
        raise ExtractionError(str(exc)) from exc


def reject_extraction(root: Path, source_id: str, reviewer: str) -> dict:
    return _decide(root, source_id, REVIEW_REJECTED, reviewer)
