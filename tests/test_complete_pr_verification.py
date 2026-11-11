"""Automated tests for Three-State Complete PR Docker Verification and Negative Controls."""

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from bug_fixer.models import ValidationSummary, ValidationCheck, TestCaseResult
from bug_fixer.verification import verify_complete_pr_package


@pytest.fixture
def canonical_release_dir() -> Path:
    return Path(r"C:\Users\Siddhu\OneDrive\Desktop\bug fixer\.bug-fixer-data\release_artifacts\ml-code-review-assistant-ap002")


@pytest.fixture
def upstream_repo() -> Path:
    return Path(r"C:\Users\Siddhu\OneDrive\Desktop\bug fixer\.bug-fixer-data\demo_repo\ml-code-review-assistant")


def test_corrupted_patch_hash_rejected(canonical_release_dir, upstream_repo):
    """If complete.patch is corrupted or does not match manifest hash, verification must fail."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        corrupted_dir = Path(tmp_dir) / "corrupted_release"
        shutil.copytree(canonical_release_dir, corrupted_dir)
        
        # Tamper with complete.patch
        patch_file = corrupted_dir / "complete.patch"
        patch_file.write_text(patch_file.read_text(encoding="utf-8") + "\n# corrupted line\n", encoding="utf-8")

        ok, rep = verify_complete_pr_package(corrupted_dir, upstream_repo, runner="local")
        assert ok is False
        assert "Patch hash mismatch" in rep.get("error", "")


def test_wrong_base_sha_rejected(canonical_release_dir, upstream_repo):
    """If base SHA is invalid or all-zeros, verification must fail."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        bad_base_dir = Path(tmp_dir) / "bad_base_release"
        shutil.copytree(canonical_release_dir, bad_base_dir)

        manifest_file = bad_base_dir / "manifest.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["base_sha"] = "0" * 40
        manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        ok, rep = verify_complete_pr_package(bad_base_dir, upstream_repo, runner="local")
        assert ok is False
        assert "Invalid base commit SHA" in rep.get("error", "") or "Failed to checkout" in rep.get("error", "")


def test_wrong_tree_id_rejected(canonical_release_dir, upstream_repo):
    """If reconstructed git tree does not match manifest candidate_tree_id, verification must fail."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        bad_tree_dir = Path(tmp_dir) / "bad_tree_release"
        shutil.copytree(canonical_release_dir, bad_tree_dir)

        manifest_file = bad_tree_dir / "manifest.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["candidate_tree_id"] = "a" * 40  # bogus tree id
        manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        # Mock validate_workspace to return passed for State 1, failed for State 2, passed for State 3
        s1 = ValidationSummary(passed=True, checks=[ValidationCheck(name="pytest", passed=True)], duration_seconds=0.1)
        s2 = ValidationSummary(
            passed=False,
            checks=[ValidationCheck(name="pytest", passed=False, output="AssertionError: AP002 incorrectly flagged immutable tuple default")],
            failing_assertions=["E       AssertionError: AP002 incorrectly flagged immutable tuple default"],
            duration_seconds=0.1,
        )
        s3 = ValidationSummary(passed=True, checks=[ValidationCheck(name="pytest", passed=True)], duration_seconds=0.1)

        with patch("bug_fixer.verification.validate_workspace", side_effect=[s1, s2, s3, s3]):
            ok, rep = verify_complete_pr_package(bad_tree_dir, upstream_repo, runner="local")
            assert ok is False
            assert "Candidate tree mismatch" in rep.get("error", "")
            assert rep.get("tree_match") is False


def test_already_green_reproducer_rejected(canonical_release_dir, upstream_repo):
    """If reproducer test passes on base commit without patch, it is rejected (no defect reproduced)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        green_dir = Path(tmp_dir) / "green_release"
        shutil.copytree(canonical_release_dir, green_dir)

        # State 1 passes, State 2 also passes (already green!)
        s1 = ValidationSummary(passed=True, checks=[ValidationCheck(name="pytest", passed=True)], duration_seconds=0.1)
        s2_already_green = ValidationSummary(passed=True, checks=[ValidationCheck(name="pytest", passed=True)], duration_seconds=0.1)

        with patch("bug_fixer.verification.validate_workspace", side_effect=[s1, s2_already_green]):
            ok, rep = verify_complete_pr_package(green_dir, upstream_repo, runner="local")
            assert ok is False
            assert "State 2 failed: reproducer test unexpectedly passed on base commit" in rep.get("error", "")
            assert rep.get("state2_outcome") == "unexpected_pass"


def test_unrelated_exception_in_reproducer_rejected(canonical_release_dir, upstream_repo):
    """If reproducer fails with an unrelated exception (e.g. ZeroDivisionError), it is rejected."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        bad_dir = Path(tmp_dir) / "unrelated_exc_release"
        shutil.copytree(canonical_release_dir, bad_dir)

        s1 = ValidationSummary(passed=True, checks=[ValidationCheck(name="pytest", passed=True)], duration_seconds=0.1)
        s2_unrelated = ValidationSummary(
            passed=False,
            checks=[ValidationCheck(name="pytest", passed=False, output="ZeroDivisionError: division by zero", exit_code=1)],
            failing_assertions=["E       ZeroDivisionError: division by zero"],
            duration_seconds=0.1,
        )

        with patch("bug_fixer.verification.validate_workspace", side_effect=[s1, s2_unrelated]):
            ok, rep = verify_complete_pr_package(bad_dir, upstream_repo, runner="local")
            assert ok is False
            assert "unrelated exception instead of expected assertion failure" in rep.get("error", "")
            assert rep.get("state2_outcome") == "unrelated_exception"


def test_setup_failure_in_reproducer_rejected(canonical_release_dir, upstream_repo):
    """If reproducer fails during fixture setup (outcome='error'), it is rejected as setup failure."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        setup_err_dir = Path(tmp_dir) / "setup_err_release"
        shutil.copytree(canonical_release_dir, setup_err_dir)

        nodeid = "tests/test_project4.py::test_ap002_immutable_tuple_default"
        s1 = ValidationSummary(passed=True, checks=[ValidationCheck(name="pytest", passed=True)], duration_seconds=0.1)
        s2_setup_err = ValidationSummary(
            passed=False,
            checks=[ValidationCheck(name="pytest", passed=False, output="fixture setup error", exit_code=1)],
            test_cases=[
                TestCaseResult(
                    nodeid=nodeid,
                    file="tests/test_project4.py",
                    name="test_ap002_immutable_tuple_default",
                    outcome="error",
                    message="Fixture 'db_session' failed during setup",
                )
            ],
            duration_seconds=0.1,
        )

        with patch("bug_fixer.verification.validate_workspace", side_effect=[s1, s2_setup_err]):
            ok, rep = verify_complete_pr_package(setup_err_dir, upstream_repo, runner="local")
            assert ok is False
            assert "setup/fixture phase rather than test call execution" in rep.get("error", "")
            assert rep.get("state2_outcome") == "setup_failure"


def test_timeout_in_reproducer_rejected(canonical_release_dir, upstream_repo):
    """If reproducer times out during reproduction, it is rejected."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        timeout_dir = Path(tmp_dir) / "timeout_release"
        shutil.copytree(canonical_release_dir, timeout_dir)

        s1 = ValidationSummary(passed=True, checks=[ValidationCheck(name="pytest", passed=True)], duration_seconds=0.1)
        s2_timeout = ValidationSummary(
            passed=False,
            checks=[ValidationCheck(name="pytest", passed=False, output="Timed out after 90s", exit_code=None)],
            reproducer_outcome="timeout",
            duration_seconds=90.0,
        )

        with patch("bug_fixer.verification.validate_workspace", side_effect=[s1, s2_timeout]):
            ok, rep = verify_complete_pr_package(timeout_dir, upstream_repo, runner="local")
            assert ok is False
            assert "reproducer test timed out on base commit" in rep.get("error", "")
            assert rep.get("state2_outcome") == "timeout"


def test_signature_mismatch_in_reproducer_rejected(canonical_release_dir, upstream_repo):
    """If reproducer assertion fails with an unexpected message not matching manifest signature, it is rejected."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        mismatch_dir = Path(tmp_dir) / "mismatch_release"
        shutil.copytree(canonical_release_dir, mismatch_dir)

        s1 = ValidationSummary(passed=True, checks=[ValidationCheck(name="pytest", passed=True)], duration_seconds=0.1)
        s2_mismatch = ValidationSummary(
            passed=False,
            checks=[ValidationCheck(name="pytest", passed=False, output="AssertionError: completely different failure", exit_code=1)],
            failing_assertions=["E       AssertionError: completely different failure"],
            duration_seconds=0.1,
        )

        with patch("bug_fixer.verification.validate_workspace", side_effect=[s1, s2_mismatch]):
            ok, rep = verify_complete_pr_package(mismatch_dir, upstream_repo, runner="local")
            assert ok is False
            assert "reproducer assertion did not match expected failure signature" in rep.get("error", "")
            assert rep.get("state2_outcome") == "signature_mismatch"

