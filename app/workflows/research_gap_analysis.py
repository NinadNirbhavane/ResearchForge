"""Research gap analysis workflow (RF-017).

Identifies CANDIDATE gaps from an APPROVED RF-016 synthesis draft. Output
is a reviewable draft (`outputs/drafts/research_gap_analysis_draft.json`),
never an accepted research direction by itself.

Method (deterministic; no AI required):
- Eligibility: the synthesis draft must exist and be APPROVED (human
  decision per RF-016). Evidence is read from that draft's `evidence`
  section, which by construction contains only APPROVED extraction data —
  so every gap entry carries the source_id + extraction_id it came from.
- `directly_evidenced_limitations`: evidence items explicitly labelled
  "Limitation:" / "Limitations:" — verbatim, classified directly.
- `inferred_patterns`: the SAME text (normalized whitespace/case)
  reported by >= 2 independent sources. Basis is stated explicitly as a
  cross-source pattern — not a causal or novelty claim.
- `potential_research_opportunities`: only items explicitly labelled
  "Gap:" / "Research gap:" / "Opportunity:" in the approved evidence.
  These are author-identified candidates; they are NOT vetted, and when
  none exist the section states that explicitly instead of guessing.
- `unverified_novelty_hypotheses`: always empty in deterministic mode.
  This workflow NEVER claims a gap is globally novel just because the
  imported literature does not address it; the draft carries a fixed
  novelty disclaimer saying exactly that.
- Insufficient coverage: fewer than MINIMUM_SOURCES approved evidence
  sources sets `insufficient_coverage` with an explicit reason.

Evidence vs interpretation: `candidate_gaps` holds verbatim evidence only.
`interpretation` is separate, `NOT GENERATED` by default, and labelled
AI-GENERATED + PENDING HUMAN REVIEW if add_ai_interpretation() is
explicitly invoked through RF-014's run_generation() (usage/error logging
included; provider failures are marked FAILED, never fabricated).

Governance: draft starts review="PENDING HUMAN REVIEW";
approve_gap_analysis() requires a reviewer and an APPROVED draft cannot
be overwritten.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.core import approvals
from app.core.sources import SourceError
from app.core.usage_log import run_generation
from app.providers.contracts import ProviderRequest
from app.providers.errors import ProviderError
from app.workflows.literature_synthesis import (MINIMUM_SOURCES,
                                                REVIEW_APPROVED,
                                                REVIEW_PENDING,
                                                load_draft as load_synthesis)

DRAFT_RELPATH = "outputs/drafts/research_gap_analysis_draft.json"
TASK = "research_gap_analysis"
LIMITATION_LABELS = {"limitation", "limitations"}
GAP_LABELS = {"gap", "gaps", "research gap", "research gaps",
              "opportunity", "opportunities", "future research"}

NOVELTY_DISCLAIMER = (
    "A gap in the imported literature is NOT evidence of global novelty. "
    "Novelty can only be established against the wider literature through "
    "an explicit external search and human judgement; this workflow makes "
    "no novelty claim.")


class GapAnalysisError(SourceError):
    """Gap analysis refused/invalid (reuses the RF-010 error hierarchy)."""


def analysis_path(root: Path) -> Path:
    return root / DRAFT_RELPATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _label(text: str) -> str:
    return text.split(":", 1)[0].strip().lower()


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


def _iter_evidence(evidence: dict):
    for kind in ("findings", "claims", "methods", "other_evidence"):
        for entry in evidence.get(kind, []):
            yield entry


def _load_approved_synthesis(root: Path) -> dict:
    draft = load_synthesis(root)
    if draft is None:
        raise GapAnalysisError(
            "no synthesis draft found; run literature synthesis (RF-016) "
            "and approve it before gap analysis")
    if draft.get("review") != REVIEW_APPROVED:
        raise GapAnalysisError(
            f"synthesis draft review is {draft.get('review')!r}; only an "
            "APPROVED synthesis may feed gap analysis")
    return draft


def build_analysis(root: Path) -> dict:
    """Deterministic candidate-gap draft from an APPROVED synthesis."""
    synthesis = _load_approved_synthesis(root)
    evidence = synthesis["evidence"]
    entries = list(_iter_evidence(evidence))

    limitations = [dict(e) for e in entries
                   if _label(e["text"]) in LIMITATION_LABELS
                   or e.get("classification") == "limitation"]
    gaps = [dict(e) for e in entries
            if _label(e["text"]) in GAP_LABELS
            or e.get("classification") == "future_work"]

    patterns = {}
    for entry in entries:
        patterns.setdefault(_normalized(entry["text"]), []).append(entry)
    inferred = []
    for group in patterns.values():
        sources = sorted({e["source_id"] for e in group})
        if len(sources) >= 2:
            inferred.append({
                "text": group[0]["text"],
                "basis": f"identical finding reported by {len(sources)} "
                         "independent sources (cross-source pattern, not a "
                         "causal or novelty claim)",
                "source_ids": sources,
                "extraction_ids": sorted({e["extraction_id"] for e in group}),
            })
    inferred.sort(key=lambda p: p["text"])

    source_ids = sorted({e["source_id"] for e in entries})
    insufficient = len(source_ids) < MINIMUM_SOURCES
    return {
        "document": "research_gap_analysis",
        "version": 1,
        "status": "DRAFT",
        "review": REVIEW_PENDING,
        "reviewed_by": None,
        "reviewed_at": None,
        "generated_at": _now(),
        "novelty_disclaimer": NOVELTY_DISCLAIMER,
        "source_synthesis": {
            "path": "outputs/drafts/literature_review_draft.json",
            "review": synthesis["review"],
            "reviewed_by": synthesis.get("reviewed_by"),
            "generated_at": synthesis.get("generated_at"),
        },
        "eligibility": {
            "evidence_sources": source_ids,
            "evidence_source_count": len(source_ids),
            "minimum_sources": MINIMUM_SOURCES,
            "insufficient_coverage": insufficient,
            "coverage_reason": None if not insufficient else
            (f"{len(source_ids)} approved evidence source(s); at least "
             f"{MINIMUM_SOURCES} are required for any gap inference — "
             "candidate gaps below are under-supported"),
        },
        "candidate_gaps": {   # verbatim evidence only, never AI text
            "directly_evidenced_limitations": limitations,
            "inferred_patterns": inferred,
            "potential_research_opportunities": gaps,
            "unverified_novelty_hypotheses": [],
            "opportunities_note": None if gaps else
            ("No explicitly labelled gaps/opportunities exist in the "
             "approved evidence; none were inferred (that requires "
             "interpretation, human or AI, and is never guessed here)."),
            "novelty_note": ("Novelty cannot be established from the "
                             "imported literature alone; this section stays "
                             "empty by design. Any hypothesis must be "
                             "verified against the wider literature first."),
        },
        "interpretation": {   # separate by design; never auto-approved
            "status": "NOT GENERATED",
            "origin": None,
            "provider": None,
            "model": None,
            "conflicting_findings": [],
            "underexplored_areas": [],
            "potential_opportunities": [],
            "novelty_hypotheses": [],   # always UNVERIFIED, never claims
            "review": REVIEW_PENDING,
            "error": None,
            "note": "Deterministic analyses contain no interpretation. "
                    "Conflicts, underexplored areas and opportunities "
                    "require explicit AI interpretation or human review.",
        },
    }


def _write(root: Path, analysis: dict) -> None:
    path = analysis_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(analysis, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def load_analysis(root: Path) -> dict | None:
    path = analysis_path(root)
    if not path.is_file():
        return None
    try:
        analysis = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GapAnalysisError(f"gap analysis draft unreadable: {exc}") from exc
    if not isinstance(analysis, dict) or "candidate_gaps" not in analysis:
        raise GapAnalysisError("gap analysis draft malformed")
    return analysis


def save_analysis(root: Path, analysis: dict) -> dict:
    existing = load_analysis(root)
    if existing is not None and existing.get("review") == REVIEW_APPROVED:
        raise GapAnalysisError(
            "the saved gap analysis is APPROVED; it cannot be overwritten "
            "(human decision stands)")
    _write(root, analysis)
    return analysis


def analyze_gaps(root: Path) -> dict:
    """Build and save a fresh deterministic candidate-gap draft."""
    return save_analysis(root, build_analysis(root))


def _interpretation_prompt(analysis: dict) -> str:
    lines = ["You are assisting a research gap analysis. Below are",
             "verbatim evidence excerpts from an APPROVED literature",
             "synthesis. Identify ONLY: conflicting findings between",
             "sources, underexplored areas, potential research",
             "opportunities, and unverified novelty hypotheses. Cite",
             "sources by their bracketed IDs. Do not invent papers,",
             "citations, findings or novelty claims; absence of coverage",
             "is not novelty. If evidence is insufficient, say so.", ""]
    evidence = analysis["candidate_gaps"]
    for key in ("directly_evidenced_limitations", "inferred_patterns",
                "potential_research_opportunities"):
        for entry in evidence.get(key, []):
            ids = (entry.get("source_ids")
                   or [entry.get("source_id")])
            lines.append(f"[{', '.join(ids)}] {entry['text']}")
    return "\n".join(lines)


def add_ai_interpretation(root: Path, analysis: dict, provider,
                          task: str = TASK) -> dict:
    """Explicitly request AI interpretation of the approved evidence via
    RF-014's run_generation(). Stored ONLY in the separate `interpretation`
    section, labelled AI-GENERATED + PENDING HUMAN REVIEW. Novelty output
    stays an UNVERIFIED hypothesis list — never a claim."""
    interpretation = analysis["interpretation"]
    try:
        response = run_generation(
            provider, ProviderRequest(
                prompt=_interpretation_prompt(analysis),
                structured={"format": "json"}), task, root)
    except ProviderError as error:
        interpretation.update({
            "status": "FAILED", "origin": "AI-GENERATED",
            "provider": provider.provider_id, "model": None,
            "error": type(error).__name__, "review": REVIEW_PENDING,
        })
        return analysis
    try:
        parsed = json.loads(response.content)
        if not isinstance(parsed, dict):
            raise ValueError("interpretation is not a JSON object")
    except (ValueError, TypeError):
        interpretation.update({
            "status": "FAILED", "origin": "AI-GENERATED",
            "provider": provider.provider_id, "model": response.model,
            "error": "unparseable provider response",
            "review": REVIEW_PENDING,
        })
        return analysis
    interpretation.update({
        "status": "COMPLETED",
        "origin": "AI-GENERATED",
        "provider": response.provider,
        "model": response.model,
        "error": None,
        "review": REVIEW_PENDING,
    })
    for key in ("conflicting_findings", "underexplored_areas",
                "potential_opportunities", "novelty_hypotheses"):
        value = parsed.get(key)
        interpretation[key] = value if isinstance(value, list) else []
    return analysis


def approve_gap_analysis(root: Path, reviewer: str) -> dict:
    """Explicit human approval. Never automatic; a gap only becomes an
    accepted research direction through this call."""
    if not reviewer or not reviewer.strip():
        raise GapAnalysisError("a reviewer name is required for approval")
    analysis = load_analysis(root)
    if analysis is None:
        raise GapAnalysisError("no gap analysis draft exists; run the "
                               "analysis first")
    analysis["review"] = REVIEW_APPROVED
    analysis["reviewed_by"] = reviewer.strip()
    analysis["reviewed_at"] = _now()
    _write(root, analysis)
    approvals.record_decision(root, "gap_analysis", "research_gap_analysis",
                              "APPROVED", reviewer.strip())
    return analysis
