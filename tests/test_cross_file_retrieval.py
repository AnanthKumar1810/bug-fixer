"""Test cross-file localization, callee resolution, and delivery freshness."""

import hashlib
from pathlib import Path
import pytest

from bug_fixer.docker_runner import validate_workspace
from bug_fixer.models import IssueInfo
from bug_fixer.retrieval import collect_evidence_excerpts
from bug_fixer.workspace import Workspace


def test_cross_file_callee_reaches_prompt_with_zero_tool_reads(cross_file_fixture: Path):
    """File C (service/formatter.py) must reach prompt excerpts with 0 explicit tool reads."""
    ws = Workspace(cross_file_fixture)
    # 1. Run validation to get failure traceback
    val = validate_workspace(cross_file_fixture, selected_tests=["tests/test_greeting.py::test_greeting"], runner="local")
    assert not val.passed
    assert val.traceback_frames

    issue = IssueInfo(
        url="https://github.com/acme/project/issues/1",
        owner="acme",
        repo="project",
        number=1,
        title="Greeting bug in main.py",
        body="Calling main.run() fails",
    )

    # 2. Collect excerpts using dynamic retrieval
    excerpts = collect_evidence_excerpts(ws, val, issue, attempt=1)

    # 3. Verify service/formatter.py is in excerpts
    paths = [e.path.replace("\\", "/") for e in excerpts]
    assert "service/formatter.py" in paths

    # 4. Verify ZERO explicit model tool reads were needed
    assert len(ws.explicit_reads) == 0
    assert len(ws.sent_to_model) > 0


def test_freshness_after_edit_updates_content_hash(cross_file_fixture: Path):
    """After editing file C, the next packet contains the fresh content and updated hash."""
    ws = Workspace(cross_file_fixture)
    val = validate_workspace(cross_file_fixture, selected_tests=["tests/test_greeting.py::test_greeting"], runner="local")
    issue = IssueInfo(
        url="https://github.com/acme/project/issues/1",
        owner="acme",
        repo="project",
        number=1,
        title="Greeting issue",
        body="run() returns wrong prefix",
    )

    # Initial excerpt
    excs_1 = collect_evidence_excerpts(ws, val, issue, attempt=1)
    fmt_exc_1 = next(e for e in excs_1 if "formatter.py" in e.path)
    hash_1 = fmt_exc_1.content_hash

    # Edit formatter.py
    ws.replace_text("service/formatter.py", "Goodbye", "Farewell")

    # Second excerpt collection
    excs_2 = collect_evidence_excerpts(ws, val, issue, attempt=2)
    fmt_exc_2 = next(e for e in excs_2 if "formatter.py" in e.path)
    hash_2 = fmt_exc_2.content_hash

    assert hash_1 != hash_2
    assert "Farewell" in fmt_exc_2.content


def test_caller_only_workaround_is_rejected_by_second_caller_and_direct_callee(cross_file_fixture: Path):
    """A workaround in main.py still fails direct callee and second caller tests."""
    ws = Workspace(cross_file_fixture)
    # Simulate caller-only workaround
    ws.replace_text("main.py", "return format_greeting('World')", "return 'Hello, World!'")

    # Validate full suite
    full_val = validate_workspace(cross_file_fixture, runner="local")
    assert not full_val.passed
    failed_names = [tc.name for tc in full_val.test_cases if tc.outcome == "failed"]
    assert "test_formatter_direct" in failed_names
    assert "test_second_caller" in failed_names


def test_replace_text_crlf_and_trailing_whitespace_tolerance(tmp_path: Path):
    """replace_text gracefully handles CRLF vs LF and trailing whitespace differences."""
    sample_file = tmp_path / "sample.py"
    # Write file with Windows CRLF and a trailing space on line 2
    sample_file.write_bytes(b"def compute(x):\r\n    y = x + 1   \r\n    return y\r\n")

    ws = Workspace(tmp_path)

    # Replace using pure LF and without trailing spaces
    old_snippet = "def compute(x):\n    y = x + 1\n    return y"
    new_snippet = "def compute(x):\n    y = x + 2\n    return y"

    ws.replace_text("sample.py", old_snippet, new_snippet)
    updated = sample_file.read_text(encoding="utf-8")
    assert "y = x + 2" in updated


def test_build_compact_repair_packet_syntax_error_directive():
    """Syntax or Indentation errors trigger the syntax corrective directive, not logic abandonment."""
    from bug_fixer.models import ValidationSummary
    from bug_fixer.retrieval import build_compact_repair_packet

    issue = IssueInfo(
        url="https://github.com/acme/project/issues/10",
        owner="acme",
        repo="project",
        number=10,
        title="Syntax error fix",
        body="Fix it",
    )
    val = ValidationSummary(passed=False, exit_code=1, failing_assertions=["IndentationError: unexpected indent"])

    # 1. With syntax failure in earlier attempts
    earlier_syntax = [{
        "attempt": 1,
        "changed_files": ["foo.py"],
        "status": "failed",
        "failures": ["IndentationError: unexpected indent"],
        "patch_diff": "+    x = 1",
    }]
    packet_syntax = build_compact_repair_packet(issue, "tests/test_x.py::test_1", val, [], attempt=2, earlier_attempts=earlier_syntax)
    assert "SYNTAX or INDENTATION error" in packet_syntax
    assert "Do NOT abandon the repair concept" in packet_syntax

    # 2. With behavioral assertion failure in earlier attempts
    earlier_logic = [{
        "attempt": 1,
        "changed_files": ["foo.py"],
        "status": "failed",
        "failures": ["AssertionError: expected 5 got 4"],
        "patch_diff": "+    return 4",
    }]
    packet_logic = build_compact_repair_packet(issue, "tests/test_x.py::test_1", val, [], attempt=2, earlier_attempts=earlier_logic)
    assert "Do NOT repeat the rejected edits shown above" in packet_logic
    assert "SYNTAX or INDENTATION error" not in packet_logic

