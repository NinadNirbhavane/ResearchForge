"""Central approval ledger and fingerprint verification (RF-020).

Existing workflows keep their single approval entry points (RF-015
approve_extraction, RF-016 approve_synthesis, RF-017
approve_gap_analysis, RF-018 approve_section). This module does not
duplicate that logic — approval decisions are NOT changed here; it adds
the missing cross-artifact audit trail:

- an append-only JSONL ledger at outputs/approvals/approval_ledger.jsonl
  recording reviewer, decision, timestamp, artifact identity and a
  sha256 fingerprint of the approved artifact content;
- verification that recomputes fingerprints, so a change to an approved
  artifact is detectable (tamper evidence), never silently accepted.

Fingerprints bind the approval to the artifact's exact content. For
section drafts the fingerprint covers the approved VERSION only, so a
later version cannot invalidate an earlier approval.

Ledger appends are best-effort: an unwritable ledger returns False and
never blocks or reverses a human decision (graceful degradation, RF-014
convention). Reading skips malformed lines rather than failing.

No new dependencies; no network access anywhere in this module.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

LEDGER_RELPATH = "outputs/approvals/approval_ledger.jsonl"
KINDS = ("extraction", "synthesis", "gap_analysis", "section")
DECISIONS = ("APPROVED", "REJECTED")

_ARTIFACT_PATHS = {
    "synthesis": "outputs/drafts/literature_review_draft.json",
    "gap_analysis": "outputs/drafts/research_gap_analysis_draft.json",
}


def canonical_sha256(data) -> str:
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ledger_path(root: Path) -> Path:
    return root / LEDGER_RELPATH


def _read_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def artifact_fingerprint(root: Path, kind: str, artifact_id: str,
                         version: int | None = None) -> str | None:
    """sha256 of the artifact's canonical content, or None when the
    artifact does not exist. Sections fingerprint one version (default:
    the latest)."""
    if kind == "extraction":
        path = root / "sources" / "extracted_notes" / \
            f"{artifact_id}.extraction.json"
        return _fingerprint_file(path)
    if kind in _ARTIFACT_PATHS:
        return _fingerprint_file(root / _ARTIFACT_PATHS[kind])
    if kind == "section":
        data = _read_json(root / "outputs" / "sections" /
                          f"{artifact_id}.json")
        if not isinstance(data, dict):
            return None
        versions = data.get("versions") or []
        if version is None:
            target = versions[-1] if versions else None
        else:
            target = next((v for v in versions
                           if v.get("version") == version), None)
        return canonical_sha256(target) if target is not None else None
    return None


def _fingerprint_file(path: Path) -> str | None:
    data = _read_json(path)
    return None if data is None else canonical_sha256(data)


def record_decision(root: Path, kind: str, artifact_id: str, decision: str,
                    reviewer: str, version: int | None = None) -> bool:
    """Append one approval decision to the ledger. Called by the existing
    approval workflows; returns False (never raises) if the ledger cannot
    be written."""
    if kind not in KINDS:
        raise ValueError(f"unknown artifact kind: {kind!r}")
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {'. '.join(DECISIONS)}")
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "artifact_kind": kind,
        "artifact_id": artifact_id,
        "version": version,
        "decision": decision,
        "reviewer": reviewer,
        "sha256": artifact_fingerprint(root, kind, artifact_id, version),
    }
    try:
        path = ledger_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


def load_ledger(root: Path) -> list:
    path = ledger_path(root)
    if not path.is_file():
        return []
    entries = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("artifact_kind") in KINDS:
            entries.append(entry)
    return entries


def verify_ledger(root: Path) -> list:
    """Recompute every approved entry's fingerprint against the artifact
    as it exists now. Returns rows: {entry, status, detail}."""
    rows = []
    for entry in load_ledger(root):
        if entry.get("decision") != "APPROVED":
            continue
        current = artifact_fingerprint(root, entry["artifact_kind"],
                                       entry["artifact_id"],
                                       entry.get("version"))
        if current is None:
            status, detail = "MISSING", "artifact no longer exists"
        elif current != entry.get("sha256"):
            status, detail = ("TAMPERED", "artifact content changed after "
                                          "approval; the approval no longer "
                                          "matches the artifact")
        else:
            status, detail = "OK", "fingerprint matches"
        rows.append({"entry": entry, "status": status, "detail": detail})
    return rows
