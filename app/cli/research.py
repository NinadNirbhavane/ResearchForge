"""`researchforge research` — read-only management of the research
knowledge base (RF-009): list, show, validate.

Filesystem access lives in list_documents/read_document/validate_documents;
CLI handlers only format and print. Access is restricted to the nine
approved documents (by identifier or approved relative path); absolute
paths and traversal refs are rejected before any filesystem call.
"""
import re
from pathlib import Path

DOCUMENT_IDS = (
    "research_charter", "research_baseline", "research_questions",
    "objectives", "methodology", "architecture_specification",
    "decisions_register", "limitations", "unresolved_questions",
)
DOCUMENTS = {doc_id: f"research/{doc_id}.md" for doc_id in DOCUMENT_IDS}

# Minimum structural headings per document (substring match, one per line).
REQUIRED_HEADINGS = {
    "research_charter": ("## Working title", "## Research problem",
                         "## Geographic scope", "## Target population",
                         "## Research status", "## Proposed contribution",
                         "## Research principles"),
    "research_baseline": ("## Canonical system concept", "## Operating modes",
                          "## Glossary"),
    "research_questions": ("## RQ1",),
    "objectives": ("## Primary objective", "## Supporting objectives",
                   "## Proposed evaluation objectives"),
    "methodology": ("## Design type", "## Manual field audits", "## Ethics"),
    "architecture_specification": ("## Layer overview", "## Offline behavior",
                                   "## Privacy boundaries"),
    "decisions_register": ("## Established decisions", "## Proposed decisions"),
    "limitations": ("## Known or anticipated limitations",),
    "unresolved_questions": ("## Marker legend", "## 1. Literature verification"),
}
MARKERS = ("[VERIFY SOURCE]", "[DECISION REQUIRED]",
           "[VALIDATE THROUGH PILOT]", "[NOT YET ESTABLISHED]")


def resolve_document(root: Path, ref: str):
    """Return (path, relative_path) for an approved document reference.

    Accepts a document identifier or an approved relative path. Rejects
    absolute paths, traversal, and anything outside the approved set
    before touching the filesystem.
    """
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("empty document reference")
    ref = ref.strip()
    if Path(ref).is_absolute() or (len(ref) > 1 and ref[1] == ":"):
        raise ValueError(f"absolute paths are not accepted: {ref}")
    if ".." in Path(ref.replace("\\", "/")).parts:
        raise ValueError(f"path traversal is not accepted: {ref}")
    if ref in DOCUMENTS:
        rel = DOCUMENTS[ref]
    else:
        norm = ref.replace("\\", "/")
        if norm not in DOCUMENTS.values():
            raise ValueError(f"unknown research document: {ref}")
        rel = norm
    return root / rel, rel


def list_documents(root: Path):
    rows = []
    for doc_id in DOCUMENT_IDS:
        path = root / DOCUMENTS[doc_id]
        rows.append({
            "id": doc_id,
            "path": DOCUMENTS[doc_id],
            "exists": path.is_file(),
            "size": path.stat().st_size if path.is_file() else None,
        })
    return rows


def read_document(root: Path, ref: str) -> tuple:
    """Return (relative_path, text) preserving UTF-8 content verbatim."""
    path, rel = resolve_document(root, ref)
    return rel, path.read_text(encoding="utf-8")


def validate_documents(root: Path):
    """Read-only structural checks. Returns {"rows": [...], "unexpected": [...]}
    where each row is {"id","path","level","messages":[...]} with level in
    PASS/WARNING/ERROR."""
    rows = []
    for doc_id in DOCUMENT_IDS:
        rel = DOCUMENTS[doc_id]
        path = root / rel
        messages, level = [], "PASS"
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            rows.append({"id": doc_id, "path": rel, "level": "ERROR",
                         "messages": ["file is missing"]})
            continue
        except OSError as exc:
            rows.append({"id": doc_id, "path": rel, "level": "ERROR",
                         "messages": [f"unreadable: {exc}"]})
            continue
        if not text.strip():
            rows.append({"id": doc_id, "path": rel, "level": "WARNING",
                         "messages": ["file is empty"]})
            continue
        missing = [h for h in REQUIRED_HEADINGS[doc_id]
                   if not any(line.startswith(h) for line in text.splitlines())]
        if missing:
            messages.append("missing required sections: " + ", ".join(missing))
            level = "WARNING"
        if not any(marker in text for marker in MARKERS):
            messages.append("no uncertainty markers present")
            level = "WARNING"
        rows.append({"id": doc_id, "path": rel, "level": level,
                     "messages": messages})

    unexpected = sorted(
        p.relative_to(root).as_posix()
        for p in (root / "research").glob("*.md")
        if p.relative_to(root).as_posix() not in DOCUMENTS.values())
    return {"rows": rows, "unexpected": unexpected}


# ---------------------------------------------------------------- presentation

def cmd_list(args) -> int:
    root = Path(__file__).resolve().parents[2]
    rows = list_documents(root)
    print(f"{'ID':<28} {'PATH':<40} {'STATUS':<10} SIZE")
    for row in rows:
        status = "PRESENT" if row["exists"] else "MISSING"
        size = str(row["size"]) if row["exists"] else "-"
        print(f"{row['id']:<28} {row['path']:<40} {status:<10} {size}")
    missing = [r for r in rows if not r["exists"]]
    print(f"\n{len(rows) - len(missing)} of {len(rows)} documents present"
          + (f"; {len(missing)} missing [WARNING]" if missing else ""))
    return 0


def cmd_show(args) -> int:
    root = Path(__file__).resolve().parents[2]
    try:
        rel, text = read_document(root, args.document)
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        return 2
    except FileNotFoundError:
        print(f"[ERROR] document not found: {args.document}")
        return 1
    except OSError as exc:
        print(f"[ERROR] unreadable: {exc}")
        return 1
    print(f"[{rel}]")
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def cmd_validate(args) -> int:
    root = Path(__file__).resolve().parents[2]
    results = validate_documents(root)
    has_error = False
    for row in results["rows"]:
        detail = "; ".join(row["messages"]) if row["messages"] else "OK"
        print(f"{row['level']:<8} {row['path']}: {detail}")
        if row["level"] == "ERROR":
            has_error = True
    if results["unexpected"]:
        print(f"[WARNING] unexpected files in research/: "
              + ", ".join(results["unexpected"]))
    print("\nValidation result: "
          + ("ERROR" if has_error else "PASS"
             if not any(r["level"] == "WARNING" for r in results["rows"])
             else "PASS with warnings"))
    return 1 if has_error else 0


def setup(subparsers):
    parser = subparsers.add_parser(
        "research", help="manage research knowledge-base documents (read-only)")
    sub = parser.add_subparsers(dest="research_command", metavar="<action>",
                                required=True)
    p_list = sub.add_parser("list", help="list the approved research documents")
    p_list.set_defaults(handler=cmd_list)
    p_show = sub.add_parser("show", help="print an approved research document")
    p_show.add_argument("document", help="document identifier or relative path")
    p_show.set_defaults(handler=cmd_show)
    p_val = sub.add_parser("validate", help="read-only structural validation")
    p_val.set_defaults(handler=cmd_validate)
