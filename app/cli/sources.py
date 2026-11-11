"""`researchforge sources` — CLI presentation for the source import
workflow (RF-010). All filesystem/metadata logic lives in app.core.sources.
"""
import json
import sys
from pathlib import Path

from app.core import sources as core


def cmd_import(args, root=None) -> int:
    root = root or Path(__file__).resolve().parents[2]
    try:
        record = core.import_source(root, args.file,
                                    provenance=args.provenance,
                                    verification_status=args.status)
    except core.DuplicateSourceError as exc:
        print(f"[DUPLICATE] {exc}")
        return exc.exit_code
    except core.SourceError as exc:
        print(f"[ERROR] {exc}")
        return exc.exit_code
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        return 1
    except OSError as exc:
        print(f"[ERROR] import failed, no record kept: {exc}")
        return 1
    print(f"[OK] imported {record['source_id']}")
    for key in ("original_filename", "stored_location", "source_type",
                "size_bytes", "provenance", "verification_status"):
        print(f"  {key}: {record[key]}")
    for warning in record["validation_warnings"]:
        print(f"  [WARNING] bibtex: {warning}")
    print("  note: staged for review; not added to literature matrix, "
          "evidence register, or references.bib")
    return 0


def cmd_list(args, root=None) -> int:
    root = root or Path(__file__).resolve().parents[2]
    manifest = core.load_manifest(root)
    records = manifest["sources"]
    print(f"{'ID':<18} {'TYPE':<10} {'PROVENANCE':<12} {'STATUS':<20} "
          f"{'SIZE':>10} ORIGINAL")
    for sid in sorted(records):
        r = records[sid]
        print(f"{sid:<18} {r['source_type']:<10} {r['provenance']:<12} "
              f"{r['verification_status']:<20} {r['size_bytes']:>10} "
              f"{r['original_filename']}")
    print(f"\n{len(records)} source(s) imported")
    return 0


def cmd_show(args, root=None) -> int:
    root = root or Path(__file__).resolve().parents[2]
    record = core.get_source(root, args.source_id)
    if record is None:
        # Identifiers are looked up in the manifest only — a traversal-like
        # string can never reach the filesystem as a path.
        print(f"[ERROR] unknown source id: {args.source_id}")
        return 1
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def cmd_validate(args, root=None) -> int:
    root = root or Path(__file__).resolve().parents[2]
    rows = core.validate_sources(root)
    if not rows:
        print("No sources imported; nothing to validate.")
        return 0
    has_error = False
    for row in rows:
        print(f"{row['level']:<8} {row['source_id']}: {'; '.join(row['messages'])}")
        has_error = has_error or row["level"] == "ERROR"
    print("\nValidation result: " + ("ERROR" if has_error else "PASS"))
    return 1 if has_error else 0


def setup(subparsers):
    parser = subparsers.add_parser(
        "sources", help="import and inspect research source files",
        epilog=("Native formats: .md .txt .csv .bib (UTF-8 text), "
                ".docx (text extracted locally with the standard library), "
                ".pdf (requires the external 'pdftotext' utility; otherwise "
                "convert the PDF to UTF-8 text and import that file). "
                "Importing never approves evidence."))
    sub = parser.add_subparsers(dest="sources_command", metavar="<action>",
                                required=True)

    p_import = sub.add_parser(
        "import", help="import a source file (DOCX, PDF, MD, TXT, BibTeX, CSV)",
        epilog=("PDF files without 'pdftotext' cannot be converted: export "
                "the text externally (e.g. `pdftotext file.pdf out.txt`), "
                "then import 'out.txt'. OCR is not supported."))
    p_import.add_argument("file", help="path to the file to import")
    p_import.add_argument("--provenance", choices=core.PROVENANCES,
                          default="researcher",
                          help="origin platform if supplied (default: researcher)")
    p_import.add_argument("--status", choices=core.EVIDENCE_STATUSES,
                          default="SEARCH RESULT ONLY",
                          help="verification status; importing never implies "
                               "FULL TEXT READ")
    p_import.set_defaults(handler=cmd_import)

    p_list = sub.add_parser("list", help="list imported sources")
    p_list.set_defaults(handler=cmd_list)

    p_show = sub.add_parser("show", help="show metadata of one source by id")
    p_show.add_argument("source_id", help="source identifier, e.g. SRC-ab12cd34ef56")
    p_show.set_defaults(handler=cmd_show)

    p_val = sub.add_parser("validate", help="re-verify stored files against the manifest")
    p_val.set_defaults(handler=cmd_validate)
