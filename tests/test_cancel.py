"""Abort of LLM work (src.llm.cancel) — no real LLM needed.

A tiny fake server stands in for Ollama's /v1/chat/completions so we can
make a request hang and check that an abort ends it at once.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.llm import client, resolution
from src.llm.cancel import CancelToken, Cancelled, current_token, use_token
from src.llm.tasks._base import DetectionResult

REPLY = {"choices": [{"message": {"content": '{"ok": true}'}}]}


class _FakeOllama(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — /api/tags health check
        self.server.hits += 1
        body = json.dumps({"models": [{"name": "fake:latest"}]}).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 — http.server API
        self.server.hits += 1
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.server.status != 200:
            self.send_response(self.server.status)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        time.sleep(self.server.delay)
        body = json.dumps(REPLY).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # client went away (aborted)

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def fake_llm(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
    server.daemon_threads = True
    server.hits, server.delay, server.status = 0, 0.0, 200
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(client, "LLM_HOST", f"http://127.0.0.1:{server.server_address[1]}")
    yield server
    server.shutdown()
    server.server_close()


def _chat():
    return client._post_chat([{"role": "user", "content": "hi"}], timeout=30.0)


def test_without_token_behaviour_is_unchanged(fake_llm):
    assert _chat() == '{"ok": true}'
    fake_llm.status = 500
    with pytest.raises(urllib.error.HTTPError) as e:
        _chat()
    assert e.value.code == 500


def test_health_check_uses_the_same_client(fake_llm, monkeypatch):
    assert client.llm_ready() == (True, ["fake:latest"])
    fake_llm.status = 503
    assert client.llm_ready() == (False, [])
    monkeypatch.setattr(client, "LLM_HOST", "http://127.0.0.1:9")  # nothing listens
    assert client.llm_ready() == (False, [])


def test_abort_ends_request_in_flight_immediately(fake_llm):
    fake_llm.delay = 10.0  # would block for 10 s without an abort
    token = CancelToken()
    outcome: dict = {}

    def job():
        t0 = time.monotonic()
        try:
            with use_token(token):
                _chat()
            outcome["result"] = "completed"
        except Cancelled:
            outcome["result"] = "cancelled"
        outcome["elapsed"] = time.monotonic() - t0

    worker = threading.Thread(target=job)
    worker.start()
    time.sleep(0.3)  # request is now waiting on the server
    token.cancel()
    worker.join(timeout=5)

    assert outcome["result"] == "cancelled"
    assert outcome["elapsed"] < 1.5


def test_cancelled_token_sends_no_further_requests(fake_llm):
    token = CancelToken()
    token.cancel()
    with use_token(token), pytest.raises(Cancelled):
        _chat()
    assert fake_llm.hits == 0


def test_cancelled_is_not_swallowed_by_broad_except():
    # Graceful-degradation code uses `except Exception`; an abort must pass.
    with pytest.raises(Cancelled):
        try:
            raise Cancelled()
        except Exception:  # noqa: BLE001
            pytest.fail("Cancelled was caught as a regular Exception")


def test_sweep_returns_partial_results_on_abort(monkeypatch):
    calls = []

    def fake_detect(issue_key, row, sqlite_path):
        calls.append(row["i"])
        if row["i"] == 4:  # user presses Abort during the 4th LLM call
            current_token().cancel()
            current_token().raise_if_cancelled()
        return DetectionResult(flagged=True, rationale="", confidence=1.0, suggested_value=None)

    monkeypatch.setattr(resolution, "detect_with_llm", fake_detect)
    token = CancelToken()
    with use_token(token):
        out = resolution.detect_all_with_llm("x", [{"i": i} for i in range(1, 11)], "db.sqlite")

    assert token.cancelled
    assert [row["i"] for row, _ in out] == [1, 2, 3]  # verdicts before the abort survive
    assert calls == [1, 2, 3, 4]  # nothing judged after the abort


def test_aborted_exploration_keeps_previous_guide(monkeypatch, tmp_path):
    from src.exploration import explorer_agent, report_store

    src_db = Path("data/synthetic/order-management-baseline.sqlite")
    if src_db.stat().st_size < 1024:
        pytest.skip("dataset is a Git LFS pointer — run `git lfs pull`")
    db = tmp_path / "log.sqlite"
    shutil.copy(src_db, db)

    old_guide = {"object_types": {"orders": {"note": "from an earlier run"}}}
    report_store.save_json(report_store.guide_path(db), old_guide)
    report_store.report_path(db).write_text("old report", encoding="utf-8")

    def aborted_llm(*args, **kwargs):
        raise Cancelled()

    monkeypatch.setattr(explorer_agent, "call_llm", aborted_llm)
    with pytest.raises(Cancelled):
        explorer_agent.explore_database(db, model="any")

    assert report_store.load_guide(db) == old_guide
    assert report_store.load_report(db) == "old report"
