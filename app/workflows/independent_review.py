"""Independent review workflow (RF-019).

Reviews existing artifacts WITHOUT modifying them: a section-draft version
(RF-018) is checked, and the report is written separately to
outputs/review_reports/<review_id>.json. No auto-approval of anything.

Two clearly separated finding sources:
1. `deterministic_findings` — verified mechanical checks:
   - provenance: every evidence_ref's source_id exists in the source
     manifest and its extraction_id matches the derived EXT- id;
   - citations: prose citations must exist in the approved evidence set
     (anything else is an invented citation -> HIGH);
   - unsupported statements: prose sentences without a citation;
   - draft integrity: the reviewed version must be a completed DRAFT.
2. `ai_findings` — optional, via RF-014's run_generation(); each finding
   is labelled origin="AI-GENERATED", verified=false and is NEVER merged
   into the deterministic list.

Also recorded separately: `external_reviews` (manual paste of Gemini /
Perplexity / ChatGPT critique per RF-010's import philosophy — stored
verbatim, never auto-accepted) and `contradiction_references` (conflicts
already recorded by an approved synthesis/gap-analysis AI interpretation;
quoted with their origin, not re-verified here).

Severity rules (documented, applied consistently):
- HIGH     missing/invalid citation or provenance problem, or the artifact
           is not a completed draft — the draft cannot be trusted as-is;
- MEDIUM   an individual sentence without evidence support, or a
           provenance detail that needs correction;
- LOW      advisory/housekeeping issue;
- INFO     context or governance note (e.g. version still pending review);
- UNSPECIFIED  AI reviewer supplied no valid severity (never invented).

Findings are never auto-resolved: each starts undecided and only
decide_finding() — explicit, reviewer-named — can accept or reject it.
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from app.core.sources import SourceError, load_manifest
from app.core.usage_log import run_generation
from app.providers.contracts import ProviderRequest
from app.providers.errors import ProviderError
from app.workflows.literature_synthesis import (REVIEW_APPROVED,
                                                REVIEW_PENDING,
                                                load_draft as load_synthesis,
                                                _extraction_id)
from app.workflows.research_gap_analysis import load_analysis
from app.workflows.section_drafting import (CITATION_RE, STATUS_DRAFT,
                                            load_section)

REPORTS_DIR = "outputs/review_reports"
TASK = "academic_review"
SEVERITIES = ("HIGH", "MEDIUM", "LOW", "INFO", "UNSPECIFIED")
CATEGORIES = ("factual/evidence", "methodological", "structural",
              "writing-quality")
PLATFORMS = ("gemini", "perplexity", "chatgpt", "other")
CONTEXT_DOCS = ("research_questions.md", "objectives.md", "methodology.md")
DECISION_ACCEPTED = "ACCEPTED"
DECISION_REJECTED = "REJECTED"


class ReviewError(SourceError):
    """Review refused/invalid (reuses the RF-010 error hierarchy)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(data) -> str:
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_review_id(section: str) -> str:
    nonce = hashlib.sha256(f"{section}:{time.time_ns()}".encode()).hexdigest()
    return f"RVW-{nonce[:12]}"


def review_path(root: Path, review_id: str) -> Path:
    return root / REPORTS_DIR / f"{review_id}.json"


def _save(root: Path, review: dict) -> None:
    path = review_path(root, review["review_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(review, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def load_review(root: Path, review_id: str) -> dict | None:
    path = review_path(root, review_id)
    if not path.is_file():
        return None
    try:
        review = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewError(f"review '{review_id}' unreadable: {exc}") from exc
    if not isinstance(review, dict) or review.get("review_id") != review_id:
        raise ReviewError(f"review '{review_id}' malformed")
    return review


def _finding(prefix, number, category, severity, message, rationale,
             verified=True):
    if severity not in SEVERITIES:
        raise ReviewError(f"invalid severity: {severity!r}")
    if category not in CATEGORIES:
        raise ReviewError(f"invalid category: {category!r}")
    return {"finding_id": f"{prefix}{number}", "origin": "DETERMINISTIC"
            if verified else "AI-GENERATED", "verified": verified,
            "category": category, "severity": severity,
            "message": message, "rationale": rationale,
            "decision": None, "decided_by": None, "decided_at": None}


def deterministic_checks(root: Path, version: dict) -> list:
    findings, n = [], 0

    def add(*args, **kwargs):
        nonlocal n
        n += 1
        findings.append(_finding("F", n, *args, **kwargs))

    if version.get("status") != STATUS_DRAFT or not version.get("prose"):
        add("factual/evidence", "HIGH",
            "reviewed version is not a completed draft",
            f"status is {version.get('status')!r}; nothing can be verified, "
            "so the draft must be regenerated before review")
        return findings

    manifest = load_manifest(root)
    known_sources = manifest["sources"]
    evidence_ids = set()
    for ref in version.get("evidence_refs", []):
        source_id = ref.get("source_id")
        expected = _extraction_id(source_id) if source_id else None
        if source_id not in known_sources:
            add("factual/evidence", "HIGH",
                f"evidence references unknown source {source_id!r}",
                "the cited source is absent from the source manifest, so "
                "its provenance cannot be verified")
        elif ref.get("extraction_id") != expected:
            add("factual/evidence", "MEDIUM",
                f"extraction id mismatch for {source_id}",
                f"recorded {ref.get('extraction_id')!r}, expected "
                f"{expected!r} (extraction is 1:1 with its source)")
        else:
            evidence_ids.add(source_id)

    prose = version.get("prose") or ""
    cited = set(CITATION_RE.findall(prose))
    invalid = sorted(c for c in cited if c not in evidence_ids)
    for citation in invalid:
        add("factual/evidence", "HIGH",
            f"cited source {citation} is not in the approved evidence set",
            "the draft cites a source that was never supplied as approved "
            "evidence; treat as an invented citation until verified")
    if prose and not cited:
        add("factual/evidence", "HIGH",
            "prose contains no citations at all",
            "no statement can be traced to approved evidence")

    unsupported = version.get("unsupported_statements") or []
    for sentence in unsupported:
        add("factual/evidence", "MEDIUM",
            f"unsupported statement: {sentence[:160]}",
            "sentence carries no [SRC-...] citation, so it is not "
            "evidence-backed and needs citation, verification or removal")

    if version.get("review") == REVIEW_PENDING:
        add("structural", "INFO",
            f"draft version {version.get('version')} is still PENDING "
            "HUMAN REVIEW",
            "review findings apply to an unapproved draft; approval is a "
            "separate human decision")
    return findings


def _contradiction_references(root: Path) -> list:
    refs = []
    synthesis = load_synthesis(root)
    if synthesis and synthesis.get("review") == REVIEW_APPROVED:
        interpretation = synthesis.get("interpretation") or {}
        if interpretation.get("status") == "COMPLETED":
            for text in interpretation.get("contradictions", []):
                refs.append({"source": "literature_synthesis",
                             "origin": "AI-GENERATED", "text": text})
    analysis = load_analysis(root)
    if analysis and analysis.get("review") == REVIEW_APPROVED:
        interpretation = analysis.get("interpretation") or {}
        if interpretation.get("status") == "COMPLETED":
            for text in interpretation.get("conflicting_findings", []):
                refs.append({"source": "research_gap_analysis",
                             "origin": "AI-GENERATED", "text": text})
    return refs


def _context_docs(root: Path) -> tuple:
    """Approved research documents for the consistency check (RF-019 #5).
    Read-only; a missing document is reported, never invented."""
    docs, missing = {}, []
    for name in CONTEXT_DOCS:
        path = root / "research" / name
        if not path.is_file():
            missing.append(name)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            missing.append(name)
            continue
        docs[name] = {"sha256": hashlib.sha256(
            text.encode("utf-8")).hexdigest(), "text": text}
    return docs, missing


def _ai_prompt(version: dict, context_docs: dict) -> str:
    lines = ["Independently review this manuscript section draft.",
             "Report findings as JSON: {\"findings\": [{category,",
             "severity, message, rationale}]} where category is one of",
             ", ".join(CATEGORIES), "and severity one of HIGH, MEDIUM,",
             "LOW, INFO. Do not invent evidence or citations; cite the",
             "draft's own statements. Do not resolve or approve anything.",
             ""]
    lines += ["RESEARCH QUESTIONS/OBJECTIVES/METHODOLOGY (approved):"]
    for name, doc in context_docs.items():
        lines.append(f"--- {name} ---")
        lines.append(doc["text"][:4000])
    lines += ["", "APPROVED EVIDENCE AVAILABLE:"]
    for ref in version.get("evidence_refs", []):
        lines.append(f"[{ref.get('source_id')}] {ref.get('text')}")
    lines += ["", "DRAFT PROSE:", version.get("prose") or ""]
    return "\n".join(lines)


def _ai_review(root: Path, version: dict, context_docs: dict, provider,
               task: str) -> dict:
    try:
        response = run_generation(
            provider, ProviderRequest(
                prompt=_ai_prompt(version, context_docs),
                structured={"format": "json"}), task, root)
    except ProviderError as error:
        return {"status": "FAILED", "origin": "AI-GENERATED",
                "provider": provider.provider_id, "model": None,
                "error": type(error).__name__, "findings": []}
    try:
        parsed = json.loads(response.content)
        raw = parsed.get("findings") if isinstance(parsed, dict) else None
        if not isinstance(raw, list):
            raise ValueError("findings must be a list")
    except (ValueError, TypeError):
        return {"status": "FAILED", "origin": "AI-GENERATED",
                "provider": provider.provider_id, "model": response.model,
                "error": "unparseable provider response", "findings": []}
    findings = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        findings.append(_finding(
            "A", index,
            item.get("category") if item.get("category") in CATEGORIES
            else "writing-quality",
            item.get("severity") if item.get("severity") in SEVERITIES
            else "UNSPECIFIED",
            str(item.get("message") or "AI review finding"),
            str(item.get("rationale") or
                "no rationale supplied by the AI reviewer"),
            verified=False))
    return {"status": "COMPLETED", "origin": "AI-GENERATED",
            "provider": response.provider, "model": response.model,
            "error": None, "findings": findings}


def review_section(root: Path, section: str, version: int | None = None,
                   provider=None, task: str = TASK,
                   reviewer_notes: str | None = None) -> dict:
    """Review one section-draft version without modifying it. Writes a
    separate report; no automatic approval of anything."""
    data = load_section(root, section)
    if data is None or not data["versions"]:
        raise ReviewError(f"no draft exists for section '{section}'")
    target = data["versions"][-1] if version is None else next(
        (v for v in data["versions"] if v["version"] == version), None)
    if target is None:
        raise ReviewError(f"version {version} of '{section}' does not exist")

    context_docs, missing_docs = _context_docs(root)
    findings = deterministic_checks(root, target)
    if missing_docs:
        findings.append(_finding(
            "F", len(findings) + 1, "methodological", "INFO",
            "research context documents missing: " + ", ".join(missing_docs),
            "consistency with approved research questions/objectives/"
            "methodology cannot be checked deterministically without them"))
    report = {
        "review_id": _new_review_id(section),
        "created_at": _now(),
        "origin": "INDEPENDENT REVIEW",
        "review": REVIEW_PENDING,
        "task": task,
        "reviewed_artifact": {
            "kind": "section_draft",
            "path": f"outputs/sections/{section}.json",
            "section": section,
            "version": target["version"],
            "sha256": _fingerprint(target),
            "draft_origin": target.get("origin"),
            "draft_review": target.get("review"),
        },
        "context_docs": {name: doc["sha256"]
                         for name, doc in context_docs.items()},
        "deterministic_findings": findings,
        "ai_review": {"status": "NOT REQUESTED", "origin": None,
                      "provider": None, "model": None, "error": None,
                      "findings": []},
        "contradiction_references": _contradiction_references(root),
        "external_reviews": [],
        "reviewer_notes": reviewer_notes,
    }
    if provider is not None:
        report["ai_review"] = _ai_review(root, target, context_docs,
                                         provider, task)
    _save(root, report)
    return report


def add_external_review(root: Path, review_id: str, text: str,
                        platform: str) -> dict:
    """Record a manually pasted external critique (Gemini/Perplexity/
    ChatGPT). Stored separately and never auto-accepted."""
    if platform not in PLATFORMS:
        raise ReviewError(f"unknown platform {platform!r} (expected one of "
                          f"{', '.join(PLATFORMS)})")
    if not isinstance(text, str) or not text.strip():
        raise ReviewError("external review text must not be empty")
    review = load_review(root, review_id)
    if review is None:
        raise ReviewError(f"review '{review_id}' does not exist")
    review["external_reviews"].append({
        "origin": "EXTERNAL MANUAL IMPORT",
        "platform": platform,
        "recorded_at": _now(),
        "text": text,
        "decision": None,
        "decided_by": None,
        "decided_at": None,
    })
    _save(root, review)
    return review


def _find(review: dict, finding_id: str):
    pool = (review["deterministic_findings"]
            + review["ai_review"]["findings"])
    for item in pool:
        if item["finding_id"] == finding_id:
            return item
    return None


def decide_finding(root: Path, review_id: str, finding_id: str,
                   decision: str, reviewer: str) -> dict:
    """Explicit human decision on ONE finding. Findings are never resolved
    automatically, and a decided finding is final."""
    if not reviewer or not reviewer.strip():
        raise ReviewError("a reviewer name is required for a decision")
    if decision not in (DECISION_ACCEPTED, DECISION_REJECTED):
        raise ReviewError(f"decision must be {DECISION_ACCEPTED} or "
                          f"{DECISION_REJECTED}")
    review = load_review(root, review_id)
    if review is None:
        raise ReviewError(f"review '{review_id}' does not exist")
    finding = _find(review, finding_id)
    if finding is None:
        raise ReviewError(f"finding '{finding_id}' is not part of review "
                          f"'{review_id}'")
    if finding["decision"] is not None:
        raise ReviewError(f"finding '{finding_id}' was already decided "
                          f"({finding['decision']}); decisions are final")
    finding["decision"] = decision
    finding["decided_by"] = reviewer.strip()
    finding["decided_at"] = _now()
    _save(root, review)
    return finding
