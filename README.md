# Bug Fixer (PS-05 Automated Software Repair)

**Bug Fixer** is an auditable, automated software repair system that executes a strict GitHub issue-to-draft-PR workflow. Built with Python 3.13, pytest, Docker isolation (`bugfixer-validator:py313`), SQLite audit logging, and local Ollama (`qwen2.5-coder:7b`).

---

## Quickstart & Setup

```powershell
# 1. Install editable package
pip install -e .

# 2. Check dependencies and runtime readiness
python -m bug_fixer.cli doctor

# 3. Run complete verification test suite (57 tests)
python -m pytest -q
```

---

## 3-Minute Judge Walkthrough

### 1. Inspect the Corrected Real-Repository Repair (7-Part Judge Decision Trace)
```powershell
python -m bug_fixer.cli show --run run_909759fa3e22
```
Outputs the complete chain of custody for public repository `AnanthKumar1810/ml-code-review-assistant`:
1. **Issue**: URL, base commit `6ffa5b7e9b60`, and provenance.
2. **Failing Test**: Locked reproducer `test_ap002_precision_and_recall` and failure assertions on negative false positives.
3. **Source Excerpt**: Target callee `src/patterns.py` (lines 1-50) delivered to Qwen with SHA-256 fingerprint.
4. **Candidate Diff**: Targeted unified diff properly grouping alternatives `(\[\]|\{\})` in `src/patterns.py`.
5. **Clean Replay & Validation**: Docker suite pass (12/12), manifest integrity, and patch challenge survival.
6. **Review Status**: Truthful publication mode (`simulated_local`), publication commit `e9b807f8`, and `not_requested` reviewer status.
7. **Judge Decision Trace**: Complete 5-point decision narrative (Observed Failure -> Next Action -> Evidence Returned -> State Carried Forward -> Outcome).

### 2. Inspect the Initial Attempt with the False-Positive Flaw
```powershell
python -m bug_fixer.cli show --run run_21c66b738258
```
Shows the first attempt where Qwen generated un-grouped regex `\[\]|\{\}`, introducing false positives on valid dict assignments.

### 3. Independent Docker Replay
Replay clean-checkout validation in Docker from scratch on the corrected repair:
```powershell
python -m bug_fixer.cli verify --run run_909759fa3e22 --source .bug-fixer-data/demo_repo/ml-code-review-assistant --runner docker
```

### 4. Generalization Patch Challenge
Challenge candidate patch against multi-parameter input variations and negative controls:
```powershell
python -m bug_fixer.cli challenge --run run_909759fa3e22 --source .bug-fixer-data/demo_repo/ml-code-review-assistant --runner docker
```

### 5. Evaluate the 7-Case Precision & Recall Matrix
Compare the detector across Pinned Base, 1st Showcase Patch, and Corrected Patch:
```powershell
python .bug-fixer-data/test_evaluation_matrix.py
```

### 4. Cross-File Fixture & Caller Workaround Demonstration
Inspect how a superficial caller-only workaround is rejected while genuine callee repair passes:
```powershell
# Passes: Callee repair
python -m bug_fixer.cli show --run run_236a3f132689

# Blocked from publication: Caller workaround
python -m bug_fixer.cli show --run run_demo_failed_chal
```

### 5. Fresh Benchmark Pilot (Paired 6-Run Evaluation)
Inspect the 6-run paired evaluation across `boltons` and `more_itertools` (Traceback Retrieval: 2/3 vs Baseline: 0/3):
```powershell
python -m benchmarks.run_fresh_pilot
```

---

## CLI Command Reference

* `python -m bug_fixer.cli doctor`: Check Git, Docker, Ollama, and model availability.
* `python -m bug_fixer.cli fix --issue <URL> --source <DIR> --runner docker`: Run automated repair.
* `python -m bug_fixer.cli show --run <RUN_ID>`: Display structured 6-step audit report.
* `python -m bug_fixer.cli verify --run <RUN_ID> --runner docker`: Replay independent clean checkout verification.
* `python -m bug_fixer.cli challenge --run <RUN_ID> --runner docker`: Execute patch generalization challenge.
* `python -m bug_fixer.cli publish --run <RUN_ID> --remote <TARGET>`: Publish candidate branch and draft PR.

---

## Architecture

- **`bug_fixer/db.py`**: SQLite database (`bug_fixer.db`) recording run metadata, state transitions, and audit events.
- **`bug_fixer/docker_runner.py`**: Hardened Docker runner (`--network=none`, `--read-only`, `--cap-drop=ALL`, 512m limit).
- **`bug_fixer/retrieval.py`**: Dynamic traceback retrieval extracting callee implementation frames and test imports.
- **`bug_fixer/llm_adapter.py`**: Local Ollama adapter querying `qwen2.5-coder:7b`. Supports structured JSON and CLI-style tool calls.
- **`bug_fixer/verification.py`**: Generates unified diffs, verifies patch reconstruction, validates trees, and enforces challenge eligibility.
- **`bug_fixer/reporting.py`**: Generates `report.json` and human-auditable `pr-body.md`.
- **`bug_fixer/publication.py`**: Pushes candidate branch to Git remote, verifies push destination, and creates draft PR.
