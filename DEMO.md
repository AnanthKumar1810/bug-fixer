# Bug Fixer — Three-Minute Judge Walkthrough (PS-05)

This demo presents the complete, auditable issue-to-PR workflow for **Bug Fixer** (PS-05). All steps execute from the canonical submission folder `C:\Users\Siddhu\OneDrive\Desktop\bug fixer\` on Windows 11 with Docker (`bugfixer-validator:py313`) and Ollama (`qwen2.5-coder:7b`).

---

## 1. The User's Problem (30 seconds)
In Python, default arguments are evaluated once at function definition time. Using a mutable default like `def configure(settings={}): pass` causes state to leak across function calls. 

In `AnanthKumar1810/ml-code-review-assistant`, anti-pattern detector `AP002` originally only caught empty lists (`items=[]`), completely missing empty dictionary literals. A developer filed an issue reporting this false negative.

---

## 2. The Agent's Decision (30 seconds)
Inspect the first repair attempt (`run_21c66b738258`):
```powershell
python -m bug_fixer.cli show --run run_21c66b738258
```
- **Observed Failure**: `tests/test_project4.py::test_detect_mutable_default_dict` failed with `AssertionError: AP002 must detect mutable dictionary default argument`.
- **Dynamic Traceback Extraction**: Bug Fixer analyzed the failure frame, traced imports to callee `src/patterns.py`, and extracted lines 1–50 (`SHA256: f595132f`) into prompt context.
- **Model Action**: Qwen edited `src/patterns.py` (line 32), changing `regex=r"def\s+\w+\([^)]*=\s*\[\]"` to `regex=r"def\s+\w+\([^)]*=\s*\[\]|\{\}"`.

---

## 3. The First Patch's Mistake (45 seconds)
While the first patch satisfied the initial positive reproducer, it introduced a severe **false positive**:
Because disjunction (`|`) has lower precedence than concatenation, `def\s+\w+\([^)]*=\s*\[\]|\{\}` matches empty dictionaries **anywhere on any line**!

Valid Python code was falsely flagged with critical `AP002` warnings:
- Plain variable assignment: `settings = {}`
- Function return value: `def initialize(): return {}`
- Code comment: `# Example: settings = {}`

Run the evaluation matrix comparing behavior across inputs:
```powershell
python .bug-fixer-data/test_evaluation_matrix.py
```
Output:
```
Input                                    | Expected | Base (5f13700)  | Showcase (1st Patch) | Corrected Patch
-------------------------------------------------------------------------------------------------------------------
def configure(items=[]): pass            | FLAG     | FLAG            | FLAG                 | FLAG [PASS]    
def configure(settings={}): pass         | FLAG     | NO FLAG [FAIL]  | FLAG                 | FLAG [PASS]    
def configure(name, settings={}): pass   | FLAG     | NO FLAG [FAIL]  | FLAG                 | FLAG [PASS]    
settings = {}                            | NO FLAG  | NO FLAG         | FLAG [FAIL]          | NO FLAG [PASS] 
def initialize(): return {}              | NO FLAG  | NO FLAG         | FLAG [FAIL]          | NO FLAG [PASS] 
def configure(settings=None): pass       | NO FLAG  | NO FLAG         | NO FLAG              | NO FLAG [PASS] 
# Example: settings = {}                 | NO FLAG  | NO FLAG         | FLAG [FAIL]          | NO FLAG [PASS] 
```

---

## 4. The Verified Repair (45 seconds)
Bug Fixer was run against the comprehensive precision-and-recall reproducer (`run_909759fa3e22`):
```powershell
python -m bug_fixer.cli show --run run_909759fa3e22
```
- **The Corrected Diff**: Properly groups the alternatives inside parentheses:
  ```diff
  -        regex=r"def\s+\w+\([^)]*=\s*\[\]|\{\}",
  +        regex=r"def\s+\w+\([^)]*=\s*(\[\]|\{\})",
  ```
- **Independent Docker Replay**:
  ```powershell
  python -m bug_fixer.cli verify --run run_909759fa3e22 --source .bug-fixer-data/demo_repo/ml-code-review-assistant --runner docker
  ```
  Verified from fresh base checkout in Docker: reproducer passes, full suite passes (12/12), tree ID matches `197206927219...`.
- **Generalization Patch Challenge**:
  ```powershell
  python -m bug_fixer.cli challenge --run run_909759fa3e22 --source .bug-fixer-data/demo_repo/ml-code-review-assistant --runner docker
  ```
  Candidate patch passed all checks and survived distinguishing multi-parameter variations (`Intact: YES`).

---

## 5. Human Review & Safe Publication Handoff (30 seconds)

### A. Local Verified Publication Rehearsal
```powershell
python -m bug_fixer.cli publish --run run_909759fa3e22 --source .bug-fixer-data/demo_repo/ml-code-review-assistant --remote .bug-fixer-data/demo_remote.git --reviewer lead-reviewer
```
- Publication commit: `e9b807f8` pushed to bare repository `demo_remote.git`.
- Remote Head Tree ID `19720692721949b247b7f11b21adea6aebb61055` matches verified replay tree byte-for-byte.
- Generated draft PR text in `.bug-fixer-data/artifacts/run_909759fa3e22/pr-body.md` truthfully labeled: `simulated_local` (no fabricated live GitHub approval rules).

### B. Live GitHub Publication Gate (Operator Approval Required)
To publish this verified patch to live GitHub:
1. Operator sets GitHub token: `$env:GITHUB_TOKEN = "ghp_YourToken"`
2. Operator runs publish with explicit confirmation:
   ```powershell
   python -m bug_fixer.cli publish --run run_909759fa3e22 --source .bug-fixer-data/demo_repo/ml-code-review-assistant --remote https://github.com/AnanthKumar1810/ml-code-review-assistant.git --reviewer "AnanthKumar1810" --confirm-remote
   ```

---

## 1. Issue and Original Defect (Fixture Run) (30 seconds)

Inspect the recorded run `run_5525c66d9a46` for issue [#103](https://github.com/fast-org/greeting-service/issues/103):

```bash
bug-fixer show --run run_5525c66d9a46
```

**Key Evidence Identified:**
- **Issue**: `https://github.com/fast-org/greeting-service/issues/103` (provenance: `fixture`)
- **Pinned Base Commit**: `8331bb1d806ab683f55c2846b3c543717c09128c`
- **Locked Reproducer**: `tests/test_greeting.py::test_greeting` (SHA256: `48b466f964f5...`)
- **Original Failure**: `AssertionError: assert 'Goodbye, World!' == 'Hello, World!'`
- **Manifest Integrity**: `[PASS] Intact` (all artifact hashes verified)

---

## 2. The Caller Workaround Trap (20 seconds)

A superficial model repair often edits caller `main.py` directly to return the literal `'Hello, World!'`:

```diff
--- a/main.py
+++ b/main.py
@@ -1,4 +1,4 @@
 from service.formatter import format_greeting

 def run():
-    return format_greeting('World')
+    return 'Hello, World!'
```

While this naive workaround passes the original reproducer (`tests/test_greeting.py::test_greeting`), it leaves the real bug in `service/formatter.py` untouched, breaking other callers and input variations.

---

## 3. Patch Challenge: Independent Generalization Checks (40 seconds)

Run the patch challenge across the pinned base, the historical workaround (negative control), and the verified candidate patch:

```bash
bug-fixer challenge --run run_5525c66d9a46 --runner docker
```

**Observed Challenge Results:**
```
================================ PATCH CHALLENGE RESULTS ================================
Run ID:            run_5525c66d9a46
Challenge Status:  PASSED (Candidate passed all checks and survived 3 distinguishing challenge test(s).)
Candidate Tree:    1178b68be4d99c4b3441ccb4d52e4199ff8dffbd (Intact: YES)
Docker Overhead:   9.62s

+--------------------------------------------------+----------+-----------------+-----------+------------------+
| Check / Test ID                                  | Base     | Workaround (neg)| Candidate | Discriminates?   |
+--------------------------------------------------+----------+-----------------+-----------+------------------+
| tests/test_greeting.py::test_greeting            | FAILED   | PASS (fooled)   | PASSED    | Reproducer       |
| tests/test_greeting.py::test_formatter_direct    | FAILED   | FAIL (exposed)  | PASSED    | YES (direct_callee) |
| tests/test_greeting.py::test_second_caller       | FAILED   | FAIL (exposed)  | PASSED    | YES (second_caller) |
| ...variations.py::test_greeting_input_variations | FAILED   | FAIL (exposed)  | PASSED    | YES (input_variation) |
+--------------------------------------------------+----------+-----------------+-----------+------------------+

Distinguishing Checks:
  - tests/test_greeting.py::test_formatter_direct
  - tests/test_greeting.py::test_second_caller
  - tests/test_challenge_variations.py::test_greeting_input_variations

Strongest Survived Challenge:
  tests/test_greeting.py::test_formatter_direct & tests/test_greeting.py::test_second_caller & tests/test_challenge_variations.py::test_greeting_input_variations
```

**Key Findings:**
1. **Tree Immutability**: Candidate git tree (`1178b68be4d99c4b3441ccb4d52e4199ff8dffbd`) is identical before and after challenge (`tree_intact=True`).
2. **Discriminating Generalization**: 3 independent checks exposed the workaround while confirming candidate correctness across multiple callers and input values.
3. **Honest Overhead**: Docker execution overhead measured and recorded (`9.62s`).

---

## 4. Candidate Patch & Evidence-Backed Retrieval (15 seconds)

Inspect the candidate patch generated by local Qwen 2.5 Coder 7B:

```diff
--- a/service/formatter.py
+++ b/service/formatter.py
@@ -1,3 +1,3 @@
 def format_greeting(name):
-    # Defect: wrong greeting prefix
-    return f'Goodbye, {name}!'
+    # Fixed: return Hello greeting
+    return f'Hello, {name}!'
```

Dynamic traceback retrieval delivered `service/formatter.py` into Qwen's prompt context with **zero tool reads**, enabling a 1-shot repair on attempt 1 of 2.

---

## 5. Clean Replay, Truthful Draft PR & Destination Binding (15 seconds)

### A. Independent Docker Replay
```bash
bug-fixer verify --run run_5525c66d9a46 --runner docker
```
- Base defect reproduced on clean clone of `8331bb1d...`: `PASS`
- Candidate patch applied cleanly: `PASS`
- Maintained suite and locked reproducer passed: `PASS`
- Candidate tree identity verified: `1178b68be4d99c4b3441ccb4d52e4199ff8dffbd`

### B. Safe Local Publication
```bash
bug-fixer publish --run run_5525c66d9a46 --remote .bug-fixer-data/test_demo_remote.git
```

### C. Truthful Human-Review Claims
Inspect Section 5 of `.bug-fixer-data/artifacts/run_5525c66d9a46/pr-body.md` or `local_draft_pr.json`:
- **Truthful Claim**: *"Simulated local publication. No GitHub reviewer was requested. Branch protection is not applicable."*
- **Reviewer Status**: `not_requested`
- **Branch Protection**: `not_applicable` (no fabricated live GitHub approval rules)

### D. Publication Destination Safety Guard
Attempting to push fixture run `run_5525c66d9a46` (`fast-org/greeting-service`) to an upstream remote stops immediately before any network write:
```bash
bug-fixer publish --run run_5525c66d9a46 --remote https://github.com/psf/requests.git
# Output: Error: Publication destination mismatch: Run 'run_5525c66d9a46' was recorded for repository 'fast-org/greeting-service', but publication target push destination is 'psf/requests' (https://github.com/psf/requests.git). Cross-repository remote write refused.
```

---

## 6. Demonstration: Failed Challenge Blocks Publication (10 seconds)

Inspect run `run_demo_failed_chal`, where the candidate patch passed the initial reproducer but failed the generalization patch challenge:

```bash
bug-fixer show --run run_demo_failed_chal
```
**Output:**
```
Eligibility Gate:  [INELIGIBLE]
Disqualifications:
  - Patch challenge failed for candidate patch. Failed check(s): tests/test_greeting.py::test_formatter_direct. Evidence at: ...\artifacts\run_demo_failed_chal\challenge.json

Patch Challenge Info:
  Status:             FAILED (Candidate patch failed direct callee check)
  Tree Immutability:  PASSED
```

Attempting publication immediately halts without any Git push or PR creation:
```bash
bug-fixer publish --run run_demo_failed_chal --remote .bug-fixer-data/test_demo_remote.git
```
**Output:**
```
Error: Run 'run_demo_failed_chal' is NOT eligible for publication.
  - Patch challenge failed for candidate patch. Failed check(s): tests/test_greeting.py::test_formatter_direct. Evidence at: ...\artifacts\run_demo_failed_chal\challenge.json
```

---

## 7. Demonstration: Misleading `origin` Push URL Blocked (10 seconds)

When a local repository's `origin` remote has an innocent fetch URL (`fast-org/greeting-service.git`) but its effective **push URL** is pointed at a divergent repository (`psf/requests.git`):

```bash
bug-fixer publish --run run_5525c66d9a46 --remote origin --source .bug-fixer-data/demo_misleading_origin
```
**Output:**
```
Error: Publication destination mismatch: Run 'run_5525c66d9a46' was recorded for repository 'fast-org/greeting-service', but publication target push destination is 'psf/requests' (https://github.com/psf/requests.git). Cross-repository remote write refused to prevent publishing patches to incorrect upstream.
```
Bug Fixer inspects effective push URLs with `git remote get-url --push --all origin` inside the repository context, detects the mismatch, and aborts before writing anything.

---

## 8. Summary: Safe Local Demo & Path to Real GitHub PR

### Safe Local Demo (Zero Network Writes)
```bash
bug-fixer publish --run run_5525c66d9a46 --remote .bug-fixer-data/test_demo_remote.git
```
- Pushes commit `5b5e7129` to the local bare repository.
- Generates `local_draft_pr.json` truthfully labeled: *"Simulated local publication. No GitHub reviewer was requested. Branch protection is not applicable."*

### One Remaining Operator Step for a Real GitHub PR
To publish a live draft PR to an actual remote repository on GitHub:
1. Export a valid GitHub token:
   ```bash
   export GITHUB_TOKEN="ghp_YourGitHubToken"   # or set in environment
   ```
2. Run `publish` with the explicit confirmation flag:
   ```bash
   bug-fixer publish --run run_5525c66d9a46 --remote https://github.com/fast-org/greeting-service.git --reviewer "lead-architect" --confirm-remote
   ```
   The `--confirm-remote` flag ensures operator consent before any live network write.
