# Bug Fixer

**Bug Fixer** is an auditable, automated software repair system that executes a strict GitHub issue-to-draft-PR workflow. Built with Python, pytest, Docker isolation, SQLite event logging, and local Ollama (`qwen2.5-coder:7b`).

---

## 5 Review Questions Answered by Every Run

Every report (`report.json` and `pr-body.md`) directly answers:
1. **What failed on the original commit?** (Base SHA, failing test node ID, assertion error, exit status)
2. **What evidence did Qwen receive?** (Traceback callee frames, excerpts, content hashes, diagnostic prompt)
3. **What did it actually edit?** (Changed files, candidate patch diff, executed tool calls vs prose claims)
4. **Which tests passed on that exact patch?** (Locked reproducer test and complete maintained suite)
5. **Who must review it before merge?** (Designated reviewer, draft PR status, branch protection check, no auto-merge)

---

## Workflow & State Machine

```
issue_loaded → base_pinned → reproduced → repairing → verified → ready_for_review
```

Terminal / recoverable states: `not_reproduced`, `environment_error`, `repair_failed`, `publication_pending`, `published`.

---

## CLI Commands

```powershell
# Check dependencies and blockers
bug-fixer doctor

# Execute an automated issue-to-patch repair
bug-fixer fix --issue https://github.com/OWNER/REPO/issues/123 --source . --runner docker

# Resume a checkpointed run from SQLite
bug-fixer resume --run RUN_ID

# Publish verified patch to remote and create draft PR
bug-fixer publish --run RUN_ID --reviewer GITHUB_USERNAME --remote origin
```

---

## Architecture

- **`bug_fixer/db.py`**: Single SQLite database (`bug_fixer.db`) for run metadata, state transitions, and audit events. Artifact store for diffs, JUnit results, prompt packets, and reports.
- **`bug_fixer/docker_runner.py`**: Hardened Docker runner (`--network=none`, `--read-only`, `--cap-drop=ALL`, `512m` RAM limit). Never falls back to host execution on container failure.
- **`bug_fixer/retrieval.py`**: 4-tier failure-driven retrieval prioritizing deepest callee implementation frames and test imports. Re-reads edited files fresh on every attempt.
- **`bug_fixer/llm_adapter.py`**: Local Ollama adapter querying `qwen2.5-coder:7b`. Enforces 4-field diagnosis and executes workspace tool calls. Records `no_change` if model outputs prose without tools.
- **`bug_fixer/verification.py`**: Generates unified candidate diff, verifies that applying the patch to a clean pinned base exactly reconstructs the tested workspace, and checks publication eligibility.
- **`bug_fixer/reporting.py`**: Generates `report.json` and human-readable `pr-body.md`.
- **`bug_fixer/publication.py`**: Pushes candidate branch to Git remote, opens draft PR, requests reviewer, and checks branch protection.

---

## Verification & Testing

Run all 22 automated acceptance tests:

```powershell
python -m pytest tests/ -v
```
