"""`researchforge export` commands (MVP-03).

`export docx` assembles APPROVED manuscript section versions into an
editable DOCX and writes an export manifest. All logic lives in
app.exporters.docx; this module only parses arguments and prints.
"""
from pathlib import Path

from app.core.sources import SourceError
from app.exporters import docx


def cmd_export_docx(args, root=None) -> int:
    root = root or Path(__file__).resolve().parents[2]
    try:
        if args.check:
            plan = docx.plan_export(root, args.sections)
            if not plan["sections"]:
                print("[ERROR] no approved sections are available for "
                      "export; approve a section draft first")
                return 2
            print("Export check: PASS (nothing was written)")
            for section in plan["sections"]:
                print(f"  {section['section']} v{section['version']} "
                      f"({section['heading']}) — approved by "
                      f"{section['approved_by']}, fingerprint "
                      f"{section['fingerprint'][:12]}…")
            for excluded in plan["excluded"]:
                print(f"  [SKIP] {excluded['section']}: {excluded['reason']}")
            return 0
        manifest = docx.export_docx(
            root, args.output, title=args.title or "Manuscript",
            sections=args.sections, force=args.force)
    except SourceError as exc:
        print(f"[ERROR] {exc}")
        return exc.exit_code
    print(f"Exported {manifest['output']['path']} "
          f"({manifest['output']['size_bytes']} bytes)")
    for section in manifest["sections"]:
        print(f"  {section['section']} v{section['version']} "
              f"({section['heading']})")
    print(f"  citations preserved: "
          f"{len(manifest['citation_identifiers'])} "
          f"({len(manifest['unresolved_references'])} unresolved — see "
          "references section)")
    print(f"  manifest: {manifest['manifest_path']}")
    print(f"  validation: {manifest['validation']['status']}")
    if args.libreoffice:
        result = docx.libreoffice_check(root / manifest["output"]["path"])
        print(f"  LibreOffice check: {result['status']} ({result['detail']})")
    return 0


def setup(subparsers):
    parser = subparsers.add_parser(
        "export", help="export approved manuscript content",
        epilog=("Only APPROVED section versions are exported and their "
                "approval fingerprints are verified first. Exporting never "
                "approves anything."))
    sub = parser.add_subparsers(dest="export_format", metavar="<format>",
                                required=True)
    p_docx = sub.add_parser(
        "docx", help="assemble approved sections into a DOCX",
        epilog=("Refuses to overwrite an existing output without --force. "
                "References stay as internal [SRC-...] identifiers because "
                "no verified bibliographic metadata exists yet."))
    p_docx.add_argument("--output", default=docx.DEFAULT_OUTPUT,
                        help=f"output path (default: {docx.DEFAULT_OUTPUT})")
    p_docx.add_argument("--title", default=None,
                        help="document title (default: Manuscript)")
    p_docx.add_argument("--sections", nargs="+", choices=sorted(docx.SECTIONS),
                        help="only export these sections (canonical order; "
                             "each must be approved)")
    p_docx.add_argument("--check", action="store_true",
                        help="verify approvals/fingerprints and list what "
                             "would be exported; writes nothing")
    p_docx.add_argument("--force", action="store_true",
                        help="overwrite an existing output file")
    p_docx.add_argument("--libreoffice", action="store_true",
                        help="also verify the document with LibreOffice if "
                             "it is installed")
    p_docx.set_defaults(handler=cmd_export_docx)
