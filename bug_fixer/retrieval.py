"""Evidence retrieval, traceback analysis, callee resolution, and prompt packet assembly."""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path
from typing import Any

from bug_fixer.models import IssueInfo, PromptExcerpt, ValidationSummary
from bug_fixer.workspace import Workspace

MAX_EXCERPT_LINES = 50


def called_function_excerpts(workspace, test_path, line_no, attempt):
    """Locate imported functions exercised by the failing test, including package re-exports."""
    try:
        tree = ast.parse(workspace.read_text(test_path, record_tool=False))
    except (ValueError, SyntaxError, OSError):
        return []
    test = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                 and n.name.startswith("test") and n.lineno <= line_no <= n.end_lineno), None)
    if test is None:
        return []
    bindings = {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                module = node.module if isinstance(node, ast.ImportFrom) else alias.name
                if module:
                    bindings[alias.asname or alias.name] = (module, alias.name)
    targets = set()
    for call in (n.func for n in ast.walk(test) if isinstance(n, ast.Call)):
        if isinstance(call, ast.Name) and call.id in bindings:
            targets.add(bindings[call.id])
        elif isinstance(call, ast.Attribute) and isinstance(call.value, ast.Name) and call.value.id in bindings:
            targets.add((bindings[call.value.id][0], call.attr))
    matches = []
    for module, symbol in sorted(targets)[:5]:
        rel = module.replace(".", "/")
        direct = workspace.root / (rel + ".py")
        folder = workspace.root / rel
        paths = [direct] if direct.is_file() else []
        if folder.is_dir():
            from itertools import islice
            paths.extend(islice(folder.glob("*.py"), 40))
        for path in paths:
            name = path.relative_to(workspace.root).as_posix()
            try:
                content = workspace.read_text(name, record_tool=False)
                func = next((n for n in ast.parse(content).body
                             if isinstance(n, ast.FunctionDef) and n.name == symbol), None)
            except (OSError, SyntaxError, ValueError):
                continue
            if func:
                start = max(func.lineno, func.end_lineno - MAX_EXCERPT_LINES + 1)
                matches.append(PromptExcerpt(path=name, start_line=start, end_line=func.end_lineno,
                    reason=f"Function {symbol} called by failing test {test.name}",
                    content="".join(content.splitlines(True)[start-1:func.end_lineno]),
                    content_hash=hashlib.sha256(content.encode()).hexdigest(), attempt=attempt))
                break
    return matches


def is_test_file(path_str: str) -> bool:
    norm = path_str.replace("\\", "/").lower()
    parts = norm.split("/")
    name = parts[-1]
    return (
        any(p in {"tests", "test"} for p in parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def resolve_import_to_path(workspace: Workspace, source_file: str, module_name: str) -> str | None:
    """Resolve an imported module name to a relative file path in the workspace."""
    parts = module_name.split(".")
    # 1. As relative package
    cand1 = "/".join(parts) + ".py"
    if (workspace.root / cand1).is_file():
        return cand1
    # 2. In same dir as source_file
    source_dir = Path(source_file).parent.as_posix()
    if source_dir != ".":
        cand2 = f"{source_dir}/{cand1}"
        if (workspace.root / cand2).is_file():
            return cand2
    # 3. Search in src/
    cand3 = f"src/{cand1}"
    if (workspace.root / cand3).is_file():
        return cand3
    return None


def find_callee_files_from_ast(workspace: Workspace, file_path: str) -> list[str]:
    """Find local implementation files imported by a file."""
    callees: list[str] = []
    try:
        content = workspace.read_text(file_path, record_tool=False)
        tree = ast.parse(content, filename=file_path)
    except Exception:
        return callees

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = resolve_import_to_path(workspace, file_path, alias.name)
                if resolved and resolved not in callees and not is_test_file(resolved):
                    callees.append(resolved)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                resolved = resolve_import_to_path(workspace, file_path, node.module)
                if resolved and resolved not in callees and not is_test_file(resolved):
                    callees.append(resolved)
    return callees


def extract_terms_from_issue(issue: IssueInfo) -> list[str]:
    """Extract symbol names and file paths mentioned in the issue title and body."""
    terms: list[str] = []
    text = f"{issue.title}\n{issue.body}"
    # File paths
    file_matches = re.findall(r"([A-Za-z0-9_./\\-]+\.py)", text)
    for fm in file_matches:
        norm = fm.replace("\\", "/").strip("./")
        if norm not in terms:
            terms.append(norm)
    # Python identifiers
    id_matches = re.findall(r"`([A-Za-z0-9_]+)`", text)
    for im in id_matches:
        if im not in terms and len(im) > 2:
            terms.append(im)
    return terms


def collect_evidence_excerpts(
    workspace: Workspace,
    validation: ValidationSummary,
    issue: IssueInfo,
    attempt: int,
    earlier_attempts: list[dict[str, Any]] | None = None,
) -> list[PromptExcerpt]:
    """
    Retrieve candidate source excerpts using the four-tier priority:
    1. Actual implementation frames in the latest traceback (deepest callee first).
    2. Imports/definitions connected to failing assertion when traceback ends in a test.
    3. Explicit paths or symbols named in the issue.
    4. Earlier attempts from this run.
    """
    excerpts: list[PromptExcerpt] = []
    seen_paths: set[str] = set()

    # Priority 1: Traceback implementation frames
    impl_frames: list[tuple[str, int, str]] = []
    test_frames: list[tuple[str, int, str]] = []

    for file_path, line_no, func_name in validation.traceback_frames:
        norm_path = file_path.replace("\\", "/")
        # check if it exists in workspace
        target = workspace.root / norm_path
        if not target.is_file():
            # Try to resolve relative to workspace
            matched = list(workspace.root.rglob(Path(norm_path).name))
            if matched:
                norm_path = matched[0].relative_to(workspace.root).as_posix()
            else:
                continue

        if is_test_file(norm_path):
            test_frames.append((norm_path, line_no, func_name))
        else:
            impl_frames.append((norm_path, line_no, func_name))

    # Deepest implementation frame first (callee C)
    for norm_path, line_no, func_name in reversed(impl_frames):
        if norm_path not in seen_paths:
            seen_paths.add(norm_path)
            content = workspace.read_text(norm_path, record_tool=False)
            lines = content.splitlines(keepends=True)
            start_l = max(1, line_no - 15)
            end_l = min(len(lines), line_no + 15)
            chunk = "".join(lines[start_l - 1:end_l])
            chash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            excerpts.append(PromptExcerpt(
                path=norm_path,
                start_line=start_l,
                end_line=end_l,
                reason=f"Priority 1: Traceback implementation frame in {func_name}() at line {line_no}",
                content=chunk,
                content_hash=chash,
                attempt=attempt,
            ))

    # Priority 2: If no impl frames or only test frames, find callees imported by the test or caller
    if not impl_frames and test_frames:
        for t_path, t_line, t_func in test_frames:
            for excerpt in called_function_excerpts(workspace, t_path, t_line, attempt):
                if excerpt.path not in seen_paths:
                    excerpts.append(excerpt)
                    seen_paths.add(excerpt.path)
            callees = find_callee_files_from_ast(workspace, t_path)
            for c_path in callees:
                if c_path not in seen_paths:
                    seen_paths.add(c_path)
                    content = workspace.read_text(c_path, record_tool=False)
                    lines = content.splitlines(keepends=True)
                    start_l = 1
                    end_l = min(len(lines), MAX_EXCERPT_LINES)
                    chunk = "".join(lines[start_l - 1:end_l])
                    chash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    excerpts.append(PromptExcerpt(
                        path=c_path,
                        start_line=start_l,
                        end_line=end_l,
                        reason=f"Priority 2: Callee module imported by test {t_path}",
                        content=chunk,
                        content_hash=chash,
                        attempt=attempt,
                    ))

    # If an impl frame was found, also check if it imports deeper callees
    for norm_path, _, _ in impl_frames:
        deeper = find_callee_files_from_ast(workspace, norm_path)
        for d_path in deeper:
            if d_path not in seen_paths:
                seen_paths.add(d_path)
                content = workspace.read_text(d_path, record_tool=False)
                lines = content.splitlines(keepends=True)
                chunk = "".join(lines[:MAX_EXCERPT_LINES])
                chash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                excerpts.append(PromptExcerpt(
                    path=d_path,
                    start_line=1,
                    end_line=min(len(lines), MAX_EXCERPT_LINES),
                    reason=f"Priority 2: Callee imported by {norm_path}",
                    content=chunk,
                    content_hash=chash,
                    attempt=attempt,
                ))

    # Priority 3: Explicit paths or symbols named in issue
    issue_terms = extract_terms_from_issue(issue)
    for term in issue_terms:
        if term.endswith(".py") and (workspace.root / term).is_file():
            if term not in seen_paths:
                seen_paths.add(term)
                content = workspace.read_text(term, record_tool=False)
                lines = content.splitlines(keepends=True)
                chunk = "".join(lines[:MAX_EXCERPT_LINES])
                chash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                excerpts.append(PromptExcerpt(
                    path=term,
                    start_line=1,
                    end_line=min(len(lines), MAX_EXCERPT_LINES),
                    reason=f"Priority 3: Explicit path mentioned in issue ({term})",
                    content=chunk,
                    content_hash=chash,
                    attempt=attempt,
                ))
        elif len(term) >= 3 and term.isidentifier():
            # Search workspace for symbol definition
            matches = workspace.search_files(f"def {term}", ".")
            if "no matches" not in matches:
                for line in matches.splitlines()[:2]:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        p_file = parts[0]
                        if p_file not in seen_paths and (workspace.root / p_file).is_file():
                            seen_paths.add(p_file)
                            content = workspace.read_text(p_file, record_tool=False)
                            lines = content.splitlines(keepends=True)
                            chunk = "".join(lines[:MAX_EXCERPT_LINES])
                            chash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                            excerpts.append(PromptExcerpt(
                                path=p_file,
                                start_line=1,
                                end_line=min(len(lines), MAX_EXCERPT_LINES),
                                reason=f"Priority 3: Symbol definition for `{term}`",
                                content=chunk,
                                content_hash=chash,
                                attempt=attempt,
                            ))

    # Record all delivered excerpts in workspace prompt delivery tracking
    for exc in excerpts:
        workspace.record_prompt_delivery(
            path=exc.path,
            start_line=exc.start_line,
            end_line=exc.end_line,
            reason=exc.reason,
            attempt=attempt,
            content_hash=exc.content_hash,
        )

    return excerpts


def build_compact_repair_packet(
    issue: IssueInfo,
    reproducer_nodeid: str,
    validation: ValidationSummary,
    excerpts: list[PromptExcerpt],
    attempt: int,
    earlier_attempts: list[dict[str, Any]] | None = None,
) -> str:
    """Build compact, structured prompt packet for Qwen."""
    lines: list[str] = [
        "=== BUG FIX REPAIR TASK ===",
        f"Issue #{issue.number}: {issue.title}",
        f"Description: {issue.body.strip()}",
        "",
        f"=== REPRODUCER TEST (LOCKED) ===",
        f"Target Test: {reproducer_nodeid}",
        f"Reproducer Status: FAILED (Exit Code: {validation.exit_code})",
    ]

    if validation.failing_assertions:
        lines.append("Failing Assertions / Errors:")
        for fa in validation.failing_assertions[:4]:
            lines.append(f"  {fa}")

    if validation.checks:
        last_check = validation.checks[-1]
        lines.append("\nTest Output Summary:")
        lines.append(last_check.output[-1500:])

    if earlier_attempts:
        lines.append(f"\n=== PREVIOUS ATTEMPTS IN THIS RUN (ATTEMPT {attempt}) ===")
        for prev in earlier_attempts:
            lines.append(f"- Attempt {prev.get('attempt')}: Edited {prev.get('changed_files', [])}, Result: {prev.get('status')} (Contradicted expected outcome)")
            if prev.get("failures"):
                for fail in prev.get("failures")[:3]:
                    lines.append(f"  * Suite/regression failure: {fail}")

    lines.append("\n=== RELEVANT SOURCE EXCERPTS (FRESH) ===")
    if not excerpts:
        lines.append("(No source excerpts found via traceback or references; use read_file or search_files tool)")
    for exc in excerpts:
        lines.append(f"--- File: {exc.path} (Lines {exc.start_line}-{exc.end_line}) [SHA256: {exc.content_hash[:12]}] ---")
        lines.append(f"Reason: {exc.reason}")
        numbered_lines = []
        for i, line in enumerate(exc.content.splitlines(), exc.start_line):
            numbered_lines.append(f"{i:4d} | {line}")
        lines.append("\n".join(numbered_lines))
        lines.append("")

    lines.append("""=== REQUIRED DIAGNOSIS AND ACTION ===
You must provide a structured diagnosis with exactly these 4 fields:
1. SUSPECTED_CAUSE: [Root cause explanation based on the evidence]
2. SUPPORTING_EVIDENCE_IDS: [File paths and lines supporting this cause]
3. NEXT_CHECK_OR_EDIT: [Exact change or tool call to execute]
4. EXPECTED_OBSERVABLE_RESULT: [What the reproducer test will return once fixed]

Then, invoke the workspace tool to perform the edit (e.g. `replace_text`, `replace_line`, or `write_file`).
Note: Describing a fix without calling a tool will result in NO change to the files.
Do NOT edit any test files or configuration files. Only implementation source code may be edited.""")

    return "\n".join(lines)
