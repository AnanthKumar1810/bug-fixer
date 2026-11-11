"""Deterministic execution and validation inside isolated Docker containers."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from bug_fixer.models import TestCaseResult, ValidationCheck, ValidationSummary

MAX_OUTPUT_CHARS = 12_000

# Regex patterns for frame and assertion extraction
_PYTEST_FRAME = re.compile(
    r"(?m)^\s*([A-Za-z0-9_./\\-]+\.py):(\d+):(?:\s*(?:in\s+([A-Za-z0-9_<>]+)|([A-Za-z0-9_]+(?:Error|Exception|Failure))|AssertionError))?"
)
_PYTHON_FRAME = re.compile(
    r'(?m)^\s*File\s+["\']([^"\']+\.py)["\'],\s+line\s+(\d+)(?:,\s+in\s+([A-Za-z0-9_<>]+))?'
)
_FAILED_SUMMARY_LINE = re.compile(
    r"(?m)^\s*FAILED\s+([A-Za-z0-9_./\\-]+\.py)::([A-Za-z0-9_]+)"
)
_ASSERTION_LINE = re.compile(r"(?m)^\s*(E\s+[A-Za-z0-9_]+Error:.*|>\s+assert.*)")
_GENERIC_ERROR_LINE = re.compile(r"(?m)^\s*([A-Za-z0-9_]+(?:Error|Exception|Failure):.*)")


def parse_junit_xml(xml_path: Path) -> list[TestCaseResult]:
    """Parse pytest JUnit XML into machine-readable TestCaseResult objects."""
    if not xml_path or not xml_path.is_file():
        return []
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except (ET.ParseError, OSError, ValueError):
        return []

    results: list[TestCaseResult] = []
    for tc in root.iter("testcase"):
        file_attr = tc.attrib.get("file", "").strip()
        name_attr = tc.attrib.get("name", "").strip()
        classname_attr = tc.attrib.get("classname", "").strip()
        time_attr = tc.attrib.get("time", "0.0")
        try:
            duration = float(time_attr)
        except (ValueError, TypeError):
            duration = 0.0

        if file_attr:
            norm_file = file_attr.replace("\\", "/")
            nodeid = f"{norm_file}::{name_attr}"
        elif classname_attr:
            norm_file = classname_attr.replace(".", "/") + ".py"
            nodeid = f"{norm_file}::{name_attr}"
        else:
            norm_file = ""
            nodeid = name_attr

        outcome: Literal["passed", "failed", "skipped", "error"] = "passed"
        message = ""
        failure = tc.find("failure")
        error = tc.find("error")
        skipped = tc.find("skipped")

        if failure is not None:
            outcome = "failed"
            message = failure.attrib.get("message") or (failure.text or "").strip()
        elif error is not None:
            outcome = "error"
            message = error.attrib.get("message") or (error.text or "").strip()
        elif skipped is not None:
            outcome = "skipped"
            message = skipped.attrib.get("message") or (skipped.text or "").strip()

        results.append(
            TestCaseResult(
                nodeid=nodeid,
                file=norm_file,
                name=name_attr,
                outcome=outcome,
                duration_seconds=round(duration, 3),
                message=message[:2000] if message else "",
            )
        )
    return results


def extract_traceback_frames(text: str) -> list[tuple[str, int, str]]:
    """Extract (file_path, line_number, function_name) from traceback text."""
    if not text:
        return []
    frames: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int, str]] = set()

    for match in _PYTEST_FRAME.finditer(text):
        file_path = match.group(1).replace("\\", "/")
        try:
            line_no = int(match.group(2))
        except ValueError:
            continue
        func_name = match.group(3) or match.group(4) or ""
        frame = (file_path, line_no, func_name)
        if frame not in seen:
            seen.add(frame)
            frames.append(frame)

    for match in _PYTHON_FRAME.finditer(text):
        file_path = match.group(1).replace("\\", "/")
        try:
            line_no = int(match.group(2))
        except ValueError:
            continue
        func_name = match.group(3) or ""
        frame = (file_path, line_no, func_name)
        if frame not in seen:
            seen.add(frame)
            frames.append(frame)

    for match in _FAILED_SUMMARY_LINE.finditer(text):
        file_path = match.group(1).replace("\\", "/")
        func_name = match.group(2) or ""
        frame = (file_path, 1, func_name)
        if frame not in seen:
            seen.add(frame)
            frames.append(frame)

    return frames


def extract_failing_assertions(text: str) -> list[str]:
    """Extract failing assertion lines and exceptions from pytest output."""
    if not text:
        return []
    assertions: list[str] = []
    seen: set[str] = set()

    for match in _ASSERTION_LINE.finditer(text):
        line = match.group(1).strip()
        if line and line not in seen:
            seen.add(line)
            assertions.append(line)

    for match in _GENERIC_ERROR_LINE.finditer(text):
        line = match.group(1).strip()
        if line and line not in seen:
            seen.add(line)
            assertions.append(line)

    return assertions[:10]


def _safe_host_env(workspace: Path) -> dict[str, str]:
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE")
    env = {k: os.environ[k] for k in allowed if k in os.environ}
    env.update({
        "PYTHONPATH": str(workspace),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def build_docker_cmd(
    root: Path,
    args: list[str],
    *,
    extra_mounts: list[str] | None = None,
    image: str = "bugfixer-validator:py313",
) -> tuple[list[str], str]:
    name = "bugfixer-run-" + uuid.uuid4().hex[:12]
    # Mount workspace readonly
    mount_args: list[str] = ["--mount", f"type=bind,source={root},target=/workspace,readonly"]
    if extra_mounts:
        for m in extra_mounts:
            mount_args.extend(["--mount", m])

    cmd = [
        "docker", "run", "--rm", "--pull=never", "--name", name,
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--pids-limit=256", "--memory=512m", "--cpus=1",
        "--user=65534:65534", "--tmpfs=/tmp:rw,nosuid,size=64m",
        *mount_args,
        "--workdir=/workspace",
        "--env=PYTHONPATH=/workspace:/workspace/src",
        "--env=PYTHONDONTWRITEBYTECODE=1",
        "--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        image,
        "python", *args,
    ]
    return cmd, name


def execute_validation_process(
    name: str,
    command: list[str],
    cwd: Path,
    timeout_seconds: int,
    *,
    container_name: str | None = None,
    is_docker: bool = True,
) -> ValidationCheck:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            env=_safe_host_env(cwd) if not is_docker else None,
            shell=False,
            timeout=timeout_seconds,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        duration = round(time.monotonic() - started, 3)
        output = (proc.stdout + "\n" + proc.stderr).strip()[-MAX_OUTPUT_CHARS:]
        return ValidationCheck(
            name=name,
            passed=(proc.returncode == 0),
            duration_seconds=duration,
            exit_code=proc.returncode,
            output=output,
        )
    except subprocess.TimeoutExpired as exc:
        duration = round(time.monotonic() - started, 3)
        if container_name:
            try:
                subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        detail = exc.stdout or ""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        return ValidationCheck(
            name=name,
            passed=False,
            duration_seconds=duration,
            exit_code=None,
            output=(str(detail) + f"\nTimed out after {timeout_seconds}s")[-MAX_OUTPUT_CHARS:],
        )
    except OSError as exc:
        duration = round(time.monotonic() - started, 3)
        return ValidationCheck(
            name=name,
            passed=False,
            duration_seconds=duration,
            exit_code=None,
            output=f"Could not execute process: {exc}",
        )


def validate_workspace(
    workspace: Path,
    *,
    timeout_seconds: int = 90,
    selected_tests: list[str] | None = None,
    runner: str = "docker",
    image: str = "bugfixer-validator:py313",
) -> ValidationSummary:
    """Validate a workspace. If runner='docker', NEVER fall back to host execution."""
    if runner not in {"local", "docker"}:
        raise ValueError("runner must be 'local' or 'docker'")

    root = workspace.resolve()
    started = time.monotonic()

    # 1. Syntax check
    syntax_cmd = [
        "-c",
        "import ast,pathlib; [ast.parse(p.read_text(encoding='utf-8'), filename=str(p)) for p in pathlib.Path('.').rglob('*.py') if '.git' not in p.parts]"
    ]
    if runner == "docker":
        command, container = build_docker_cmd(root, syntax_cmd, image=image)
        checks = [execute_validation_process("python-syntax", command, root, timeout_seconds, container_name=container, is_docker=True)]
    else:
        checks = [execute_validation_process("python-syntax", [sys.executable, *syntax_cmd], root, timeout_seconds, is_docker=False)]

    # If Docker fails to execute at all, do NOT fall back to host! Mark as environment_error.
    if runner == "docker":
        c0 = checks[0]
        docker_failed = (
            c0.exit_code is None
            or c0.exit_code == 125
            or "Error response from daemon" in c0.output
            or "failed to connect to the docker API" in c0.output
            or "docker: " in c0.output
            or "Could not execute" in c0.output
        )
        if docker_failed:
            return ValidationSummary(
                passed=False,
                checks=checks,
                test_cases=[],
                issues=[f"Docker daemon or validator image unavailable: {c0.output}"],
                status="environment_error",
                selected_tests=[],
                duration_seconds=round(time.monotonic() - started, 3),
                reproducer_outcome="environment_error",
            )

    # 2. Pytest execution
    test_files_exist = any(root.rglob("test_*.py")) or any(root.rglob("*_test.py"))
    issues: list[str] = []
    test_cases: list[TestCaseResult] = []
    reproducer_outcome = "unknown"

    if test_files_exist:
        targets = selected_tests or ["."]
        if runner == "docker":
            with tempfile.TemporaryDirectory() as temp_dir:
                reports_dir = Path(temp_dir)
                paths = ["/workspace/" + p if p != "." and not p.startswith("/") else p for p in targets]
                pytest_cmd = [
                    "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp=/tmp/pytest",
                    "--junitxml=/reports/junit.xml", "-o", "junit_family=xunit2", *paths
                ]
                # Notice: bind mount for /reports is default read-write (no invalid ,rw flag)
                command, container = build_docker_cmd(
                    root, pytest_cmd, extra_mounts=[f"type=bind,source={reports_dir},target=/reports"], image=image
                )
                test_check = execute_validation_process("pytest", command, root, timeout_seconds, container_name=container, is_docker=True)
                checks.append(test_check)
                test_cases.extend(parse_junit_xml(reports_dir / "junit.xml"))
        else:
            with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
                temp_junit_path = Path(tf.name)
            try:
                test_check = execute_validation_process(
                    "pytest",
                    [sys.executable, "-m", "pytest", "-q", f"--junitxml={temp_junit_path}", "-o", "junit_family=xunit2", *targets],
                    root, timeout_seconds, is_docker=False
                )
                checks.append(test_check)
                test_cases.extend(parse_junit_xml(temp_junit_path))
            finally:
                temp_junit_path.unlink(missing_ok=True)

        # Distinguish reproducer outcome
        last_check = checks[-1]
        all_output = "\n".join(c.output for c in checks)
        failing_assertions = extract_failing_assertions(all_output)
        tb_frames = extract_traceback_frames(all_output)

        if "Timed out" in last_check.output or last_check.exit_code is None:
            reproducer_outcome = "timeout"
            status = "timeout"
            issues.append(f"Validation timed out after {timeout_seconds}s")
        elif last_check.exit_code in (2, 4, 5) or "ModuleNotFoundError" in all_output or "ImportError" in all_output:
            reproducer_outcome = "missing_dependency"
            status = "collection_error"
            issues.append(f"Missing dependency or collection error (exit={last_check.exit_code}): {last_check.output[-800:]}")
        elif last_check.passed:
            reproducer_outcome = "passed"
            status = "verified" if all(c.passed for c in checks) else "failed"
        else:
            reproducer_outcome = "matching_failure"
            status = "failed"
            issues.append(f"Test failure: {failing_assertions[:2]}")
    else:
        issues.append("No test files found; pytest was skipped")
        status = "syntax_only" if checks[0].passed else "failed"
        failing_assertions = []
        tb_frames = []

    passed = test_files_exist and all(c.passed for c in checks)
    return ValidationSummary(
        passed=passed,
        checks=checks,
        test_cases=test_cases,
        issues=issues,
        status=status,
        selected_tests=selected_tests or (["."] if test_files_exist else []),
        duration_seconds=round(time.monotonic() - started, 3),
        exit_code=checks[-1].exit_code if checks else None,
        reproducer_outcome=reproducer_outcome,
        failing_assertions=failing_assertions,
        traceback_frames=tb_frames,
    )
