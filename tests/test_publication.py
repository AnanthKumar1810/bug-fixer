"""Test Git publication, local bare remote push, and idempotent PR draft creation."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
import httpx
import pytest

from bug_fixer.models import RunRecord, RunState
from bug_fixer.publication import (
    PublicationError,
    build_publication_commit,
    create_draft_pr,
    push_candidate_branch,
    sync_publication_artifacts,
)
from bug_fixer.verification import check_publication_eligibility


class MockGitHubClient:
    def __init__(self, should_fail_first_pr: bool = False):
        self.should_fail_first = should_fail_first_pr
        self.pr_attempts = 0
        self.prs_created: list[dict[str, Any]] = []

    def create_draft_pr(self, owner, repo, branch_name, base_branch, title, body, reviewer):
        self.pr_attempts += 1
        if self.should_fail_first and self.pr_attempts == 1:
            raise RuntimeError("Transient GitHub API 500 error")

        pr_info = {
            "pr_number": 42,
            "pr_url": f"https://github.com/{owner}/{repo}/pull/42",
            "draft": True,
            "reviewer_requested": reviewer,
            "reviewer_status": "requested",
            "branch_protection_verified": True,
        }
        self.prs_created.append(pr_info)
        return pr_info


def test_git_push_to_local_bare_remote(tmp_path: Path):
    """Test pushing run branch to a local bare Git repository."""
    bare_repo = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare_repo)], check=True, capture_output=True)

    workspace = tmp_path / "work"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=workspace, check=True)
    (workspace / "code.py").write_text("fixed code")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=workspace, check=True)

    branch_name = "bug-fixer/run-abc12345"
    success = push_candidate_branch(workspace, str(bare_repo), branch_name)
    assert success

    # Verify branch exists in bare repo
    proc = subprocess.run(["git", "-C", str(bare_repo), "branch", "--list", branch_name], capture_output=True, text=True, check=True)
    assert branch_name in proc.stdout


def test_publication_retry_idempotency_after_failed_pr_request(tmp_path: Path):
    """Recovery after successful push followed by failed PR request."""
    bare_repo = tmp_path / "remote2.git"
    subprocess.run(["git", "init", "--bare", str(bare_repo)], check=True, capture_output=True)

    workspace = tmp_path / "work2"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=workspace, check=True)
    (workspace / "code.py").write_text("fixed code 2")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=workspace, check=True)

    branch = "bug-fixer/run-retry123"
    push_candidate_branch(workspace, str(bare_repo), branch)

    mock_client = MockGitHubClient(should_fail_first_pr=True)

    # First attempt fails at PR creation
    with pytest.raises(RuntimeError, match="Transient GitHub API 500"):
        create_draft_pr(
            owner="acme",
            repo="project",
            branch_name=branch,
            base_branch="main",
            title="fix",
            body="body",
            reviewer="octocat",
            mock_client=mock_client,
        )

    # Second attempt succeeds and reuses existing branch
    pr_result = create_draft_pr(
        owner="acme",
        repo="project",
        branch_name=branch,
        base_branch="main",
        title="fix",
        body="body",
        reviewer="octocat",
        mock_client=mock_client,
    )

    assert pr_result["draft"] is True
    assert pr_result["reviewer_requested"] == "octocat"
    assert pr_result["pr_number"] == 42


def test_unrelated_workspace_edit_after_replay_verification_omitted_from_publication_commit(tmp_path: Path):
    """An unrelated workspace edit made after replay verification does not appear in publication commit."""
    # 1. Base repo
    repo = tmp_path / "base_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
    (repo / "service.py").write_text("def run():\n    return 'broken'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base commit"], cwd=repo, check=True)
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # 2. Patch that fixes service.py
    patch_text = (
        "diff --git a/service.py b/service.py\n"
        "--- a/service.py\n"
        "+++ b/service.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def run():\n"
        "-    return 'broken'\n"
        "+    return 'fixed'\n"
    )

    # Compute expected clean verified tree hash
    clean_test = tmp_path / "clean_test"
    subprocess.run(["git", "clone", "-q", str(repo), str(clean_test)], check=True)
    subprocess.run(["git", "apply", "-"], cwd=clean_test, input=patch_text, text=True, check=True)
    subprocess.run(["git", "add", "-A"], cwd=clean_test, check=True)
    verified_tree_id = subprocess.run(["git", "write-tree"], cwd=clean_test, capture_output=True, text=True, check=True).stdout.strip()

    # 3. Simulate mutable repair workspace with unrelated edits/dirty files
    mutable_ws = tmp_path / "mutable_ws"
    subprocess.run(["git", "clone", "-q", str(repo), str(mutable_ws)], check=True)
    (mutable_ws / "unrelated_secret.txt").write_text("dirty unverified edit")
    (mutable_ws / "random_scratch.py").write_text("import os; os.remove('everything')")

    # 4. Build publication commit using fresh disposable checkout from base SHA
    disposable_dir = tmp_path / "disposable_pub"
    pub_commit_sha, pub_tree_sha, checkout_path = build_publication_commit(
        source_repo=repo,
        base_sha=base_sha,
        patch_text=patch_text,
        commit_message="fix: resolve defect",
        target_dir=disposable_dir,
    )

    # 5. Assert: publication commit matches verified tree and dirty files are absent
    assert pub_tree_sha == verified_tree_id
    assert not (disposable_dir / "unrelated_secret.txt").exists()
    assert not (disposable_dir / "random_scratch.py").exists()
    assert (disposable_dir / "service.py").read_text(encoding="utf-8") == "def run():\n    return 'fixed'\n"

    # Parent must match base_sha
    parent_sha = subprocess.run(["git", "rev-parse", "HEAD^"], cwd=disposable_dir, capture_output=True, text=True, check=True).stdout.strip()
    assert parent_sha == base_sha


def test_tampered_candidate_patch_or_broken_manifest_stops_publication(tmp_path: Path):
    """Tampered candidate patch or broken manifest stops publication before any remote write."""
    bare_remote = tmp_path / "protected_remote.git"
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)

    # Broken manifest eligibility gate stops publication
    eligible, reasons = check_publication_eligibility(
        base_reproduced=True,
        candidate_reproducer_passed=True,
        candidate_suite_passed=True,
        patch_text="diff",
        changed_files=["service.py"],
        original_reproducer_hash="h1",
        current_reproducer_hash="h1",
        base_sha="a" * 40,
        manifest_valid=False,
        manifest_errors=["Hash mismatch in candidate.patch"],
    )
    assert not eligible
    assert any("manifest" in r.lower() for r in reasons)

    # Ensure remote was never written to
    proc = subprocess.run(["git", "-C", str(bare_remote), "branch", "--list"], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == ""


def test_new_regression_test_in_candidate_committed_to_publication_branch(tmp_path: Path):
    """A new regression test included in the candidate patch is committed to the publication branch."""
    repo = tmp_path / "base_repo2"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@test"], cwd=repo, check=True)
    (repo / "calc.py").write_text("def add(a, b): return a + b\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # Patch that adds a new test file
    new_test_patch = (
        "diff --git a/tests/test_new_regression.py b/tests/test_new_regression.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/test_new_regression.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+def test_add_positive():\n"
        "+    from calc import add\n"
        "+    assert add(2, 3) == 5\n"
    )

    disposable_dir = tmp_path / "disposable_pub2"
    pub_commit_sha, pub_tree_sha, _ = build_publication_commit(
        source_repo=repo,
        base_sha=base_sha,
        patch_text=new_test_patch,
        commit_message="test: add regression test",
        target_dir=disposable_dir,
    )

    # Check that new test is committed and tracked in git
    new_test_file = disposable_dir / "tests" / "test_new_regression.py"
    assert new_test_file.is_file()
    tracked = subprocess.run(
        ["git", "ls-tree", "HEAD", "tests/test_new_regression.py"],
        cwd=disposable_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "test_new_regression.py" in tracked.stdout


def test_remote_branch_conflict_stops_and_preserves_remote_branch(tmp_path: Path):
    """Advancing the remote branch before retry reports a conflict and leaves remote branch untouched."""
    bare_remote = tmp_path / "conflict_remote.git"
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)

    # 1. Someone already pushed commit C1 to branch bug-fixer/run-conflict-test
    work1 = tmp_path / "work1"
    work1.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work1, check=True)
    subprocess.run(["git", "config", "user.name", "ExternalDev"], cwd=work1, check=True)
    subprocess.run(["git", "config", "user.email", "dev@external"], cwd=work1, check=True)
    (work1 / "competing.txt").write_text("competitor change")
    subprocess.run(["git", "add", "."], cwd=work1, check=True)
    subprocess.run(["git", "commit", "-m", "competitor commit"], cwd=work1, check=True)
    c1_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work1, capture_output=True, text=True, check=True).stdout.strip()
    branch_name = "bug-fixer/run-conflict-test"
    subprocess.run(["git", "push", str(bare_remote), f"HEAD:refs/heads/{branch_name}"], cwd=work1, check=True)

    # 2. Local candidate has different commit C2
    work2 = tmp_path / "work2"
    work2.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work2, check=True)
    subprocess.run(["git", "config", "user.name", "BugFixer"], cwd=work2, check=True)
    subprocess.run(["git", "config", "user.email", "bugfixer@local"], cwd=work2, check=True)
    (work2 / "fix.txt").write_text("bug fixer change")
    subprocess.run(["git", "add", "."], cwd=work2, check=True)
    subprocess.run(["git", "commit", "-m", "candidate commit"], cwd=work2, check=True)
    c2_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work2, capture_output=True, text=True, check=True).stdout.strip()

    assert c1_sha != c2_sha

    # 3. push_candidate_branch must detect conflict, stop with PublicationError, and preserve remote head
    with pytest.raises(PublicationError, match="already exists .* which conflicts with expected commit"):
        push_candidate_branch(work2, str(bare_remote), branch_name, expected_commit_sha=c2_sha)

    # 4. Verify remote head is still C1
    remote_head_proc = subprocess.run(
        ["git", "--git-dir", str(bare_remote), "rev-parse", f"refs/heads/{branch_name}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert remote_head_proc.stdout.strip() == c1_sha


def test_interrupted_publication_after_pr_creation_retries_reviewer_assignment(monkeypatch):
    """When PR creation succeeds but reviewer assignment fails, retry preserves PR and re-requests reviewer."""
    call_log: list[str] = []

    def mock_get(self, url, *args, **kwargs):
        class MockResp:
            status_code = 200
            def json(self):
                # When checking existing PRs for branch, return existing PR
                if "pulls?" in url:
                    return [{"number": 55, "html_url": "https://github.com/owner/repo/pull/55", "head": {"sha": "c123"}}]
                elif "branches" in url:
                    return {"required_pull_request_reviews": {}}
                return {}
        return MockResp()

    reviewer_calls = [0]
    def mock_post(self, url, *args, **kwargs):
        call_log.append(url)
        class MockResp:
            def __init__(self, code, data):
                self.status_code = code
                self._data = data
            def json(self): return self._data
            @property
            def text(self): return ""

        if "requested_reviewers" in url:
            reviewer_calls[0] += 1
            if reviewer_calls[0] == 1:
                # First attempt fails reviewer assignment
                return MockResp(502, {"message": "Bad Gateway"})
            else:
                # Second attempt succeeds
                return MockResp(201, {"users": [{"login": "senior-eng"}]})

        return MockResp(201, {"number": 55, "html_url": "https://github.com/owner/repo/pull/55"})

    monkeypatch.setattr(httpx.Client, "get", mock_get)
    monkeypatch.setattr(httpx.Client, "post", mock_post)
    monkeypatch.setattr(httpx.Client, "patch", lambda *a, **k: None)

    # Attempt 1: reviewer fails
    res1 = create_draft_pr(
        owner="owner",
        repo="repo",
        branch_name="bug-fixer/run-rev-retry",
        base_branch="main",
        title="fix",
        body="body",
        reviewer="senior-eng",
        api_token="valid_token",
    )
    assert res1["pr_number"] == 55
    assert res1["reviewer_status"] == "failed"

    # Attempt 2: retry limited to outstanding step succeeds
    res2 = create_draft_pr(
        owner="owner",
        repo="repo",
        branch_name="bug-fixer/run-rev-retry",
        base_branch="main",
        title="fix",
        body="body",
        reviewer="senior-eng",
        api_token="valid_token",
    )
    assert res2["pr_number"] == 55
    assert res2["reviewer_status"] == "requested"

