"""`researchforge` workflow commands (MVP-01): extraction, synthesis,
gap analysis, section drafting and independent review.

Integration only — every handler calls the existing workflow functions
directly (no workflow logic lives here). Read-only commands never build a
provider; AI runs only when the command itself is explicitly invoked and,
where the workflow supports it, only with `--ai`.

Exit codes: 0 success / read-only OK; 1 operation completed with a
failure state (extraction FAILED/EMPTY/UNSUPPORTED, requested AI step
failed, artifact missing); 2 refused or invalid request (SourceError
family, provider/config errors).
"""
import json
import sys
from pathlib import Path

from app.core import model_config
from app.core.sources import SourceError, get_source, load_manifest
from app.providers.errors import ProviderError
from app.workflows import independent_review as ir
from app.workflows import literature_synthesis as ls
from app.workflows import paper_extraction as pe
from app.workflows import research_gap_analysis as ga
from app.workflows import section_drafting as sd


def _root(root):
    return root or Path(__file__).resolve().parents[2]


def _safe(value) -> str:
    """Console-safe text: never crash on characters the terminal encoding
    cannot represent (e.g. Greek symbols from real papers on cp1252)."""
    text = str(value)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, "replace").decode(encoding, "replace")
    except LookupError:
        return text


def _print_json(data) -> None:
    print(_safe(json.dumps(data, indent=2, ensure_ascii=False,
                           sort_keys=True)))


def _build_provider(root: Path, task: str):
    """Construct the configured provider for an explicitly requested AI
    step. Uses RF-013 config + task resolution; no network at construction.
    Applied only inside --ai / drafting handlers."""
    config = model_config.load_config(root)
    resolved = model_config.resolve_task(config, task)
    provider = model_config.build_provider(config, resolved["provider"], root)
    if resolved.get("model") and resolved["model"] != getattr(
            provider, "default_model", None):
        provider.default_model = resolved["model"]  # task-level override
    return provider


def _fail(exc) -> int:
    print(f"[ERROR] {exc}")
    return getattr(exc, "exit_code", 2)


# ------------------------------------------------------------ extraction

def cmd_extraction_run(args, root=None) -> int:
    root = _root(root)
    try:
        record = pe.extract_source(root, args.source_id)
    except SourceError as exc:
        return _fail(exc)
    outcome = record["outcome"]
    print(f"{record['source_id']}: {outcome}")
    if record.get("error"):
        print(f"  reason: {_safe(record['error'])}")
    print(f"  items: {len(record['items'])}")
    print(f"  review: {record['review']}")
    if record.get("extraction_tool"):
        print(f"  tool: {record['extraction_tool']} "
              f"({record.get('extraction_tool_version') or 'version unknown'})")
    if record.get("pages"):
        print(f"  pages: {record['pages']}")
    for limitation in record.get("limitations") or []:
        print(f"  limitation: {limitation}")
    print(f"  record: sources/extracted_notes/"
          f"{record['source_id']}.extraction.json")
    if outcome == "COMPLETED":
        print("Next: researchforge approve extraction "
              f"{record['source_id']} --reviewer <name>")
        return 0
    print("[WARNING] no usable evidence was extracted from this source")
    return 1


def cmd_extractions_list(args, root=None) -> int:
    root = _root(root)
    manifest = load_manifest(root)
    rows = []
    for source_id in sorted(manifest["sources"]):
        try:
            record = pe.load_extraction(root, source_id)
        except SourceError:
            rows.append((source_id, "UNREADABLE", "-", 0))
            continue
        if record is None:
            rows.append((source_id, "NOT EXTRACTED", "-", 0))
        else:
            rows.append((source_id, record.get("outcome", "?"),
                         record.get("review", "?"),
                         len(record.get("items", []))))
    if not rows:
        print("No sources imported; nothing to extract.")
        return 0
    print(f"{'SOURCE':<20} {'OUTCOME':<13} {'REVIEW':<22} ITEMS")
    for source_id, outcome, review, items in rows:
        print(f"{source_id:<20} {outcome:<13} {review:<22} {items}")
    return 0


def cmd_extractions_show(args, root=None) -> int:
    root = _root(root)
    if get_source(root, args.source_id) is None:
        print(f"[ERROR] unknown source id: {args.source_id}")
        return 1
    try:
        record = pe.load_extraction(root, args.source_id)
    except SourceError as exc:
        return _fail(exc)
    if record is None:
        print(f"[ERROR] no extraction record for {args.source_id}; run: "
              f"researchforge extraction run {args.source_id}")
        return 1
    _print_json(record)
    return 0


def cmd_extraction_items(args, root=None) -> int:
    root = _root(root)
    try:
        record = pe.load_extraction(root, args.source_id)
    except SourceError as exc:
        return _fail(exc)
    if record is None:
        print(f"[ERROR] no extraction record for {args.source_id}")
        return 1
    items = record.get("items", [])
    shown = 0
    for index, item in enumerate(items, start=1):
        decision = item.get("decision") or "PENDING"
        if args.pending and decision != "PENDING":
            continue
        shown += 1
        print(f"{index:>4} {item.get('extraction_id', '?'):<16} "
              f"{(item.get('section') or '-'):<15} "
              f"p{item.get('page') or '?':<3} "
              f"{(item.get('classification') or '?'):<22} "
              f"{decision:<8} {_safe(item['text'][:90])!r}")
    print(f"\n{shown} of {len(items)} candidate(s) shown; "
          f"record review: {record.get('review')}")
    print("Review one candidate: researchforge extraction review "
          f"{args.source_id} --item <number> --decision approve|reject "
          "--reviewer <name>")
    return 0


def cmd_extraction_review(args, root=None) -> int:
    root = _root(root)
    reference = args.item
    if isinstance(reference, str) and reference.isdigit():
        reference = int(reference)
    decision = ("APPROVED" if args.decision == "approve" else "REJECTED")
    try:
        item = pe.decide_item(root, args.source_id, reference, decision,
                              args.reviewer)
    except SourceError as exc:
        return _fail(exc)
    record = pe.load_extraction(root, args.source_id)
    print(f"{item['extraction_id']}: {decision} by {item['reviewed_by']}")
    print(f"  candidate decisions: {record.get('item_decisions')}")
    return 0


# ------------------------------------------------------------ synthesis

def cmd_synthesis_run(args, root=None) -> int:
    root = _root(root)
    try:
        draft = ls.synthesize(root)
    except SourceError as exc:
        return _fail(exc)
    ai_failed = False
    if args.ai:
        try:
            provider = _build_provider(root, ls.TASK)
            draft = ls.add_ai_interpretation(root, draft, provider)
            draft = ls.save_draft(root, draft)
        except (ProviderError, SourceError) as exc:
            ai_failed = True
            print(f"[WARNING] AI interpretation failed: {exc}")
            draft = ls.load_draft(root) or draft
        else:
            if draft["interpretation"]["status"] == "FAILED":
                ai_failed = True
                print("[WARNING] AI interpretation failed: "
                      f"{draft['interpretation'].get('error')}")
    eligibility = draft["eligibility"]
    evidence = draft["evidence"]
    print(f"Synthesis draft: outputs/drafts/literature_review_draft.json")
    print(f"  approved sources: {len(eligibility['approved_extractions'])}")
    print(f"  excluded: {len(eligibility['excluded'])}")
    print(f"  findings/claims/methods: "
          f"{len(evidence['findings'])}/{len(evidence['claims'])}/"
          f"{len(evidence['methods'])}")
    if eligibility["insufficient_evidence"]:
        print(f"  [WARNING] insufficient evidence: "
              f"{eligibility['insufficient_reason']}")
    print(f"  interpretation: {draft['interpretation']['status']}")
    print(f"  review: {draft['review']}")
    print("Next: researchforge synthesize show, then: researchforge approve "
          "synthesis --reviewer <name>")
    return 1 if ai_failed else 0


def cmd_synthesis_show(args, root=None) -> int:
    root = _root(root)
    try:
        draft = ls.load_draft(root)
    except SourceError as exc:
        return _fail(exc)
    if draft is None:
        print("[ERROR] no synthesis draft; run: researchforge synthesis run")
        return 1
    _print_json(draft)
    return 0


# ------------------------------------------------------------ gaps

def cmd_gaps_run(args, root=None) -> int:
    root = _root(root)
    try:
        analysis = ga.analyze_gaps(root)
    except SourceError as exc:
        return _fail(exc)
    ai_failed = False
    if args.ai:
        try:
            provider = _build_provider(root, ga.TASK)
            analysis = ga.add_ai_interpretation(root, analysis, provider)
            analysis = ga.save_analysis(root, analysis)
        except (ProviderError, SourceError) as exc:
            ai_failed = True
            print(f"[WARNING] AI interpretation failed: {exc}")
            analysis = ga.load_analysis(root) or analysis
        else:
            if analysis["interpretation"]["status"] == "FAILED":
                ai_failed = True
                print("[WARNING] AI interpretation failed: "
                      f"{analysis['interpretation'].get('error')}")
    gaps = analysis["candidate_gaps"]
    eligibility = analysis["eligibility"]
    print("Gap analysis draft: outputs/drafts/"
          "research_gap_analysis_draft.json")
    print(f"  evidenced limitations: "
          f"{len(gaps['directly_evidenced_limitations'])}")
    print(f"  inferred patterns: {len(gaps['inferred_patterns'])}")
    print(f"  opportunities: "
          f"{len(gaps['potential_research_opportunities'])}")
    if eligibility["insufficient_coverage"]:
        print(f"  [WARNING] insufficient coverage: "
              f"{eligibility['coverage_reason']}")
    print(f"  interpretation: {analysis['interpretation']['status']}")
    print(f"  review: {analysis['review']}")
    print("Next: researchforge gaps show, then: researchforge approve "
          "gap-analysis --reviewer <name>")
    return 1 if ai_failed else 0


def cmd_gaps_show(args, root=None) -> int:
    root = _root(root)
    try:
        analysis = ga.load_analysis(root)
    except SourceError as exc:
        return _fail(exc)
    if analysis is None:
        print("[ERROR] no gap analysis draft; run: researchforge gaps run")
        return 1
    _print_json(analysis)
    return 0


# ------------------------------------------------------------ drafts

def cmd_drafts_run(args, root=None) -> int:
    root = _root(root)
    context = args.context
    if args.context_file:
        try:
            context = Path(args.context_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[ERROR] cannot read context file: {exc}")
            return 2
    try:
        provider = _build_provider(root, sd.TASK)
    except (ProviderError, SourceError) as exc:
        return _fail(exc)
    try:
        version = sd.draft_section(root, args.section, provider,
                                   researcher_context=context)
    except ProviderError as exc:
        print(f"[ERROR] generation failed (a FAILED version was recorded): "
              f"{exc}")
        return 2
    except SourceError as exc:
        return _fail(exc)
    print(f"Section '{args.section}' version {version['version']}: "
          f"{version['status']} ({version['origin']}, {version['review']})")
    print(f"  model: {version['model']}")
    print(f"  citations: valid={len(version['citations']['valid'])} "
          f"invalid={len(version['citations']['invalid'])}")
    print(f"  unsupported statements: "
          f"{len(version['unsupported_statements'])}")
    print(f"  record: outputs/sections/{args.section}.json")
    print("Next: researchforge reviews run "
          f"{args.section} --version {version['version']}, then: "
          f"researchforge approve section {args.section} "
          f"--version {version['version']} --reviewer <name>")
    return 0


def cmd_drafts_list(args, root=None) -> int:
    root = _root(root)
    rows = []
    for section in sorted(sd.SECTIONS):
        try:
            data = sd.load_section(root, section)
        except SourceError:
            rows.append((section, "-", "-", "UNREADABLE"))
            continue
        if data is None or not data["versions"]:
            continue
        latest = data["versions"][-1]
        rows.append((section, len(data["versions"]), latest["version"],
                     f"{latest['review']}"))
    if not rows:
        print("No section drafts; run: researchforge drafts run <section>")
        return 0
    print(f"{'SECTION':<20} {'VERSIONS':>8} {'LATEST':>7} REVIEW")
    for section, count, latest, review in rows:
        print(f"{section:<20} {count:>8} {latest:>7} {review}")
    return 0


def cmd_drafts_show(args, root=None) -> int:
    root = _root(root)
    try:
        data = sd.load_section(root, args.section)
    except SourceError as exc:
        return _fail(exc)
    if data is None or not data["versions"]:
        print(f"[ERROR] no draft for section '{args.section}'; run: "
              f"researchforge drafts run {args.section}")
        return 1
    if args.version is None:
        _print_json(data["versions"][-1])
        return 0
    match = next((v for v in data["versions"]
                  if v["version"] == args.version), None)
    if match is None:
        print(f"[ERROR] version {args.version} of '{args.section}' does not "
              "exist")
        return 1
    _print_json(match)
    return 0


# ------------------------------------------------------------ reviews

def cmd_reviews_run(args, root=None) -> int:
    root = _root(root)
    provider = None
    ai_requested = bool(args.ai)
    if ai_requested:
        try:
            provider = _build_provider(root, ir.TASK)
        except (ProviderError, SourceError) as exc:
            return _fail(exc)
    try:
        review = ir.review_section(root, args.section, version=args.version,
                                   provider=provider)
    except SourceError as exc:
        return _fail(exc)
    findings = review["deterministic_findings"]
    counts = {sev: 0 for sev in ("HIGH", "MEDIUM", "LOW", "INFO")}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    print(f"Review {review['review_id']}: section '{args.section}' "
          f"version {review['reviewed_artifact']['version']}")
    print("  deterministic findings: "
          + ", ".join(f"{sev}={counts[sev]}"
                      for sev in ("HIGH", "MEDIUM", "LOW", "INFO")))
    print(f"  contradiction references: "
          f"{len(review['contradiction_references'])}")
    print(f"  AI review: {review['ai_review']['status']}")
    print(f"  report: outputs/review_reports/{review['review_id']}.json")
    print("  findings are undecided; decisions are recorded separately "
          "(no auto-approval)")
    if ai_requested and review["ai_review"]["status"] == "FAILED":
        print(f"[WARNING] AI review failed: {review['ai_review']['error']}")
        return 1
    return 0


def cmd_reviews_list(args, root=None) -> int:
    root = _root(root)
    directory = root / ir.REPORTS_DIR
    rows = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                review = ir.load_review(root, path.stem)
            except SourceError:
                continue
            if review is None:
                continue
            rows.append((review["review_id"],
                         review["reviewed_artifact"].get("section", "?"),
                         review["reviewed_artifact"].get("version", "?"),
                         review.get("created_at", "?"),
                         review.get("review", "?"),
                         len(review.get("deterministic_findings", [])),
                         (review.get("ai_review") or {}).get("status", "?")))
    if not rows:
        print("No review reports; run: researchforge reviews run <section>")
        return 0
    rows.sort(key=lambda r: r[3], reverse=True)
    print(f"{'REVIEW':<18} {'SECTION':<18} {'VER':>4} {'CREATED':<20} "
          f"{'REVIEW':<22} {'FINDINGS':>8} AI")
    for rid, section, version, created, review, findings, ai in rows:
        print(f"{rid:<18} {section:<18} {version:>4} {created:<20} "
              f"{review:<22} {findings:>8} {ai}")
    return 0


def cmd_reviews_show(args, root=None) -> int:
    root = _root(root)
    try:
        review = ir.load_review(root, args.review_id)
    except SourceError as exc:
        return _fail(exc)
    if review is None:
        print(f"[ERROR] review not found: {args.review_id}")
        return 1
    _print_json(review)
    return 0


def cmd_reviews_add_external(args, root=None) -> int:
    root = _root(root)
    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[ERROR] cannot read critique file: {exc}")
        return 2
    try:
        review = ir.add_external_review(root, args.review_id, text,
                                        args.platform)
    except SourceError as exc:
        return _fail(exc)
    print(f"External {args.platform} critique recorded in review "
          f"{review['review_id']} (stored separately; not accepted "
          "automatically)")
    return 0


# ------------------------------------------------------------ registration

def setup_extraction(subparsers):
    parser = subparsers.add_parser(
        "extraction", help="extract one imported source into an evidence "
                           "record (deterministic, no AI)")
    sub = parser.add_subparsers(dest="extraction_command",
                                metavar="<action>", required=True)
    run = sub.add_parser("run", help="extract a source")
    run.add_argument("source_id", help="source identifier, e.g. SRC-ab12cd34ef56")
    run.set_defaults(handler=cmd_extraction_run)
    p_list = sub.add_parser("list", help="list extraction records")
    p_list.set_defaults(handler=cmd_extractions_list)
    p_show = sub.add_parser("show", help="show one extraction record (JSON)")
    p_show.add_argument("source_id", help="source identifier")
    p_show.set_defaults(handler=cmd_extractions_show)
    p_items = sub.add_parser(
        "items", help="list evidence candidates with section, page and "
                      "classification")
    p_items.add_argument("source_id", help="source identifier")
    p_items.add_argument("--pending", action="store_true",
                         help="only show candidates still awaiting a decision")
    p_items.set_defaults(handler=cmd_extraction_items)
    p_review = sub.add_parser(
        "review", help="approve or reject ONE evidence candidate (final)")
    p_review.add_argument("source_id", help="source identifier")
    p_review.add_argument("--item", required=True,
                          help="candidate number (from 'extraction items') or "
                               "extraction id")
    p_review.add_argument("--decision", required=True,
                          choices=("approve", "reject"))
    p_review.add_argument("--reviewer", required=True,
                          help="name of the human reviewer")
    p_review.set_defaults(handler=cmd_extraction_review)


def setup_synthesis(subparsers):
    parser = subparsers.add_parser(
        "synthesis", help="literature synthesis draft (RF-016)")
    sub = parser.add_subparsers(dest="synthesis_command", metavar="<action>",
                                required=True)
    run = sub.add_parser("run", help="build the synthesis draft from "
                                     "APPROVED extractions")
    run.add_argument("--ai", action="store_true",
                     help="also request AI interpretation (explicit API call; "
                          "agreements/contradictions stay unverified)")
    run.set_defaults(handler=cmd_synthesis_run)
    show = sub.add_parser("show", help="show the saved synthesis draft (JSON)")
    show.set_defaults(handler=cmd_synthesis_show)
    parser.epilog = ("Approve with: researchforge approve synthesis "
                     "--reviewer <name>")


def setup_gaps(subparsers):
    parser = subparsers.add_parser(
        "gaps", help="research gap analysis draft (RF-017)")
    sub = parser.add_subparsers(dest="gaps_command", metavar="<action>",
                                required=True)
    run = sub.add_parser("run", help="analyse gaps from an APPROVED "
                                     "synthesis")
    run.add_argument("--ai", action="store_true",
                     help="also request AI interpretation (explicit API call; "
                          "novelty hypotheses stay UNVERIFIED)")
    run.set_defaults(handler=cmd_gaps_run)
    show = sub.add_parser("show", help="show the saved gap analysis (JSON)")
    show.set_defaults(handler=cmd_gaps_show)
    parser.epilog = ("Approve with: researchforge approve gap-analysis "
                     "--reviewer <name>")


def setup_drafts(subparsers):
    parser = subparsers.add_parser(
        "drafts", help="manuscript section drafts (RF-018)")
    sub = parser.add_subparsers(dest="drafts_command", metavar="<action>",
                                required=True)
    run = sub.add_parser("run", help="draft one supported section "
                                     "(explicit AI generation)")
    run.add_argument("section", choices=sorted(sd.SECTIONS),
                     help="section name (" + ", ".join(sorted(sd.SECTIONS))
                          + ")")
    context = run.add_mutually_exclusive_group()
    context.add_argument("--context", help="researcher-provided context "
                                           "(not evidence)")
    context.add_argument("--context-file",
                         help="UTF-8 file with researcher-provided context")
    run.set_defaults(handler=cmd_drafts_run)
    p_list = sub.add_parser("list", help="list section drafts and versions")
    p_list.set_defaults(handler=cmd_drafts_list)
    p_show = sub.add_parser("show", help="show a draft version (JSON)")
    p_show.add_argument("section", choices=sorted(sd.SECTIONS))
    p_show.add_argument("--version", type=int, default=None,
                        help="version number (default: latest)")
    p_show.set_defaults(handler=cmd_drafts_show)
    parser.epilog = ("Approve with: researchforge approve section <section> "
                     "--version <n> --reviewer <name>")


def setup_reviews(subparsers):
    parser = subparsers.add_parser(
        "reviews", help="independent review reports (RF-019)")
    sub = parser.add_subparsers(dest="reviews_command", metavar="<action>",
                                required=True)
    run = sub.add_parser("run", help="review a section draft version")
    run.add_argument("section", choices=sorted(sd.SECTIONS))
    run.add_argument("--version", type=int, default=None,
                     help="draft version (default: latest)")
    run.add_argument("--ai", action="store_true",
                     help="also request AI review (explicit API call; "
                          "findings stay unverified)")
    run.set_defaults(handler=cmd_reviews_run)
    p_list = sub.add_parser("list", help="list review reports")
    p_list.set_defaults(handler=cmd_reviews_list)
    p_show = sub.add_parser("show", help="show one review report (JSON)")
    p_show.add_argument("review_id", help="review id, e.g. RVW-ab12cd34ef56")
    p_show.set_defaults(handler=cmd_reviews_show)
    p_ext = sub.add_parser(
        "add-external", help="record a manually pasted external critique "
                             "(Gemini/Perplexity/ChatGPT)")
    p_ext.add_argument("review_id", help="review id")
    p_ext.add_argument("--platform", choices=ir.PLATFORMS, required=True)
    p_ext.add_argument("--file", required=True,
                       help="UTF-8 file containing the pasted critique")
    p_ext.set_defaults(handler=cmd_reviews_add_external)
