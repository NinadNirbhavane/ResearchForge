"""Tests for the local browser workspace (UI-01). All fixtures are
temporary; the real research workspace is never mutated."""
import http.client
import json
import csv
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock
from pathlib import Path

from app.core import locking
from app.core import sources as source_core
from app.core.sources import import_source, load_manifest, write_manifest
from app.main import build_parser
from app.providers.contracts import ProviderResponse, Usage
from app.ui import server as ui
from app.workflows import independent_review as ir
from app.workflows import literature_synthesis as ls
from app.workflows import research_gap_analysis as ga
from app.workflows import section_drafting as sd
from app.workflows.paper_extraction import (approve_extraction,
                                            extract_source,
                                            load_extraction)

PAPER = """A Synthetic Paper on Sidewalk Sensing

Abstract
This paper studies sidewalk conditions for pedestrians in a synthetic city.

1. Introduction
Urban walkability matters. There is a lack of longitudinal evidence on
sidewalk hazards.

2. Methods
We collected 1,200 street-level images and trained a detector.

3. Results
The detector achieved an F1 score of 0.82. Precision was 0.79. Recall was
0.85 on the test set. These results suggest practical viability.

4. Discussion
These results suggest that vision models can support audits.

5. Limitations
The dataset covers a single city.

6. Conclusion
Future work should test other cities.
"""

PAPER_TWO = """A Second Synthetic Paper

1. Introduction
Crowdsourced imagery is useful for walkability assessment.

3. Results
The study found seasonal differences in visual walkability.
"""


def make_project():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "in").mkdir()
    (root / "sources" / "papers").mkdir(parents=True)
    (root / "sources" / "extracted_notes").mkdir(parents=True)
    (root / "outputs").mkdir()
    (root / "PROJECT.md").write_text(
        "# PROJECT.md\n## Working title\nSynthetic Walkability Study\n",
        encoding="utf-8")
    (root / "research").mkdir()
    (root / "research" / "research_charter.md").write_text(
        "# Research Charter\n\n## Research problem\n\n"
        "Synthetic pedestrians lack route-level walkability information.\n\n"
        "**Status:** accepted for the test fixture.\n",
        encoding="utf-8")
    (root / "progress.md").write_text(
        "# progress.md\n## Current phase\nPhase test - synthetic workspace.\n",
        encoding="utf-8")
    ids = []
    for index, text in enumerate((PAPER, PAPER_TWO)):
        path = root / "in" / f"paper{index}.txt"
        path.write_text(text, encoding="utf-8")
        sid = import_source(root, path,
                            verification_status="FULL TEXT READ")["source_id"]
        extract_source(root, sid)
        ids.append(sid)
    brief_path = root / "in" / "research_brief.txt"
    brief_path.write_text(
        "Research Proposal\n\n1. Introduction\n\nThis is the researcher's "
        "own brief, not an external paper.\n", encoding="utf-8")
    brief = import_source(root, brief_path, provenance="researcher",
                          verification_status="GREY LITERATURE")
    extract_source(root, brief["source_id"])
    ids.append(brief["source_id"])
    # Verified literature metadata for the first source (title mapping),
    # plus a metadata-only record with no source id.
    header = ("paper_id,title,authors,publication_year,publication_type,"
              "journal_or_conference,publisher,doi,publisher_url,"
              "research_problem,research_objectives,methodology,dataset,"
              "study_location,key_findings,limitations,relevance_to_research,"
              "relevant_research_question,evidence_status,full_text_available,"
              "verification_notes,date_added\n")
    rows = [
        ["paper1", "Synthetic Sidewalk Paper", "Doe, J.; Roe, R.", "2025",
         "journal article", "Synthetic Journal", "Test Press",
         "10.0000/synth", "https://doi.org/10.0000/synth"] + [""] * 9 +
        ["FULL TEXT READ", "yes", f"Source id: {ids[0]}", "2026-11-11"],
        ["review1", "Synthetic Review", "Moe, A.", "2026", "journal article",
         "Synthetic Review Journal", "Test Press", "10.0000/review",
         "https://doi.org/10.0000/review"] + [""] * 9 +
        ["METADATA VERIFIED", "no", "Full text not obtained", "2026-11-11"],
    ]
    path = root / "sources" / "literature_matrix.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(header)
        writer = csv.writer(handle)
        for row in rows:
            writer.writerow(row)
    return tmp, root, ids


class ServerFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.root, cls.ids = make_project()
        cls.server = ui.create_server(cls.root, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]
        cls.origin = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None,
                host=None, origin=True, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                timeout=10)
        request_headers = {}
        if host is not None:
            request_headers["Host"] = host
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(body))
        if token:
            request_headers["X-ResearchForge-Token"] = self.server.token
        if origin:
            request_headers["Origin"] = self.origin
        if headers:
            request_headers.update(headers)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        content_type = response.getheader("Content-Type") or ""
        data = None
        if "application/json" in content_type:
            data = json.loads(raw.decode("utf-8"))
        connection.close()
        return response.status, data, raw, dict(response.getheaders())

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post_decision(self, payload, **kwargs):
        body = json.dumps(payload).encode("utf-8")
        return self.request("POST", "/api/decisions", body=body, **kwargs)

    def candidate_ids(self, source_id=None, decision="all"):
        query = f"/api/candidates?decision={decision}&page_size=100"
        if source_id:
            query += f"&source={source_id}"
        status, data, _raw, _headers = self.get(query)
        self.assertEqual(status, 200, data)
        return [item["candidate_id"] for item in data["items"]]

    def pending_payload(self, reviewer="Dr Reviewer"):
        ids = self.candidate_ids(decision="pending")
        self.assertTrue(ids)
        source_id = ids[0].split("-")[1]
        return {"source_id": "SRC-" + source_id,
                "candidate_id": ids[0], "decision": "approve",
                "reviewer": reviewer, "confirm": True}


class StaticAndStatusTests(ServerFixture):
    def test_index_serves_injected_token_and_csp(self):
        status, _data, raw, headers = self.get("/")
        self.assertEqual(status, 200)
        text = raw.decode("utf-8")
        self.assertIn("ResearchForge", text)
        self.assertIn('name="rf-token"', text)
        self.assertNotIn("__RF_TOKEN__", text)
        self.assertIn("data-theme", text)
        self.assertIn("Content-Security-Policy", headers)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")

    def test_static_assets_are_allowlisted(self):
        status, _c, raw, headers = self.get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers["Content-Type"])
        self.assertIn(b"function", raw)
        status, _c, _raw, _h = self.get("/static/../server.py")
        self.assertEqual(status, 404)
        status, _c, _raw, _h = self.get("/static/unknown.css")
        self.assertEqual(status, 404)

    def test_status_reports_real_counts_without_paths(self):
        status, data, raw, _headers = self.get("/api/status")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["sources"], 2)
        self.assertGreater(data["candidates"]["pending"], 0)
        self.assertEqual(data["workspace"], self.root.name)
        self.assertNotIn(str(self.root), raw.decode("utf-8"))

    def test_sources_include_literature_metadata_and_counts(self):
        status, data, _raw, _headers = self.get("/api/sources")
        self.assertEqual(status, 200)
        papers = {row["source_id"]: row for row in data["sources"]}
        first = papers[self.ids[0]]
        self.assertEqual(first["title"], "Synthetic Sidewalk Paper")
        self.assertIn("Doe, J.", first["authors"])
        self.assertEqual(first["year"], "2025")
        self.assertGreater(first["candidates"]["pending"], 0)

    def test_source_detail_and_unknown_source(self):
        status, data, _raw, _h = self.get(f"/api/sources/{self.ids[0]}")
        self.assertEqual(status, 200)
        self.assertEqual(data["source"]["source_id"], self.ids[0])
        status, data, _raw, _h = self.get("/api/sources/SRC-000000000000")
        self.assertEqual(status, 404)
        status, _d, _r, _h = self.get("/api/sources/../../etc/passwd")
        self.assertEqual(status, 404)

    def test_document_served_inline_for_text(self):
        status, _data, raw, headers = self.get(
            f"/api/sources/{self.ids[0]}/document")
        self.assertEqual(status, 200)
        self.assertIn("text/plain", headers["Content-Type"])
        self.assertIn(b"sidewalk", raw)
        self.assertIn("inline", headers["Content-Disposition"])

    def test_document_path_containment(self):
        outside = self.root.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        manifest = load_manifest(self.root)
        original = manifest["sources"][self.ids[0]]["stored_location"]
        manifest["sources"][self.ids[0]]["stored_location"] = \
            "../../outside.txt"
        write_manifest(self.root, manifest)
        try:
            status, _d, _r, _h = self.get(
                f"/api/sources/{self.ids[0]}/document")
            self.assertEqual(status, 403)
        finally:
            manifest["sources"][self.ids[0]]["stored_location"] = original
            write_manifest(self.root, manifest)
            outside.unlink()


class DashboardTests(ServerFixture):
    def test_overview_uses_real_records(self):
        status, data, raw, _h = self.get("/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual(data["project"]["title"], "Synthetic Walkability Study")
        self.assertIn("Synthetic pedestrians", data["project"]["description"])
        self.assertIn("Phase test", data["project"]["phase"])
        self.assertEqual(data["sources"]["total"], 3)
        self.assertEqual(data["sources"]["full_text"], 2)
        self.assertEqual(data["sources"]["research_brief"], 1)
        self.assertEqual(data["sources"]["metadata_only"], 1)
        self.assertEqual(data["sources"]["verified_records"], 2)
        keys = [step["key"] for step in data["pipeline"]]
        self.assertEqual(keys, ["ingestion", "evidence", "synthesis",
                                "gaps", "drafting", "review", "export"])
        self.assertEqual(data["pipeline"][0]["state"], "complete")
        self.assertEqual(data["pipeline"][1]["state"], "in_progress")
        self.assertEqual(data["pipeline"][2]["state"], "not_started")
        self.assertTrue(data["activity"])
        self.assertTrue(data["blockers"])
        text = raw.decode("utf-8").lower()
        for invented in ("percent", "readiness", "quality score", "deadline"):
            self.assertNotIn(invented, text)

    def test_source_categories_are_distinguishable(self):
        status, data, _raw, _h = self.get("/api/sources")
        self.assertEqual(status, 200)
        categories = {row["source_id"]: row["category"]
                      for row in data["sources"]}
        self.assertEqual(categories[self.ids[0]], "external_literature")
        self.assertEqual(categories[self.ids[2]], "research_brief")
        brief = next(row for row in data["sources"]
                     if row["source_id"] == self.ids[2])
        self.assertFalse(brief["full_text_available"])
        self.assertTrue(brief["extracted"])

    def test_source_detail_exposes_extraction_metadata(self):
        status, data, _raw, _h = self.get(f"/api/sources/{self.ids[0]}")
        self.assertEqual(status, 200)
        extraction = data["extraction"]
        self.assertEqual(extraction["outcome"], "COMPLETED")
        self.assertTrue(extraction["tool"])
        self.assertIsInstance(extraction["limitations"], list)

    def test_index_shell_navigation_and_workspaces(self):
        status, _d, raw, _h = self.get("/")
        self.assertEqual(status, 200)
        text = raw.decode("utf-8")
        for label in ("Overview", "Evidence Review", "Source Library",
                      "Literature Synthesis", "Research Gaps", "Manuscript",
                      "Reviews", "Export"):
            self.assertIn(label, text)
        for marker in ("view-synthesis", "view-gaps", "view-manuscript",
                       "view-reviews", "view-export"):
            self.assertIn(marker, text)
        self.assertNotIn("view-forthcoming", text)
        self.assertIn("Research command centre", text)


class CandidateReadTests(ServerFixture):
    def test_pagination_returns_distinct_pages(self):
        status, page1, _raw, _h = self.get(
            "/api/candidates?decision=all&page_size=2&page=1")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(page1["total"], 4)
        self.assertGreaterEqual(page1["pages"], 2)
        self.assertEqual(len(page1["items"]), 2)
        status, page2, _raw, _h = self.get(
            "/api/candidates?decision=all&page_size=2&page=2")
        ids1 = {item["candidate_id"] for item in page1["items"]}
        ids2 = {item["candidate_id"] for item in page2["items"]}
        self.assertFalse(ids1 & ids2)

    def test_source_filter(self):
        status, data, _raw, _h = self.get(
            f"/api/candidates?decision=all&source={self.ids[1]}&page_size=100")
        self.assertEqual(status, 200)
        self.assertTrue(data["items"])
        self.assertTrue(all(item["source_id"] == self.ids[1]
                            for item in data["items"]))

    def test_search_and_classification_filters(self):
        status, data, _raw, _h = self.get(
            "/api/candidates?decision=all&q=seasonal&page_size=100")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["total"], 1)
        status, data, _raw, _h = self.get(
            "/api/candidates?decision=all&classification=empirical_result"
            "&page_size=100")
        self.assertEqual(status, 200)
        self.assertTrue(all(item["classification"] == "empirical_result"
                            for item in data["items"]))

    def test_candidate_detail_has_complete_text_and_provenance(self):
        candidate_id = self.candidate_ids()[0]
        status, data, _raw, _h = self.get(
            "/api/candidates/" + candidate_id)
        self.assertEqual(status, 200)
        item = data["item"]
        self.assertEqual(item["candidate_id"], candidate_id)
        self.assertTrue(item["text"])
        self.assertIn("source", data)
        for key in ("section", "page", "paragraph", "classification",
                    "extraction_method", "decision"):
            self.assertIn(key, item)

    def test_unknown_candidate_and_invalid_pagination(self):
        status, _d, _r, _h = self.get("/api/candidates/EXT-000000000000-001")
        self.assertEqual(status, 404)
        status, _d, _r, _h = self.get("/api/candidates?page=abc")
        self.assertEqual(status, 400)
        status, _d, _r, _h = self.get("/api/candidates?source=../../x")
        self.assertEqual(status, 404)

    def test_untrusted_text_is_json_not_html(self):
        candidate_id = self.candidate_ids()[0]
        status, data, raw, headers = self.get(
            "/api/candidates/" + candidate_id)
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertNotIn(b"<script", raw.lower())


class SecurityTests(ServerFixture):
    def test_missing_and_wrong_token_rejected(self):
        payload = self.pending_payload()
        status, data, _r, _h = self.post_decision(payload, token=False)
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "invalid_token")
        status, data, _r, _h = self.post_decision(
            payload, headers={"X-ResearchForge-Token": "wrong"})
        self.assertEqual(status, 403)

    def test_bad_origin_rejected(self):
        status, data, _r, _h = self.post_decision(
            self.pending_payload(),
            headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "invalid_origin")

    def test_bad_host_rejected(self):
        status, data, _r, _h = self.get(
            "/api/status", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "invalid_host")

    def test_wrong_content_type_rejected(self):
        body = json.dumps(self.pending_payload()).encode("utf-8")
        status, data, _r, _h = self.request(
            "POST", "/api/decisions", body=body,
            headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_malformed_json_rejected(self):
        status, data, _r, _h = self.request(
            "POST", "/api/decisions", body=b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "invalid_json")

    def test_oversized_body_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                timeout=10)
        connection.putrequest("POST", "/api/decisions")
        connection.putheader("Host", f"127.0.0.1:{self.port}")
        connection.putheader("Origin", self.origin)
        connection.putheader("X-ResearchForge-Token", self.server.token)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "70000")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        response.read()
        connection.close()

    def test_missing_reviewer_and_confirmation(self):
        payload = self.pending_payload()
        bad = dict(payload, reviewer="")
        status, data, _r, _h = self.post_decision(bad)
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "reviewer_required")
        bad = dict(payload, confirm=False)
        status, data, _r, _h = self.post_decision(bad)
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "confirmation_required")

    def test_invalid_decision_and_unknown_route(self):
        payload = dict(self.pending_payload(), decision="maybe")
        status, data, _r, _h = self.post_decision(payload)
        self.assertEqual(status, 400)
        status, _d, _r, _h = self.get("/api/decisions")
        self.assertEqual(status, 404)
        status, _d, _r, _h = self.get("/api/unknown")
        self.assertEqual(status, 404)


class DecisionTests(ServerFixture):
    def _pick_pending_index(self, start=0):
        payload = self.pending_payload()
        return payload

    def test_approve_records_decision_and_counts(self):
        payload = self.pending_payload(reviewer="Reviewer One")
        status, data, _r, _h = self.post_decision(payload)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["candidate"]["decision"], "APPROVED")
        record = load_extraction(self.root, payload["source_id"])
        item = next(candidate for candidate in record["items"]
                    if candidate["extraction_id"] == payload["candidate_id"])
        self.assertEqual(item["decision"], "APPROVED")
        self.assertEqual(item["reviewed_by"], "Reviewer One")
        self.assertTrue(item["reviewed_at"])
        self.assertGreaterEqual(
            data["candidates"]["approved"], 1)

    def test_reject_records_decision(self):
        payload = self.pending_payload(reviewer="Reviewer Two")
        payload["decision"] = "reject"
        status, data, _r, _h = self.post_decision(payload)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["candidate"]["decision"], "REJECTED")
        record = load_extraction(self.root, payload["source_id"])
        item = next(candidate for candidate in record["items"]
                    if candidate["extraction_id"] == payload["candidate_id"])
        self.assertEqual(item["decision"], "REJECTED")

    def test_duplicate_decision_returns_409(self):
        payload = self.pending_payload(reviewer="Reviewer Three")
        status, _d, _r, _h = self.post_decision(payload)
        self.assertEqual(status, 200)
        status, data, _r, _h = self.post_decision(payload)
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "already_decided")

    def test_candidate_from_other_source_is_rejected(self):
        payload = self.pending_payload()
        other = [sid for sid in self.ids if sid != payload["source_id"]][0]
        payload["source_id"] = other
        status, data, _r, _h = self.post_decision(payload)
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "unknown_candidate")


class LockTests(unittest.TestCase):
    def test_manifest_lock_coordinates_imports_across_processes(self):
        tmp, root, _ids = make_project()
        with tmp:
            lock_file = root / "sources" / source_core.MANIFEST_LOCK_NAME
            marker = root / "held.marker"
            script = (
                "import sys, time\n"
                "sys.path.insert(0, r'" + str(Path.cwd()) + "')\n"
                "from pathlib import Path\n"
                "from app.core import locking\n"
                f"with locking.exclusive_lock(Path(r'{lock_file}'), timeout=5):\n"
                f"    Path(r'{marker}').write_text('held')\n"
                "    time.sleep(0.8)\n"
            )
            script_path = root / "holder.py"
            script_path.write_text(script, encoding="utf-8")
            incoming = root / "in" / "lock-test.txt"
            incoming.write_text("Finding: locked import test.\n",
                                encoding="utf-8")
            process = subprocess.Popen([sys.executable, str(script_path)])
            try:
                deadline = time.time() + 5
                while not marker.exists() and time.time() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                with mock.patch.object(source_core, "IMPORT_LOCK_TIMEOUT", 0.2):
                    with self.assertRaises(source_core.SourceError) as cm:
                        source_core.import_source(root, incoming)
                self.assertIn("another import", str(cm.exception))
            finally:
                process.wait(timeout=10)
            record = source_core.import_source(root, incoming)
            self.assertTrue(record["source_id"].startswith("SRC-"))
            self.assertEqual(len(load_manifest(root)["sources"]), 4)

    def test_cross_process_lock_blocks_then_releases(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        marker = root / "held.marker"
        lock_file = locking.lock_path(root)
        script = (
            "import sys, time\n"
            "sys.path.insert(0, r'" + str(Path.cwd()) + "')\n"
            "from pathlib import Path\n"
            "from app.core import locking\n"
            f"with locking.exclusive_lock(Path(r'{lock_file}'), timeout=5):\n"
            f"    Path(r'{marker}').write_text('held')\n"
            "    time.sleep(0.8)\n"
        )
        script_path = root / "holder.py"
        script_path.write_text(script, encoding="utf-8")
        with tmp:
            process = subprocess.Popen([sys.executable, str(script_path)])
            try:
                deadline = time.time() + 5
                while not marker.exists() and time.time() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                with self.assertRaises(locking.LockTimeout):
                    with locking.exclusive_lock(lock_file, timeout=0.2):
                        pass
            finally:
                process.wait(timeout=10)
            with locking.exclusive_lock(lock_file, timeout=2):
                pass


class FakeProvider:
    provider_id = "deepseek"

    def __init__(self, content="{}"):
        self.content = content
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return ProviderResponse(content=self.content, provider="deepseek",
                                model="deepseek-flash",
                                usage=Usage(input_tokens=1, output_tokens=1,
                                            total_tokens=2))


def make_workflow_project(approve_sources=True, synthesize=True,
                          approve_synthesis=False, draft=False,
                          approve_section=False, review=False):
    tmp, root, ids = make_project()
    if approve_sources:
        for source_id in ids[:2]:
            approve_extraction(root, source_id, "R")
    if synthesize:
        ls.synthesize(root)
    if approve_synthesis:
        ls.approve_synthesis(root, "R")
    if draft:
        version = sd.draft_section(
            root, "literature_review",
            FakeProvider(json.dumps({"prose":
                f"Approved evidence says walking matters [{ids[0]}]."})))
        if approve_section:
            sd.approve_section(root, "literature_review", "R",
                               version=version["version"])
    if review:
        ir.review_section(root, "literature_review")
    return tmp, root, ids


class WorkflowServer:
    def __init__(self, root):
        self.tmp, self.root = root[0], root[1]
        self.ids = root[2]
        self.server = ui.create_server(self.root, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def request(self, method, path, payload=None, **kwargs):
        connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                timeout=10)
        body = json.dumps(payload).encode("utf-8") if payload is not None \
            else None
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        if kwargs.pop("token", True):
            headers["X-ResearchForge-Token"] = self.server.token
        if kwargs.pop("origin", True):
            headers["Origin"] = self.origin
        headers.update(kwargs.get("headers", {}))
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        data = None
        if "application/json" in (response.getheader("Content-Type") or ""):
            data = json.loads(raw.decode("utf-8"))
        connection.close()
        return response.status, data, raw

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, payload=None, **kwargs):
        return self.request("POST", path, payload=payload or {}, **kwargs)

    def upload(self, name, data, provenance="researcher",
               status="SEARCH RESULT ONLY", confirm=True, **kwargs):
        query = urllib.parse.urlencode({
            "name": name,
            "provenance": provenance,
            "status": status,
            "confirm": "true" if confirm else "false",
        })
        connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                timeout=20)
        headers = {"Host": f"127.0.0.1:{self.port}",
                   "Content-Type": "application/octet-stream",
                   "Content-Length": str(len(data)),
                   "X-ResearchForge-Token": self.server.token,
                   "Origin": self.origin}
        headers.update(kwargs.get("headers", {}))
        connection.request("POST", "/api/sources/import?" + query,
                           body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        body = json.loads(raw.decode("utf-8")) if raw else None
        connection.close()
        return response.status, body, raw


class WorkflowApiTests(unittest.TestCase):
    def start(self, **kwargs):
        server = WorkflowServer(make_workflow_project(**kwargs))
        self.addCleanup(server.close)
        return server

    def test_synthesis_state_and_approval_flow(self):
        api = self.start()
        status, data, _r = api.get("/api/synthesis")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["exists"])
        self.assertEqual(data["review"], "PENDING HUMAN REVIEW")
        self.assertGreaterEqual(data["approved_evidence_count"], 2)
        self.assertGreater(data["evidence_count"], 0)

        status, _d, _r = api.post("/api/synthesis/run", {"ai": False})
        self.assertEqual(status, 400)  # confirmation required
        status, data, _r = api.post("/api/synthesis/run",
                                    {"ai": False, "confirm": True})
        self.assertEqual(status, 200, data)
        status, _d, _r = api.post("/api/synthesis/approve", {"confirm": True})
        self.assertEqual(status, 400)  # reviewer required
        status, data, _r = api.post("/api/synthesis/approve",
                                    {"reviewer": "R", "confirm": True})
        self.assertEqual(status, 200, data)
        status, data, _r = api.post("/api/synthesis/approve",
                                    {"reviewer": "R", "confirm": True})
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "already_approved")

    def test_ai_is_opt_in_and_logged(self):
        api = self.start()
        fake = FakeProvider(json.dumps({"agreements": ["x"],
                                        "contradictions": [],
                                        "limitations": [],
                                        "unanswered_questions": []}))
        with mock.patch.object(ui, "build_provider", return_value=fake):
            status, data, _r = api.post(
                "/api/synthesis/run", {"ai": False, "confirm": True})
            self.assertEqual(status, 200)
            self.assertEqual(fake.calls, 0)
            self.assertEqual(data["interpretation"]["status"],
                             "NOT GENERATED")
            status, data, _r = api.post(
                "/api/synthesis/run", {"ai": True, "confirm": True})
            self.assertEqual(status, 200)
            self.assertEqual(fake.calls, 1)
            self.assertEqual(data["interpretation"]["status"], "COMPLETED")
        usage = (api.root / "logs" / "usage.jsonl")
        self.assertTrue(usage.is_file(), "usage logging must be preserved")

    def test_gaps_blocked_until_synthesis_approved(self):
        api = self.start(approve_synthesis=False)
        status, data, _r = api.get("/api/gaps")
        self.assertEqual(status, 200, data)
        self.assertFalse(data["synthesis_ready"])
        self.assertTrue(data["blocked_reason"])
        status, data, _r = api.post("/api/gaps/run", {"confirm": True})
        self.assertEqual(status, 400)
        self.assertIn("APPROVED synthesis", data["error"]["message"])

    def test_gaps_run_and_approve(self):
        api = self.start(approve_synthesis=True)
        status, data, _r = api.get("/api/gaps")
        self.assertTrue(data["synthesis_ready"])
        status, data, _r = api.post("/api/gaps/run", {"confirm": True})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["exists"])
        status, _d, _r = api.post("/api/gaps/approve", {"confirm": True})
        self.assertEqual(status, 400)
        status, data, _r = api.post("/api/gaps/approve",
                                    {"reviewer": "R", "confirm": True})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["review"], "APPROVED")
        status, _d, _r = api.post("/api/gaps/approve",
                                  {"reviewer": "R", "confirm": True})
        self.assertEqual(status, 409)

    def test_manuscript_blocked_and_draft_approve_sequence(self):
        api = self.start(approve_synthesis=False)
        status, data, _r = api.get("/api/manuscript")
        self.assertFalse(data["synthesis_approved"])
        self.assertTrue(data["blocked_reason"])
        with mock.patch.object(ui, "build_provider",
                               side_effect=AssertionError("no provider")):
            status, _d, _r = api.get("/api/manuscript")
        self.assertEqual(status, 200)

        api2 = self.start(approve_synthesis=True)
        fake = FakeProvider(json.dumps({"prose":
            "Evidence about walkability [SRC-000000000000]."}))
        with mock.patch.object(ui, "build_provider", return_value=fake):
            status, _d, _r = api2.post("/api/manuscript/draft",
                                       {"section": "literature_review"})
            self.assertEqual(status, 400)  # confirmation required
            status, data, _r = api2.post(
                "/api/manuscript/draft",
                {"section": "literature_review",
                 "context": "Test context", "confirm": True})
            self.assertEqual(status, 200, data)
            self.assertEqual(data["version"], 1)
            self.assertEqual(fake.calls, 1)
        status, _d, _r = api2.post("/api/manuscript/approve",
                                   {"section": "literature_review",
                                    "version": 1, "confirm": True})
        self.assertEqual(status, 400)  # reviewer required
        status, data, _r = api2.post("/api/manuscript/approve",
                                     {"section": "literature_review",
                                      "version": 1, "reviewer": "R",
                                      "confirm": True})
        self.assertEqual(status, 200, data)
        status, data, _r = api2.get("/api/manuscript")
        section = next(item for item in data["sections"]
                       if item["section"] == "literature_review")
        self.assertEqual(section["latest"]["review"], "APPROVED")
        self.assertTrue(section["latest"]["prose"])
        self.assertTrue(any(item["section"] is None
                            for item in data["structure"]))

    def test_reviews_run_list_detail_and_external(self):
        api = self.start(approve_synthesis=True, draft=True)
        status, data, _r = api.post("/api/reviews/run",
                                    {"section": "literature_review",
                                     "confirm": True})
        self.assertEqual(status, 200, data)
        review_id = data["review_id"]
        self.assertTrue(review_id)
        status, data, _r = api.get("/api/reviews")
        self.assertEqual(status, 200)
        self.assertTrue(any(item["review_id"] == review_id
                            for item in data["reviews"]))
        status, data, _r = api.get("/api/reviews/" + review_id)
        self.assertEqual(status, 200)
        self.assertIn("deterministic_findings", data)
        status, _d, _r = api.post("/api/reviews/external",
                                  {"review_id": review_id,
                                   "platform": "gemini", "text": ""})
        self.assertEqual(status, 400)
        status, data, _r = api.post(
            "/api/reviews/external",
            {"review_id": review_id, "platform": "gemini",
             "text": "External critique.", "reviewer": "R",
             "confirm": True})
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["external_reviews"]), 1)

    def test_export_requires_approval_confirmation_and_serves_file(self):
        api = self.start(approve_synthesis=True)  # no approved section
        status, data, _r = api.get("/api/export")
        self.assertEqual(status, 200)
        self.assertFalse(data["ready"])
        status, _d, _r = api.post("/api/export/docx",
                                  {"confirm": True})
        self.assertEqual(status, 400)

        api2 = self.start(approve_synthesis=True, draft=True,
                          approve_section=True)
        status, data, _r = api2.get("/api/export")
        self.assertEqual(status, 200)
        self.assertTrue(data["ready"])
        self.assertEqual(len(data["sections"]), 1)
        status, _d, _r = api2.post("/api/export/docx", {})
        self.assertEqual(status, 400)
        status, data, _r = api2.post("/api/export/docx", {
            "output": "test.docx", "title": "Test",
            "sections": ["literature_review"], "confirm": True})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["manifest"]["validation"]["status"], "PASS")
        status, _d, raw = api2.get("/api/export/download?file=test.docx")
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"PK"))
        status, _d, _r = api2.get(
            "/api/export/download?file=../papers/x.docx")
        self.assertEqual(status, 404)
        status, data, _r = api2.post("/api/export/docx", {
            "output": "test.docx", "confirm": True})
        self.assertEqual(status, 400)  # overwrite protection
        status, data, _r = api2.post("/api/export/docx", {
            "output": "test.docx", "force": True, "confirm": True})
        self.assertEqual(status, 200)

    def test_version_preservation_and_concurrent_lock_mapping(self):
        api = self.start(approve_synthesis=True, draft=True,
                         approve_section=True)
        fake = FakeProvider(json.dumps({"prose": "Second draft [SRC-x]."}))
        with mock.patch.object(ui, "build_provider", return_value=fake):
            status, data, _r = api.post("/api/manuscript/draft", {
                "section": "literature_review", "confirm": True})
            self.assertEqual(status, 200)
        status, data, _r = api.get("/api/manuscript")
        section = next(item for item in data["sections"]
                       if item["section"] == "literature_review")
        self.assertEqual(len(section["versions"]), 2)
        self.assertEqual(section["versions"][0]["review"], "APPROVED")
        self.assertEqual(section["versions"][1]["review"],
                         "PENDING HUMAN REVIEW")
        with mock.patch.object(ui, "workflow_lock",
                               side_effect=locking.LockTimeout("busy")):
            status, data, _r = api.post("/api/synthesis/run",
                                        {"confirm": True})
            self.assertEqual(status, 409)
            self.assertEqual(data["error"]["code"], "write_conflict")


class IngestionApiTests(unittest.TestCase):
    def start(self):
        server = WorkflowServer(make_project())
        self.addCleanup(server.close)
        return server

    def staging_files(self, root):
        staging = root / "outputs" / "imports"
        if not staging.is_dir():
            return []
        return [path for path in staging.rglob("*") if path.is_file()]

    def test_import_text_success_preview_and_category(self):
        api = self.start()
        before = len(load_manifest(api.root)["sources"])
        status, data, _raw = api.upload(
            "new-note.txt", b"Finding: imported text works.\n",
            provenance="other")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["status"], "imported")
        self.assertEqual(data["extraction"]["outcome"], "COMPLETED")
        self.assertEqual(data["extraction"]["review"], "PENDING HUMAN REVIEW")
        self.assertEqual(len(load_manifest(api.root)["sources"]), before + 1)
        self.assertEqual(self.staging_files(api.root), [])
        source_id = data["source"]["source_id"]

        status, detail, _raw = api.get("/api/sources/" + source_id)
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["checksum"]), 64)
        self.assertEqual(detail["extraction"]["outcome"], "COMPLETED")
        self.assertIn("characters", detail["extraction"])

        status, preview, _raw = api.get(
            "/api/sources/" + source_id + "/preview")
        self.assertEqual(status, 200)
        self.assertIn("imported text works", preview["text"])

        status, data, _raw = api.get("/api/sources?validate=1")
        row = next(item for item in data["sources"]
                   if item["source_id"] == source_id)
        self.assertEqual(row["category"], "other")
        self.assertEqual(row["validation"]["level"], "PASS")
        paper = next(item for item in data["sources"]
                     if item["source_id"] == api.ids[0])
        brief = next(item for item in data["sources"]
                     if item["source_id"] == api.ids[2])
        self.assertEqual(paper["category"], "external_literature")
        self.assertEqual(brief["category"], "research_brief")

    def test_duplicate_import_does_not_touch_extraction(self):
        api = self.start()
        payload = b"Finding: duplicate test content.\n"
        status, first, _raw = api.upload("first.txt", payload)
        self.assertEqual(status, 200)
        source_id = first["source"]["source_id"]
        record_path = (api.root / "sources" / "extracted_notes" /
                       f"{source_id}.extraction.json")
        before = record_path.read_bytes()
        count = len(load_manifest(api.root)["sources"])

        status, second, _raw = api.upload("second-name.txt", payload)
        self.assertEqual(status, 200)
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(load_manifest(api.root)["sources"]), count)
        self.assertEqual(record_path.read_bytes(), before)
        self.assertEqual(self.staging_files(api.root), [])

    def test_unsupported_invalid_empty_and_traversal_files(self):
        api = self.start()
        status, data, _raw = api.upload("malware.exe", b"MZ")
        self.assertEqual(status, 415)
        self.assertEqual(data["error"]["code"], "unsupported_format")
        status, data, _raw = api.upload("fake.pdf", b"not a pdf")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "rejected")
        self.assertIn("PDF", data["message"])
        status, data, _raw = api.upload("bad.docx", b"not a zip")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "rejected")
        status, data, _raw = api.upload("empty.txt", b"")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "empty_file")
        for name in ("../escape.txt", "..\\escape.txt", "dir/file.txt",
                     ""):
            status, data, _raw = api.upload(name, b"x")
            self.assertEqual(status, 400, name)
            self.assertEqual(data["error"]["code"], "invalid_filename")
        self.assertFalse((api.root.parent / "escape.txt").exists())
        self.assertEqual(self.staging_files(api.root), [])

    def test_non_utf8_import_is_not_reported_as_successful_extraction(self):
        api = self.start()
        status, data, _raw = api.upload(
            "broken.txt", b"\xff\xfe not utf8 \x00\x80")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "imported")
        self.assertEqual(data["extraction"]["outcome"], "FAILED")
        self.assertIn("UTF-8", data["extraction"]["error"])
        self.assertEqual(data["extraction"]["review"], "PENDING HUMAN REVIEW")

    def test_oversize_upload_rejected(self):
        api = self.start()
        connection = http.client.HTTPConnection("127.0.0.1", api.port,
                                                timeout=10)
        connection.putrequest("POST", "/api/sources/import?"
                              "name=huge.txt&confirm=true")
        connection.putheader("Host", f"127.0.0.1:{api.port}")
        connection.putheader("Origin", api.origin)
        connection.putheader("X-ResearchForge-Token", api.server.token)
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(ui.MAX_IMPORT_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        response.read()
        connection.close()
        self.assertEqual(self.staging_files(api.root), [])

    def test_preview_unavailable_for_binary_formats(self):
        api = self.start()
        status, data, _raw = api.upload("doc.pdf", b"%PDF-1.4\nfake pdf")
        self.assertEqual(status, 200)
        source_id = data["source"]["source_id"]
        status, data, _raw = api.get(
            f"/api/sources/{source_id}/preview")
        self.assertEqual(status, 415)
        self.assertEqual(data["error"]["code"], "preview_unavailable")

    def test_validation_error_detected(self):
        api = self.start()
        status, data, _raw = api.upload("validate-me.txt", b"text content\n")
        source_id = data["source"]["source_id"]
        record = load_manifest(api.root)["sources"][source_id]
        stored = api.root / record["stored_location"]
        stored.write_bytes(b"tampered")
        status, data, _raw = api.get("/api/sources?validate=1")
        row = next(item for item in data["sources"]
                   if item["source_id"] == source_id)
        self.assertEqual(row["validation"]["level"], "ERROR")

    def test_import_lock_conflict_returns_409(self):
        api = self.start()
        with mock.patch.object(ui, "workflow_lock",
                               side_effect=locking.LockTimeout("busy")):
            status, data, _raw = api.upload("locked.txt", b"content")
            self.assertEqual(status, 409)
            self.assertEqual(data["error"]["code"], "write_conflict")

    def test_import_never_approves_or_changes_decisions(self):
        api = self.start()
        status, data, _raw = api.upload("neutral.txt", b"Statement.\n")
        source_id = data["source"]["source_id"]
        record = load_extraction(api.root, source_id)
        self.assertEqual(record["review"], "PENDING HUMAN REVIEW")
        self.assertEqual(record["item_decisions"],
                         {"approved": 0, "rejected": 0,
                          "pending": len(record["items"])})
        self.assertFalse((api.root / "outputs" / "approvals" /
                          "approval_ledger.jsonl").exists())
        for existing in api.ids:
            self.assertIsNotNone(load_extraction(api.root, existing))


class CliCompatibilityTests(unittest.TestCase):
    def test_ui_command_parses_and_existing_commands_still_parse(self):
        parser = build_parser()
        args = parser.parse_args(["ui", "--port", "0", "--no-open"])
        self.assertEqual(args.port, 0)
        self.assertTrue(args.no_open)
        self.assertIn("ui", parser.format_help())
        for argv in (["status"], ["sources", "list"], ["research", "list"],
                     ["config", "show"], ["approvals", "list"],
                     ["drafts", "list"]):
            self.assertTrue(callable(parser.parse_args(argv).handler))


if __name__ == "__main__":
    unittest.main()
