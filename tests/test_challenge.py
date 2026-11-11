"""Tests for the patch challenge and publication destination verification."""

import json
from pathlib import Path
import pytest

from bug_fixer.challenge import (
    ChallengeCheckResult,
    ChallengeSummary,
    GREETING_CALLER_WORKAROUND,
    format_challenge_markdown,
    format_challenge_table,
    get_historical_workaround,
    run_patch_challenge,
)
from bug_fixer.config import Settings
from bug_fixer.db import EvidenceStore, compute_sha256
from bug_fixer.models import RunRecord, RunState
from bug_fixer.publication import (
    create_draft_pr,
    verify_publication_destination,
)
from bug_fixer.reporting import render_review_claims


def test_destination_verification_matches_or_rejects():
    """Verify that destination checker accepts matching repos and rejects mismatches."""
    run = RunRecord(
        run_id="run_dest_test",
        issue_url="https://github.com/fast-org/greeting-service/issues/1",
        issue_number=1,
        repo_owner="fast-org",
        repo_name="greeting-service",
        base_sha="abcdef1234567890abcdef1234567890abcdef12",
        reproducer_nodeid="tests/test_greeting.py::test_greeting",
        reproducer_hash="dummyhash",
        stage=RunState.READY_FOR_REVIEW,
        status="ready",
        created_at="2026-11-11T00:00:00Z",
        updated_at="2026-11-11T00:00:00Z",
    )

    # 1. Matching remote destination with confirmation
    ok1, msg1 = verify_publication_destination("https://github.com/fast-org/greeting-service.git", run, confirm_remote=True)
    assert ok1 is True
    assert "matches recorded repository" in msg1

    # 2. Case-insensitive matching with confirmation
    ok2, msg2 = verify_publication_destination("https://github.com/FAST-ORG/GREETING-SERVICE.git", run, confirm_remote=True)
    assert ok2 is True

    # 3. Mismatched destination (e.g. attempting to push greeting service to psf/requests)
    ok3, msg3 = verify_publication_destination("https://github.com/psf/requests.git", run)
    assert ok3 is False
    assert "Publication destination mismatch" in msg3
    assert "psf/requests" in msg3
    assert "fast-org/greeting-service" in msg3

    # 4. Local bare repos allowed
    ok4, msg4 = verify_publication_destination(".bug-fixer-data/test_demo_remote.git", run)
    assert ok4 is True

    # 5. Unknown alias without repo context or unconfigured alias is rejected
    ok5, msg5 = verify_publication_destination("unknown_remote_alias", run)
    assert ok5 is False
    assert "does not exist and is not a valid URL" in msg5


def test_render_review_claims_honesty():
    """Simulated local publication must never claim live review enforcement or requested reviewers."""
    # Simulated local
    sim_claims = render_review_claims(
        reviewer="senior-engineer",
        reviewer_status="not_requested",
        publication_mode="simulated_local",
        branch_protection="not_applicable",
        is_published=True,
    )
    assert sim_claims["reviewer_status"] == "not_requested"
    assert sim_claims["branch_protection"] == "not_applicable"
    assert sim_claims["branch_protection_enforced"] is False
    assert "Simulated local publication. No GitHub reviewer was requested. Branch protection is not applicable." in sim_claims["summary_text"]
    assert "Simulated local publication" in sim_claims["markdown"]

    # Live published with verified required reviews
    live_claims = render_review_claims(
        reviewer="senior-reviewer",
        reviewer_status="requested",
        publication_mode="published_github",
        branch_protection="verified_required",
        is_published=True,
    )
    assert live_claims["reviewer_status"] == "requested"
    assert live_claims["branch_protection"] == "verified_required"
    assert live_claims["branch_protection_enforced"] is True
    assert "@senior-reviewer" in live_claims["markdown"]
    assert "verified_required" in live_claims["summary_text"]

    # Live published with unknown branch rules (e.g. 403 Forbidden)
    unknown_claims = render_review_claims(
        reviewer="senior-reviewer",
        reviewer_status="requested",
        publication_mode="published_github",
        branch_protection="unknown",
        is_published=True,
    )
    assert unknown_claims["branch_protection"] == "unknown"
    assert unknown_claims["branch_protection_enforced"] is False
    assert "unknown" in unknown_claims["summary_text"]
    assert "Unable to verify branch protection rules" in unknown_claims["markdown"]


def test_challenge_formatting_table_and_markdown():
    """Verify challenge table and markdown formatters contain distinguishing badges and immutability."""
    checks = [
        ChallengeCheckResult(
            test_id="tests/test_greeting.py::test_greeting",
            description="Original reproducer",
            base_outcome="failed",
            workaround_outcome="passed",
            candidate_outcome="passed",
            discriminates=True,
            role="reproducer",
        ),
        ChallengeCheckResult(
            test_id="tests/test_greeting.py::test_formatter_direct",
            description="Callee direct check",
            base_outcome="failed",
            workaround_outcome="failed",
            candidate_outcome="passed",
            discriminates=True,
            role="direct_callee",
        ),
        ChallengeCheckResult(
            test_id="tests/test_greeting.py::test_second_caller",
            description="Second caller check",
            base_outcome="failed",
            workaround_outcome="failed",
            candidate_outcome="passed",
            discriminates=True,
            role="second_caller",
        ),
    ]

    summary = ChallengeSummary(
        run_id="run_mock_chal",
        status="passed",
        candidate_tree_before="1178b68be4d99c4b3441ccb4d52e4199ff8dffbd",
        candidate_tree_after="1178b68be4d99c4b3441ccb4d52e4199ff8dffbd",
        tree_intact=True,
        checks=checks,
        distinguishing_checks=[
            "tests/test_greeting.py::test_formatter_direct",
            "tests/test_greeting.py::test_second_caller",
        ],
        strongest_survived_challenge="tests/test_greeting.py::test_formatter_direct & tests/test_greeting.py::test_second_caller",
        workaround_tested=True,
        overhead_seconds=4.5,
        details="Candidate passed all checks and survived 2 distinguishing challenge test(s).",
    )

    table_text = format_challenge_table(summary)
    assert "PATCH CHALLENGE RESULTS" in table_text
    assert "Intact: YES" in table_text
    assert "PASS (fooled)" in table_text
    assert "FAIL (exposed)" in table_text
    assert "YES (direct_callee)" in table_text
    assert "YES (second_caller)" in table_text

    md_text = format_challenge_markdown(summary)
    assert "### Patch Challenge: Generalization Verification" in md_text
    assert "Verified immutable across challenge: `True`" in md_text
    assert "`PASS` *(fooled)*" in md_text
    assert "`FAIL` *(exposed)*" in md_text
    assert "**YES** (direct_callee)" in md_text
    assert "**YES** (second_caller)" in md_text
    assert "Strongest Survived Challenge" in md_text


def test_challenge_run_on_greeting_service(tmp_path: Path):
    """End-to-end patch challenge on greeting fixture distinguishes caller workaround from candidate."""
    settings = Settings.from_env()
    store = EvidenceStore(settings.data_dir / "bug_fixer.db", settings.data_dir / "artifacts")
    run_rec = store.get_run("run_5525c66d9a46")
    if not run_rec:
        pytest.skip("run_5525c66d9a46 not present in local database.")

    # Execute challenge with local or docker runner
    ok, summary = run_patch_challenge(
        "run_5525c66d9a46",
        store,
        settings,
        runner="local",
    )

    assert ok is True
    assert summary.status == "passed"
    assert summary.tree_intact is True
    assert summary.workaround_tested is True
    assert len(summary.distinguishing_checks) >= 2

    # Verify reproducer fooled the workaround
    reproducer_check = next(c for c in summary.checks if c.role == "reproducer")
    assert reproducer_check.base_outcome in ("failed", "error")
    assert reproducer_check.workaround_outcome == "passed"
    assert reproducer_check.candidate_outcome == "passed"

    # Verify direct callee and second caller exposed the workaround
    direct_check = next(c for c in summary.checks if c.role == "direct_callee")
    assert direct_check.base_outcome in ("failed", "error")
    assert direct_check.workaround_outcome in ("failed", "error")
    assert direct_check.candidate_outcome == "passed"

    second_check = next(c for c in summary.checks if c.role == "second_caller")
    assert second_check.base_outcome in ("failed", "error")
    assert second_check.workaround_outcome in ("failed", "error")
    assert second_check.candidate_outcome == "passed"

    # Verify artifact was persisted and manifest remains intact
    chal_file = store.artifacts_dir / "run_5525c66d9a46" / "challenge.json"
    assert chal_file.is_file()
    man_ok, man_errs = store.verify_manifest("run_5525c66d9a46")
    assert man_ok is True
    assert not man_errs
