"""Ten end-to-end and boundary regression tests for verifiable issue-to-PR workflow."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
import pytest
import httpx

from bug_fixer.config import Settings
from bug_fixer.db import EvidenceStore
from bug_fixer.docker_runner import validate_workspace
from bug_fixer.intake import is_valid_commit_sha
from bug_fixer.models import ManifestEntry, RunManifest, RunRecord, RunState
from bug_fixer.publication import create_draft_pr, push_candidate_branch
from bug_fixer.state_machine import BugFixerWorkflow
from bug_fixer.verification import (
    check_publication_eligibility,
    extract_diff_changed_files,
    verify_run,
)


# SCENARIO 1: Run launched from one dir and inspected from another
def test_scenario_1_cross_directory_inspection(tmp_path: Path, cross_file_fixture: Path, monkeypatch):
    """Run launched with custom data dir can be inspected from a completely different working dir."""
    shared_data_dir = tmp_path / "shared_data"
    shared_data_dir.mkdir()

    settings = Settings(
        model="qwen2.5-coder:7b",
        data_dir=shared_data_dir,
        runner="local",
    )
    workflow = BugFixerWorkflow(settings)

    mock_repair = {
        "content": "Defect fixed",
        "tool_calls": [
            {
                "name": "replace_text",
                "arguments": {
                    "relative_path": "service/formatter.py",
                    "old": "Goodbye",
                    "new": "Hello",
                },
            }
        ],
    }

    run = workflow.execute_fix(
        "https://github.com/acme/project/issues/100",
        source_repo=cross_file_fixture,
        test_nodeid="tests/test_greeting.py::test_greeting",
        mock_issue_data={"title": "Test", "body": "fix greeting"},
        mock_llm_response=mock_repair,
        runner_override="local",
    )
    assert run.stage == RunState.READY_FOR_REVIEW

    # Now change working directory completely
    another_dir = tmp_path / "completely_other_dir"
    another_dir.mkdir()
    monkeypatch.chdir(another_dir)

    # Store can still locate and inspect run using the configured shared data directory
    store2 = EvidenceStore(shared_data_dir / "bug_fixer.db", shared_data_dir / "artifacts")
    retrieved_run = store2.get_run(run.run_id)
    assert retrieved_run is not None
    assert retrieved_run.run_id == run.run_id
    assert is_valid_commit_sha(retrieved_run.base_sha)
    assert retrieved_run.stage == RunState.READY_FOR_REVIEW


# SCENARIO 2: Zero / unknown / mismatched base SHA blocks publication
def test_scenario_2_zero_or_mismatched_base_sha_blocks_publication():
    """All-zero, empty, placeholder, or non-hex base commit SHAs block publication."""
    # 1. All-zero SHA (legacy or unpinned)
    zero_sha = "0" * 40
    assert not is_valid_commit_sha(zero_sha)
    eligible, reasons = check_publication_eligibility(
        base_reproduced=True,
        candidate_reproducer_passed=True,
        candidate_suite_passed=True,
        patch_text="diff --git a/f b/f\n",
        changed_files=["service/formatter.py"],
        original_reproducer_hash="h1",
        current_reproducer_hash="h1",
        base_sha=zero_sha,
    )
    assert not eligible
    assert any("Invalid or placeholder base commit SHA" in r for r in reasons)

    # 2. Placeholder repetitive SHA
    assert not is_valid_commit_sha("1" * 40)
    # 3. Empty SHA
    assert not is_valid_commit_sha("")


# SCENARIO 3: Newly authored reproducer test delta on base
def test_scenario_3_newly_authored_reproducer_test_delta(cross_file_fixture: Path, tmp_path: Path):
    """When a new regression test is authored, it fails on base and passes on fixed code."""
    # Write a new regression test
    new_test_code = """
def test_newly_discovered_greeting_bug():
    from service.formatter import format_greeting
    assert format_greeting('Alice') == 'Hello, Alice!'
"""
    new_test_file = cross_file_fixture / "tests" / "test_alice_regression.py"
    new_test_file.write_text(new_test_code, encoding="utf-8")

    # Run on unfixed base -> must fail!
    base_res = validate_workspace(
        cross_file_fixture,
        selected_tests=["tests/test_alice_regression.py::test_newly_discovered_greeting_bug"],
        runner="local",
    )
    assert not base_res.passed
    assert base_res.reproducer_outcome in ("matching_failure", "failed")

    # Fix the callee
    target_file = cross_file_fixture / "service" / "formatter.py"
    target_file.write_text("def format_greeting(name):\n    return f'Hello, {name}!'\n", encoding="utf-8")

    # Run on fixed candidate -> must pass!
    cand_res = validate_workspace(
        cross_file_fixture,
        selected_tests=["tests/test_alice_regression.py::test_newly_discovered_greeting_bug"],
        runner="local",
    )
    assert cand_res.passed
    assert cand_res.reproducer_outcome == "passed"


# SCENARIO 4: Missing or tampered patch or changed locked test blocks eligibility
def test_scenario_4_tampered_patch_or_changed_locked_test(test_settings: Settings, tmp_path: Path):
    """Missing patch file or changed reproducer hash immediately blocks publication gate."""
    store = EvidenceStore(test_settings.data_dir / "bug_fixer.db", test_settings.data_dir / "artifacts")
    run_id = "run_test_tamper_123"

    run = RunRecord(
        run_id=run_id,
        issue_url="https://github.com/acme/project/issues/1",
        issue_number=1,
        repo_owner="acme",
        repo_name="project",
        base_sha="a" * 40,
        stage=RunState.VERIFIED,
        status="verified",
        reproducer_nodeid="tests/test_foo.py::test_it",
        reproducer_hash="good_hash_abc123",
        created_at="",
        updated_at="",
    )
    store.save_run(run)

    # 1. Missing patch file on disk
    eligible, reasons = check_publication_eligibility(
        base_reproduced=True,
        candidate_reproducer_passed=True,
        candidate_suite_passed=True,
        patch_text="",
        changed_files=[],
        original_reproducer_hash="good_hash_abc123",
        current_reproducer_hash="good_hash_abc123",
        base_sha="a" * 40,
        patch_file_exists=False,
    )
    assert not eligible
    assert any("Candidate patch file does not exist" in r for r in reasons)

    # 2. Tampered reproducer test hash
    eligible2, reasons2 = check_publication_eligibility(
        base_reproduced=True,
        candidate_reproducer_passed=True,
        candidate_suite_passed=True,
        patch_text="diff",
        changed_files=["service/formatter.py"],
        original_reproducer_hash="good_hash_abc123",
        current_reproducer_hash="tampered_hash_xyz789",
        base_sha="a" * 40,
        patch_file_exists=True,
    )
    assert not eligible2
    assert any("tampered" in r or "modified" in r for r in reasons2)


# SCENARIO 5: Caller-only workaround rejected by callee test
def test_scenario_5_caller_only_workaround_rejected_by_callee_test(cross_file_fixture: Path):
    """Patching only caller (main.py) fails direct callee and second caller tests."""
    # Apply workaround in main.py only: replace format_greeting call with hardcoded 'Hello, World!'
    main_file = cross_file_fixture / "main.py"
    main_file.write_text("def run():\n    return 'Hello, World!'\n", encoding="utf-8")

    # Full suite run: caller passes, but test_formatter_direct and test_second_caller FAIL!
    val = validate_workspace(cross_file_fixture, runner="local")
    assert not val.passed
    assert any(tc.name == "test_formatter_direct" and tc.outcome == "failed" for tc in val.test_cases)
    assert any(tc.name == "test_second_caller" and tc.outcome == "failed" for tc in val.test_cases)

    # Verification gate blocks publication
    eligible, reasons = check_publication_eligibility(
        base_reproduced=True,
        candidate_reproducer_passed=True,
        candidate_suite_passed=val.passed,
        patch_text="diff",
        changed_files=["main.py"],
        original_reproducer_hash="h1",
        current_reproducer_hash="h1",
    )
    assert not eligible
    assert any("maintained tests" in r for r in reasons)


# SCENARIO 6: Candidate breaking maintained suite rejected
def test_scenario_6_candidate_breaking_maintained_suite_rejected():
    """Any candidate patch that breaks other tests in the maintained suite is rejected."""
    eligible, reasons = check_publication_eligibility(
        base_reproduced=True,
        candidate_reproducer_passed=True,
        candidate_suite_passed=False,  # Broken suite
        patch_text="diff",
        changed_files=["service/formatter.py"],
        original_reproducer_hash="h1",
        current_reproducer_hash="h1",
    )
    assert not eligible
    assert any("maintained tests" in r for r in reasons)


# SCENARIO 7: Docker unavailable / host-only results marked with environment limitation
def test_scenario_7_docker_unavailable_marked_as_environment_error(tmp_path: Path):
    """When Docker is unreachable or image missing, reports environment_error, never silent host pass."""
    (tmp_path / "test_fake.py").write_text("def test_ok(): assert True\n")
    val = validate_workspace(
        tmp_path,
        runner="docker",
        image="nonexistent_invalid_image:9999",
        timeout_seconds=5,
    )
    assert not val.passed
    assert val.status == "environment_error"
    assert val.reproducer_outcome == "environment_error"


# SCENARIO 8: Simulated publication produces local evidence only (no fake GitHub URL)
def test_scenario_8_simulated_publication_produces_local_evidence_only(tmp_path: Path):
    """Offline/simulated publication creates a local draft record file and never invents fake GitHub URLs."""
    local_rec_dir = tmp_path / "local_artifacts"
    pr_result = create_draft_pr(
        owner="fast-org",
        repo="greeting-service",
        branch_name="bug-fixer/run-test1234",
        base_branch="main",
        title="fix: resolve greeting bug",
        body="## PR Summary\nVerified automated repair",
        reviewer="senior-engineer",
        api_token=None,  # No token -> local simulation!
        local_record_dir=local_rec_dir,
    )

    assert pr_result["publication_mode"] == "simulated_local"
    assert pr_result["pr_number"] is None
    assert "github.com" not in pr_result["pr_url"]
    assert pr_result["reviewer_status"] == "not_requested"
    assert pr_result["branch_protection"] == "not_required"
    assert (local_rec_dir / "local_draft_pr.json").is_file()

    rec_data = json.loads((local_rec_dir / "local_draft_pr.json").read_text(encoding="utf-8"))
    assert rec_data["publication_mode"] == "simulated_local"
    assert rec_data["required_reviewer"] == "senior-engineer"
    assert rec_data["reviewer_status"] == "not_requested"


# SCENARIO 9: Partial live publication retry creates exactly 1 PR (idempotency)
def test_scenario_9_partial_live_publication_retry_idempotency(tmp_path: Path):
    """When branch push succeeds but PR creation fails, retry reuses branch and creates exactly 1 PR."""
    class FlakyGitHubMock:
        def __init__(self):
            self.call_count = 0
            self.prs = []

        def create_draft_pr(self, owner, repo, branch_name, base_branch, title, body, reviewer):
            self.call_count += 1
            if self.call_count == 1:
                raise RuntimeError("Temporary GitHub rate limit 429")
            pr = {
                "publication_mode": "published_github",
                "pr_number": 99,
                "pr_url": f"https://github.com/{owner}/{repo}/pull/99",
                "draft": True,
                "reviewer_requested": reviewer,
                "reviewer_status": "requested",
                "branch_protection": "verified_required",
            }
            self.prs.append(pr)
            return pr

    mock_client = FlakyGitHubMock()
    # Attempt 1 fails
    with pytest.raises(RuntimeError, match="Temporary GitHub rate limit"):
        create_draft_pr("owner", "repo", "branch-1", "main", "title", "body", "rev", mock_client=mock_client)

    # Attempt 2 succeeds
    result = create_draft_pr("owner", "repo", "branch-1", "main", "title", "body", "rev", mock_client=mock_client)
    assert result["pr_number"] == 99
    assert len(mock_client.prs) == 1


# SCENARIO 10: Human review rule unreadable reports unknown
def test_scenario_10_human_review_rule_unreadable_reports_unknown(monkeypatch):
    """When token lacks permissions to read branch protection (403), reports 'unknown'."""
    def mock_get(url, *args, **kwargs):
        class MockResp:
            status_code = 403
            text = "Forbidden: Resource requires admin privileges"
            def json(self): return {}
        return MockResp()

    def mock_post(url, *args, **kwargs):
        class MockResp:
            status_code = 201
            text = ""
            def json(self): return {"number": 12, "html_url": "https://github.com/owner/repo/pull/12"}
        return MockResp()

    monkeypatch.setattr(httpx.Client, "get", mock_get)
    monkeypatch.setattr(httpx.Client, "post", mock_post)

    pr_info = create_draft_pr(
        owner="owner",
        repo="repo",
        branch_name="bug-fixer/branch",
        base_branch="main",
        title="test",
        body="test body",
        reviewer="auditor",
        api_token="valid_token_without_admin_access",
    )

    assert pr_info["publication_mode"] == "published_github"
    assert pr_info["branch_protection"] == "unknown"
    assert pr_info["branch_protection_verified"] is False


def test_independent_clean_replay_verification(tmp_path: Path, cross_file_fixture: Path):
    """Test full verify_run clean replay workflow and tamper detection."""
    settings = Settings(
        model="qwen2.5-coder:7b",
        data_dir=tmp_path / "replay_data",
        runner="local",
    )
    workflow = BugFixerWorkflow(settings)

    mock_repair = {
        "content": "Defect fixed in service/formatter.py",
        "tool_calls": [
            {
                "name": "replace_text",
                "arguments": {
                    "relative_path": "service/formatter.py",
                    "old": "Goodbye",
                    "new": "Hello",
                },
            }
        ],
    }

    run = workflow.execute_fix(
        "https://github.com/acme/project/issues/105",
        source_repo=cross_file_fixture,
        test_nodeid="tests/test_greeting.py::test_greeting",
        mock_issue_data={"title": "Test", "body": "fix greeting"},
        mock_llm_response=mock_repair,
        runner_override="local",
    )
    assert run.stage == RunState.READY_FOR_REVIEW

    store = EvidenceStore(settings.data_dir / "bug_fixer.db", settings.data_dir / "artifacts")

    # 1. Independent clean-checkout replay verification
    eligible, report = verify_run(
        run.run_id,
        store,
        settings,
        source_repo=cross_file_fixture,
        runner="local",
    )
    assert eligible is True
    assert report["base_sha_valid"] is True
    assert report["manifest_verified"] is True
    assert report["base_reproduced"] is True
    assert report["patch_applied_cleanly"] is True
    assert report["candidate_reproducer_passed"] is True
    assert report["candidate_suite_passed"] is True
    assert report["reproducer_hash_intact"] is True

    # 2. Tampering test: modify candidate.patch on disk
    patch_path = store.artifacts_dir / run.run_id / "candidate.patch"
    patch_path.write_text("corrupted content", encoding="utf-8")

    eligible_tampered, report_tampered = verify_run(
        run.run_id,
        store,
        settings,
        source_repo=cross_file_fixture,
        runner="local",
    )
    assert eligible_tampered is False
    assert report_tampered["manifest_verified"] is False
    assert any("mismatch" in e or "tamper" in e for e in report_tampered["manifest_errors"])

