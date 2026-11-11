"""Test independent validation, publication blocking gates, and strict Docker isolation."""

from pathlib import Path
import pytest

from bug_fixer.docker_runner import validate_workspace
from bug_fixer.verification import check_publication_eligibility


def test_docker_failure_never_falls_back_to_host(tmp_path: Path):
    """When runner='docker' and an invalid image is used, it fails with environment_error and never falls back."""
    (tmp_path / "test_simple.py").write_text("def test_it(): assert True\n")

    summary = validate_workspace(
        tmp_path,
        runner="docker",
        image="non_existent_validator_image_12345:never_exists",
        timeout_seconds=5,
    )
    # Must report environment_error, never silent host pass!
    assert not summary.passed
    assert summary.status == "environment_error"
    assert summary.reproducer_outcome == "environment_error"


def test_tampered_reproducer_blocks_publication():
    """If the reproducer test hash was tampered with, publication is blocked."""
    eligible, reasons = check_publication_eligibility(
        base_reproduced=True,
        candidate_reproducer_passed=True,
        candidate_suite_passed=True,
        patch_text="--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-1\n+2\n",
        changed_files=["service/formatter.py"],
        original_reproducer_hash="hash_alpha_123",
        current_reproducer_hash="hash_tampered_456",
    )
    assert not eligible
    assert any("tampered" in r or "modified" in r for r in reasons)


def test_prohibited_test_file_edit_blocks_publication():
    """Modifying a test file or configuration file blocks publication."""
    eligible, reasons = check_publication_eligibility(
        base_reproduced=True,
        candidate_reproducer_passed=True,
        candidate_suite_passed=True,
        patch_text="diff",
        changed_files=["tests/test_greeting.py"],
        original_reproducer_hash="h1",
        current_reproducer_hash="h1",
    )
    assert not eligible
    assert any("Prohibited" in r for r in reasons)


def test_failing_suite_blocks_publication():
    """If maintained suite failed on candidate, publication is blocked."""
    eligible, reasons = check_publication_eligibility(
        base_reproduced=True,
        candidate_reproducer_passed=True,
        candidate_suite_passed=False,
        patch_text="diff",
        changed_files=["service/formatter.py"],
        original_reproducer_hash="h1",
        current_reproducer_hash="h1",
    )
    assert not eligible
    assert any("maintained tests" in r for r in reasons)


def test_out_of_scope_edit_blocks_publication():
    """When allowed_paths is enforced, editing outside scope blocks publication."""
    eligible, reasons = check_publication_eligibility(
        base_reproduced=True,
        candidate_reproducer_passed=True,
        candidate_suite_passed=True,
        patch_text="diff",
        changed_files=["main.py"],
        original_reproducer_hash="h1",
        current_reproducer_hash="h1",
        allowed_paths=["service/formatter.py"],
    )
    assert not eligible
    assert any("allowed edit scope" in r for r in reasons)
