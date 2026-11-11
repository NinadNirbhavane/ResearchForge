"""Controlled manuscript section drafting (RF-018).

Drafts ONE manuscript section at a time from APPROVED research material.
Never writes into the final manuscript; never auto-approves.

Inputs (approved only):
- the APPROVED RF-016 synthesis draft (required) — evidence entries keep
  their source_id + extraction_id, so every cited statement stays
  traceable;
- the APPROVED RF-017 gap analysis (optional) — its candidate gaps are
  included when approved, otherwise excluded with an explicit note;
- explicit researcher-provided context (clearly labelled, never treated
  as evidence).

Generation: RF-014's run_generation() (provider constructed by the caller
via RF-013 build_provider in real use). Drafts are labelled AI-GENERATED
and PENDING HUMAN REVIEW; the model is instructed to cite only the given
[SRC-...] ids and to invent nothing (no references, authors, DOIs,
datasets, numerical results or claimed experiments).

Post-generation audit (deterministic, independent of the model):
- `citations.valid` lists cited source ids that exist in the evidence set;
- `citations.invalid` lists any citation the model invented;
- `unsupported_statements` lists prose sentences without a [SRC-...]
  citation — explicitly marked, never silently presented as established.

Storage: outputs/sections/<section>.json holds an append-only `versions`
list. Existing versions are never mutated except by approving them; an
approved version is immutable (re-approval refused, new drafts append a
new version). Provider failures append a FAILED version for the audit
trail and then re-raise.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from app.core import approvals
from app.core.sources import SourceError
from app.core.usage_log import run_generation
from app.providers.contracts import ProviderRequest
from app.providers.errors import ProviderError
from app.workflows.literature_synthesis import (REVIEW_APPROVED,
                                                REVIEW_PENDING,
                                                load_draft as load_synthesis)
from app.workflows.research_gap_analysis import load_analysis

SECTIONS = {
    "introduction": "Introduce the research problem and objectives.",
    "literature_review": "Synthesize the approved literature evidence.",
    "methodology": "Describe the research design as decided by the "
                   "researcher; do not claim experiments already performed.",
    "discussion": "Interpret approved findings against the research "
                  "questions.",
    "conclusion": "State conclusions supported by approved evidence only.",
}
SECTIONS_DIR = "outputs/sections"
TASK = "section_drafting"
CITATION_RE = re.compile(r"\[(SRC-[0-9a-f]{12})\]")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
STATUS_DRAFT = "DRAFT"
UNSUPPORTED_POLICY = (
    "Sentences without a [SRC-...] citation are NOT evidence-backed. They "
    "must be verified, cited or removed by the human reviewer before "
    "approval; the workflow never presents them as established facts.")


class DraftingError(SourceError):
    """Drafting refused/invalid (reuses the RF-010 error hierarchy)."""


def section_path(root: Path, section: str) -> Path:
    return root / SECTIONS_DIR / f"{section}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_section(section: str) -> None:
    if section not in SECTIONS:
        raise DraftingError(
            f"unknown section '{section}' (supported: "
            f"{', '.join(sorted(SECTIONS))}; draft one section at a time)")


def load_section(root: Path, section: str) -> dict | None:
    _check_section(section)
    path = section_path(root, section)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DraftingError(f"section record '{section}' unreadable: "
                            f"{exc}") from exc
    if not isinstance(data, dict) or data.get("section") != section or \
            not isinstance(data.get("versions"), list):
        raise DraftingError(f"section record '{section}' malformed")
    return data


def _save(root: Path, section: str, data: dict) -> None:
    path = section_path(root, section)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def _approved_synthesis(root: Path) -> dict:
    draft = load_synthesis(root)
    if draft is None:
        raise DraftingError(
            "no synthesis draft found; run and approve literature synthesis "
            "(RF-016) before drafting")
    if draft.get("review") != REVIEW_APPROVED:
        raise DraftingError(
            f"synthesis draft review is {draft.get('review')!r}; only an "
            "APPROVED synthesis may feed section drafting")
    return draft


def _evidence(root: Path):
    """Approved evidence entries (verbatim, with ids) + gap exclusions."""
    synthesis = _approved_synthesis(root)
    evidence, notes = [], []
    seen = set()
    for kind in ("findings", "claims", "methods", "other_evidence"):
        for entry in synthesis["evidence"].get(kind, []):
            key = (entry.get("source_id"), entry.get("extraction_id"),
                   entry.get("text"))
            if key in seen:
                continue
            seen.add(key)
            evidence.append({"kind": kind.rstrip("s"), **entry})
    analysis = load_analysis(root)
    if analysis is None:
        notes.append("no gap analysis draft exists; drafting uses synthesis "
                     "evidence only")
    elif analysis.get("review") != REVIEW_APPROVED:
        notes.append(f"gap analysis review is {analysis.get('review')!r}; "
                     "its candidate gaps are excluded")
    else:
        for kind, entries in analysis["candidate_gaps"].items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                ids = entry.get("source_ids") or [entry.get("source_id")]
                key = (tuple(ids), entry.get("text"))
                if key in seen:
                    continue
                seen.add(key)
                evidence.append({"kind": kind.rstrip("s"),
                                 "text": entry.get("text"),
                                 "source_ids": ids,
                                 "extraction_ids":
                                     entry.get("extraction_ids") or []})
    return evidence, notes, synthesis


def _prompt(section: str, evidence: list, researcher_context: str | None):
    lines = [f"Draft the '{section}' section of a research paper.",
             SECTIONS[section], "",
             "Rules: cite approved evidence ONLY using its bracketed",
             "source id (e.g. [SRC-0123456789ab]). Never invent",
             "citations, references, authors, DOIs, datasets, findings or",
             "numerical results, and never claim experiments were",
             "performed. Omit anything the evidence does not support.", ""]
    if researcher_context:
        lines += ["RESEARCHER-PROVIDED CONTEXT (not evidence; may inform "
                  "framing, must not be cited):",
                  researcher_context, ""]
    lines.append("APPROVED EVIDENCE:")
    for entry in evidence:
        ids = entry.get("source_ids") or [entry.get("source_id")]
        lines.append(f"[{', '.join(ids)}] {entry['text']}")
    return "\n".join(lines)


def _audit(prose: str, evidence: list) -> dict:
    known = set()
    for entry in evidence:
        known.update(entry.get("source_ids") or [entry.get("source_id")])
    cited = CITATION_RE.findall(prose)
    unsupported = []
    for paragraph in prose.split("\n\n"):
        for sentence in SENTENCE_SPLIT.split(paragraph.strip()):
            sentence = sentence.strip()
            if sentence and not CITATION_RE.search(sentence):
                unsupported.append(sentence)
    return {
        "citations": {
            "valid": sorted({c for c in cited if c in known}),
            "invalid": sorted({c for c in cited if c not in known}),
        },
        "unsupported_statements": unsupported,
        "unsupported_policy": UNSUPPORTED_POLICY,
    }


def draft_section(root: Path, section: str, provider,
                  researcher_context: str | None = None,
                  task: str = TASK) -> dict:
    """Generate one section draft version. Provider failures append a
    FAILED version (audit trail) and re-raise."""
    _check_section(section)
    evidence, notes, synthesis = _evidence(root)
    data = load_section(root, section) or {"section": section, "versions": []}
    version = len(data["versions"]) + 1
    base = {
        "version": version,
        "created_at": _now(),
        "origin": "AI-GENERATED",
        "review": REVIEW_PENDING,
        "reviewed_by": None,
        "reviewed_at": None,
        "task": task,
        "provider": getattr(provider, "provider_id", None),
        "model": None,
        "prose": None,
        "evidence_refs": evidence,      # kept apart from generated prose
        "researcher_context": researcher_context,
        "researcher_context_label": ("RESEARCHER-PROVIDED CONTEXT — not "
                                     "evidence") if researcher_context else None,
        "citations": {"valid": [], "invalid": []},
        "unsupported_statements": [],
        "unsupported_policy": UNSUPPORTED_POLICY,
        "evidence_notes": notes,
        "source_synthesis": {"reviewed_by": synthesis.get("reviewed_by"),
                             "generated_at": synthesis.get("generated_at")},
        "usage": None,
        "error": None,
    }
    try:
        response = run_generation(
            provider, ProviderRequest(
                prompt=_prompt(section, evidence, researcher_context),
                structured={"format": "json"}), task, root)
    except ProviderError as error:
        base.update({"status": "FAILED", "error": type(error).__name__})
        data["versions"].append(base)
        _save(root, section, data)
        raise
    try:
        parsed = json.loads(response.content)
        prose = parsed.get("prose") if isinstance(parsed, dict) else None
        if not isinstance(prose, str) or not prose.strip():
            raise ValueError("response has no usable 'prose' field")
    except (ValueError, TypeError) as exc:
        base.update({"status": "FAILED",
                     "error": f"unusable provider output: {exc}"})
        data["versions"].append(base)
        _save(root, section, data)
        raise DraftingError(base["error"]) from exc
    usage = response.usage
    base.update({
        "status": STATUS_DRAFT,
        "model": response.model,
        "prose": prose,                 # generated prose, always labelled
        "usage": {"input_tokens": usage.input_tokens,
                  "output_tokens": usage.output_tokens,
                  "total_tokens": usage.total_tokens},
        **_audit(prose, evidence),
    })
    data["versions"].append(base)
    _save(root, section, data)
    return base


def approve_section(root: Path, section: str, reviewer: str,
                    version: int | None = None) -> dict:
    """Explicit human approval of ONE draft version. Never automatic;
    approved versions are immutable afterwards."""
    if not reviewer or not reviewer.strip():
        raise DraftingError("a reviewer name is required for approval")
    data = load_section(root, section)
    if data is None or not data["versions"]:
        raise DraftingError(f"no draft exists for section '{section}'")
    target = data["versions"][-1] if version is None else next(
        (v for v in data["versions"] if v["version"] == version), None)
    if target is None:
        raise DraftingError(f"version {version} of '{section}' does not exist")
    if target["status"] != STATUS_DRAFT or not target.get("prose"):
        raise DraftingError(
            f"version {target['version']} is not a completed draft "
            f"(status {target['status']!r}); it cannot be approved")
    if target["review"] == REVIEW_APPROVED:
        raise DraftingError(
            f"version {target['version']} of '{section}' is already "
            "APPROVED and immutable")
    target["review"] = "APPROVED"
    target["reviewed_by"] = reviewer.strip()
    target["reviewed_at"] = _now()
    _save(root, section, data)
    approvals.record_decision(root, "section", section, "APPROVED",
                              reviewer.strip(), version=target["version"])
    return target
