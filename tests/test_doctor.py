"""Test the bug-fixer doctor command and dependency detection."""

import pytest
from bug_fixer.cli import run_doctor
from bug_fixer.config import Settings


def test_doctor_detects_system_dependencies(capsys):
    settings = Settings.from_env()
    code = run_doctor(settings)
    out, _ = capsys.readouterr()
    assert "BUG FIXER DOCTOR REPORT" in out
    assert "[PASS] Git:" in out
    assert "[PASS] Python:" in out
    assert "[PASS] pytest:" in out
    # Exit code is 0 when all critical dependencies (Git, Docker, Ollama, Qwen) pass
    assert code in (0, 1)


def test_doctor_reports_unreachable_ollama(capsys, monkeypatch):
    bad_settings = Settings(ollama_base_url="http://127.0.0.1:99999")
    code = run_doctor(bad_settings)
    out, _ = capsys.readouterr()
    assert "[FAIL] Ollama Service:" in out
    assert "OVERALL STATUS: BLOCKERS IDENTIFIED" in out
    assert code == 1
