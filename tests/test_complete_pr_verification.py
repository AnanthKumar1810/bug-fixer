"""Automated tests for Three-State Complete PR Docker Verification and Negative Controls."""

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from bug_fixer.models import ValidationSummary, ValidationCheck
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
        s2 = ValidationSummary(passed=False, checks=[ValidationCheck(name="pytest", passed=False, output="AssertionError")], duration_seconds=0.1)
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
