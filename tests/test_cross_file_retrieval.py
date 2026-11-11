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
