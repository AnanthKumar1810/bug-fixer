"""Regression test for CLI-style tool call extraction in llm_adapter."""

from bug_fixer.llm_adapter import extract_fallback_tool_calls


def test_extract_cli_style_replace_line():
    """Verify that CLI-style replace_line from Qwen diagnosis prose is parsed into a tool call."""
    text = (
        "SUSPECTED_CAUSE: Defect in tail function\n"
        "NEXT_CHECK_OR_EDIT: `replace_line more_itertools/recipes.py 156 \"return islice(iterable, max(0, size - n), None)\"`\n"
        "EXPECTED_OBSERVABLE_RESULT: tests pass\n"
    )
    calls = extract_fallback_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "replace_line"
    assert calls[0]["arguments"]["relative_path"] == "more_itertools/recipes.py"
    assert calls[0]["arguments"]["line"] == 156
    assert calls[0]["arguments"]["new"] == "return islice(iterable, max(0, size - n), None)"


def test_extract_cli_style_replace_text():
    """Verify that CLI-style replace_text from Qwen diagnosis prose is parsed into a tool call."""
    text = (
        "I will fix the defect by replacing the old line:\n"
        "`replace_text service/formatter.py \"Goodbye\" \"Hello\"`\n"
    )
    calls = extract_fallback_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "replace_text"
    assert calls[0]["arguments"]["relative_path"] == "service/formatter.py"
    assert calls[0]["arguments"]["old"] == "Goodbye"
    assert calls[0]["arguments"]["new"] == "Hello"
