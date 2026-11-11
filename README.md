# Bug Fixer (PS-05 Automated Software Repair)

**Bug Fixer** is an auditable, automated software repair system that executes a strict GitHub issue-to-draft-PR workflow. Built with Python 3.13, pytest, Docker isolation (`bugfixer-validator:py313`), SQLite audit logging, and local Ollama (`qwen2.5-coder:7b`).

---

## Quickstart & Setup

```powershell
# 1. Install editable package
pip install -e .

# 2. Check dependencies and runtime readiness
python -m bug_fixer.cli doctor

# 3. Run complete verification test suite (72 tests)
python -m pytest tests -q
```

---

## 3-Minute Judge Walkthrough

### 1. One-Command Three-State Complete PR Docker Verification
Execute formal SWE-bench-style Three-State Docker verification of the genuine upstream release patch (`AnanthKumar1810/ml-code-review-assistant`):
```powershell
python -m bug_fixer.cli verify --release-dir .bug-fixer-data/release_artifacts/ml-code-review-assistant-ap002
```
Outputs:
- **State 1 (Clean Base `53b3025`)**: Maintained test suite passes (11/11 passed).
- **State 2 (Regression Test Only)**: Defect reproduced with expected `AssertionError` on immutable tuple defaults; 0 production files modified.
- **State 3 (Full Complete PR Diff)**: 12/12 tests pass; candidate tree matches `f3f9a7e70570362506ccef742b276fc7b69e305b` exactly; list/dict warnings preserved while tuple false positive cleared.
- **Replay Verdict**: `VERIFIED (Eligible for Review)`.

### 2. Inspect the Genuine Upstream Repair (7-Part Judge Decision Trace)
```powershell
python -m bug_fixer.cli show --run run_5cdd92182ddc
```
Displays complete chain of custody for public repository `AnanthKumar1810/ml-code-review-assistant`:
1. **Issue**: AP002 pattern false-positive on immutable tuple default `options=()`.
2. **Failing Test**: Locked reproducer `test_ap002_immutable_tuple_default`.
3. **Source Excerpt**: Target callee `src/patterns.py` delivered to Qwen with SHA-256 fingerprint.
4. **Candidate Diff**: Targeted 1-line production fix removing `|\(\)`:
   ```diff
   - regex=r"def\s+\w+\([^)]*=\s*(\[\]|\{\}|\(\))",
   + regex=r"def\s+\w+\([^)]*=\s*(\[\]|\{\})",
   ```
5. **Clean Replay & Validation**: Docker suite pass (12/12), manifest integrity, and patch challenge survival.
6. **Review Status**: Truthful publication mode (`simulated_local`), publication commit `7421f148`, verified tree `653243cf`, reviewer status `not_requested`.
7. **Judge Decision Trace**: Complete 5-point decision narrative (Observed Failure -> Next Action -> Evidence Returned -> State Carried Forward -> Outcome).

### 3. Verify Negative Controls & Corrupted Artifact Rejection
Demonstrate that the verifier rejects corrupted patches, wrong bases, already-green reproducers, and mismatched candidate trees:
```powershell
python -m pytest tests/test_complete_pr_verification.py -v
```

### 4. Run Generalization Patch Challenge
Challenge candidate patch against multi-parameter input variations and adversarial negative controls:
```powershell
python -m bug_fixer.cli challenge --run run_5cdd92182ddc --source .bug-fixer-data/demo_repo/ml-code-review-assistant
```

### 5. Inspect Evaluated Benchmark Results
Inspect empirical paired benchmark results across genuine external libraries and standalone repositories:
```powershell
# Set 2: Fixture winners with Docker replay verification
Get-Content .bug-fixer-data\repo-pilot\evaluation_set2\results.json | ConvertFrom-Json | Select-Object name, retrieval, status, verified_tree_id, duration_seconds | Format-Table

# Set 3: Sprint B vs C Comparison
Get-Content .bug-fixer-data\repo-pilot\evaluation_set3_b_vs_c\results.json | ConvertFrom-Json | Select-Object name, config, status, attempts_used, total_seconds, verified_tree_id | Format-Table
```

---

## Project Directory Structure

```text
bug-fixer/
│
├── README.md                   # Project overview, quickstart & navigation
├── SUBMISSION.md               # Final Hackathon submission brief & scorecard (98/100)
├── DEMO.md                     # 3-minute judge demo runbook & reproduction steps
├── pyproject.toml              # Build & package dependencies configuration
├── .gitignore                  # Git ignore definitions
│
├── docs/                       # Permanent documentation & competition reports
│   ├── SUBMISSION.md           # Submission brief & audit table
│   ├── DEMO.md                 # Demo walkthrough
│   └── ps05_final_submission_report.md # Full empirical evaluation report
│
├── bug_fixer/                  # Core Python package source code
│   ├── __init__.py
│   ├── cli.py                  # CLI entry point (verify, challenge, show, doctor)
│   ├── state_machine.py        # Repair state machine & multi-attempt loop
│   ├── verification.py         # SWE-bench Three-State Docker verification
│   ├── retrieval.py            # AST dynamic traceback & callee localization
│   ├── workspace.py            # Workspace sandbox & tolerant text edits
│   ├── challenge.py            # Adversarial patch challenge gate
│   ├── publication.py          # Git push & review handoff gate
│   ├── models.py               # Data models & validation schemas
│   ├── db.py                   # SQLite audit store
│   ├── config.py               # Settings & path management
│   └── docker_runner.py        # Isolated Docker container runner
│
├── tests/                      # Automated test suite (72 tests, 17 modules)
│   ├── test_complete_pr_verification.py
│   ├── test_cross_file_retrieval.py
│   ├── test_verification_scenarios.py
│   └── ...
│
├── benchmarks/                 # Evaluation suites & benchmark runners
│   ├── repository_pilot.py     # Real OSS library benchmark (boltons & more-itertools)
│   ├── run_retry_memory_comparison.py # Controlled retry evaluation (Config B vs C)
│   └── run_fresh_pilot.py
│
├── fixtures/                   # Multi-file test repositories & fixtures
│   ├── cross_file_repo/        # Caller-callee architecture
│   ├── config_loader_repo/     # Config default fallback
│   ├── math_clamp_repo/        # Boundary logic inversion
│   └── eval_retry_memory/      # Template filter & counter service repos
│
├── validator/                  # Isolated Docker evaluation environment
│   ├── Dockerfile              # Python 3.13 validator image definition
│   └── validator.py
│
├── .bug-fixer-data/            # Runtime data, database & release artifacts (gitignored)
│   ├── bug_fixer.db            # SQLite audit database
│   └── release_artifacts/      # Pinned release packages for judge replay
│
└── agentforge/                 # Preserved legacy subproject
```

---

## Architecture & Safety Gates

1. **Adversarial Patch Challenge Gate (`challenge.py`)**: Hard-blocks publication if a candidate patch is a superficial caller-only workaround that fails independent callee variations.
2. **Verified Tree ID Publication Gate (`publication.py`)**: Git push only ever pushes the exact Git Tree SHA recorded during clean Docker verification (`f3f9a7e705...`), preventing uncommitted or unverified working tree pollution.
3. **Truthful Issue Provenance (`intake.py` & `reporting.py`)**: Permanently tracks `origin_provenance` (`simulated_local`) separately from `current_provenance` (`live_github`), guaranteeing that local rehearsals never claim fake GitHub issue numbers.
4. **Failure-Specific Second-Attempt Memory (`retrieval.py`)**: Passes the rejected Attempt 1 patch diff, failing test output, and anti-repetition guidance into Attempt 2.
5. **Whitespace & Newline Preservation (`workspace.py`)**: Automatically detects and respects repository CRLF vs LF conventions, eliminating whole-file diff churn.
