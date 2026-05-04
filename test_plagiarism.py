"""Integration tests for /commons/plagiarism — run against a live
surface-api at 127.0.0.1:3000 with a service-key + an existing seeded
agent (the test harness provisions one).

Network-dependent: hits real DuckDuckGo. Marked accordingly so CI can
opt-out via `pytest -m "not network"`.
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import httpx
import pytest


FIX = Path(__file__).parent / "fixtures"
BASE = os.environ.get("SURFACE_BASE_URL", "http://127.0.0.1:3000")
SVC_KEY = os.environ.get("SURFACE_SERVICE_KEY") or os.environ.get("SERVICE_API_KEY")
AGENT_ID = os.environ.get("SURFACE_TEST_AGENT_ID")


def _client() -> httpx.Client:
    if not SVC_KEY or not AGENT_ID:
        pytest.skip("SURFACE_SERVICE_KEY + SURFACE_TEST_AGENT_ID required")
    return httpx.Client(
        base_url=BASE,
        headers={"X-Service-Key": SVC_KEY, "X-Agent-Id": AGENT_ID},
        timeout=180.0,
    )


def test_health_public():
    r = httpx.get(f"{BASE}/commons/plagiarism/health", timeout=10)
    assert r.status_code == 200
    j = r.json()
    assert j["service"] == "commons.plagiarism"
    assert "available" in j
    assert "sc_cost" in j
    assert set(j["sensitivities"]) == {"low", "med", "high"}


def test_auth_rejected_without_key():
    r = httpx.post(
        f"{BASE}/commons/plagiarism/check",
        json={"text": "hello world", "sensitivity": "med"},
        timeout=10,
    )
    assert r.status_code in (401, 422)


@pytest.mark.network
def test_check_known_plagiarized():
    """Wikipedia-derived text should score > 0% match."""
    text = (FIX / "plagiarism_known.txt").read_text()
    with _client() as c:
        r = c.post("/commons/plagiarism/check",
                   json={"text": text, "sensitivity": "med"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["match_pct"] > 0, f"expected matches in known-plagiarized text: {j}"
        assert any("wikipedia" in m["source_url"].lower() or m["source_url"]
                   for m in j["matched_phrases"]), j["matched_phrases"]
        assert j["sc_cost"] == 8.0


@pytest.mark.network
def test_check_original_low_match():
    """Made-up nonsense should score low (often 0%)."""
    text = (FIX / "plagiarism_original.txt").read_text()
    with _client() as c:
        r = c.post("/commons/plagiarism/check",
                   json={"text": text, "sensitivity": "med"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["match_pct"] <= 50.0, f"original text matched too high: {j}"


def test_check_text_too_short():
    with _client() as c:
        r = c.post("/commons/plagiarism/check",
                   json={"text": "hi", "sensitivity": "med"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["note"] == "text_too_short"
        assert j["match_pct"] == 0.0


def test_check_bad_sensitivity():
    with _client() as c:
        r = c.post("/commons/plagiarism/check",
                   json={"text": "some text here", "sensitivity": "extreme"})
        assert r.status_code == 422


@pytest.mark.network
def test_check_doc_txt():
    with _client() as c:
        files = [("file", ("known.txt",
                           (FIX / "plagiarism_known.txt").read_bytes(), "text/plain"))]
        r = c.post("/commons/plagiarism/check-doc",
                   files=files, data={"sensitivity": "med"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["sc_cost"] == 10.0
        assert j["filename"] == "known.txt"
        assert j["extracted_chars"] > 0


def test_check_doc_unsupported():
    with _client() as c:
        files = [("file", ("foo.exe", b"MZ\x90\x00garbage", "application/octet-stream"))]
        r = c.post("/commons/plagiarism/check-doc",
                   files=files, data={"sensitivity": "med"})
        assert r.status_code == 400
