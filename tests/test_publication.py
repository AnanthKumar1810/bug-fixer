"""Test Git publication, local bare remote push, and idempotent PR draft creation."""

import subprocess
from pathlib import Path
from typing import Any
import pytest

from bug_fixer.publication import create_draft_pr, push_candidate_branch


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
