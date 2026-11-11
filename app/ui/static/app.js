/* ResearchForge UI-02 — Research command centre.
   Vanilla JS, no dependencies. All source-derived text uses textContent. */
(() => {
  "use strict";

  const tokenMeta = document.querySelector('meta[name="rf-token"]');
  const token = tokenMeta ? tokenMeta.content : "";
  const $ = (id) => document.getElementById(id);

  const VIEWS = {
    overview: { title: "Overview", crumb: "Overview" },
    review: { title: "Evidence Review", crumb: "Evidence Review" },
    library: { title: "Source Library", crumb: "Source Library" },
    synthesis: { title: "Literature Synthesis", crumb: "Literature Synthesis" },
    gaps: { title: "Research Gaps", crumb: "Research Gaps" },
    manuscript: { title: "Manuscript", crumb: "Manuscript" },
    reviews: { title: "Reviews", crumb: "Reviews" },
    export: { title: "Export", crumb: "Export" },
  };

  const state = {
    overview: null,
    sources: [],
    view: "overview",
    connection: "unknown",
    reviewLoaded: false,
    libraryLoaded: false,
    review: {
      activeSource: "",
      decision: "pending",
      classification: "",
      q: "",
      page: 1,
      pages: 1,
      total: 0,
      items: [],
      selectedId: null,
      detail: null,
    },
    importFiles: [],
    library: { query: "", status: "", availability: "", category: "",
               extraction: "", validation: "", sort: "import",
               selected: null },
    modal: null,
    deciding: false,
    workflow: { synthesis: null, gaps: null, manuscript: null,
                reviews: null, export: null },
    msSection: null,
    msVersion: null,
    reviewId: null,
  };

  class UIError extends Error {
    constructor(status, code, message) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  /* ------------------------------------------------------------- helpers */
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function titleCase(value) {
    if (!value) return "Unclassified";
    return String(value).replace(/_/g, " ")
      .replace(/\b\w/g, (character) => character.toUpperCase());
  }

  function short(value, length) {
    const text = String(value || "");
    return text.length > length ? text.slice(0, length - 1) + "…" : text;
  }

  function toast(message, kind) {
    const node = el("div", "toast" + (kind ? " " + kind : ""), message);
    $("toast-region").appendChild(node);
    setTimeout(() => node.remove(), 4200);
  }

  async function request(path, options) {
    const method = options.method || "GET";
    const attempts = method === "GET" ? 2 : 1;
    let lastError = null;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        const response = await fetch(path, options);
        if (method === "GET" && response.status >= 500 && attempt === 0) {
          await new Promise((resolve) => setTimeout(resolve, 500));
          continue;
        }
        return response;
      } catch (error) {
        lastError = error;
        if (attempt + 1 < attempts) {
          await new Promise((resolve) => setTimeout(resolve, 600));
        }
      }
    }
    throw lastError || new Error("request failed");
  }

  async function api(path, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    if (options.method === "POST") {
      headers["Content-Type"] = "application/json";
      headers["X-ResearchForge-Token"] = token;
    }
    let response;
    try {
      response = await request(path, { ...options, headers,
        credentials: "same-origin" });
    } catch (error) {
      setConnection(false);
      throw new UIError(0, "connection",
        "Cannot reach the local ResearchForge server.");
    }
    let body = null;
    try {
      body = await response.json();
    } catch (error) {
      body = null;
    }
    if (!response.ok || body === null) {
      const detail = (body && body.error) || {};
      throw new UIError(response.status, detail.code || "error",
        detail.message || ("Request failed (" + response.status + ")."));
    }
    setConnection(true);
    return body;
  }

  function setConnection(online) {
    state.connection = online ? "online" : "offline";
    ["connection", "sidebar-connection"].forEach((id) => {
      const node = $(id);
      if (!node) return;
      node.classList.toggle("is-online", online);
      node.classList.toggle("is-offline", !online);
    });
    $("connection-text").textContent = online ? "Local server" : "Offline";
    $("sidebar-connection-text").textContent = online ? "Online" : "Offline";
  }

  function showError(error) {
    $("error-text").textContent = error.message || String(error);
    $("error-banner").hidden = false;
  }

  function hideError() {
    $("error-banner").hidden = true;
  }

  function reviewerValue() {
    return $("reviewer-input").value.trim();
  }

  /* --------------------------------------------------------------- theme */
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    $("theme-toggle").textContent = theme === "dark" ? "☾" : "☀";
    try { localStorage.setItem("rf-theme", theme); } catch (error) { /* */ }
  }

  function initTheme() {
    let theme = "dark";
    try {
      theme = localStorage.getItem("rf-theme") ||
        (window.matchMedia("(prefers-color-scheme: light)").matches
          ? "light" : "dark");
    } catch (error) { /* */ }
    applyTheme(theme);
  }

  function restoreReviewer() {
    try {
      const saved = localStorage.getItem("rf-reviewer");
      if (saved) $("reviewer-input").value = saved;
    } catch (error) { /* */ }
    updateReviewerUI();
  }

  function updateReviewerUI() {
    const name = reviewerValue();
    const label = $("decision-reviewer");
    if (label) label.textContent = name || "not set";
    if (state.review.detail) setDecisionEnabled();
  }

  /* ------------------------------------------------------------ overview */
  async function refreshAll() {
    hideError();
    try {
      const [overview, sources] = await Promise.all([
        api("/api/overview"), api("/api/sources?validate=1"),
      ]);
      state.overview = overview;
      state.sources = sources.sources || [];
      renderOverview();
      renderSidebar();
      if (state.view === "review") {
        renderReviewSources();
        await loadCandidates();
      }
      if (state.view === "library") renderLibrary();
      if (["synthesis", "gaps", "manuscript", "reviews", "export"]
          .includes(state.view)) {
        await loadWorkflow(state.view);
      }
    } catch (error) {
      setConnection(false);
      showError(error);
    }
  }

  function renderSidebar() {
    const project = state.overview.project;
    $("side-project-title").textContent = project.title;
    $("side-project-phase").textContent = project.phase || "Stage unavailable";
    $("app-version").textContent = "v" + project.version;
    $("crumb-workspace").textContent = project.workspace;
    const pending = state.overview.candidates.pending;
    const badge = $("nav-pending");
    badge.hidden = pending === 0;
    badge.textContent = pending.toLocaleString();
  }

  function renderOverview() {
    const data = state.overview;
    const project = data.project;
    $("ov-title").textContent = project.title;
    $("ov-description").textContent = project.description ||
      "No research problem statement is recorded for this workspace yet.";
    $("ov-phase").textContent = project.phase || "Stage unavailable";
    $("ov-workspace").textContent = project.workspace;

    const counts = data.candidates;
    $("ov-pending").textContent = counts.pending.toLocaleString();
    $("ov-approved").textContent = counts.approved.toLocaleString();
    $("ov-rejected").textContent = counts.rejected.toLocaleString();
    $("ov-sources").textContent = data.sources.total;
    $("ov-source-detail").textContent =
      data.sources.full_text + " full text · " +
      data.sources.research_brief + " research brief";

    renderPipeline(data.pipeline);
    renderBlockers(data.blockers);
    renderActivity(data.activity);

    const split = $("ov-source-split");
    clear(split);
    [["Full text", data.sources.full_text],
     ["Research brief", data.sources.research_brief],
     ["Metadata only", data.sources.metadata_only],
     ["Verified records", data.sources.verified_records]].forEach(
      ([label, value]) => {
        const item = el("span");
        item.appendChild(document.createTextNode(label + " "));
        item.appendChild(el("b", "", String(value)));
        split.appendChild(item);
      });
  }

  function renderPipeline(pipeline) {
    const list = $("ov-pipeline");
    clear(list);
    pipeline.forEach((step) => {
      const row = el("div", "pipeline-row");
      row.appendChild(el("span", "pipeline-dot " + step.state));
      const main = el("div");
      main.appendChild(el("div", "pipeline-label", step.label));
      main.appendChild(el("div", "pipeline-detail", step.detail));
      row.appendChild(main);
      row.appendChild(el("span", "pipeline-state " + step.state,
        step.state.replace("_", " ")));
      list.appendChild(row);
    });
  }

  function renderBlockers(blockers) {
    const list = $("ov-blockers");
    clear(list);
    $("ov-blocker-count").textContent = blockers.length
      ? blockers.length + " item" + (blockers.length === 1 ? "" : "s")
      : "";
    if (!blockers.length) {
      list.appendChild(el("div", "blocker-empty",
        "No outstanding blockers are recorded for this workspace."));
      return;
    }
    blockers.forEach((blocker) => {
      const box = el("div", "blocker " + blocker.severity);
      box.appendChild(el("div", "blocker-title", blocker.title));
      box.appendChild(el("div", "blocker-detail", blocker.detail));
      list.appendChild(box);
    });
  }

  function renderActivity(activity) {
    const list = $("ov-activity");
    clear(list);
    if (!activity.length) {
      list.appendChild(el("div", "blocker-empty",
        "No recorded activity yet."));
      return;
    }
    activity.forEach((item) => {
      const row = el("div", "activity-row");
      row.appendChild(el("span", "activity-mark"));
      const main = el("div", "activity-main");
      main.appendChild(el("div", "activity-title", item.title));
      main.appendChild(el("div", "activity-detail", item.detail || ""));
      row.appendChild(main);
      row.appendChild(el("span", "activity-time",
        (item.at || "").replace("T", " ").slice(0, 16)));
      list.appendChild(row);
    });
  }

  /* --------------------------------------------------------------- views */
  function switchView(view) {
    if (!VIEWS[view]) return;
    state.view = view;
    document.querySelectorAll(".view").forEach((section) => {
      section.hidden = true;
      section.classList.remove("is-active");
    });
    document.querySelectorAll(".nav-item[data-view]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.view === view);
    });
    $("page-title").textContent = VIEWS[view].title;
    $("crumb-page").textContent = VIEWS[view].crumb;

    const section = $("view-" + view);
    if (section) {
      section.hidden = false;
      section.classList.add("is-active");
    }
    if (view === "overview") {
      if (!state.overview) refreshAll();
      return;
    }
    if (view === "review") {
      renderReviewSources();
      if (!state.reviewLoaded) {
        state.reviewLoaded = true;
        loadCandidates({ resetPage: true });
      }
      return;
    }
    if (view === "library") {
      renderLibrary();
      return;
    }
    loadWorkflow(view);
  }

  async function loadWorkflow(view) {
    try {
      if (view === "synthesis") await loadSynthesis();
      if (view === "gaps") await loadGaps();
      if (view === "manuscript") await loadManuscript();
      if (view === "reviews") await loadReviews();
      if (view === "export") await loadExport();
    } catch (error) {
      showError(error);
      renderWorkflowError(view, error);
    }
  }

  function renderWorkflowError(view, error) {
    const target = {
      synthesis: "synth-status",
      gaps: "gaps-status",
      manuscript: "ms-sections",
      reviews: "reviews-list",
      export: "export-readiness",
    }[view];
    if (!target || !$(target)) return;
    clear($(target));
    $(target).appendChild(el("div", "notice blocking", error.message));
  }

  function stateBadge(status) {
    const value = String(status || "unknown").toLowerCase()
      .replace(/ /g, "_");
    const label = String(status || "unknown").replace(/_/g, " ");
    return el("span", "state-badge " + value, label);
  }

  /* ----------------------------------------------------------- synthesis */
  async function loadSynthesis() {
    const data = await api("/api/synthesis");
    state.workflow.synthesis = data;
    renderSynthesis(data);
  }

  function renderSynthesis(data) {
    const summary = $("synth-summary");
    if (!data.exists) {
      summary.textContent = data.error || "No synthesis draft exists yet.";
    } else {
      summary.textContent = "Draft review: " + (data.review || "unknown") +
        " · " + data.approved_evidence_count +
        " approved source(s) · " + data.evidence_count +
        " evidence entries.";
    }
    $("synth-ai").disabled = false;
    $("synth-run").disabled = false;
    $("synth-approve").disabled = !data.exists ||
      data.review === "APPROVED" || data.approved_evidence_count === 0;

    const status = $("synth-status");
    clear(status);
    [["Draft", data.exists ? "Present" : "Missing"],
     ["Review state", data.review || "—"],
     ["Approved evidence sources", String(data.approved_evidence_count || 0)],
     ["Evidence entries", String(data.evidence_count || 0)],
     ["Generated", (data.generated_at || "—").replace("T", " ")],
     ["Approved by", data.reviewed_by || "—"]].forEach(([key, value]) => {
      const row = el("div", "kv");
      row.appendChild(el("span", "", key));
      row.appendChild(el("span", "", String(value)));
      status.appendChild(row);
    });
    if (data.exists) status.appendChild(stateBadge(data.review));
    if (data.ai_error) {
      status.appendChild(el("div", "notice blocking",
        "AI interpretation failed: " + data.ai_error +
        ". The deterministic draft was saved."));
    }

    const note = $("synth-note");
    clear(note);
    if (data.blocked_reason) {
      note.appendChild(el("div", "notice blocking", data.blocked_reason));
    } else {
      note.appendChild(el("div", "notice ok",
        "Every entry below is verbatim approved evidence with source IDs."));
    }

    const list = $("synth-evidence");
    clear(list);
    const groups = data.evidence || {};
    const total = Object.values(groups).reduce(
      (count, entries) => count + (entries || []).length, 0);
    $("synth-evidence-count").textContent = total + " entries";
    if (!total) {
      list.appendChild(el("div", "empty",
        "No approved evidence entries yet. Approve candidates in Evidence Review."));
      return;
    }
    Object.entries(groups).forEach(([kind, entries]) => {
      (entries || []).forEach((entry) => {
        const item = el("div", "evidence-item");
        const head = el("div", "ev-head");
        head.appendChild(el("span", "tag classification", titleCase(kind)));
        if (entry.classification) {
          head.appendChild(el("span", "tag", titleCase(entry.classification)));
        }
        item.appendChild(head);
        item.appendChild(el("div", "ev-text", entry.text || ""));
        const meta = el("div", "ev-meta");
        meta.textContent = [
          entry.source_id || "",
          entry.section ? "§ " + entry.section : "",
          entry.page ? "p." + entry.page : "",
          entry.candidate_id || "",
        ].filter(Boolean).join(" · ");
        item.appendChild(meta);
        list.appendChild(item);
      });
    });
  }

  function runSynthesisAction() {
    const ai = $("synth-ai").checked;
    openConfirm({
      kicker: "Run literature synthesis",
      title: "Generate a synthesis draft?",
      body: (ai ? "This will run the existing pipeline with an explicit AI " +
              "interpretation call. The call is logged for usage and is " +
              "never retried automatically. "
              : "This runs deterministic synthesis over approved evidence. ") +
            "It will not approve anything.",
      label: "Run synthesis",
      onConfirm: async () => {
        const data = await api("/api/synthesis/run", {
          method: "POST",
          body: JSON.stringify({ ai, confirm: true }),
        });
        toast("Synthesis draft generated.", "success");
        state.workflow.synthesis = data;
        renderSynthesis(data);
        await refreshAll();
      },
    });
  }

  function approveSynthesisAction() {
    const reviewer = reviewerValue();
    if (!reviewer) { toast("Set your reviewer name in the header.", "error"); return; }
    openConfirm({
      kicker: "Human approval",
      title: "Approve this synthesis?",
      body: "Synthesis will be marked APPROVED by " + reviewer +
        ". Approval is final and cannot be overwritten.",
      label: "Approve synthesis",
      onConfirm: async () => {
        await api("/api/synthesis/approve", {
          method: "POST",
          body: JSON.stringify({ reviewer, confirm: true }),
        });
        toast("Synthesis approved.", "success");
        await loadSynthesis();
        await refreshAll();
      },
    });
  }

  /* ---------------------------------------------------------------- gaps */
  async function loadGaps() {
    const data = await api("/api/gaps");
    state.workflow.gaps = data;
    renderGaps(data);
  }

  function renderGaps(data) {
    $("gaps-summary").textContent = data.exists
      ? "Gap analysis review: " + (data.review || "unknown") +
        " · synthesis approved: " + (data.synthesis_ready ? "yes" : "no")
      : (data.error || "No gap analysis yet.");
    $("gaps-run").disabled = !data.synthesis_ready;
    $("gaps-approve").disabled = !data.exists || data.review === "APPROVED";

    const status = $("gaps-status");
    clear(status);
    [["Synthesis approved", data.synthesis_ready ? "Yes" : "No"],
     ["Gap draft", data.exists ? "Present" : "Missing"],
     ["Review state", data.review || "—"],
     ["Generated", (data.generated_at || "—").replace("T", " ")],
     ["Approved by", data.reviewed_by || "—"]].forEach(([key, value]) => {
      const row = el("div", "kv");
      row.appendChild(el("span", "", key));
      row.appendChild(el("span", "", String(value)));
      status.appendChild(row);
    });
    if (data.exists) status.appendChild(stateBadge(data.review));

    const note = $("gaps-note");
    clear(note);
    if (data.blocked_reason) {
      note.appendChild(el("div", "notice blocking", data.blocked_reason));
    }
    if (data.novelty_disclaimer) {
      note.appendChild(el("div", "notice", data.novelty_disclaimer));
    }
    if (data.ai_error) {
      note.appendChild(el("div", "notice blocking",
        "AI interpretation failed: " + data.ai_error));
    }

    const content = $("gaps-content");
    clear(content);
    if (!data.exists) {
      content.appendChild(el("div", "empty",
        data.blocked_reason || "Run gap analysis to see candidate gaps."));
      return;
    }
    const gaps = data.candidate_gaps || {};
    [["directly_evidenced_limitations", "Directly evidenced limitations"],
     ["inferred_patterns", "Cross-source patterns"],
     ["potential_research_opportunities", "Explicitly labelled opportunities"],
     ["unverified_novelty_hypotheses", "Unverified novelty hypotheses"]
    ].forEach(([key, label]) => {
      const entries = gaps[key] || [];
      const group = el("div", "gap-group");
      group.appendChild(el("h4", "", label + " (" + entries.length + ")"));
      if (!entries.length) {
        group.appendChild(el("div", "empty small",
          key === "unverified_novelty_hypotheses"
            ? "Always empty: novelty is never claimed by this workflow."
            : "None recorded."));
      }
      entries.forEach((entry) => {
        const item = el("div", "evidence-item");
        item.appendChild(el("div", "ev-text", entry.text || ""));
        const meta = el("div", "ev-meta");
        meta.textContent = [
          entry.source_id || (entry.source_ids || []).join(", "),
          entry.basis || "",
        ].filter(Boolean).join(" · ");
        item.appendChild(meta);
        group.appendChild(item);
      });
      content.appendChild(group);
    });
    const interpretation = data.interpretation || {};
    if (interpretation.status === "COMPLETED") {
      const group = el("div", "gap-group");
      group.appendChild(el("h4", "", "AI interpretation (unverified)"));
      ["conflicting_findings", "underexplored_areas",
       "potential_opportunities", "novelty_hypotheses"].forEach((key) => {
        (interpretation[key] || []).forEach((text) => {
          group.appendChild(el("div", "evidence-item",
            typeof text === "string" ? text : JSON.stringify(text)));
        });
      });
      content.appendChild(group);
    }
  }

  function runGapsAction() {
    const ai = $("gaps-ai").checked;
    openConfirm({
      kicker: "Run gap analysis",
      title: "Generate a gap-analysis draft?",
      body: (ai ? "This includes an explicit AI interpretation call, logged " +
              "for usage and never retried automatically. "
              : "This runs deterministic gap analysis over the approved synthesis. ") +
            "It will not approve anything.",
      label: "Run analysis",
      onConfirm: async () => {
        const data = await api("/api/gaps/run", {
          method: "POST", body: JSON.stringify({ ai, confirm: true }),
        });
        toast("Gap analysis generated.", "success");
        state.workflow.gaps = data;
        renderGaps(data);
        await refreshAll();
      },
    });
  }

  function approveGapsAction() {
    const reviewer = reviewerValue();
    if (!reviewer) { toast("Set your reviewer name in the header.", "error"); return; }
    openConfirm({
      kicker: "Human approval",
      title: "Approve this gap analysis?",
      body: "The gap analysis will be approved by " + reviewer +
        ". Novelty remains unverified.",
      label: "Approve analysis",
      onConfirm: async () => {
        await api("/api/gaps/approve", {
          method: "POST", body: JSON.stringify({ reviewer, confirm: true }),
        });
        toast("Gap analysis approved.", "success");
        await loadGaps();
        await refreshAll();
      },
    });
  }

  /* ---------------------------------------------------------- manuscript */
  async function loadManuscript() {
    const data = await api("/api/manuscript");
    state.workflow.manuscript = data;
    renderManuscript(data);
  }

  function renderManuscript(data) {
    $("ms-summary").textContent = data.blocked_reason ||
      "Approved synthesis available. Section drafting is enabled.";

    const structure = $("ms-structure");
    clear(structure);
    (data.structure || []).forEach((item) => {
      const row = el("div", "structure-item" +
        (item.section ? " is-supported" : ""));
      row.appendChild(el("span", "", item.label));
      row.appendChild(el("span", "muted",
        item.section ? "draftable" : "not available"));
      if (item.note) row.appendChild(el("span", "support-note", item.note));
      if (item.section) {
        row.addEventListener("click", () => selectSection(item.section));
      }
      structure.appendChild(row);
    });

    const list = $("ms-sections");
    clear(list);
    (data.sections || []).forEach((section) => {
      const button = el("button", "section-item" +
        (state.msSection === section.section ? " is-active" : ""));
      button.type = "button";
      button.appendChild(el("span", "name", section.label));
      const latest = section.latest;
      const sub = el("span", "sub");
      sub.textContent = latest
        ? "v" + latest.version + " · " + latest.review +
          " · " + (latest.created_at || "").slice(0, 10)
        : "No drafts";
      button.appendChild(sub);
      button.addEventListener("click", () => selectSection(section.section));
      list.appendChild(button);
    });

    if (state.msSection) {
      const section = (data.sections || []).find(
        (item) => item.section === state.msSection);
      if (section) renderSection(section);
    }
  }

  function selectSection(name) {
    state.msSection = name;
    state.msVersion = null;
    const data = state.workflow.manuscript;
    if (data) renderManuscript(data);
  }

  function renderSection(section) {
    $("ms-empty").hidden = true;
    $("ms-reader").hidden = false;
    $("ms-reader-title").textContent = section.label;
    const select = $("ms-version-select");
    clear(select);
    if (!section.versions.length) {
      $("ms-prose").textContent = "No draft yet. Use 'Draft new version' below.";
      $("ms-unsupported").textContent = "";
      $("ms-meta").textContent = "";
      $("ms-provenance").textContent = "";
      $("ms-approve").disabled = true;
      return;
    }
    section.versions.forEach((version) => {
      select.appendChild(new Option(
        "v" + version.version + " · " + version.review, version.version));
    });
    const selected = (state.msVersion &&
      section.versions.find((v) => v.version === state.msVersion)) ||
      section.versions[section.versions.length - 1];
    select.value = selected.version;
    state.msVersion = selected.version;
    select.onchange = () => {
      state.msVersion = Number(select.value);
      renderSection(section);
    };

    const provenance = $("ms-provenance");
    clear(provenance);
    [["Version " + selected.version, "strong"],
     [selected.review || "unknown", ""],
     [selected.origin || "", ""],
     [selected.model || "", ""]].forEach(([text, kind]) => {
      if (text) provenance.appendChild(el("span",
        "prov-chip" + (kind ? " " + kind : ""), text));
    });
    $("ms-prose").textContent = selected.prose || "(no prose)";
    const meta = $("ms-meta");
    clear(meta);
    const citations = selected.citations || {};
    meta.appendChild(el("span", "tag", "valid citations " +
      (citations.valid || []).length));
    meta.appendChild(el("span", "tag", "invalid citations " +
      (citations.invalid || []).length));
    meta.appendChild(el("span", "tag", "evidence refs " +
      (selected.evidence_refs || []).length));
    if (selected.created_at) {
      meta.appendChild(el("span", "muted",
        selected.created_at.replace("T", " ")));
    }
    const unsupported = selected.unsupported || [];
    $("ms-unsupported").textContent = unsupported.length
      ? unsupported.join("\n\n")
      : "No uncited sentences recorded.";
    $("ms-approve").disabled = selected.review === "APPROVED";
    if (selected.error) {
      $("ms-unsupported").textContent += "\n\nError: " + selected.error;
    }
    const reviews = section.reviews || [];
    $("ms-summary").textContent = (state.workflow.manuscript.blocked_reason ||
      "Approved synthesis available.") +
      (reviews.length ? " · " + reviews.length + " review report(s)" : "");
  }

  function draftSectionAction() {
    if (!state.msSection) return;
    if (!state.workflow.manuscript.synthesis_approved) {
      toast("Drafting is blocked until synthesis is approved.", "error");
      return;
    }
    const context = $("ms-context").value.trim();
    const section = (state.workflow.manuscript.sections || []).find(
      (item) => item.section === state.msSection);
    openConfirm({
      kicker: "Draft section",
      title: "Draft a new version of " + (section ? section.label : state.msSection) +
        "?",
      body: "This calls the configured DeepSeek provider through the existing " +
        "pipeline. The call is logged for usage and never retried " +
        "automatically. A new version is appended; previous versions stay.",
      label: "Draft version",
      onConfirm: async () => {
        const data = await api("/api/manuscript/draft", {
          method: "POST",
          body: JSON.stringify({ section: state.msSection, context,
                                 confirm: true }),
        });
        toast("Draft version " + data.version + " created.", "success");
        state.msVersion = data.version;
        await loadManuscript();
        await refreshAll();
      },
    });
  }

  function approveSectionAction() {
    const reviewer = reviewerValue();
    if (!reviewer) { toast("Set your reviewer name in the header.", "error"); return; }
    if (!state.msSection || !state.msVersion) return;
    openConfirm({
      kicker: "Human approval",
      title: "Approve this section version?",
      body: "Section " + state.msSection + " version " + state.msVersion +
        " will be approved by " + reviewer +
        ". Approved versions are immutable.",
      label: "Approve version",
      onConfirm: async () => {
        await api("/api/manuscript/approve", {
          method: "POST",
          body: JSON.stringify({ section: state.msSection,
                                 version: state.msVersion,
                                 reviewer, confirm: true }),
        });
        toast("Section version approved.", "success");
        await loadManuscript();
        await refreshAll();
      },
    });
  }

  /* ------------------------------------------------------------- reviews */
  async function loadReviews() {
    const data = await api("/api/reviews");
    state.workflow.reviews = data;
    renderReviews(data);
  }

  function renderReviews(data) {
    const reports = data.reviews || [];
    $("reviews-summary").textContent = reports.length +
      " review report(s) recorded.";
    $("reviews-count").textContent = reports.length + " total";

    const list = $("reviews-list");
    clear(list);
    const externalSelect = $("external-review-id");
    clear(externalSelect);
    if (!reports.length) {
      list.appendChild(el("div", "empty",
        "No review reports yet. Run a review to create one."));
    }
    reports.forEach((report) => {
      const button = el("button", "review-row" +
        (state.reviewId === report.review_id ? " is-active" : ""));
      button.type = "button";
      button.dataset.reviewId = report.review_id;
      button.appendChild(el("span", "name",
        (report.section || "unknown") + " · v" + (report.version ?? "?")));
      button.appendChild(el("span", "sub",
        report.review_id + " · " + (report.created_at || "").slice(0, 16) +
        " · " + report.findings.length + " findings"));
      button.addEventListener("click", () => selectReview(report.review_id));
      list.appendChild(button);
      externalSelect.appendChild(new Option(
        report.review_id + " (" + report.section + " v" + report.version + ")",
        report.review_id));
    });

    const sectionSelect = $("review-section");
    const current = sectionSelect.value;
    clear(sectionSelect);
    (data.sections || []).forEach((name) => {
      sectionSelect.appendChild(new Option(titleCase(name), name));
    });
    if (current) sectionSelect.value = current;

    if (state.reviewId) selectReview(state.reviewId);
  }

  async function selectReview(reviewId) {
    state.reviewId = reviewId;
    const detail = $("reviews-detail");
    clear(detail);
    detail.appendChild(el("div", "empty small", "Loading review…"));
    try {
      const review = await api("/api/reviews/" + encodeURIComponent(reviewId));
      renderReviewDetail(review);
      document.querySelectorAll("#reviews-list .review-row").forEach((row) => {
        row.classList.toggle("is-active", row.dataset.reviewId === reviewId);
      });
    } catch (error) {
      showError(error);
      clear(detail);
      detail.appendChild(el("div", "notice blocking", error.message));
    }
  }

  function renderReviewDetail(review) {
    const detail = $("reviews-detail");
    clear(detail);
    const artifact = review.reviewed_artifact || {};
    $("review-detail-title").textContent = "Review " + review.review_id;
    const facts = el("div", "wf-body");
    [["Section", artifact.section], ["Version", artifact.version],
     ["Status", review.review], ["AI review", (review.ai_review || {}).status],
     ["Created", (review.created_at || "").replace("T", " ")],
     ["External critiques", (review.external_reviews || []).length]
    ].forEach(([key, value]) => {
      const row = el("div", "kv");
      row.appendChild(el("span", "", key));
      row.appendChild(el("span", "", String(value ?? "—")));
      facts.appendChild(row);
    });
    detail.appendChild(facts);
    (review.deterministic_findings || []).forEach((finding) => {
      const item = el("div", "finding " +
        String(finding.severity || "").toLowerCase());
      const head = el("div", "f-head");
      head.appendChild(el("span", "severity " + finding.severity,
        finding.severity));
      head.appendChild(el("span", "", finding.category || ""));
      head.appendChild(el("span", "muted", finding.finding_id || ""));
      item.appendChild(head);
      item.appendChild(el("div", "f-msg", finding.message || ""));
      item.appendChild(el("div", "f-why", finding.rationale || ""));
      detail.appendChild(item);
    });
    (review.external_reviews || []).forEach((external) => {
      const box = el("div", "finding low");
      box.appendChild(el("div", "f-head", "External · " +
        external.platform + " · " + external.recorded_at));
      box.appendChild(el("div", "f-msg", external.text || ""));
      detail.appendChild(box);
    });
  }

  function runReviewAction() {
    const section = $("review-section").value;
    const versionText = $("review-version").value.trim();
    const version = versionText ? Number(versionText) : null;
    if (!section) { toast("Select a section.", "error"); return; }
    if (versionText && (!Number.isInteger(version) || version < 1)) {
      toast("Version must be a positive integer.", "error");
      return;
    }
    const ai = $("review-ai").checked;
    openConfirm({
      kicker: "Run independent review",
      title: "Review " + section + "?",
      body: (ai ? "This includes an explicit AI review call, logged for usage " +
              "and never retried automatically. "
              : "This runs deterministic checks only (citations, provenance, " +
                "unsupported statements). ") +
            "No findings are approved automatically.",
      label: "Run review",
      onConfirm: async () => {
        const review = await api("/api/reviews/run", {
          method: "POST",
          body: JSON.stringify({ section, version, ai, confirm: true }),
        });
        toast("Review report created.", "success");
        state.reviewId = review.review_id;
        await loadReviews();
        await refreshAll();
      },
    });
  }

  async function addExternalReviewAction() {
    const reviewId = $("external-review-id").value;
    const text = $("external-text").value.trim();
    const reviewer = $("external-reviewer").value.trim();
    if (!reviewId || !text) {
      toast("Select a report and paste the critique.", "error");
      return;
    }
    if (!reviewer) {
      toast("Enter who is importing the critique.", "error");
      return;
    }
    openConfirm({
      kicker: "External critique",
      title: "Record external review?",
      body: "The pasted " + $("external-platform").value +
        " critique will be stored separately from internal findings. It is " +
        "never accepted automatically.",
      label: "Record critique",
      onConfirm: async () => {
        await api("/api/reviews/external", {
          method: "POST",
          body: JSON.stringify({
            review_id: reviewId,
            platform: $("external-platform").value,
            text, reviewer, confirm: true,
          }),
        });
        toast("External critique recorded.", "success");
        $("external-text").value = "";
        $("external-file").value = "";
        await loadReviews();
      },
    });
  }

  /* -------------------------------------------------------------- export */
  async function loadExport() {
    const data = await api("/api/export");
    state.workflow.export = data;
    renderExport(data);
  }

  function renderExport(data) {
    $("export-summary").textContent = data.ready
      ? "Ready: " + data.sections.length + " approved section(s) available."
      : (data.error || "Not ready: no approved manuscript sections.");

    const readiness = $("export-readiness");
    clear(readiness);
    [["Section approvals", data.ready,
      data.ready ? data.sections.length + " approved" : "none approved"],
     ["Excluded sections", data.excluded.length === 0,
      data.excluded.length + " excluded"],
     ["Citations preserved", true, data.citations.length + " citation id(s)"],
     ["Unresolved references", data.unresolved.length === 0,
      data.unresolved.length + " unresolved (labelled in output)"]
    ].forEach(([label, ok, detail]) => {
      const item = el("div", "check-item " + (ok ? "ok" : "blocked"));
      item.appendChild(el("span", "mark"));
      item.appendChild(el("span", "", label + " — " + detail));
      readiness.appendChild(item);
    });
    if (data.error) {
      readiness.appendChild(el("div", "notice blocking", data.error));
    }

    const sections = $("export-sections");
    clear(sections);
    if (!data.sections.length) {
      sections.appendChild(el("div", "empty small",
        "No approved sections will be exported."));
    }
    data.sections.forEach((section) => {
      const row = el("label", "export-row");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = true;
      box.value = section.section;
      box.dataset.exportSection = "1";
      row.appendChild(box);
      row.appendChild(el("span", "", section.heading + " v" +
        section.version));
      row.appendChild(el("span", "muted", "approved by " +
        (section.approved_by || "—")));
      sections.appendChild(row);
    });

    const excluded = $("export-excluded");
    clear(excluded);
    if (!data.excluded.length) {
      excluded.appendChild(el("div", "empty small", "Nothing excluded."));
    }
    data.excluded.forEach((item) => {
      excluded.appendChild(el("div", "export-row",
        item.section + " — " + item.reason));
    });

    const list = $("export-list");
    clear(list);
    if (!data.exports.length) {
      list.appendChild(el("div", "empty small", "No exports yet."));
    }
    data.exports.forEach((item) => {
      const row = el("div", "export-row");
      const link = el("a", "text-button", item.name);
      link.href = "/api/export/download?file=" +
        encodeURIComponent(item.name);
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      row.appendChild(link);
      row.appendChild(el("span", "muted",
        Math.round(item.size_bytes / 1024) + " KB"));
      list.appendChild(row);
    });
    $("export-run").disabled = !data.ready;
  }

  function runExportAction() {
    const sections = [...document.querySelectorAll(
      "[data-export-section]:checked")].map((box) => box.value);
    if (!sections.length) {
      toast("Select at least one approved section.", "error");
      return;
    }
    const name = $("export-filename").value.trim() || "manuscript.docx";
    const force = $("export-force").checked;
    openConfirm({
      kicker: "DOCX export",
      title: (force ? "Overwrite " : "Generate ") + name + "?",
      body: "Approved sections only (" + sections.join(", ") +
        "). Approval fingerprints are verified before writing; unresolved " +
        "references stay labelled in the document.",
      label: "Generate DOCX",
      onConfirm: async () => {
        const result = await api("/api/export/docx", {
          method: "POST",
          body: JSON.stringify({
            output: name,
            title: $("export-title").value.trim() || "Manuscript",
            sections, force, confirm: true,
          }),
        });
        toast("DOCX generated and validated.", "success");
        const box = $("export-result");
        clear(box);
        box.appendChild(el("div", "notice ok",
          "Written: " + result.manifest.output.path +
          " · validation " + result.manifest.validation.status));
        const link = el("a", "button", "Download " + result.download);
        link.href = "/api/export/download?file=" +
          encodeURIComponent(result.download);
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        box.appendChild(link);
        await loadExport();
        await refreshAll();
      },
    });
  }

  /* -------------------------------------------------------------- review */
  function renderReviewSources() {
    const list = $("source-list");
    clear(list);
    $("source-count").textContent = state.sources.length + " total";
    $("source-all").classList.toggle("is-active",
      state.review.activeSource === "");
    $("source-all").onclick = () => setReviewSource("");
    state.sources.forEach((source) => {
      const button = el("button", "source-item" +
        (state.review.activeSource === source.source_id
          ? " is-active" : ""));
      button.type = "button";
      button.appendChild(el("span", "source-title",
        short(source.title, 95)));
      const sub = el("span", "source-sub");
      if (source.authors) sub.appendChild(el("span", "",
        short(source.authors, 44)));
      if (source.year) sub.appendChild(el("span", "", source.year));
      sub.appendChild(el("span", "",
        source.category === "research_brief"
          ? "Research brief" : (source.verification_status || "")));
      button.appendChild(sub);
      const counts = source.candidates || {};
      const row = el("span", "source-counts");
      row.appendChild(document.createTextNode("Pending "));
      row.appendChild(el("b", "", String(counts.pending ?? 0)));
      row.appendChild(document.createTextNode(" · Approved "));
      row.appendChild(el("b", "", String(counts.approved ?? 0)));
      button.appendChild(row);
      button.addEventListener("click", () =>
        setReviewSource(source.source_id));
      list.appendChild(button);
    });
  }

  function setReviewSource(sourceId) {
    state.review.activeSource = sourceId;
    renderReviewSources();
    loadCandidates({ resetPage: true });
  }

  async function loadCandidates(options = {}) {
    if (options.resetPage) state.review.page = 1;
    const params = new URLSearchParams({
      page: String(state.review.page),
      page_size: "20",
      decision: state.review.decision,
    });
    if (state.review.activeSource) {
      params.set("source", state.review.activeSource);
    }
    if (state.review.classification) {
      params.set("classification", state.review.classification);
    }
    if (state.review.q) params.set("q", state.review.q);
    showCandidateSkeleton();
    try {
      const data = await api("/api/candidates?" + params.toString());
      state.review.items = data.items || [];
      state.review.total = data.total || 0;
      state.review.pages = data.pages || 1;
      if (state.review.page > state.review.pages) {
        state.review.page = state.review.pages;
        return loadCandidates();
      }
      renderCandidates();
      updatePager();
      const listed = state.review.items.some(
        (item) => item.candidate_id === state.review.selectedId);
      if (!listed && state.review.items.length) {
        await selectCandidate(state.review.items[0].candidate_id, false);
      } else if (!state.review.items.length) {
        state.review.selectedId = null;
        state.review.detail = null;
        $("reader").hidden = true;
        $("reader-empty").hidden = false;
        $("reader-empty").querySelector("h3").textContent =
          "Nothing to review here";
        $("reader-empty").querySelector("p").textContent =
          "This filter has no candidates. Try another decision, source or search.";
      }
    } catch (error) {
      showError(error);
    }
  }

  function showCandidateSkeleton() {
    const list = $("candidate-list");
    clear(list);
    const wrap = el("div", "skeleton-list");
    for (let index = 0; index < 4; index += 1) {
      wrap.appendChild(el("div", "skeleton"));
    }
    list.appendChild(wrap);
  }

  function renderCandidates() {
    const list = $("candidate-list");
    clear(list);
    $("queue-count").textContent =
      state.review.total + " candidate" +
      (state.review.total === 1 ? "" : "s");
    if (!state.review.items.length) {
      const empty = el("div", "empty");
      empty.appendChild(el("div", "", "No candidates match this view."));
      empty.appendChild(el("div", "",
        "Adjust the filters or choose another source."));
      list.appendChild(empty);
      return;
    }
    state.review.items.forEach((item) => {
      const button = el("button", "candidate-item" +
        (state.review.selectedId === item.candidate_id
          ? " is-active" : ""));
      button.type = "button";
      const top = el("div", "candidate-top");
      top.appendChild(el("span", "tag classification",
        titleCase(item.classification)));
      if (item.decision) {
        top.appendChild(el("span",
          "tag decision-" + item.decision.toLowerCase(), item.decision));
      }
      button.appendChild(top);
      button.appendChild(el("div", "candidate-preview",
        item.preview || "(no text)"));
      const meta = el("div", "candidate-meta");
      meta.textContent = ["p." + (item.page ?? "?"),
        item.section || "no section", "#" + item.index].join(" · ");
      button.appendChild(meta);
      button.addEventListener("click", () =>
        selectCandidate(item.candidate_id));
      list.appendChild(button);
    });
  }

  function updatePager() {
    $("page-label").textContent =
      "Page " + state.review.page + " of " + state.review.pages;
    $("page-prev").disabled = state.review.page <= 1;
    $("page-next").disabled = state.review.page >= state.review.pages;
  }

  async function selectCandidate(candidateId, scroll = true) {
    state.review.selectedId = candidateId;
    renderCandidates();
    $("reader").hidden = true;
    $("reader-empty").hidden = false;
    $("reader-empty").querySelector("h3").textContent = "Loading passage…";
    $("reader-empty").querySelector("p").textContent =
      "Reading the extracted candidate and its provenance.";
    try {
      const detail = await api("/api/candidates/" +
        encodeURIComponent(candidateId));
      state.review.detail = detail;
      renderReader(detail);
      if (scroll) {
        const active = $("candidate-list").querySelector(".is-active");
        if (active && active.scrollIntoView) {
          active.scrollIntoView({ block: "nearest" });
        }
      }
    } catch (error) {
      showError(error);
    }
  }

  function renderReader(detail) {
    const source = detail.source || {};
    const item = detail.item || {};
    $("reader-empty").hidden = true;
    $("reader").hidden = false;

    const provenance = $("provenance");
    clear(provenance);
    [
      [short(source.title || source.source_id, 90), "strong"],
      ["Page " + (item.page ?? "n/a"), ""],
      ["Section: " + (item.section || "unclassified"), ""],
      ["Paragraph " + (item.paragraph ?? "n/a"), ""],
      [item.candidate_id || "", ""],
    ].forEach(([text, kind]) => {
      provenance.appendChild(el("span",
        "prov-chip" + (kind ? " " + kind : ""), text));
    });

    $("passage-title").textContent = titleCase(item.classification) +
      (item.section ? " · " + titleCase(item.section) : "");
    $("candidate-text").textContent = item.text || "";
    $("paragraph-text").textContent =
      item.paragraph_text || "(not recorded)";
    $("context-block").open = false;

    const grid = $("meta-grid");
    clear(grid);
    [["Source", source.title || source.source_id || ""],
     ["Source ID", source.source_id || ""],
     ["Classification", titleCase(item.classification)],
     ["Coarse kind", item.kind || ""],
     ["Page", item.page ?? "not recorded"],
     ["Section", item.section || "not detected"],
     ["Paragraph", item.paragraph ?? "not recorded"],
     ["Extraction", item.extraction_method || ""],
     ["Extracted", item.extracted_at || ""]].forEach(([label, value]) => {
      const box = el("div");
      box.appendChild(el("dt", "", label));
      box.appendChild(el("dd", "", String(value)));
      grid.appendChild(box);
    });

    const link = $("document-link");
    if (source.source_id && source.has_document) {
      link.hidden = false;
      link.href = "/api/sources/" +
        encodeURIComponent(source.source_id) + "/document";
      const kind = (source.document_kind || "").toLowerCase();
      link.textContent = kind === "docx" ? "Download source DOCX"
        : kind === "pdf" ? "Open source PDF" : "Open source text";
    } else {
      link.hidden = true;
      link.removeAttribute("href");
    }

    const panel = $("decision-panel");
    const decided = $("decided-box");
    if (item.decision) {
      panel.hidden = true;
      decided.hidden = false;
      decided.className =
        "decided-box " + item.decision.toLowerCase();
      clear(decided);
      decided.appendChild(el("div", "",
        item.decision === "APPROVED" ? "Approved as evidence" : "Rejected"));
      decided.appendChild(el("div", "",
        "by " + (item.reviewed_by || "unknown reviewer") +
        (item.reviewed_at ? " · " + item.reviewed_at : "")));
    } else {
      panel.hidden = false;
      decided.hidden = true;
      setDecisionEnabled();
    }
  }

  function setDecisionEnabled() {
    const ready = reviewerValue().length > 0;
    $("approve-button").disabled = !ready;
    $("reject-button").disabled = !ready;
    $("decision-reviewer").textContent = reviewerValue() || "not set";
    $("decision-hint").textContent = ready
      ? "Decisions are final and recorded with your name and timestamp."
      : "Set a reviewer identity in the header to enable decisions.";
  }

  function stepCandidate(direction) {
    if (!state.review.items.length) return;
    const index = state.review.items.findIndex(
      (item) => item.candidate_id === state.review.selectedId);
    if (index === -1) {
      selectCandidate(state.review.items[0].candidate_id);
      return;
    }
    const next = index + direction;
    if (next >= 0 && next < state.review.items.length) {
      selectCandidate(state.review.items[next].candidate_id);
    } else if (next >= state.review.items.length &&
               state.review.page < state.review.pages) {
      state.review.page += 1;
      loadCandidates().then(() => {
        if (state.review.items.length) {
          selectCandidate(state.review.items[0].candidate_id);
        }
      });
    } else if (next < 0 && state.review.page > 1) {
      state.review.page -= 1;
      loadCandidates().then(() => {
        const items = state.review.items;
        if (items.length) {
          selectCandidate(items[items.length - 1].candidate_id);
        }
      });
    }
  }

  /* ------------------------------------------------------------- decisions */
  function openConfirm({ kicker, title, body, label, danger, onConfirm }) {
    state.modal = { onConfirm };
    $("modal-kicker").textContent = kicker || "Confirm action";
    $("modal-title").textContent = title || "Confirm action?";
    $("modal-body").textContent = body || "";
    $("modal-confirm").textContent = label || "Confirm";
    $("modal-confirm").className = "button" +
      (danger ? " button-reject" : "");
    $("modal").hidden = false;
    $("modal-cancel").focus();
  }

  function openDecisionModal(kind) {
    if (!state.review.detail || !state.review.detail.item) return;
    const reviewer = reviewerValue();
    if (!reviewer) {
      toast("Set your reviewer name in the header first.", "error");
      $("reviewer-input").focus();
      return;
    }
    if (state.review.detail.item.decision) {
      toast("This candidate already has a decision.", "error");
      return;
    }
    const label = kind === "approve" ? "Approve as evidence" : "Reject";
    const candidateId = state.review.detail.item.candidate_id;
    const sourceId = state.review.detail.source.source_id;
    openConfirm({
      kicker: "Confirm " + label.toLowerCase(),
      title: label + " this passage?",
      body: "Candidate " + candidateId + " from \"" +
        short(state.review.detail.source.title, 70) + "\" will be recorded " +
        "as " + label.toLowerCase() + " by " + reviewer +
        ". This decision is final and cannot be changed.",
      label,
      danger: kind === "reject",
      onConfirm: async () => {
        await api("/api/decisions", {
          method: "POST",
          body: JSON.stringify({
            source_id: sourceId,
            candidate_id: candidateId,
            decision: kind,
            reviewer,
            confirm: true,
          }),
        });
        toast(kind === "approve" ? "Passage approved." : "Passage rejected.",
          "success");
        await refreshAll();
        await loadCandidates();
      },
    });
  }

  function closeModal() {
    state.modal = null;
    $("modal").hidden = true;
  }

  async function runModalAction() {
    if (!state.modal || state.deciding) return;
    const action = state.modal.onConfirm;
    state.deciding = true;
    $("modal-confirm").disabled = true;
    try {
      await action();
      closeModal();
    } catch (error) {
      closeModal();
      if (error.status === 409) {
        toast(error.message + " — reloaded the latest state.", "error");
        await refreshAll();
        if (state.view === "review") await loadCandidates();
        if (state.view === "synthesis") await loadSynthesis();
        if (state.view === "gaps") await loadGaps();
        if (state.view === "manuscript") await loadManuscript();
      } else {
        showError(error);
        toast(error.message, "error");
      }
    } finally {
      state.deciding = false;
      $("modal-confirm").disabled = false;
    }
  }

  /* -------------------------------------------------------------- library */
  function categoryLabel(source) {
    if (source.category === "research_brief") return "Research brief";
    if (source.category === "external_literature") return "External literature";
    return "Other material";
  }

  function renderLibrary() {
    const statuses = Array.from(new Set(state.sources
      .map((source) => source.verification_status)
      .filter(Boolean))).sort();
    const statusSelect = $("lib-status");
    const currentStatus = statusSelect.value;
    clear(statusSelect);
    statusSelect.appendChild(new Option("All statuses", ""));
    statuses.forEach((status) => {
      statusSelect.appendChild(new Option(status, status));
    });
    statusSelect.value = currentStatus;

    const query = state.library.query.toLowerCase();
    let rows = state.sources.filter((source) => {
      const haystack = [source.title, source.authors, source.source_id,
        source.doi, source.year, source.filename]
        .join(" ").toLowerCase();
      if (query && !haystack.includes(query)) return false;
      if (state.library.status &&
          source.verification_status !== state.library.status) return false;
      if (state.library.category &&
          source.category !== state.library.category) return false;
      if (state.library.availability === "full" &&
          !source.full_text_available) return false;
      if (state.library.availability === "metadata" &&
          source.full_text_available) return false;
      if (state.library.extraction) {
        if (state.library.extraction === "none") {
          if (source.outcome) return false;
        } else if (source.outcome !== state.library.extraction) {
          return false;
        }
      }
      if (state.library.validation) {
        const level = source.validation && source.validation.level;
        if (level !== state.library.validation) return false;
      }
      return true;
    });
    if (state.library.sort === "title") {
      rows.sort((a, b) => a.title.localeCompare(b.title));
    } else if (state.library.sort === "year") {
      rows.sort((a, b) => String(b.year).localeCompare(String(a.year)));
    }
    $("library-count").textContent =
      rows.length + " of " + state.sources.length + " sources";

    const list = $("library-list");
    clear(list);
    if (!rows.length) {
      list.appendChild(el("div", "empty",
        "No sources match these filters."));
      return;
    }
    rows.forEach((source) => {
      const row = el("div", "library-row" +
        (state.library.selected === source.source_id ? " is-active" : ""));
      const main = el("div");
      main.appendChild(el("div", "library-title", source.title));
      const meta = el("div", "library-meta");
      meta.textContent = [source.authors || "Authors not recorded",
        source.year || "Year unknown", source.source_id,
        source.filename].filter(Boolean).join(" · ");
      main.appendChild(meta);
      const statusLine = el("div", "library-meta");
      statusLine.textContent = [
        source.outcome ? "extraction " + source.outcome.toLowerCase() : "not extracted",
        source.review || "",
        source.validation ? "validation " + source.validation.level : "",
      ].filter(Boolean).join(" · ");
      main.appendChild(statusLine);
      row.appendChild(main);

      const counts = source.candidates || {};
      const countBox = el("div", "library-counts");
      countBox.appendChild(el("span", "", "Pending "));
      countBox.appendChild(el("b", "", String(counts.pending ?? 0)));
      countBox.appendChild(el("span", "", "Approved "));
      countBox.appendChild(el("b", "", String(counts.approved ?? 0)));
      row.appendChild(countBox);

      const tag = el("span", "category-tag" +
        (source.category === "research_brief" ? " brief"
          : source.category === "other" ? " other" : ""),
        categoryLabel(source));
      row.appendChild(tag);

      const actions = el("div", "library-actions");
      const open = el("button", "button", "Details");
      open.type = "button";
      open.addEventListener("click", (event) => {
        event.stopPropagation();
        selectLibrarySource(source.source_id);
      });
      const review = el("button", "button button-primary", "Review");
      review.type = "button";
      review.disabled = !(counts.pending > 0);
      review.addEventListener("click", (event) => {
        event.stopPropagation();
        state.review.activeSource = source.source_id;
        state.reviewLoaded = true;
        switchView("review");
        loadCandidates({ resetPage: true });
      });
      actions.appendChild(open);
      actions.appendChild(review);
      row.appendChild(actions);
      row.addEventListener("click", () =>
        selectLibrarySource(source.source_id));
      list.appendChild(row);
    });
  }

  async function selectLibrarySource(sourceId) {
    state.library.selected = sourceId;
    renderLibrary();
    const detail = $("library-detail");
    clear(detail);
    detail.appendChild(el("div", "empty small", "Loading source…"));
    try {
      const data = await api("/api/sources/" + encodeURIComponent(sourceId));
      renderLibraryDetail(data);
    } catch (error) {
      showError(error);
      clear(detail);
      detail.appendChild(el("div", "empty small", error.message));
    }
  }

  function renderLibraryDetail(data) {
    const source = data.source;
    const extraction = data.extraction;
    const detail = $("library-detail");
    clear(detail);
    const body = el("div", "source-detail-body");
    body.appendChild(el("span", "category-tag" +
      (source.category === "research_brief" ? " brief"
        : source.category === "other" ? " other" : ""),
      categoryLabel(source)));
    body.appendChild(el("h3", "source-detail-title", source.title));
    body.appendChild(el("div", "source-detail-authors",
      source.authors || "Authors not recorded"));
    body.appendChild(el("div", "muted",
      [source.year || "Year unknown", source.verification_status || ""]
        .filter(Boolean).join(" · ")));

    const listEl = el("dl", "detail-list");
    const facts = [
      ["Source ID", source.source_id],
      ["Filename", source.filename || "—"],
      ["DOI", source.doi || "not recorded"],
      ["Provenance", source.provenance || "not recorded"],
      ["Imported", source.imported_at || "not recorded"],
      ["Document", source.document_kind || "unknown"],
      ["SHA-256", data.checksum || "—"],
      ["Validation", source.validation
        ? source.validation.level + " — " +
          (source.validation.messages || []).join("; ")
        : "not checked"],
      ["Extraction", extraction ? (extraction.outcome || "unknown")
        : "not extracted"],
      ["Tool", extraction && extraction.tool
        ? extraction.tool + (extraction.tool_version
          ? " (" + extraction.tool_version + ")" : "") : "—"],
      ["Pages", extraction ? (extraction.pages ??
        (extraction.max_page || "not recorded")) : "—"],
      ["Characters", extraction ? extraction.characters : "—"],
      ["Candidates", source.candidates.total + " total · " +
        source.candidates.pending + " pending · " +
        source.candidates.approved + " approved"],
      ["Review", extraction ? (extraction.review || "—") : "—"],
    ];
    facts.forEach(([label, value]) => {
      const item = el("div", "detail-item");
      item.appendChild(el("dt", "", label));
      item.appendChild(el("dd", "", String(value)));
      listEl.appendChild(item);
    });
    body.appendChild(listEl);

    if (extraction && extraction.limitations && extraction.limitations.length) {
      body.appendChild(el("div", "eyebrow", "Extraction limitations"));
      const ul = el("ul", "limitations");
      extraction.limitations.forEach((text) => ul.appendChild(el("li", "", text)));
      body.appendChild(ul);
    }
    if (extraction && extraction.error) {
      body.appendChild(el("div", "notice blocking", extraction.error));
    }
    if (!extraction) {
      body.appendChild(el("div", "notice",
        "No extraction record exists for this source yet."));
    }

    const textKinds = ["txt", "md", "csv", "bib"];
    if (source.has_document && textKinds.includes(source.document_kind)) {
      body.appendChild(el("div", "eyebrow", "Document preview"));
      const preview = el("pre", "doc-preview", "Loading preview…");
      body.appendChild(preview);
      api("/api/sources/" + encodeURIComponent(source.source_id) +
          "/preview").then((data) => {
        preview.textContent = data.text + (data.truncated
          ? "\n\n[Preview truncated at " + data.text.length +
            " of " + data.characters + " characters]"
          : "");
      }).catch((error) => {
        preview.textContent = "Preview unavailable: " + error.message;
      });
    } else if (source.has_document) {
      body.appendChild(el("div", "notice",
        source.document_kind === "docx"
          ? "DOCX files download for reading in Word; no in-browser preview is claimed."
          : "PDF opens in the browser's native viewer."));
    }

    const actions = el("div", "detail-actions");
    if (source.has_document) {
      const link = el("a", "button",
        source.document_kind === "docx" ? "Download source"
          : "Open source");
      link.href = "/api/sources/" + encodeURIComponent(source.source_id) +
        "/document";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      actions.appendChild(link);
    }
    if (source.candidates.pending > 0) {
      const review = el("button", "button button-primary",
        "Review pending passages");
      review.type = "button";
      review.addEventListener("click", () => {
        state.review.activeSource = source.source_id;
        state.reviewLoaded = true;
        switchView("review");
        loadCandidates({ resetPage: true });
      });
      actions.appendChild(review);
    }
    body.appendChild(actions);
    detail.appendChild(body);
  }

  /* --------------------------------------------------------------- import */
  const IMPORT_EXTENSIONS = [".pdf", ".docx", ".txt", ".md", ".csv", ".bib"];
  const MAX_IMPORT_BYTES = 50 * 1024 * 1024;

  function addImportFiles(fileList) {
    const files = Array.from(fileList || []);
    files.forEach((file) => {
      const extension = ("." + file.name.split(".").pop()).toLowerCase();
      if (!IMPORT_EXTENSIONS.includes(extension)) {
        toast(file.name + ": unsupported format " + extension, "error");
        return;
      }
      if (file.size > MAX_IMPORT_BYTES) {
        toast(file.name + ": exceeds the 50 MB limit", "error");
        return;
      }
      if (file.size === 0) {
        toast(file.name + ": file is empty", "error");
        return;
      }
      const duplicate = state.importFiles.some(
        (item) => item.name === file.name && item.size === file.size);
      if (!duplicate) state.importFiles.push(file);
    });
    renderImportSelected();
  }

  function renderImportSelected() {
    const list = $("import-selected");
    clear(list);
    state.importFiles.forEach((file, index) => {
      const row = el("div", "import-file");
      row.appendChild(el("span", "import-file-name", file.name));
      row.appendChild(el("span", "muted",
        Math.max(1, Math.round(file.size / 1024)) + " KB · " +
        (file.name.split(".").pop() || "").toUpperCase()));
      const remove = el("button", "text-button", "Remove");
      remove.type = "button";
      remove.addEventListener("click", () => {
        state.importFiles.splice(index, 1);
        renderImportSelected();
      });
      row.appendChild(remove);
      list.appendChild(row);
    });
    $("import-run").disabled = state.importFiles.length === 0;
    $("import-run").textContent = "Import selected" +
      (state.importFiles.length ? " (" + state.importFiles.length + ")" : "");
  }

  function renderImportResults(results) {
    const box = $("import-results");
    clear(box);
    if (!results.length) return;
    const imported = results.filter(
      (item) => item.data.status === "imported").length;
    const duplicates = results.filter(
      (item) => item.data.status === "duplicate").length;
    const rejected = results.length - imported - duplicates;
    box.appendChild(el("div", "import-summary",
      imported + " imported · " + duplicates + " duplicate(s) · " +
      rejected + " rejected/failed"));
    results.forEach(({ file, data }) => {
      const row = el("div", "import-result " + data.status);
      row.appendChild(el("span", "import-file-name", file.name));
      row.appendChild(el("span", "tag", data.status.toUpperCase()));
      let detail = data.message || "";
      if (data.status === "imported") {
        const extraction = data.extraction || {};
        detail = "source " + data.source.source_id + " · extraction " +
          (extraction.outcome || "unknown") +
          (extraction.items !== undefined
            ? " · " + extraction.items + " candidate(s)" : "") +
          (extraction.error ? " · " + extraction.error : "");
      }
      row.appendChild(el("span", "import-detail", detail));
      box.appendChild(row);
    });
  }

  async function runImport() {
    if (!state.importFiles.length) return;
    const files = [...state.importFiles];
    const provenance = $("import-provenance").value;
    const status = $("import-status").value;
    const results = [];
    $("import-run").disabled = true;
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index];
      $("import-run").textContent =
        "Importing " + (index + 1) + " of " + files.length + "…";
      const query = new URLSearchParams({
        name: file.name,
        provenance,
        status,
        confirm: "true",
      });
      try {
        const response = await fetch(
          "/api/sources/import?" + query.toString(), {
            method: "POST",
            headers: { "X-ResearchForge-Token": token },
            body: file,
            credentials: "same-origin",
          });
        let data = null;
        try { data = await response.json(); } catch (error) { data = null; }
        if (!response.ok) {
          data = (data && data.error) || { status: "failed",
            message: "Request failed (" + response.status + ")" };
          if (!data.status) data.status = "failed";
        }
        results.push({ file, data });
      } catch (error) {
        results.push({ file, data: { status: "failed",
          message: "Cannot reach the local server." } });
      }
      renderImportResults(results);
    }
    state.importFiles = [];
    renderImportSelected();
    $("import-run").disabled = false;
    $("import-run").textContent = "Import selected";
    const imported = results.filter(
      (item) => item.data.status === "imported").length;
    toast(imported + " file(s) imported. Review extraction outcomes below.",
      imported ? "success" : "error");
    await refreshAll();
    if (state.view === "library") renderLibrary();
  }

  /* --------------------------------------------------------------- wiring */
  function bind() {
    $("theme-toggle").addEventListener("click", () => {
      applyTheme(document.documentElement.dataset.theme === "dark"
        ? "light" : "dark");
    });

    document.querySelectorAll(".nav-item[data-view]").forEach((button) => {
      button.addEventListener("click", () => switchView(button.dataset.view));
    });
    document.querySelectorAll("[data-go]").forEach((button) => {
      button.addEventListener("click", () => switchView(button.dataset.go));
    });

    $("reviewer-input").addEventListener("input", () => {
      try {
        localStorage.setItem("rf-reviewer", reviewerValue());
      } catch (error) { /* */ }
      updateReviewerUI();
    });

    document.querySelectorAll(".segmented button").forEach((button) => {
      button.addEventListener("click", () => {
        document.querySelectorAll(".segmented button").forEach((other) => {
          other.classList.toggle("is-active", other === button);
        });
        state.review.decision = button.dataset.decision;
        loadCandidates({ resetPage: true });
      });
    });

    $("classification-filter").addEventListener("change", (event) => {
      state.review.classification = event.target.value;
      loadCandidates({ resetPage: true });
    });

    let timer = null;
    $("search-input").addEventListener("input", (event) => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        state.review.q = event.target.value.trim();
        loadCandidates({ resetPage: true });
      }, 260);
    });

    $("page-prev").addEventListener("click", () => {
      if (state.review.page > 1) {
        state.review.page -= 1;
        loadCandidates();
      }
    });
    $("page-next").addEventListener("click", () => {
      if (state.review.page < state.review.pages) {
        state.review.page += 1;
        loadCandidates();
      }
    });
    $("candidate-prev").addEventListener("click", () => stepCandidate(-1));
    $("candidate-next").addEventListener("click", () => stepCandidate(1));
    $("approve-button").addEventListener("click", () =>
      openDecisionModal("approve"));
    $("reject-button").addEventListener("click", () =>
      openDecisionModal("reject"));

    $("modal-cancel").addEventListener("click", closeModal);
    $("modal-confirm").addEventListener("click", runModalAction);
    $("modal").addEventListener("click", (event) => {
      if (event.target === $("modal")) closeModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !$("modal").hidden) closeModal();
    });

    $("synth-run").addEventListener("click", runSynthesisAction);
    $("synth-approve").addEventListener("click", approveSynthesisAction);
    $("gaps-run").addEventListener("click", runGapsAction);
    $("gaps-approve").addEventListener("click", approveGapsAction);
    $("ms-draft").addEventListener("click", draftSectionAction);
    $("ms-approve").addEventListener("click", approveSectionAction);
    $("review-run").addEventListener("click", runReviewAction);
    $("external-add").addEventListener("click", addExternalReviewAction);
    $("export-run").addEventListener("click", runExportAction);
    $("external-file").addEventListener("change", async (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      try {
        $("external-text").value = await file.text();
        toast("Critique loaded from file. Review it before recording.",
          "success");
      } catch (error) {
        toast("Could not read that file.", "error");
      }
    });

    $("error-retry").addEventListener("click", refreshAll);

    $("lib-search").addEventListener("input", (event) => {
      state.library.query = event.target.value.trim();
      renderLibrary();
    });
    $("lib-status").addEventListener("change", (event) => {
      state.library.status = event.target.value;
      renderLibrary();
    });
    $("lib-availability").addEventListener("change", (event) => {
      state.library.availability = event.target.value;
      renderLibrary();
    });
    $("lib-category").addEventListener("change", (event) => {
      state.library.category = event.target.value;
      renderLibrary();
    });
    $("lib-extraction").addEventListener("change", (event) => {
      state.library.extraction = event.target.value;
      renderLibrary();
    });
    $("lib-validation").addEventListener("change", (event) => {
      state.library.validation = event.target.value;
      renderLibrary();
    });
    $("lib-sort").addEventListener("change", (event) => {
      state.library.sort = event.target.value;
      renderLibrary();
    });

    $("import-open").addEventListener("click", () => {
      $("import-panel").hidden = false;
      $("import-files").focus();
    });
    $("import-cancel").addEventListener("click", () => {
      $("import-panel").hidden = true;
      state.importFiles = [];
      renderImportSelected();
      $("import-files").value = "";
    });
    $("import-files").addEventListener("change", (event) => {
      addImportFiles(event.target.files);
      event.target.value = "";
    });
    $("import-run").addEventListener("click", runImport);
    const dropZone = $("drop-zone");
    ["dragenter", "dragover"].forEach((name) => {
      dropZone.addEventListener(name, (event) => {
        event.preventDefault();
        dropZone.classList.add("is-over");
      });
    });
    ["dragleave", "drop"].forEach((name) => {
      dropZone.addEventListener(name, (event) => {
        event.preventDefault();
        dropZone.classList.remove("is-over");
      });
    });
    dropZone.addEventListener("drop", (event) => {
      if (event.dataTransfer && event.dataTransfer.files) {
        addImportFiles(event.dataTransfer.files);
      }
    });

    let lastFailure = null;
    window.addEventListener("online", () => refreshAll());
    window.addEventListener("offline", () => setConnection(false));
    setInterval(() => {
      api("/api/status").catch((error) => {
        if (lastFailure !== error.status) {
          lastFailure = error.status;
          showError(error);
        }
      });
    }, 30000);
  }

  document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    bind();
    restoreReviewer();
    refreshAll();
    switchView("overview");
  });
})();
