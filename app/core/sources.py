"""Source import/storage core (RF-010). No CLI presentation here.

Design:
- Metadata: one JSON manifest (sources/sources_manifest.json), written
  atomically (temp file + os.replace). Chosen over SQLite per PROJECT.md
  (no database yet) and over per-source sidecar files so duplicate
  detection and validation need one small indexed file.
- Import sequence: validate -> stream SHA-256 -> duplicate/collision
  check -> staged copy (.part) -> atomic rename -> manifest update.
  If anything fails before the manifest is written, staged/stored files
  are removed, so an interrupted import leaves no completed record.
- Imported files are treated as untrusted data: they are copied verbatim,
  never executed, and never parsed as instructions.
"""
import csv
import hashlib
import io
import json
import os
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app.core import locking

MANIFEST_NAME = "sources_manifest.json"
PAPER_DIR = "sources/papers"
NOTES_DIR = "sources/extracted_notes"
MANIFEST_LOCK_NAME = ".manifest.lock"
IMPORT_LOCK_TIMEOUT = 30.0
CHUNK_SIZE = 65536

EVIDENCE_STATUSES = ("FULL TEXT READ", "ABSTRACT ONLY", "METADATA VERIFIED",
                     "SEARCH RESULT ONLY", "INACCESSIBLE", "GREY LITERATURE")
PROVENANCES = ("perplexity", "gemini", "chatgpt", "zotero", "other", "researcher")

# extension -> (storage subdir, source_type)
TYPE_BY_EXT = {
    ".pdf": (PAPER_DIR, "pdf"),
    ".docx": (PAPER_DIR, "docx"),
    ".md": (NOTES_DIR, "markdown"),
    ".txt": (NOTES_DIR, "text"),
    ".bib": (NOTES_DIR, "bibtex"),
    ".csv": (NOTES_DIR, "csv"),
}


class SourceError(Exception):
    """Rejected import/validation. exit_code distinguishes cause."""
    exit_code = 2


class DuplicateSourceError(SourceError):
    exit_code = 3  # duplicate checksum or storage collision


def manifest_path(root: Path) -> Path:
    return root / "sources" / MANIFEST_NAME


def load_manifest(root: Path) -> dict:
    path = manifest_path(root)
    if not path.is_file():
        return {"version": 1, "sources": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceError(f"source manifest unreadable or corrupt: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        raise SourceError("source manifest malformed")
    return data


def write_manifest(root: Path, manifest: dict) -> None:
    path = manifest_path(root)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def file_digest(path: Path) -> tuple:
    """Streamed SHA-256 + size; never loads the whole file into memory."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(CHUNK_SIZE)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def is_valid_pdf(path: Path) -> bool:
    """Header check only — no parsing/extraction (documented limitation)."""
    with open(path, "rb") as handle:
        return handle.read(1024).startswith(b"%PDF-")


DOCX_DOCUMENT_PART = "word/document.xml"


def is_valid_docx(path: Path) -> bool:
    """True when the file is a readable ZIP archive containing the main
    Word document part. Content parsing happens at extraction time
    (RF-015/MVP-02); macros/embedded objects are never executed."""
    try:
        with zipfile.ZipFile(path) as archive:
            return DOCX_DOCUMENT_PART in archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def validate_csv_structure(path: Path) -> int:
    """Streamed CSV structure check. Returns data-row count.
    Raises SourceError on empty/headerless/ragged/non-UTF-8 files."""
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                raise SourceError("CSV file is empty")
            if not any(cell.strip() for cell in header):
                raise SourceError("CSV header row is empty")
            count = 0
            for lineno, row in enumerate(reader, start=2):
                if len(row) != len(header):
                    raise SourceError(
                        f"CSV malformed: line {lineno} has {len(row)} fields, "
                        f"expected {len(header)}")
                count += 1
            return count
    except UnicodeDecodeError as exc:
        raise SourceError(f"CSV is not valid UTF-8: {exc}") from exc


def validate_bibtex_basic(text: str) -> list:
    """Basic well-formedness only (no full BibTeX grammar). Returns warnings;
    imports proceed with warnings recorded and staged for review."""
    warnings = []
    if text.count("{") != text.count("}"):
        warnings.append("unbalanced braces")
    if not re.search(r"@\w+\s*\{", text):
        warnings.append("no @entry found")
    return warnings


def import_source(root: Path, input_path, provenance="researcher",
                  verification_status="SEARCH RESULT ONLY") -> dict:
    """Import one file. Raises FileNotFoundError, SourceError (rejected,
    exit 2) or DuplicateSourceError (duplicate/collision, exit 3)."""
    if provenance not in PROVENANCES:
        raise SourceError(f"unknown provenance: {provenance} "
                          f"(expected one of {', '.join(PROVENANCES)})")
    if verification_status not in EVIDENCE_STATUSES:
        raise SourceError(f"verification status must be one of: "
                          f"{', '.join(EVIDENCE_STATUSES)}")
    src = Path(input_path)
    if not src.is_file():
        raise FileNotFoundError(f"input file not found: {src}")
    ext = src.suffix.lower()
    if ext not in TYPE_BY_EXT:
        raise SourceError(
            f"unsupported file type '{ext or '<none>'}' "
            f"(native: {', '.join(sorted(TYPE_BY_EXT))}; PDF text "
            "extraction needs the external 'pdftotext' utility — otherwise "
            "convert the PDF to UTF-8 text and import that file)")
    if ext == ".pdf" and not is_valid_pdf(src):
        raise SourceError("not a readable PDF (missing %PDF- header)")
    if ext == ".docx" and not is_valid_docx(src):
        raise SourceError(
            "not a readable DOCX (invalid ZIP archive or missing "
            "word/document.xml)")

    warnings = []
    if ext == ".csv":
        validate_csv_structure(src)
    elif ext == ".bib":
        warnings = validate_bibtex_basic(src.read_text(encoding="utf-8"))

    def _commit() -> dict:
        # Duplicate detection and the manifest update must be one atomic
        # critical section: CLI and UI imports share this lock.
        manifest = load_manifest(root)
        digest, size = file_digest(src)
        source_id = f"SRC-{digest[:12]}"
        if source_id in manifest["sources"]:
            existing = manifest["sources"][source_id]
            raise DuplicateSourceError(
                f"duplicate content: identical SHA-256 already imported as "
                f"{source_id} (original: {existing['original_filename']}); "
                "not imported again")

        subdir, source_type = TYPE_BY_EXT[ext]
        stored_rel = f"{subdir}/{source_id}{ext}"
        stored = root / stored_rel
        if stored.exists():
            raise DuplicateSourceError(
                f"storage collision: {stored_rel} already exists on disk; "
                "refusing to overwrite")

        record = {
            "source_id": source_id,
            "original_filename": src.name,
            "original_location": str(src.resolve()),
            "source_type": source_type,
            "imported_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "stored_location": stored_rel,
            "size_bytes": size,
            "sha256": digest,
            "provenance": provenance,
            "verification_status": verification_status,
            "import_status": "IMPORTED",
            "validation_warnings": warnings,
        }

        staged = stored.with_name(stored.name + ".part")
        try:
            with open(src, "rb") as fin, open(staged, "wb") as fout:
                shutil.copyfileobj(fin, fout, CHUNK_SIZE)
            os.replace(staged, stored)
            manifest["sources"][source_id] = record
            write_manifest(root, manifest)
        except Exception:
            # Nothing was recorded, so no misleading completed record remains.
            for leftover in (staged, stored):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            raise
        return record

    try:
        with locking.exclusive_lock(
                root / "sources" / MANIFEST_LOCK_NAME,
                timeout=IMPORT_LOCK_TIMEOUT):
            return _commit()
    except locking.LockTimeout as exc:
        raise SourceError(
            "another import is updating the source manifest; retry shortly"
        ) from exc


def get_source(root: Path, source_id: str):
    manifest = load_manifest(root)
    return manifest["sources"].get(source_id)


def validate_sources(root: Path) -> list:
    """Re-verify every manifest record: stored file present, size and
    checksum match. Returns rows for presentation."""
    manifest = load_manifest(root)
    rows = []
    for source_id in sorted(manifest["sources"]):
        record = manifest["sources"][source_id]
        stored = root / record["stored_location"]
        problems = []
        if not stored.is_file():
            problems.append("stored file missing")
        else:
            digest, size = file_digest(stored)
            if size != record["size_bytes"]:
                problems.append(f"size mismatch ({size} != {record['size_bytes']})")
            if digest != record["sha256"]:
                problems.append("checksum mismatch")
        rows.append({"source_id": source_id,
                     "level": "PASS" if not problems else "ERROR",
                     "messages": problems or ["OK"], "record": record})
    return rows
