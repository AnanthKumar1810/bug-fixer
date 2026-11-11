"""Regression test for UTF-8 patch generation and clean replay verification on files with non-ASCII characters."""

import subprocess
import tempfile
from pathlib import Path
import pytest

from bug_fixer.verification import (
    generate_candidate_patch,
    verify_patch_reconstructs_candidate,
    check_publication_eligibility,
)


def test_utf8_diff_generation_with_non_ascii_bytes():
    """Verify that git diff generation handles non-ASCII bytes without Windows cp1252 errors."""
    with tempfile.TemporaryDirectory() as tmp:
        repo_dir = Path(tmp) / "repo"
        repo_dir.mkdir()

        # Init git repo with core.autocrlf false
        subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo_dir, check=True)

        # File with non-ASCII UTF-8 bytes (such as math symbols or curly quotes that break cp1252 charmap)
        content_base = (
            "# Mathematical symbols: \u2211, \u220f, \u221a\n"
            "# More-itertools quote: \u201cquotes\u201d\n"
            "def take(n, iterable):\n"
            "    return list(islice(iterable, n + 1))\n"
        )
        test_file = repo_dir / "recipes.py"
        test_file.write_text(content_base, encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base commit with non-ascii"], cwd=repo_dir, check=True)

        # Apply single line modification
        content_fixed = (
            "# Mathematical symbols: \u2211, \u220f, \u221a\n"
            "# More-itertools quote: \u201cquotes\u201d\n"
            "def take(n, iterable):\n"
            "    return list(islice(iterable, n))\n"
        )
        test_file.write_text(content_fixed, encoding="utf-8")

        # Generate candidate patch
        patch = generate_candidate_patch(repo_dir)
        assert patch.strip(), "Patch must not be empty on files containing non-ASCII characters"
        assert "islice(iterable, n)" in patch
        assert "take" in patch

        # Positive test: verify_patch_reconstructs_candidate
        with tempfile.TemporaryDirectory() as base_tmp:
            base_dir = Path(base_tmp) / "base"
            base_dir.mkdir()
            (base_dir / "recipes.py").write_text(content_base, encoding="utf-8")

            reconstructs = verify_patch_reconstructs_candidate(base_dir, patch, repo_dir)
            assert reconstructs is True, "Clean patch application must reconstruct tested candidate"

        # Negative control 1: Tampered patch must be rejected
        with tempfile.TemporaryDirectory() as base_tmp:
            base_dir = Path(base_tmp) / "base"
            base_dir.mkdir()
            (base_dir / "recipes.py").write_text(content_base, encoding="utf-8")

            tampered_patch = patch.replace("list(islice(iterable, n))", "list(islice(iterable, n + 999))")
            assert verify_patch_reconstructs_candidate(base_dir, tampered_patch, repo_dir) is False

        # Negative control 2: Tampered candidate workspace must be rejected
        with tempfile.TemporaryDirectory() as cand_tmp:
            tampered_cand = Path(cand_tmp) / "cand"
            tampered_cand.mkdir()
            (tampered_cand / "recipes.py").write_text(content_base + "\n# extra tampered line", encoding="utf-8")

            with tempfile.TemporaryDirectory() as base_tmp:
                base_dir = Path(base_tmp) / "base"
                base_dir.mkdir()
                (base_dir / "recipes.py").write_text(content_base, encoding="utf-8")

                assert verify_patch_reconstructs_candidate(base_dir, patch, tampered_cand) is False


def test_corrupted_zero_byte_patch_blocks_publication():
    """Verify that a corrupted 0-byte patch (as in historical run_3866fc90b91e) is strictly blocked."""
    eligible, disq = check_publication_eligibility(
        base_reproduced=True,
        candidate_reproducer_passed=True,
        candidate_suite_passed=True,
        patch_text="",  # Empty 0-byte patch
        changed_files=["more_itertools/recipes.py"],
        original_reproducer_hash="abc",
        current_reproducer_hash="abc",
        base_sha="d538e0bdd8e3dec291b708b40ffdb775ae937664",
    )
    assert eligible is False
    assert any("No real implementation changes exist in candidate patch" in d for d in disq)
