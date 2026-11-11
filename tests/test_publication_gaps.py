"""Regression tests for publication gaps: failed challenge blocking, tree immutability,
and effective Git destination verification across aliases and push URLs.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import pytest

from bug_fixer.challenge import ChallengeCheckResult, ChallengeSummary
from bug_fixer.config import Settings
from bug_fixer.db import EvidenceStore, compute_sha256
from bug_fixer.models import ManifestEntry, RunManifest, RunRecord, RunState
from bug_fixer.publication import (
    PublicationError,
    create_draft_pr,
    push_candidate_branch,
    redact_credentials,
    verify_publication_destination,
)
from bug_fixer.verification import (
    check_challenge_eligibility,
    check_publication_eligibility,
    verify_run,
)


def _create_mock_run_environment(
    tmp_path: Path,
    run_id: str,
    *,
    base_sha: str = "8331bb1d806ab683f55c2846b3c543717c09128c",
    tree_id: str = "1178b68be4d99c4b3441ccb4d52e4199ff8dffbd",
    challenge_status: str | None = None,
    challenge_summary_override: dict | None = None,
    write_challenge_file: bool = True,
    corrupt_challenge_file: bool = False,
    challenge_tree_override: str | None = None,
    challenge_run_id_override: str | None = None,
    tree_changed_in_challenge: bool = False,
) -> tuple[EvidenceStore, RunRecord, Path]:
    """Helper to set up an isolated disposable run in a temporary store."""
    data_dir = tmp_path / "data"
    store = EvidenceStore(data_dir / "bug_fixer.db", data_dir / "artifacts")

    run_dir = store.artifacts_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    patch_text = (
        "diff --git a/service/formatter.py b/service/formatter.py\n"
        "--- a/service/formatter.py\n"
        "+++ b/service/formatter.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def format_greeting(name):\n"
        "-    return f'Goodbye, {name}!'\n"
        "+    return f'Hello, {name}!'\n"
    )
    patch_file = run_dir / "candidate.patch"
    patch_file.write_text(patch_text, encoding="utf-8")

    run = RunRecord(
        run_id=run_id,
        issue_url="https://github.com/fast-org/greeting-service/issues/103",
        issue_number=103,
        repo_owner="fast-org",
        repo_name="greeting-service",
        base_sha=base_sha,
        stage=RunState.READY_FOR_REVIEW,
        status="ready",
        reproducer_nodeid="tests/test_greeting.py::test_greeting",
        reproducer_hash="dummyreproducerhash123",
        candidate_patch_hash=compute_sha256(patch_text.encode("utf-8")),
        verified_tree_id=tree_id,
        created_at="2026-11-11T12:00:00Z",
        updated_at="2026-11-11T12:00:00Z",
        challenge_status=challenge_status,
    )
    store.save_run(run)

    # Setup challenge artifact if requested
    if write_challenge_file:
        chal_path = run_dir / "challenge.json"
        if corrupt_challenge_file:
            chal_path.write_text("{ this is malformed json", encoding="utf-8")
        elif challenge_summary_override is not None:
            chal_path.write_text(json.dumps(challenge_summary_override, indent=2), encoding="utf-8")
        else:
            c_tree = challenge_tree_override or tree_id
            c_tree_after = (c_tree + "_modified") if tree_changed_in_challenge else c_tree
            status_val = challenge_status or "passed"
            summary = ChallengeSummary(
                run_id=challenge_run_id_override or run_id,
                status=status_val,
                candidate_tree_before=c_tree,
                candidate_tree_after=c_tree_after,
                tree_intact=not tree_changed_in_challenge,
                checks=[
                    ChallengeCheckResult(
                        test_id="tests/test_greeting.py::test_greeting",
                        description="Reproducer",
                        base_outcome="failed",
                        workaround_outcome="passed",
                        candidate_outcome="passed" if status_val != "failed" else "failed",
                        discriminates=True,
                        role="reproducer",
                    ),
                    ChallengeCheckResult(
                        test_id="tests/test_greeting.py::test_formatter_direct",
                        description="Direct callee",
                        base_outcome="failed",
                        workaround_outcome="failed",
                        candidate_outcome="passed" if status_val != "failed" else "failed",
                        discriminates=True,
                        role="direct_callee",
                    ),
                ],
                distinguishing_checks=["tests/test_greeting.py::test_formatter_direct"],
                strongest_survived_challenge="tests/test_greeting.py::test_formatter_direct",
                overhead_seconds=2.5,
                details=f"Challenge status: {status_val}",
            )
            chal_path.write_text(json.dumps(summary.model_dump(), indent=2), encoding="utf-8")

    # Build manifest
    manifest_files = {}
    for f in run_dir.iterdir():
        raw = f.read_bytes()
        manifest_files[f.name] = ManifestEntry(
            rel_path=f.name,
            sha256=compute_sha256(raw),
            byte_size=len(raw),
        )
    manifest = RunManifest(
        run_id=run_id,
        base_sha=base_sha,
        provenance="fixture",
        created_at="2026-11-11T12:00:00Z",
        candidate_tree_id=tree_id,
        challenge_status=challenge_status,
        files=manifest_files,
    )
    store.store_manifest(run_id, manifest)

    return store, run, run_dir


# ---------------------------------------------------------------------------
# FIX 1: Enforce challenge result before publication
# ---------------------------------------------------------------------------

def test_failed_patch_challenge_blocks_publication(tmp_path: Path):
    """When candidate passes ordinary tests but fails patch challenge, publication is refused."""
    store, run, run_dir = _create_mock_run_environment(
        tmp_path,
        "run_failed_chal",
        challenge_status="failed",
    )

    chal_errs = check_challenge_eligibility(run, store, verified_tree_id=run.verified_tree_id)
    assert len(chal_errs) > 0
    assert any("Patch challenge failed for candidate patch" in e for e in chal_errs)
    assert any(str(run_dir / "challenge.json") in e for e in chal_errs)

    eligible, reasons = check_publication_eligibility(
        base_reproduced=True,
        candidate_reproducer_passed=True,
        candidate_suite_passed=True,
        patch_text="diff --git a/f b/f",
        changed_files=["service/formatter.py"],
        original_reproducer_hash=run.reproducer_hash,
        current_reproducer_hash=run.reproducer_hash,
        base_sha=run.base_sha,
        challenge_errors=chal_errs,
    )
    assert eligible is False
    assert any("Patch challenge failed" in r for r in reasons)


def test_passing_challenge_allows_publication(tmp_path: Path):
    """A valid passing challenge matching run ID and tree allows publication."""
    store, run, run_dir = _create_mock_run_environment(
        tmp_path,
        "run_pass_chal",
        challenge_status="passed",
    )

    chal_errs = check_challenge_eligibility(run, store, verified_tree_id=run.verified_tree_id)
    assert chal_errs == []

    eligible, reasons = check_publication_eligibility(
        base_reproduced=True,
        candidate_reproducer_passed=True,
        candidate_suite_passed=True,
        patch_text="diff --git a/f b/f",
        changed_files=["service/formatter.py"],
        original_reproducer_hash=run.reproducer_hash,
        current_reproducer_hash=run.reproducer_hash,
        base_sha=run.base_sha,
        challenge_errors=chal_errs,
    )
    assert eligible is True
    assert reasons == []


def test_challenge_tree_changed_or_mismatched_blocks_publication(tmp_path: Path):
    """Candidate tree changed during challenge or mismatching replay tree is rejected."""
    # 1. Tree changed during challenge
    store1, run1, _ = _create_mock_run_environment(
        tmp_path,
        "run_tree_changed",
        tree_changed_in_challenge=True,
    )
    errs1 = check_challenge_eligibility(run1, store1, verified_tree_id=run1.verified_tree_id)
    assert any("Candidate tree changed during the patch challenge" in e for e in errs1)

    # 2. Challenge references another candidate tree
    store2, run2, _ = _create_mock_run_environment(
        tmp_path,
        "run_tree_mismatch",
        challenge_tree_override="deadbeef00000000000000000000000000000000",
    )
    errs2 = check_challenge_eligibility(run2, store2, verified_tree_id=run2.verified_tree_id)
    assert any("does not match verified candidate tree" in e for e in errs2)


def test_challenge_malformed_or_belongs_to_another_run_blocks_publication(tmp_path: Path):
    """Malformed JSON, missing schema fields, or wrong run ID are each rejected."""
    # 1. Malformed JSON
    store1, run1, _ = _create_mock_run_environment(
        tmp_path,
        "run_malformed",
        corrupt_challenge_file=True,
    )
    errs1 = check_challenge_eligibility(run1, store1, verified_tree_id=run1.verified_tree_id)
    assert any("malformed" in e.lower() for e in errs1)

    # 2. Wrong run_id in challenge artifact
    store2, run2, _ = _create_mock_run_environment(
        tmp_path,
        "run_wrong_id",
        challenge_run_id_override="run_completely_different_999",
    )
    errs2 = check_challenge_eligibility(run2, store2, verified_tree_id=run2.verified_tree_id)
    assert any("belongs to run 'run_completely_different_999', but current run is 'run_wrong_id'" in e for e in errs2)


def test_missing_challenge_after_recorded_failure_blocks_publication(tmp_path: Path):
    """Deleting a challenge artifact after a recorded failure is refused."""
    store, run, run_dir = _create_mock_run_environment(
        tmp_path,
        "run_deleted_failure",
        challenge_status="failed",
        write_challenge_file=False,  # challenge file was deleted or not created
    )
    # Even if challenge.json is missing, store knows it had challenge_status="failed"
    errs = check_challenge_eligibility(run, store, verified_tree_id=run.verified_tree_id)
    assert len(errs) > 0
    assert any("previously recorded challenge" in e and "missing from artifacts" in e for e in errs)


def test_legacy_run_never_challenged_remains_eligible(tmp_path: Path):
    """A legacy run that genuinely pre-dates challenge retains previous eligibility behavior."""
    store, run, _ = _create_mock_run_environment(
        tmp_path,
        "run_legacy_nochal",
        challenge_status=None,
        write_challenge_file=False,
    )
    errs = check_challenge_eligibility(run, store, verified_tree_id=run.verified_tree_id)
    assert errs == []

    eligible, reasons = check_publication_eligibility(
        base_reproduced=True,
        candidate_reproducer_passed=True,
        candidate_suite_passed=True,
        patch_text="diff --git a/f b/f",
        changed_files=["service/formatter.py"],
        original_reproducer_hash=run.reproducer_hash,
        current_reproducer_hash=run.reproducer_hash,
        base_sha=run.base_sha,
        challenge_errors=errs,
    )
    assert eligible is True


# ---------------------------------------------------------------------------
# FIX 2: Git push destination verification across aliases and push URLs
# ---------------------------------------------------------------------------

def _setup_git_repo_with_remote(
    tmp_path: Path,
    remote_name: str,
    fetch_url: str,
    push_url: str | None = None,
    extra_push_urls: list[str] | None = None,
) -> Path:
    """Helper to initialize a Git repository with specific remote configuration."""
    repo_dir = tmp_path / "source_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "TestUser"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@test.local"], check=True)
    (repo_dir / "README.md").write_text("# Repo", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "init"], check=True)

    subprocess.run(["git", "-C", str(repo_dir), "remote", "add", remote_name, fetch_url], check=True)
    if push_url:
        subprocess.run(["git", "-C", str(repo_dir), "remote", "set-url", "--push", remote_name, push_url], check=True)
    if extra_push_urls:
        for extra in extra_push_urls:
            subprocess.run(["git", "-C", str(repo_dir), "remote", "set-url", "--add", "--push", remote_name, extra], check=True)
    return repo_dir


def test_origin_points_to_local_bare_remote_succeeds_as_local_simulation(tmp_path: Path):
    """When origin points to a local bare repository, publication succeeds as simulated_local."""
    bare_remote = tmp_path / "bare_remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare_remote)], check=True)

    repo_dir = _setup_git_repo_with_remote(tmp_path, "origin", str(bare_remote))

    run = RunRecord(
        run_id="run_dest_local",
        issue_url="https://github.com/fast-org/greeting-service/issues/103",
        issue_number=103,
        repo_owner="fast-org",
        repo_name="greeting-service",
        base_sha="8331bb1d806ab683f55c2846b3c543717c09128c",
        stage=RunState.READY_FOR_REVIEW,
        status="ready",
        reproducer_nodeid="tests/test_greeting.py::test_greeting",
        reproducer_hash="h1",
        created_at="2026-11-11T12:00:00Z",
        updated_at="2026-11-11T12:00:00Z",
    )

    dest_res = verify_publication_destination("origin", run, source_repo=repo_dir)
    assert dest_res.is_valid is True
    assert dest_res.is_live is False
    assert Path(dest_res.resolved_destination).resolve() == bare_remote.resolve()


def test_origin_different_push_url_mismatch_is_rejected_before_write(tmp_path: Path):
    """When origin fetch URL matches but push URL points to a different repo, it is rejected."""
    # fetch matches fast-org/greeting-service, but push points to psf/requests!
    repo_dir = _setup_git_repo_with_remote(
        tmp_path,
        "origin",
        fetch_url="https://github.com/fast-org/greeting-service.git",
        push_url="https://github.com/psf/requests.git",
    )

    run = RunRecord(
        run_id="run_push_mismatch",
        issue_url="https://github.com/fast-org/greeting-service/issues/103",
        issue_number=103,
        repo_owner="fast-org",
        repo_name="greeting-service",
        base_sha="8331bb1d806ab683f55c2846b3c543717c09128c",
        stage=RunState.READY_FOR_REVIEW,
        status="ready",
        reproducer_nodeid="tests/test_greeting.py::test_greeting",
        reproducer_hash="h1",
        created_at="2026-11-11T12:00:00Z",
        updated_at="2026-11-11T12:00:00Z",
    )

    dest_res = verify_publication_destination("origin", run, source_repo=repo_dir)
    assert dest_res.is_valid is False
    assert "Publication destination mismatch" in dest_res.message
    assert "psf/requests" in dest_res.message
    assert "fast-org/greeting-service" in dest_res.message


def test_matching_github_alias_without_confirm_remote_is_rejected(tmp_path: Path):
    """Matching live GitHub remote requires explicit --confirm-remote."""
    repo_dir = _setup_git_repo_with_remote(
        tmp_path,
        "origin",
        fetch_url="https://github.com/fast-org/greeting-service.git",
    )

    run = RunRecord(
        run_id="run_need_confirm",
        issue_url="https://github.com/fast-org/greeting-service/issues/103",
        issue_number=103,
        repo_owner="fast-org",
        repo_name="greeting-service",
        base_sha="8331bb1d806ab683f55c2846b3c543717c09128c",
        stage=RunState.READY_FOR_REVIEW,
        status="ready",
        reproducer_nodeid="tests/test_greeting.py::test_greeting",
        reproducer_hash="h1",
        created_at="2026-11-11T12:00:00Z",
        updated_at="2026-11-11T12:00:00Z",
    )

    # Without confirmation
    dest_res = verify_publication_destination("origin", run, source_repo=repo_dir, confirm_remote=False)
    assert dest_res.is_valid is False
    assert "CONFIRMATION REQUIRED" in dest_res.message
    assert "--confirm-remote" in dest_res.message

    # With confirmation
    dest_res_ok = verify_publication_destination("origin", run, source_repo=repo_dir, confirm_remote=True)
    assert dest_res_ok.is_valid is True
    assert dest_res_ok.is_live is True


def test_unknown_alias_and_multiple_push_urls_rejected(tmp_path: Path):
    """Unknown remote alias and remotes with multiple push destinations are rejected."""
    repo_dir = _setup_git_repo_with_remote(
        tmp_path,
        "origin",
        fetch_url="https://github.com/fast-org/greeting-service.git",
        push_url="https://github.com/fast-org/greeting-service.git",
        extra_push_urls=["https://github.com/fast-org/backup.git"],
    )

    run = RunRecord(
        run_id="run_multi_push",
        issue_url="https://github.com/fast-org/greeting-service/issues/103",
        issue_number=103,
        repo_owner="fast-org",
        repo_name="greeting-service",
        base_sha="8331bb1d806ab683f55c2846b3c543717c09128c",
        stage=RunState.READY_FOR_REVIEW,
        status="ready",
        reproducer_nodeid="tests/test_greeting.py::test_greeting",
        reproducer_hash="h1",
        created_at="2026-11-11T12:00:00Z",
        updated_at="2026-11-11T12:00:00Z",
    )

    # 1. Multiple push URLs rejected as ambiguous
    res_multi = verify_publication_destination("origin", run, source_repo=repo_dir)
    assert res_multi.is_valid is False
    assert "multiple configured push destinations" in res_multi.message
    assert "Ambiguous push destination" in res_multi.message

    # 2. Unknown alias rejected
    res_unknown = verify_publication_destination("nonexistent_alias_123", run, source_repo=repo_dir)
    assert res_unknown.is_valid is False
    assert "Unknown remote alias" in res_unknown.message


def test_credential_redaction_in_messages_and_output():
    """Credentials in URLs must be redacted from error messages."""
    url_with_token = "https://x-access-token:ghp_SuperSecretSecret@github.com/fast-org/greeting-service.git"
    redacted = redact_credentials(url_with_token)
    assert "SuperSecretSecret" not in redacted
    assert "https://***@github.com" in redacted

    url_user_pass = "https://user:password123@github.com/org/repo.git"
    redacted2 = redact_credentials(url_user_pass)
    assert "password123" not in redacted2
    assert "https://***@github.com" in redacted2
