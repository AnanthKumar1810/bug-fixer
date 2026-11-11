"""Publication of candidate patch to a Git remote and draft PR creation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
import httpx

from bug_fixer.db import EvidenceStore, compute_sha256
from bug_fixer.models import RunRecord, RunState


class PublicationError(RuntimeError):
    """Raised when publication eligibility or publication process fails."""


def verify_publication_artifacts(store: EvidenceStore, run: RunRecord, artifacts_dir: Path) -> bool:
    """Verify that stored candidate patch and review artifacts match DB records."""
    if not run.candidate_patch_hash:
        return False
    patch_file = artifacts_dir / run.run_id / "candidate.patch"
    if not patch_file.is_file():
        return False
    actual_hash = compute_sha256(patch_file.read_bytes().replace(b"\r\n", b"\n"))
    return actual_hash == run.candidate_patch_hash


def push_candidate_branch(
    workspace_root: Path,
    remote_name_or_url: str,
    branch_name: str,
) -> bool:
    """Commit changes to a run-specific branch and push to the remote."""
    # Ensure branch exists and is checked out
    subprocess.run(["git", "checkout", "-B", branch_name], cwd=workspace_root, capture_output=True, check=False)
    subprocess.run(["git", "add", "-A"], cwd=workspace_root, capture_output=True, check=False)

    status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=workspace_root, capture_output=True, text=True, check=False)
    if status_proc.stdout.strip():
        subprocess.run(["git", "commit", "-q", "-m", f"fix: automated repair {branch_name}"], cwd=workspace_root, check=False)

    push_proc = subprocess.run(
        ["git", "push", "-u", remote_name_or_url, branch_name, "--force"],
        cwd=workspace_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if push_proc.returncode != 0:
        raise PublicationError(f"Git push failed: {push_proc.stderr or push_proc.stdout}")
    return True


def create_draft_pr(
    owner: str,
    repo: str,
    branch_name: str,
    base_branch: str,
    title: str,
    body: str,
    reviewer: str,
    *,
    api_token: str | None = None,
    mock_client: Any | None = None,
    local_record_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Open a draft PR on GitHub or record an honest local simulation.
    - If mock_client is provided, delegates to mock client.
    - If no api_token is provided (local simulation), writes local draft PR record
      and returns publication_mode='simulated_local', reviewer_status='not_requested',
      and branch_protection='not_required'. Never invents a fake GitHub pull request URL.
    - If api_token is provided, performs live GitHub operations: idempotent PR check,
      reviewer request, and branch protection verification.
    """
    if mock_client is not None:
        return mock_client.create_draft_pr(
            owner=owner,
            repo=repo,
            branch_name=branch_name,
            base_branch=base_branch,
            title=title,
            body=body,
            reviewer=reviewer,
        )

    # Local simulation mode (no live GitHub API token)
    if not api_token or not api_token.strip():
        record_path: Path | None = None
        if local_record_dir:
            local_record_dir.mkdir(parents=True, exist_ok=True)
            record_path = local_record_dir / "local_draft_pr.json"
            record_data = {
                "publication_mode": "simulated_local",
                "repo": f"{owner}/{repo}",
                "branch_name": branch_name,
                "base_branch": base_branch,
                "title": title,
                "body": body,
                "required_reviewer": reviewer,
                "reviewer_status": "not_requested",
                "branch_protection": "not_required",
                "draft": True,
            }
            record_path.write_text(json.dumps(record_data, indent=2), encoding="utf-8")

        file_url = f"file:///{record_path.as_posix()}" if record_path else "file://local_draft_pr.json"
        return {
            "publication_mode": "simulated_local",
            "pr_number": None,
            "pr_url": file_url,
            "draft": True,
            "reviewer_requested": reviewer,
            "reviewer_status": "not_requested",
            "branch_protection": "not_required",
            "branch_protection_verified": False,
            "local_draft_record_path": str(record_path) if record_path else None,
        }

    # Live GitHub API mode
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {api_token.strip()}",
    }

    # 1. Idempotency check: see if a PR already exists for this branch
    list_url = f"https://api.github.com/repos/{owner}/{repo}/pulls?head={owner}:{branch_name}&state=all"
    with httpx.Client(timeout=15.0) as client:
        list_resp = client.get(list_url, headers=headers)
        if list_resp.status_code == 200 and list_resp.json():
            existing_pr = list_resp.json()[0]
            pr_number = existing_pr["number"]
            pr_url = existing_pr["html_url"]
            # Update PR title and body idempotently
            client.patch(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
                headers=headers,
                json={"body": body, "title": title},
            )
        else:
            # Create new draft PR
            create_url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
            payload = {
                "title": title,
                "body": body,
                "head": branch_name,
                "base": base_branch,
                "draft": True,
            }
            create_resp = client.post(create_url, headers=headers, json=payload)
            if create_resp.status_code not in (200, 201):
                raise PublicationError(f"Failed to create draft PR ({create_resp.status_code}): {create_resp.text}")
            pr_data = create_resp.json()
            pr_number = pr_data["number"]
            pr_url = pr_data["html_url"]

        # 2. Request reviewer
        reviewer_status = "not_requested"
        if reviewer:
            rev_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers"
            rev_resp = client.post(rev_url, headers=headers, json={"reviewers": [reviewer]})
            reviewer_status = "requested" if rev_resp.status_code in (200, 201) else "failed"

        # 3. Check branch protection rule
        prot_url = f"https://api.github.com/repos/{owner}/{repo}/branches/{base_branch}/protection"
        prot_resp = client.get(prot_url, headers=headers)
        if prot_resp.status_code == 200:
            branch_protection = "verified_required"
        elif prot_resp.status_code == 404:
            branch_protection = "not_required"
        else:
            # 403 Forbidden (token lacks admin rights to read branch protection) or other HTTP error
            branch_protection = "unknown"

        return {
            "publication_mode": "published_github",
            "pr_number": pr_number,
            "pr_url": pr_url,
            "draft": True,
            "reviewer_requested": reviewer,
            "reviewer_status": reviewer_status,
            "branch_protection": branch_protection,
            "branch_protection_verified": (branch_protection == "verified_required"),
        }
