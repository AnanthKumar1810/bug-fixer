"""Ollama LLM adapter for Qwen 2.5 Coder, supporting 4-field diagnosis and tool calls."""

from __future__ import annotations

import json
import re
from typing import Any
import httpx

from bug_fixer.models import Diagnosis
from bug_fixer.workspace import Workspace, WorkspaceError

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "replace_text",
            "description": "Replace an exact unique string in a source file with new text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "description": "Relative path to file"},
                    "old": {"type": "string", "description": "Exact text to replace (must occur exactly once)"},
                    "new": {"type": "string", "description": "Replacement text"},
                },
                "required": ["relative_path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_line",
            "description": "Replace a single numbered line in a source file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "description": "Relative path to file"},
                    "line": {"type": "integer", "description": "1-based line number"},
                    "new": {"type": "string", "description": "New line content"},
                },
                "required": ["relative_path", "line", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write entire content to a new or existing implementation file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "description": "Relative path to file"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["relative_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read entire contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "description": "Relative path to file"},
                },
                "required": ["relative_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_lines",
            "description": "Read a bounded range of lines (max 200 lines).",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "description": "Relative path to file"},
                    "start_line": {"type": "integer", "description": "1-based start line"},
                    "end_line": {"type": "integer", "description": "1-based end line"},
                },
                "required": ["relative_path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for a substring across workspace files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text query to search for"},
                    "relative_path": {"type": "string", "description": "Subdirectory to limit search (default '.')"},
                },
                "required": ["query"],
            },
        },
    },
]


def parse_diagnosis(text: str) -> Diagnosis:
    """Extract the 4 required diagnosis fields from Qwen's response."""
    cause_m = re.search(r"(?:SUSPECTED_CAUSE|1\.\s*Suspected Cause):\s*(.*?)(?=(?:SUPPORTING_EVIDENCE_IDS|2\.\s*Supporting Evidence|NEXT_CHECK_OR_EDIT|3\.\s*Next Check|$))", text, re.IGNORECASE | re.DOTALL)
    evidence_m = re.search(r"(?:SUPPORTING_EVIDENCE_IDS|2\.\s*Supporting Evidence):\s*(.*?)(?=(?:NEXT_CHECK_OR_EDIT|3\.\s*Next Check|EXPECTED_OBSERVABLE_RESULT|4\.\s*Expected Observable|$))", text, re.IGNORECASE | re.DOTALL)
    next_m = re.search(r"(?:NEXT_CHECK_OR_EDIT|3\.\s*Next Check):\s*(.*?)(?=(?:EXPECTED_OBSERVABLE_RESULT|4\.\s*Expected Observable|$))", text, re.IGNORECASE | re.DOTALL)
    result_m = re.search(r"(?:EXPECTED_OBSERVABLE_RESULT|4\.\s*Expected Observable):\s*(.*?)(?=(?:```|Tool Call|$))", text, re.IGNORECASE | re.DOTALL)

    cause = cause_m.group(1).strip() if cause_m else "Defect detected in source implementation"
    raw_evidence = evidence_m.group(1).strip() if evidence_m else ""
    evidence_ids = [e.strip() for e in re.split(r"[,;\n]+", raw_evidence) if e.strip()]
    next_check = next_m.group(1).strip() if next_m else "Apply patch to fix defect"
    exp_result = result_m.group(1).strip() if result_m else "Reproducer test passes"

    return Diagnosis(
        suspected_cause=cause[:500],
        supporting_evidence_ids=evidence_ids[:10],
        next_check_or_edit=next_check[:500],
        expected_observable_result=exp_result[:500],
    )


def extract_balanced_json(text: str, start_idx: int = 0) -> tuple[dict[str, Any] | None, int]:
    """Extract a balanced JSON object starting at or after start_idx, correctly handling nested braces and strings."""
    idx = text.find("{", start_idx)
    while idx != -1:
        depth = 0
        in_string = False
        escape = False
        end_idx = -1
        for i in range(idx, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if not in_string:
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end_idx = i + 1
                        break
        if end_idx != -1:
            try:
                candidate = json.loads(text[idx:end_idx])
                if isinstance(candidate, dict):
                    return candidate, end_idx
            except Exception:
                pass
        idx = text.find("{", idx + 1)
    return None, -1


def extract_fallback_tool_calls(text: str) -> list[dict[str, Any]]:
    """Extract tool calls if model outputs them formatted in JSON markdown blocks or inline."""
    tool_calls: list[dict[str, Any]] = []

    # 1. Code blocks with json or tool_call
    patterns = [
        r"```(?:tool_call|json)?\s*(\{\s*\"(?:name|tool|function)\":.*?\})\s*```",
        r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    ]
    for pat in patterns:
        for match in re.finditer(pat, text, re.DOTALL):
            try:
                data = json.loads(match.group(1))
                name = data.get("name") or data.get("tool") or data.get("function")
                args = data.get("arguments") or data.get("args") or data.get("parameters") or {}
                if isinstance(args, str):
                    args = json.loads(args)
                if name:
                    tool_calls.append({"name": name, "arguments": args})
            except Exception:
                continue

    if tool_calls:
        return tool_calls

    # 2. Look for explicit JSON object with "name" and "arguments" anywhere
    full_json_match = re.search(r"\{\s*\"name\"\s*:\s*\"([a-z_]+)\"\s*,\s*\"arguments\"\s*:\s*(\{.*?\})\s*\}", text, re.DOTALL)
    if full_json_match:
        try:
            name = full_json_match.group(1)
            args = json.loads(full_json_match.group(2))
            return [{"name": name, "arguments": args}]
        except Exception:
            pass

    # 3. Look for tool name followed by balanced JSON arguments (supports braces in format strings)
    tool_name_pat = re.compile(r"[`']?(replace_text|replace_line|write_file|read_file|search_files)[`']?")
    for match in tool_name_pat.finditer(text):
        name = match.group(1)
        args, _ = extract_balanced_json(text, match.end())
        if args and any(k in args for k in ("relative_path", "query", "old", "content")):
            tool_calls.append({"name": name, "arguments": args})
            return tool_calls

    # 4. Look for tool_name {"relative_path": "...", ...} or `tool_name` with arguments: `{"relative_path": ...}`
    inline_pat = re.compile(
        r"[`']?(replace_text|replace_line|write_file|read_file|search_files)[`']?(?:[^\n\r{]*?)\s*[`']?(\{[^{}\n\r]*\"(?:relative_path|query)\"[^{}\n\r]*\})[`']?",
        re.DOTALL
    )
    for match in inline_pat.finditer(text):
        name = match.group(1)
        raw_args = match.group(2)
        try:
            args = json.loads(raw_args)
            tool_calls.append({"name": name, "arguments": args})
        except Exception:
            pass

    if tool_calls:
        return tool_calls

    # 5. CLI-style command invocations, e.g.:
    # `replace_line more_itertools/recipes.py 156 "return islice(...)"`
    cli_line_pat = re.compile(
        r"[`']?replace_line[`']?\s+([a-zA-Z0-9_\-\./\\]+)\s+(\d+)\s+[\"'](.*?)[\"']",
        re.DOTALL
    )
    for match in cli_line_pat.finditer(text):
        tool_calls.append({
            "name": "replace_line",
            "arguments": {
                "relative_path": match.group(1).strip(),
                "line": int(match.group(2)),
                "new": match.group(3),
            }
        })

    # CLI-style replace_text, e.g.:
    # `replace_text path "old" "new"`
    cli_text_pat = re.compile(
        r"[`']?replace_text[`']?\s+([a-zA-Z0-9_\-\./\\]+)\s+[\"'](.*?)[\"']\s+[\"'](.*?)[\"']",
        re.DOTALL
    )
    for match in cli_text_pat.finditer(text):
        tool_calls.append({
            "name": "replace_text",
            "arguments": {
                "relative_path": match.group(1).strip(),
                "old": match.group(2),
                "new": match.group(3),
            }
        })

    return tool_calls


def execute_tool_call(workspace: Workspace, name: str, args: dict[str, Any]) -> str:
    """Dispatch a tool call against the workspace."""
    try:
        if name == "replace_text":
            return workspace.replace_text(
                args["relative_path"],
                args["old"],
                args["new"],
            )
        elif name == "replace_line":
            return workspace.replace_line(
                args["relative_path"],
                int(args["line"]),
                args["new"],
            )
        elif name == "write_file":
            return workspace.write_text(
                args["relative_path"],
                args["content"],
            )
        elif name == "read_file":
            return workspace.read_text(args["relative_path"])
        elif name == "read_lines":
            return workspace.read_lines(
                args["relative_path"],
                int(args["start_line"]),
                int(args["end_line"]),
            )
        elif name == "search_files":
            return workspace.search_files(
                args["query"],
                args.get("relative_path", "."),
            )
        else:
            return f"Error: Unknown tool '{name}'"
    except (WorkspaceError, KeyError, ValueError) as exc:
        return f"Error executing {name}: {exc}"


def call_qwen_repair(
    prompt: str,
    workspace: Workspace,
    *,
    model: str = "qwen2.5-coder:7b",
    base_url: str = "http://127.0.0.1:11434",
    timeout_seconds: int = 180,
    mock_response: dict[str, Any] | None = None,
) -> tuple[Diagnosis, str, list[dict[str, Any]]]:
    """
    Query Qwen for diagnosis and workspace repair.
    Returns (Diagnosis, raw_response, executed_tool_calls).
    If no tool call is invoked, records 'no_change'.
    """
    executed_tools: list[dict[str, Any]] = []

    if mock_response is not None:
        raw_text = mock_response.get("content", "")
        diag = parse_diagnosis(raw_text)
        tools = mock_response.get("tool_calls", [])
        if not tools:
            tools = extract_fallback_tool_calls(raw_text)
        if not tools:
            # Model proposed reasoning without executing a tool -> no_change
            return diag, raw_text, [{"tool": "none", "status": "no_change"}]
        for t in tools:
            res = execute_tool_call(workspace, t["name"], t["arguments"])
            executed_tools.append({"tool": t["name"], "args": t["arguments"], "result": res})
        return diag, raw_text, executed_tools

    endpoint = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Bug Fixer, an autonomous software repair agent. "
                    "When given a bug description and failing test, diagnose the defect "
                    "and use the provided tools (replace_text, replace_line, write_file) "
                    "to repair the implementation. Do not edit test files. Always produce "
                    "the 4 diagnostic fields followed by the tool call."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "tools": TOOL_SCHEMAS,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 1024,
        },
    }

    try:
        with httpx.Client(timeout=float(timeout_seconds)) as client:
            resp = client.post(endpoint, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"Ollama API request failed: {exc}")

    msg = data.get("message", {})
    raw_text = msg.get("content", "")
    diag = parse_diagnosis(raw_text)

    # Check for native tool_calls
    native_tools = msg.get("tool_calls", [])
    if native_tools:
        for nt in native_tools:
            fn = nt.get("function", {})
            name = fn.get("name")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if name:
                res = execute_tool_call(workspace, name, args)
                executed_tools.append({"tool": name, "args": args, "result": res})
    else:
        # Check for fallback markdown or inline tool calls
        fb_tools = extract_fallback_tool_calls(raw_text)
        for t in fb_tools:
            res = execute_tool_call(workspace, t["name"], t["arguments"])
            executed_tools.append({"tool": t["name"], "args": t["arguments"], "result": res})

    if not executed_tools:
        # No tool called -> record no_change
        executed_tools.append({"tool": "none", "status": "no_change"})

    return diag, raw_text, executed_tools
