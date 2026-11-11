"""`researchforge approve` and `researchforge approvals` (RF-020).

Approval commands delegate to the existing single approval paths in the
workflows (RF-015/016/017/018) — no approval logic is duplicated here.
`approvals list` and `approvals verify` expose the central append-only
ledger and its fingerprint verification. Never displays secrets.
"""
from pathlib import Path

from app.core import approvals
from app.core.sources import SourceError


def _root(root):
    return root or Path(__file__).resolve().parents[2]


def cmd_approve(args, root=None):
    root = _root(root)
    from app.workflows import (literature_synthesis, paper_extraction,
                               research_gap_analysis, section_drafting)
    try:
        if args.artifact == "extraction":
            record = paper_extraction.approve_extraction(
                root, args.target, args.reviewer)
            print(f"Extraction {args.target}: APPROVED "
                  f"(reviewed_by={record['reviewed_by']})")
        elif args.artifact == "synthesis":
            record = literature_synthesis.approve_synthesis(
                root, args.reviewer)
            print(f"Synthesis draft: APPROVED "
                  f"(reviewed_by={record['reviewed_by']})")
        elif args.artifact == "gap-analysis":
            record = research_gap_analysis.approve_gap_analysis(
                root, args.reviewer)
            print(f"Gap analysis: APPROVED "
                  f"(reviewed_by={record['reviewed_by']})")
        elif args.artifact == "section":
            record = section_drafting.approve_section(
                root, args.target, args.reviewer, version=args.version)
            print(f"Section '{args.target}' version {record['version']}: "
                  f"APPROVED (reviewed_by={record['reviewed_by']})")
    except SourceError as exc:
        print(f"[ERROR] {exc}")
        return exc.exit_code
    return 0


def cmd_list(args, root=None):
    root = _root(root)
    entries = approvals.load_ledger(root)
    if not entries:
        print("No approval decisions recorded.")
        return 0
    for entry in entries:
        version = ("" if entry.get("version") is None
                   else f" v{entry['version']}")
        print(f"{entry['timestamp']}  {entry['decision']:8}  "
              f"{entry['artifact_kind']}{version}  {entry['artifact_id']}  "
              f"by {entry['reviewer']}  sha256={str(entry.get('sha256'))[:12]}")
    return 0


def cmd_verify(args, root=None):
    root = _root(root)
    rows = approvals.verify_ledger(root)
    if not rows:
        print("No approved decisions to verify.")
        return 0
    failed = 0
    for row in rows:
        entry = row["entry"]
        print(f"[{row['status']}] {entry['artifact_kind']} "
              f"{entry['artifact_id']} ({entry['timestamp']}): "
              f"{row['detail']}")
        if row["status"] != "OK":
            failed += 1
    print(f"\n{len(rows) - failed}/{len(rows)} approvals verified OK.")
    return 0 if failed == 0 else 1


def setup_approve(subparsers):
    parser = subparsers.add_parser(
        "approve", help="record an explicit human approval (never automatic)")
    sub = parser.add_subparsers(dest="artifact", metavar="<artifact>",
                                required=True)
    for name, target_help in (
            ("extraction", "source id (SRC-...)"),
            ("section", "section name (e.g. introduction)")):
        sub_parser = sub.add_parser(name, help=f"approve a {name}")
        sub_parser.add_argument("target", help=target_help)
        sub_parser.add_argument("--reviewer", required=True,
                                help="name of the human reviewer")
        sub_parser.set_defaults(handler=cmd_approve, version=None)
        if name == "section":
            sub_parser.add_argument("--version", type=int, default=None,
                                    help="draft version (default: latest)")
    for name in ("synthesis", "gap-analysis"):
        sub_parser = sub.add_parser(name, help=f"approve the {name} draft")
        sub_parser.add_argument("--reviewer", required=True,
                                help="name of the human reviewer")
        sub_parser.set_defaults(handler=cmd_approve, target=None, version=None)


def setup_approvals(subparsers):
    ledger = subparsers.add_parser(
        "approvals", help="inspect the central approval ledger")
    ledger_sub = ledger.add_subparsers(dest="approvals_command",
                                       metavar="<action>", required=True)
    ledger_sub.add_parser(
        "list", help="list approval decisions").set_defaults(handler=cmd_list)
    ledger_sub.add_parser(
        "verify", help="verify approved artifacts against their "
                       "fingerprints").set_defaults(handler=cmd_verify)
