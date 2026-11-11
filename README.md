::: {align="center"}
# ResearchForge

### A local-first research workspace for evidence-driven academic writing

**Organise literature. Review evidence. Find research gaps. Draft,
review, and export --- with the researcher in control.**

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Interface](https://img.shields.io/badge/Interface-Local%20Web%20UI-6f42c1)
![Architecture](https://img.shields.io/badge/Approach-Local--first-198754)
![Status](https://img.shields.io/badge/Release-V1-informational)
:::

------------------------------------------------------------------------

## What is ResearchForge?

ResearchForge is a local-first research-management and orchestration
application designed to support the academic research workflow from
source collection to manuscript export.

It brings research documents, extracted evidence candidates, literature
synthesis, research-gap analysis, manuscript drafting, independent
reviews, and DOCX export into one workspace.

ResearchForge is designed around one important principle:

> **AI can assist the research process, but the researcher remains
> responsible for evaluating and approving evidence and written
> claims.**

The application runs locally and opens in your browser at
`127.0.0.1:8765`. Its browser interface is a local control surface for
the existing Python research engine.

## At a glance

-   **Source Library** --- import and organise research documents.
-   **Evidence Review** --- inspect candidate evidence in context and
    make individual approval or rejection decisions.
-   **Literature Synthesis** --- organise approved evidence into
    research themes.
-   **Research Gaps** --- examine potential gaps supported by the
    available evidence.
-   **Manuscript Workspace** --- draft, revise, version, and approve
    manuscript sections.
-   **Independent Reviews** --- run structured reviews of research
    outputs.
-   **DOCX Export** --- produce and validate a Word document.
-   **Human oversight** --- evidence decisions are explicit; AI-assisted
    operations are opt-in.

## Screenshots

The screenshots below are intended to show the actual running
application. Add genuine captures from your local ResearchForge instance
to the `docs/screenshots/` folder using the filenames below.

  ---------------------------------------------------------------------------
  Overview                            Source Library
  ----------------------------------- ---------------------------------------
  `docs/screenshots/overview.png`     `docs/screenshots/source-library.png`

  Capture the dashboard and           Capture the imported source list and
  navigation.                         extraction/validation status.
  ---------------------------------------------------------------------------

  --------------------------------------------------------------------------------------
  Evidence Review                          Literature Synthesis
  ---------------------------------------- ---------------------------------------------
  `docs/screenshots/evidence-review.png`   `docs/screenshots/literature-synthesis.png`

  Capture a candidate with its full        Capture the synthesis workspace and its
  passage and provenance visible.          themes/results.
  --------------------------------------------------------------------------------------

  --------------------------------------------------------------------------
  Research Gaps                          Manuscript
  -------------------------------------- -----------------------------------
  `docs/screenshots/research-gaps.png`   `docs/screenshots/manuscript.png`

  Capture the gap analysis view.         Capture a manuscript section and
                                         its version/approval controls.
  --------------------------------------------------------------------------

  Reviews and Export
  -----------------------------------------------------------------
  `docs/screenshots/reviews-export.png`
  Capture the review results and DOCX export/validation controls.

> **Screenshot note:** These are capture targets, not screenshots
> bundled with this README. Add only screenshots taken from the real
> application. Avoid including API keys, private source documents,
> unpublished research notes, personal data, or other sensitive
> information.

## Workflow

``` text
Research documents
       |
       v
   Source Library
       |
       v
  Evidence extraction
       |
       v
   Evidence Review  <--- Researcher verifies each candidate
       |
       v
 Literature Synthesis
       |
       v
 Research Gap Analysis
       |
       v
 Manuscript Drafting
       |
       v
 Independent Reviews
       |
       v
   DOCX Export
```

The workflow is iterative. You can return to source review, refine
synthesis, revise manuscript sections, and repeat reviews as your
research develops.

## Getting started

### Requirements

-   Windows 10/11 or a compatible Python environment.
-   Python 3.10 or later.
-   Git (optional, for cloning and version control).
-   Microsoft Word or a compatible editor for exported DOCX files.
-   `pdftotext` from Poppler for PDF text extraction. If it is not
    installed, PDF extraction may be reported as unsupported. DOCX, TXT,
    Markdown, CSV, and BibTeX ingestion are supported by the
    document-ingestion workflow.

### 1. Get the project

Clone the repository:

``` powershell
git clone https://github.com/NinadNirbhavane/ResearchForge.git
cd ResearchForge
```

Or download the repository ZIP from GitHub and extract it.

### 2. Create and activate a virtual environment

``` powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, you can invoke the environment's Python
executable directly:

``` powershell
.\.venv\Scripts\python.exe --version
```

### 3. Install dependencies

``` powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configure optional AI access

Copy the example environment file:

``` powershell
Copy-Item .env.example .env
```

Open `.env` and add your own DeepSeek API key only if you intend to use
the supported DeepSeek integration:

``` dotenv
DEEPSEEK_API_KEY=your-real-key-here
```

Keep `.env` private. It is excluded by `.gitignore`; never commit or
publish real API keys. ResearchForge's documented credential handling
loads the key from the project-root `.env` file.

You can use the local research-management workflow without enabling an
AI provider. AI-assisted operations should be invoked deliberately.

### 5. Launch ResearchForge

On Windows, run:

``` powershell
.\ResearchForge.bat
```

Alternatively:

``` powershell
python -m app.main ui
```

Open the local address if your browser does not open automatically:

``` text
http://127.0.0.1:8765
```

To stop the local server, return to the terminal and press `Ctrl+C`.

## How to use ResearchForge

### 1. Start in Source Library

Import the papers, reports, and other materials relevant to your
research question.

For each source, check: - Correct title, authors, year, and source
metadata. - Whether text extraction completed successfully. - Whether
validation reports any issues. - Whether the imported document is the
intended version.

Do not treat a research proposal or your own notes as external academic
literature.

### 2. Review evidence candidates

Open **Evidence Review** and inspect candidates individually.

For every candidate: 1. Read the complete passage, not just the short
excerpt. 2. Check the source and its provenance. 3. Confirm that the
passage actually supports the proposed claim. 4. Approve or reject the
candidate based on your judgement. 5. Use the available
reviewer/confirmation controls as required.

Do not bulk-approve evidence simply to move to the next stage. An
extracted passage is a candidate, not automatically a verified fact.

### 3. Build the literature synthesis

Use approved evidence to identify themes, compare findings, and organise
the literature around your research questions.

Look for: - Areas where studies agree. - Differences in methods,
datasets, settings, or findings. - Limitations acknowledged by the
original authors. - Topics that remain under-explored.

Keep the distinction between what a source states and your
interpretation of it.

### 4. Examine research gaps

Use the Research Gaps workspace to examine potential gaps emerging from
the reviewed evidence.

Treat a generated gap as a research lead that requires checking against
the underlying papers. Do not present a gap as novel or globally
unprecedented without a sufficiently broad literature search.

### 5. Draft and manage manuscript sections

Use the Manuscript workspace to create and revise sections. Preserve
version history and review the text before approving it.

Check every citation, factual claim, number, method, and technical
statement against the original source or your own verified work. Do not
present proposed methods as completed experiments or expected outcomes
as measured results.

### 6. Run independent reviews

Use the review workflow to identify issues in structure, clarity,
evidence support, methodology, and consistency. A review result is
feedback for the researcher, not an automatic certification of academic
correctness.

Resolve important issues and repeat the review when appropriate.

### 7. Export your document

Use the DOCX export workflow to generate a Word document. Open the
exported file and inspect: - Headings and section order. - Tables,
figures, captions, and references. - Page breaks and spacing. - Citation
and bibliography consistency. - Any remaining placeholders or
verification markers.

The exported document should receive a final human proofread before
submission.

## Command-line interface

ResearchForge also exposes CLI workflows. Run:

``` powershell
python -m app.main --help
```

Use the help output to discover the commands and options available in
the installed version. The browser UI and CLI operate on the same
research workspace; avoid running overlapping import or
evidence-decision operations concurrently.

## Project structure

``` text
ResearchForge/
├── app/
│   ├── cli/             # Command-line workflows
│   ├── core/            # Sources, approvals, configuration, locking
│   ├── exporters/       # DOCX export
│   ├── providers/       # AI provider interfaces and adapters
│   ├── ui/              # Local web server and browser interface
│   └── workflows/       # Extraction, synthesis, gap analysis, drafting, review
├── config/              # Application/model configuration
├── tests/               # Automated test suite
├── .env.example         # Safe environment-variable template
├── requirements.txt
├── ResearchForge.bat    # Windows launcher
└── USER_GUIDE.md        # Additional usage guidance
```

Local research materials, generated outputs, runtime state, and private
project notes are intentionally excluded from the public software
repository by `.gitignore`.

## Privacy, safety, and research integrity

-   ResearchForge is designed to run locally; the local UI binds to
    `127.0.0.1`.
-   Keep API credentials in your local `.env` file.
-   Do not publish private source files, participant data, unpublished
    findings, or personal information.
-   Review source passages before approving evidence.
-   Keep a clear distinction between source-derived facts, researcher
    interpretation, and AI-generated suggestions.
-   AI-generated text must be checked and rewritten where needed; it is
    not a substitute for reading the literature.
-   Follow your institution's research ethics, consent, privacy,
    citation, and academic-integrity requirements.

## Current limitations

-   PDF text extraction depends on Poppler's `pdftotext` utility being
    installed and available on `PATH`; OCR is not included.
-   A standalone Windows `.exe` installer is not currently provided.
-   The interface is intended for local desktop use; mobile
    responsiveness has not been fully validated.
-   Some CLI workflows do not share the UI's workflow lock. Avoid
    concurrent operations that modify the same workspace.
-   ResearchForge supports research organisation and drafting workflows;
    it does not independently verify the truth, originality, or
    completeness of academic claims.

## Testing

Run the test suite from the project root:

``` powershell
python -m unittest discover -s tests -v
```

The V1 release verification recorded **480 passing tests**. Re-run the
suite in your own environment after installing dependencies or making
changes.

## Configuration and documentation

-   [`USER_GUIDE.md`](USER_GUIDE.md) --- additional user instructions.
-   [`.env.example`](.env.example) --- environment configuration
    template.
-   [`config/models.json`](config/models.json) --- provider and model
    configuration.

## Contributing

Issues, bug reports, and focused improvements are welcome. Before
submitting a change:

1.  Describe the problem and expected behaviour.
2.  Keep private research data and credentials out of commits.
3.  Add or update tests for behavioural changes.
4.  Run the test suite.
5.  Document any new configuration or workflow.

## License

No license has been specified yet. Until a license is added to this
repository, all rights are reserved by the copyright holder; do not
assume the code is available for reuse, redistribution, or modification
under an open-source license.

------------------------------------------------------------------------

::: {align="center"}
**ResearchForge V1**\
*Make research traceable. Keep evidence reviewable. Keep the researcher
in control.*
:::
