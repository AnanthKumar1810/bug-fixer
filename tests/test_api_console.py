"""Tests for Bug Fixer Operations Console API and UI paths."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from bug_fixer.api import app, classify_publication_state, settings, store
from bug_fixer.models import RunRecord, RunState


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health_endpoint_success(client: TestClient) -> None:
    """Verify health endpoint returns ready/degraded status, component map, and test count."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "timestamp" in data
    assert "services" in data
    assert data["total_test_count"] == 72
    assert "app" in data["services"]
    assert data["services"]["app"]["status"] == "ready"
    assert "docker" in data["services"]
    assert "ollama" in data["services"]


def test_health_partial_failure_handling(client: TestClient) -> None:
    """Ensure partial service failure degrades overall status without raising 500 error."""
    with patch("subprocess.run") as mock_subproc, patch("httpx.Client.get") as mock_get:
        # Mock Docker failing with an error
        mock_subproc.return_value = MagicMock(returncode=1, stderr="Docker daemon unavailable", stdout="")
        # Mock Ollama returning 500
        mock_get.return_value = MagicMock(status_code=500)

        resp = client.get("/api/health?refresh=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("degraded", "unavailable")
        assert data["services"]["docker"]["status"] == "unavailable"
        assert "unavailable" in data["services"]["docker"]["detail"].lower()
        assert data["services"]["ollama"]["status"] == "unavailable"
        assert data["services"]["app"]["status"] == "ready"


def test_runs_list_all_and_filtering(client: TestClient) -> None:
    """Test listing runs and filtering by status and search terms."""
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    runs = resp.json()
    assert isinstance(runs, list)
    assert len(runs) > 0

    # Search for known run
    first_run_id = runs[0]["run_id"]
    search_resp = client.get(f"/api/runs?search={first_run_id}")
    assert search_resp.status_code == 200
    search_results = search_resp.json()
    assert any(r["run_id"] == first_run_id for r in search_results)

    # Filter by status
    filter_resp = client.get("/api/runs?status=published")
    assert filter_resp.status_code == 200
    for r in filter_resp.json():
        assert r["status"] == "published" or r["stage"] == "published"


def test_runs_list_legacy_record_handling(client: TestClient) -> None:
    """Ensure older or legacy records with missing/null columns do not crash the endpoint."""
    legacy_run = RunRecord(
        run_id="run_legacy_test_001",
        issue_url="https://github.com/example/repo/issues/99",
        issue_number=99,
        issue_title=None,  # Null title
        repo_owner="example",
        repo_name="repo",
        base_sha="abcdef1234567890",
        stage=RunState.VERIFIED,
        status="verified",
        reproducer_nodeid="tests/test_legacy.py::test_case",
        reproducer_hash="11223344",
        retry_budget=2,
        attempts_used=1,
        provenance="fixture",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:01:00Z",
    )
    store.save_run(legacy_run)

    resp = client.get("/api/runs?search=run_legacy_test_001")
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 1
    rec = results[0]
    assert rec["run_id"] == "run_legacy_test_001"
    assert rec["issue_title"] == "Issue #99"  # Fallback generated
    assert rec["provenance"] == "fixture"


def test_run_detail_timeline_ordering(client: TestClient) -> None:
    """Verify that run detail synthesizes all 8 chronological stages in order."""
    # Test on verified genuine upstream defect run
    resp = client.get("/api/runs/run_5cdd92182ddc")
    assert resp.status_code == 200
    data = resp.json()

    assert "run" in data
    assert "timeline" in data
    timeline = data["timeline"]
    assert len(timeline) == 8

    # Verify stage ordering
    stage_ids = [t["stage_id"] for t in timeline]
    assert stage_ids == [
        "1_intake",
        "2_reproduce",
        "3_retrieval",
        "4_repair",
        "5_patch",
        "6_validation",
        "7_replay",
        "8_publication",
    ]

    # Verify reproduction details
    repro = timeline[1]
    assert repro["status"] == "reproduced"
    assert "test_ap002_immutable_tuple_default" in repro["details"]["reproducer_nodeid"]
    assert repro["details"]["exit_code"] == 1


def test_patch_hash_and_verified_tree_display(client: TestClient) -> None:
    """Test candidate patch diff display, SHA-256 fingerprint, and candidate tree proof."""
    resp = client.get("/api/runs/run_5cdd92182ddc")
    assert resp.status_code == 200
    data = resp.json()
    patch_info = data["patch"]

    assert patch_info["has_patch"] is True
    assert any("patterns.py" in f for f in patch_info["changed_files"])
    assert patch_info["insertions"] >= 1
    assert patch_info["deletions"] >= 1
    assert len(patch_info["patch_hash"]) == 64  # SHA-256 length
    assert patch_info["verified_tree_id"] == "653243cff3bd17706e13a8a718e082db4146427b"
    assert patch_info["reconstructs_candidate"] is True


def test_publication_state_local_rehearsal_vs_real_pr() -> None:
    """Verify that local rehearsals with simulated bare remotes are never rendered as GitHub PRs."""
    # Simulated local run
    local_row = {
        "stage": "published",
        "publication_mode": "simulated_local",
        "pr_url": "file:///path/to/local_draft_pr.json",
        "reviewer_status": "not_requested",
    }
    state_key, state_label = classify_publication_state(local_row)
    assert state_key == "local_rehearsal"
    assert "Simulated" in state_label

    # Real remote GitHub run
    remote_row = {
        "stage": "published",
        "publication_mode": "github_remote",
        "pr_url": "https://github.com/org/repo/pull/12",
        "reviewer_status": "requested",
    }
    state_key, state_label = classify_publication_state(remote_row)
    assert state_key == "review_requested"
    assert "Review Requested" in state_label


def test_ready_for_review_vs_independently_verified() -> None:
    """Ensure ready_for_review is distinguished from verified without replay."""
    ready_row = {"stage": "ready_for_review", "publication_mode": None, "pr_url": None}
    state_key, state_label = classify_publication_state(ready_row)
    assert state_key == "eligible_for_review"

    failed_row = {"stage": "repair_failed", "publication_mode": None, "pr_url": None}
    state_key, state_label = classify_publication_state(failed_row)
    assert state_key == "not_eligible"


def test_demo_mode_cases(client: TestClient) -> None:
    """Verify read-only demo mode returns genuine verified cases with is_demo flag."""
    # 1. Success AP002 demo
    r1 = client.get("/api/demo/ap002_success")
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["is_demo"] is True
    assert d1["read_only"] is True
    assert "AP002" in d1["title"]
    assert d1["run_id"] == "run_5cdd92182ddc"

    # 2. Failed repair demo
    r2 = client.get("/api/demo/repair_failed")
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["is_demo"] is True
    assert d2["read_only"] is True
    assert "Exhausted" in d2["title"]
    assert d2["run_id"] == "run_29614918c993"

    # 3. Negative control rejection demo
    r3 = client.get("/api/demo/negative_control")
    assert r3.status_code == 200
    d3 = r3.json()
    assert d3["is_demo"] is True
    assert d3["verdict"] == "REJECTED"
    assert "ZeroDivisionError" in d3["rejection_reason"]


def test_demo_mode_blocks_active_repair(client: TestClient) -> None:
    """Ensure demo mode requests cannot trigger active repair, Docker, or Git operations."""
    resp = client.post("/api/repair", json={
        "preset": "cross_file_repo",
        "is_demo": True,
    })
    assert resp.status_code == 403
    assert "disabled in Demo Mode" in resp.json()["detail"]


def test_benchmarks_endpoint(client: TestClient) -> None:
    """Verify empirical benchmarks endpoint returns Set 2, Set 3, and OSS evaluations."""
    resp = client.get("/api/benchmarks")
    assert resp.status_code == 200
    data = resp.json()
    assert "set2_paired_retrieval" in data
    assert "set3_sprint_comparison" in data
    assert "oss_libraries" in data
    assert data["set2_paired_retrieval"]["sample_size"] >= 2
    assert len(data["oss_libraries"]["results"]) == 2
    assert any("boltons" in r["repository"] for r in data["oss_libraries"]["results"])


def test_console_html_rendering_and_text_safety(client: TestClient) -> None:
    """Verify that root console HTML renders safely and includes all main views."""
    resp = client.get("/")
    assert resp.status_code == 200
    html_text = resp.text

    # Main views present
    assert "view-demo" in html_text
    assert "view-runs" in html_text
    assert "view-benchmarks" in html_text
    assert "view-infographic" in html_text
    assert "view-studio" in html_text

    # Typography & Navigation
    assert "Inter" in html_text
    assert "JetBrains Mono" in html_text
    assert "Bug Fixer Operations Console" in html_text
    assert "/infographic" in html_text

    # Read-only demo mode banner
    assert "Read-Only Judge Demo Mode" in html_text


def test_infographic_endpoints(client: TestClient) -> None:
    """Verify that 16:9 architecture infographic HTML and raw SVG endpoints function correctly."""
    # 1. HTML presentation viewer
    resp_html = client.get("/infographic")
    assert resp_html.status_code == 200
    assert "text/html" in resp_html.headers.get("content-type", "")
    assert "Bug Fixer — From Issue to Verified Repair" in resp_html.text
    assert "A passing patch becomes eligible for review. A human decides whether to merge." in resp_html.text
    assert "<svg" in resp_html.text

    # 2. Raw SVG asset
    resp_svg = client.get("/infographic.svg")
    assert resp_svg.status_code == 200
    assert "image/svg+xml" in resp_svg.headers.get("content-type", "")
    assert "<svg" in resp_svg.text
    assert "</svg>" in resp_svg.text

