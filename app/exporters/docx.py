"""DOCX manuscript export (MVP-03).

Assembles APPROVED section-draft versions (RF-018) into an editable
Office Open XML document using only the standard library (zipfile +
ElementTree-free XML string building). No new dependencies.

Safety model:
- Only versions with `review == APPROVED`, `status == DRAFT` and prose
  are eligible; unapproved/failed versions are never exported.
- Every included version's fingerprint is re-computed and compared with
  the append-only approval ledger (RF-020) BEFORE writing. A missing
  ledger entry or a fingerprint mismatch refuses the whole export.
- Output overwrite is refused unless `force=True` (CLI: `--force`).
- Citations stay as internal `[SRC-...]` identifiers. No bibliographic
  metadata is invented: every citation is listed under a clearly
  labelled unresolved-references section and in the export manifest.
- An export manifest records sections, versions, fingerprints, source
  references, citation identifiers, output checksum, timestamp and
  validation results.

Limitations (documented, not hidden): text-only export (no draft
formatting, figures or tables), no table of contents (Word field TOC
cannot be validated offline), references are unresolved placeholders
until verified bibliography metadata exists.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from app.core import approvals
from app.core.sources import SourceError
from app.workflows.section_drafting import (CITATION_RE, SECTIONS,
                                            STATUS_DRAFT, load_section)

DEFAULT_OUTPUT = "outputs/docx/manuscript.docx"
DEFAULT_ORDER = ("introduction", "literature_review", "methodology",
                 "discussion", "conclusion")
HEADINGS = {
    "introduction": "Introduction",
    "literature_review": "Literature Review",
    "methodology": "Methodology",
    "discussion": "Discussion",
    "conclusion": "Conclusion",
}
REFERENCES_HEADING = "References (unresolved — metadata not available)"
REVIEW_APPROVED = "APPROVED"
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DOCX_REQUIRED_PARTS = (
    "[Content_Types].xml", "_rels/.rels", "word/document.xml",
    "word/styles.xml", "word/_rels/document.xml.rels",
    "docProps/core.xml", "docProps/app.xml",
)
EXPORT_LIMITATIONS = (
    "text-only export: draft formatting, figures and tables are not "
    "carried over",
    "no table of contents is generated (Word TOC fields cannot be "
    "validated offline)",
    "references are unresolved placeholders: no verified bibliographic "
    "metadata exists for the cited source identifiers",
)


class ExportError(SourceError):
    """Export refused/invalid (reuses the RF-010 error hierarchy)."""


# --------------------------------------------------------------- selection

def _latest_approved(data: dict):
    approved = [v for v in data.get("versions", [])
                if v.get("review") == REVIEW_APPROVED
                and v.get("status") == STATUS_DRAFT and v.get("prose")]
    return max(approved, key=lambda v: v["version"]) if approved else None


def _verify_fingerprint(root: Path, section: str, version: dict) -> str:
    number = version["version"]
    current = approvals.artifact_fingerprint(root, "section", section, number)
    entries = [entry for entry in approvals.load_ledger(root)
               if entry.get("artifact_kind") == "section"
               and entry.get("artifact_id") == section
               and entry.get("version") == number
               and entry.get("decision") == "APPROVED"]
    if not entries:
        raise ExportError(
            f"no approval ledger entry for section '{section}' version "
            f"{number}; approval cannot be verified — refusing export")
    if current is None:
        raise ExportError(
            f"section '{section}' version {number} is missing from disk; "
            "refusing export")
    if entries[-1].get("sha256") != current:
        raise ExportError(
            f"section '{section}' version {number} fingerprint mismatch: "
            "the artifact changed after approval (tampered?) — refusing "
            "export")
    return current


def plan_export(root: Path, sections=None, order=DEFAULT_ORDER) -> dict:
    """Resolve and verify the sections that would be exported. Read-only:
    never writes or modifies anything."""
    selected = list(sections) if sections else None
    if selected:
        unknown = [s for s in selected if s not in SECTIONS]
        if unknown:
            raise ExportError(
                f"unknown section(s): {', '.join(unknown)} (supported: "
                f"{', '.join(sorted(SECTIONS))})")
        not_in_order = [s for s in selected if s not in order]
        if not_in_order:
            raise ExportError(
                f"sections not in the export order: {', '.join(not_in_order)}")
    chosen, excluded = [], []
    for name in order:
        if selected and name not in selected:
            continue
        data = load_section(root, name)
        if data is None or not data.get("versions"):
            if selected:
                raise ExportError(f"no draft exists for section '{name}'")
            excluded.append({"section": name, "reason": "no draft"})
            continue
        version = _latest_approved(data)
        if version is None:
            if selected:
                raise ExportError(
                    f"section '{name}' has no APPROVED draft version; "
                    "unapproved or failed sections cannot be exported")
            latest = data["versions"][-1]
            excluded.append({"section": name,
                             "reason": f"latest version is not APPROVED "
                                       f"(status {latest.get('status')!r}, "
                                       f"review {latest.get('review')!r})"})
            continue
        fingerprint = _verify_fingerprint(root, name, version)
        citations = sorted(set(CITATION_RE.findall(version["prose"])))
        chosen.append({
            "section": name,
            "heading": HEADINGS[name],
            "version": version["version"],
            "fingerprint": fingerprint,
            "approved_by": version.get("reviewed_by"),
            "approved_at": version.get("reviewed_at"),
            "prose": version["prose"],
            "evidence_refs": [
                {"source_id": ref.get("source_id"),
                 "extraction_id": ref.get("extraction_id")}
                for ref in version.get("evidence_refs", [])],
            "citation_identifiers": citations,
        })
    return {"sections": chosen, "excluded": excluded}


# ---------------------------------------------------------------- document

def _run_xml(text: str) -> str:
    lines = text.split("\n")
    runs = ['<w:r><w:t xml:space="preserve">' + escape(line) + "</w:t></w:r>"
            for line in lines]
    return '<w:r><w:br/></w:r>'.join(runs)


def _paragraph_xml(text: str, style: str | None = None) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    if not text:
        return f"<w:p>{ppr}</w:p>"
    return f"<w:p>{ppr}{_run_xml(text)}</w:p>"


def _paragraphs(text: str) -> list:
    blocks = []
    for block in text.split("\n\n"):
        block = block.strip("\n").strip()
        if block:
            blocks.append(block)
    return blocks


def _document_xml(title: str, sections: list, unresolved: list) -> str:
    body = [_paragraph_xml(title, "Title")]
    for section in sections:
        body.append(_paragraph_xml(section["heading"], "Heading1"))
        for paragraph in _paragraphs(section["prose"]):
            body.append(_paragraph_xml(paragraph))
    if unresolved:
        body.append(_paragraph_xml(REFERENCES_HEADING, "Heading1"))
        body.append(_paragraph_xml(
            "The following citations use internal ResearchForge source "
            "identifiers. No verified bibliographic metadata (authors, "
            "title, venue, year, DOI) is available yet, so these are NOT "
            "complete academic references and must not be cited as such."))
        for item in unresolved:
            body.append(_paragraph_xml(
                f"[{item['citation_id']}] — unresolved reference: no "
                "verified bibliographic metadata available."))
    sect = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
            'w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>'
            "</w:sectPr>")
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="http://schemas.openxmlformats.org/'
            f'wordprocessingml/2006/main"><w:body>'
            + "".join(body) + sect + "</w:body></w:document>")


CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    'content-types">'
    '<Default Extension="rels" ContentType="application/vnd.'
    'openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.'
    'openxmlformats-package.core-properties+xml"/>'
    '<Override PartName="/docProps/app.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.extended-properties+xml"/>'
    "</Types>")

ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/'
    'package/2006/relationships/metadata/core-properties" '
    'Target="docProps/core.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/extended-properties" '
    'Target="docProps/app.xml"/>'
    "</Relationships>")

DOCUMENT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    "</Relationships>")

STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
    '2006/main">'
    '<w:docDefaults><w:rPrDefault><w:rPr>'
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="SimSun"/>'
    '<w:sz w:val="22"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="276" '
    'w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>'
    '<w:basedOn w:val="Normal"/><w:pPr><w:jc w:val="center"/>'
    '<w:spacing w:before="240" w:after="240"/></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1">'
    '<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
    '<w:pPr><w:keepNext/><w:spacing w:before="360" w:after="120"/>'
    '<w:outlineLvl w:val="0"/></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2">'
    '<w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>'
    '<w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/>'
    '<w:outlineLvl w:val="1"/></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="26"/></w:rPr></w:style>'
    "</w:styles>")


def _core_xml(title: str, created: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/'
            'package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{escape(title)}</dc:title>"
            "<dc:creator>ResearchForge</dc:creator>"
            "<cp:lastModifiedBy>ResearchForge</cp:lastModifiedBy>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{created}'
            "</dcterms:created>"
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{created}'
            "</dcterms:modified></cp:coreProperties>")


APP_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/'
    '2006/extended-properties"><Application>ResearchForge</Application>'
    "</Properties>")


def build_docx_bytes(title: str, sections: list, unresolved: list,
                     created: str) -> bytes:
    import io
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/document.xml",
                         _document_xml(title, sections, unresolved))
        archive.writestr("word/styles.xml", STYLES)
        archive.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)
        archive.writestr("docProps/core.xml", _core_xml(title, created))
        archive.writestr("docProps/app.xml", APP_XML)
    return buffer.getvalue()


# -------------------------------------------------------------- validation

def _document_text(xml_bytes: bytes) -> str:
    root = ElementTree.fromstring(xml_bytes)
    return "".join(node.text or "" for node in root.iter(f"{W_NS}t"))


def _heading_list(xml_bytes: bytes) -> list:
    root = ElementTree.fromstring(xml_bytes)
    headings = []
    for paragraph in root.iter(f"{W_NS}p"):
        style = paragraph.find(f"{W_NS}pPr/{W_NS}pStyle")
        if style is not None and style.get(f"{W_NS}val") == "Heading1":
            headings.append("".join(
                node.text or "" for node in paragraph.iter(f"{W_NS}t")))
    return headings


def validate_export(path: Path, manifest: dict | None = None) -> dict:
    """Offline structural validation of a generated DOCX package."""
    path = Path(path)
    checks, problems = [], []

    def check(name, ok, detail=""):
        checks.append({"check": name, "status": "PASS" if ok else "FAIL",
                       "detail": detail})
        if not ok:
            problems.append(f"{name}: {detail}")

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            corrupt = archive.testzip()
            parts = {name: archive.read(name)
                     for name in DOCX_REQUIRED_PARTS if name in names}
        check("zip_integrity", corrupt is None,
              "archive is readable" if corrupt is None
              else f"corrupt member: {corrupt}")
        missing = [p for p in DOCX_REQUIRED_PARTS if p not in names]
        check("required_parts", not missing,
              "all required parts present" if not missing
              else "missing: " + ", ".join(missing))
        wellformed = True
        for name, raw in parts.items():
            if not name.endswith((".xml", ".rels")):
                continue
            try:
                ElementTree.fromstring(raw)
            except ElementTree.ParseError as exc:
                wellformed = False
                problems.append(f"{name} not well-formed: {exc}")
        check("xml_well_formed", wellformed,
              "every XML part parses" if wellformed else "invalid XML part(s)")
        if not wellformed or missing:
            return {"status": "FAIL", "checks": checks, "problems": problems}
        text = _document_text(parts["word/document.xml"])
        headings = _heading_list(parts["word/document.xml"])
        if manifest is not None:
            expected = [s["heading"] for s in manifest.get("sections", [])]
            if manifest.get("unresolved_references"):
                expected.append(REFERENCES_HEADING)
            check("section_order", headings == expected,
                  "headings match manifest order" if headings == expected
                  else f"expected {expected}, found {headings}")
            citations = manifest.get("citation_identifiers", [])
            missing_citations = [c for c in citations if f"[{c}]" not in text]
            check("citations_preserved", not missing_citations,
                  "all citation identifiers preserved"
                  if not missing_citations
                  else "missing: " + ", ".join(missing_citations))
            recorded = (manifest.get("output") or {}).get("sha256")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            check("output_checksum", recorded == digest,
                  "manifest checksum matches file" if recorded == digest
                  else "manifest checksum does not match the file")
    except (zipfile.BadZipFile, OSError) as exc:
        check("zip_integrity", False, f"not a readable DOCX: {exc}")
        return {"status": "FAIL", "checks": checks, "problems": problems}
    return {"status": "PASS" if not problems else "FAIL",
            "checks": checks, "problems": problems}


def libreoffice_available() -> str | None:
    return shutil.which("soffice")


def libreoffice_check(path: Path) -> dict:
    """Optional extra validation when LibreOffice is already installed.
    Never installs anything."""
    executable = libreoffice_available()
    if executable is None:
        return {"status": "SKIPPED",
                "detail": "LibreOffice is not installed; not required"}
    with tempfile.TemporaryDirectory() as tmp:
        try:
            result = subprocess.run(
                [executable, "--headless", "--convert-to", "pdf",
                 "--outdir", tmp, str(path)],
                capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"status": "FAIL", "detail": str(exc)}
    return {"status": "PASS" if result.returncode == 0 else "FAIL",
            "detail": f"soffice exit {result.returncode}"}


# ------------------------------------------------------------------ export

def export_docx(root: Path, output=DEFAULT_OUTPUT, title="Manuscript",
                sections=None, order=DEFAULT_ORDER, force=False) -> dict:
    """Assemble approved sections into a DOCX + manifest. Refuses to
    overwrite an existing output unless force=True."""
    root = Path(root)
    out = Path(output)
    if not out.is_absolute():
        out = root / out
    if out.exists() and not force:
        raise ExportError(
            f"output already exists: {out} (refusing to overwrite; use "
            "--force after reviewing the existing document)")
    plan = plan_export(root, sections, order)
    chosen = plan["sections"]
    if not chosen:
        raise ExportError(
            "no approved sections are available for export; approve a "
            "section draft first")
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cited = sorted({cid for section in chosen
                    for cid in section["citation_identifiers"]})
    unresolved = [{"citation_id": cid, "status": "UNRESOLVED",
                   "reason": "no verified bibliographic metadata available"}
                  for cid in cited]
    data = build_docx_bytes(title, chosen, unresolved, created)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name + ".tmp")
    temp.write_bytes(data)
    os.replace(temp, out)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    try:
        relative = out.relative_to(root).as_posix()
    except ValueError:
        relative = str(out)
    manifest = {
        "document": "manuscript_export",
        "format": "docx",
        "created_at": created,
        "title": title,
        "output": {"path": relative, "sha256": digest,
                   "size_bytes": len(data)},
        "sections": [{key: section[key] for key in
                      ("section", "heading", "version", "fingerprint",
                       "approved_by", "approved_at", "evidence_refs",
                       "citation_identifiers")}
                     for section in chosen],
        "excluded_sections": plan["excluded"],
        "citation_identifiers": cited,
        "unresolved_references": unresolved,
        "limitations": list(EXPORT_LIMITATIONS),
        "libreoffice": None,
        "validation": None,
    }
    manifest_path = out.with_name(out.stem + ".manifest.json")
    manifest["manifest_path"] = manifest_path.relative_to(root).as_posix() \
        if manifest_path.is_relative_to(root) else str(manifest_path)
    result = validate_export(out, manifest)
    manifest["validation"] = result
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    if result["status"] != "PASS":
        for leftover in (out, manifest_path):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise ExportError("generated DOCX failed validation: "
                          + "; ".join(result["problems"]))
    return manifest
