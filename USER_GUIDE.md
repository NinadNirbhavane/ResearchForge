# ResearchForge — User Guide

ResearchForge is a local, browser-based research command centre. The research
engine and all records stay in this folder; nothing is uploaded or sent to a
cloud service. AI drafting uses only the DeepSeek key you placed in the
project-local `.env` file, and only when you explicitly request it.

---

## 1. Starting ResearchForge

1. Open the project folder: `C:\Users\NINAD\Documents\Research MCA`.
2. Double-click **`ResearchForge.bat`**.

A console window opens, the local server starts, and the workspace opens in an
application-style Microsoft Edge window (or your default browser). The console
prints the local URL: `http://127.0.0.1:8765/`.

If ResearchForge is already running, the launcher detects it and simply opens
the existing workspace instead of starting a second server.

**Stopping:** press `Ctrl+C` in the console window, or close the console window.
Closing the browser tab does not stop the server by itself.

The server listens only on `127.0.0.1` (this computer). Do not expose it to a
network.

## 2. Navigating the workspace

- **Overview** — real project/phase information, review counts, pipeline
  status, outstanding blockers and recent recorded activity.
- **Evidence Review** — pending/approved/rejected candidates with full
  passages and provenance.
- **Source Library** — imported documents, validation and extraction state.
- **Literature Synthesis**, **Research Gaps**, **Manuscript**, **Reviews**,
  **Export** — the remaining research stages.

Set your **reviewer name** in the header before approving anything. It is
stored only in this browser and is recorded with each decision.

## 3. Importing documents

1. Open **Source Library** → **Import documents**.
2. Drag files onto the drop area or choose them with the file picker.
3. Choose provenance and evidence status (default: `SEARCH RESULT ONLY`).
4. Click **Import selected**.

Supported formats: PDF, DOCX, TXT, Markdown, CSV, BibTeX — maximum 50 MB per
file. Each result is reported separately:

- **IMPORTED** — copied into `sources/` and text extraction attempted.
- **DUPLICATE** — identical content already exists; the existing source and its
  extraction are left untouched.
- **REJECTED / FAILED** — the engine refused the file (malformed PDF/DOCX,
  unsupported content, etc.); the reason is shown.

Importing never approves evidence and never classifies candidates.

## 4. Extraction limitations (important)

- **DOCX** text is extracted locally with the Python standard library.
  Formatting, footnotes, tracked changes and images are not preserved.
- **PDF** text extraction requires the external `pdftotext` utility (Poppler).
  It is **not installed on this machine by default**. Without it, PDFs import
  successfully but extraction is recorded as `UNSUPPORTED`.
- **Scanned/image-only PDFs** produce an explicit `EMPTY` result. OCR is not
  supported.
- **TXT/MD/CSV/BibTeX** require UTF-8; non-UTF-8 files import but extraction is
  recorded as `FAILED` with the reason.
- Imported text is copied; the original document is never modified.

To enable PDF extraction later, install Poppler so that `pdftotext` is on
`PATH`, then restart ResearchForge. No other configuration is required.

## 5. Reviewing evidence

Open **Evidence Review**, select a source (or all), and read candidates in the
reading pane. Each passage shows its source, page, section, paragraph and
candidate ID.

- **Approve as evidence** / **Reject passage** — one candidate at a time,
  after an explicit confirmation. Decisions are final.
- Rejected candidates never feed synthesis; approved ones do.
- Duplicate decisions from another tab are reported as a conflict and the view
  refreshes.

## 6. Literature synthesis and research gaps

- **Synthesis** runs over approved evidence only. Check the box for AI
  interpretation only if you want an extra provider call; it is logged in
  `logs/usage.jsonl` and never retried automatically.
- **Approve synthesis** requires your reviewer name and is final.
- **Research Gaps** stays blocked until synthesis is approved. It lists
  directly evidenced limitations, cross-source patterns, explicitly labelled
  opportunities and an always-empty novelty-hypothesis section. Novelty is
  never claimed from this collection alone.

## 7. Manuscript drafting and approval

Open **Manuscript** and choose a draftable section: Introduction, Literature
Review, Methodology, Discussion or Conclusion.

- **Draft new version** calls the configured provider and appends a version.
  Previous versions are never overwritten.
- Optional context is stored separately from evidence and is never cited.
- Review citations and unsupported statements before approving.
- **Approve selected version** requires explicit confirmation and a reviewer;
  approved versions are immutable.
- Objectives, Architecture, Outcome and References are shown as unavailable —
  the current engine has no draftable section for them. They are not
  fabricated.

## 8. Independent reviews

- Choose a section (and optional version) and run a review. Deterministic
  checks cover citations, provenance, unsupported statements and draft state.
  **AI review** is opt-in and logged.
- Findings show severity, category and rationale. They are not approved
  automatically.
- **External review import** stores a pasted Gemini/Perplexity/ChatGPT critique
  verbatim, separately from internal findings, with platform provenance.

## 9. DOCX export

Open **Export**:

1. Review the readiness checklist (approved sections, excluded sections,
   citation identifiers, unresolved references).
2. Choose the sections, title and filename.
3. Click **Generate DOCX** and confirm.

Only APPROVED section versions are included and their approval fingerprints are
verified. Existing files are not overwritten unless you tick **Overwrite**.
Unresolved references remain clearly labelled. Download the result from the
export list.

## 10. Blocked states

ResearchForge deliberately refuses unsafe or empty steps:

- Drafting and gap analysis are blocked until synthesis is approved.
- Synthesis with no approved evidence cannot be approved.
- Export is disabled with no approved manuscript sections.
- Empty/unsupported extraction never counts as success.
- Conflicts, missing reviewer names and missing confirmations produce clear
  messages and non-zero results.

## 11. Backing up

Everything lives in this folder. Before major changes, copy the whole
`C:\Users\NINAD\Documents\Research MCA` folder to a backup location (or use
version control, if you initialise a repository). Important folders:

- `sources/` — manifest, papers, extraction records
- `outputs/` — drafts, sections, review reports, exports, approval ledger
- `research/` — knowledge-base documents
- `config/`, `.env` — configuration and local credentials

Do not copy `.env` to shared or cloud locations.

## 12. Troubleshooting

| Symptom | What to do |
|---|---|
| Launcher says `.venv` is missing | Recreate the virtual environment and run the tests as documented in the project instructions. |
| Port 8765 already in use by another program | Start with a different port: `.venv\Scripts\python.exe -m app.main ui --port 8766`. |
| Page shows “Cannot reach the local server” | Check the console window is still open; restart the launcher. |
| PDF extraction shows `UNSUPPORTED` | Install Poppler/`pdftotext` or convert the PDF to UTF-8 text and import that file. |
| “Provider unavailable” when drafting | Check `DEEPSEEK_API_KEY` in the project `.env`; no request is made until you confirm. |
| Export refuses to overwrite | Tick **Overwrite** only after reviewing the existing document. |
| A decision reports a conflict | Another tab/process decided first; reload the view. |

No API keys, provider settings or filesystem internals are exposed in the
browser.
