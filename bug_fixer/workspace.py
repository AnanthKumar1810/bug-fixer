"""Path-confined file operations and workspace audit tracking for Bug Fixer."""

from __future__ import annotations

import hashlib
from itertools import islice
from pathlib import Path, PureWindowsPath
from typing import Any


class WorkspaceError(ValueError):
    """Raised when an operation violates workspace policy or boundaries."""


PROTECTED_FILES = {
    "conftest.py",
    "pytest.ini",
    "tox.ini",
    "test_manifest.json",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "sitecustomize.py",
    "usercustomize.py",
}


class Workspace:
    def __init__(self, root: Path, *, max_file_bytes: int = 500_000) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_file_bytes = max_file_bytes

        # Separate audit tracks
        self.tool_calls: list[dict[str, Any]] = []
        self.explicit_reads: list[dict[str, Any]] = []
        self.sent_to_model: list[dict[str, Any]] = []
        self.files_searched: list[str] = []
        self._changed_files: set[str] = set()
        self._seen_reads: set[str] = set()

    @property
    def changed_files(self) -> list[str]:
        return sorted(self._changed_files)

    def resolve(self, relative_path: str) -> Path:
        if not relative_path or not relative_path.strip():
            raise WorkspaceError("Path cannot be empty")
        posix_path = Path(relative_path)
        windows_path = PureWindowsPath(relative_path)
        if posix_path.is_absolute() or posix_path.drive or windows_path.is_absolute() or windows_path.drive:
            raise WorkspaceError("Absolute paths are not allowed")
        normalized = relative_path.replace("\\", "/")
        if ".." in normalized.split("/"):
            raise WorkspaceError("Parent-directory traversal is not allowed")
        target = (self.root / normalized).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError("Path resolves outside the run workspace") from exc
        return target

    def _sensitive(self, target: Path) -> bool:
        parts = target.relative_to(self.root).parts
        name = target.name.lower()
        return (
            any(part.lower() in {".aws", ".ssh"} for part in parts)
            or name.startswith(".env")
            or name.endswith((".pem", ".key"))
            or name in {".npmrc", ".pypirc", "credentials.json"}
            or name.startswith(("id_rsa", "id_ed25519"))
        )

    def _record_tool_call(self, tool: str, **kwargs: Any) -> None:
        record: dict[str, Any] = {"tool": tool}
        for k, v in kwargs.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                record[k] = v if not isinstance(v, str) or len(v) <= 300 else v[:300] + "..."
            elif isinstance(v, (list, dict)):
                record[k] = str(v)[:300]
        self.tool_calls.append(record)

    def record_prompt_delivery(self, path: str, start_line: int, end_line: int, reason: str, attempt: int, content_hash: str) -> None:
        self.sent_to_model.append({
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "reason": reason,
            "attempt": attempt,
            "hash": content_hash,
        })

    def read_text(self, relative_path: str, *, record_tool: bool = True) -> str:
        try:
            target = self.resolve(relative_path)
            if self._sensitive(target):
                raise WorkspaceError("Credential or sensitive path cannot be read")
            if not target.is_file():
                raise WorkspaceError(f"File does not exist: {relative_path}")
            if target.stat().st_size > self.max_file_bytes:
                raise WorkspaceError("File exceeds the configured size limit")
            content = target.read_text(encoding="utf-8")
            if record_tool:
                self._record_tool_call("read_file", path=relative_path, status="ok", chars=len(content))
                key = f"{relative_path}:all"
                if key not in self._seen_reads:
                    self._seen_reads.add(key)
                    self.explicit_reads.append({"path": relative_path, "type": "read_text", "lines": "all", "chars": len(content)})
            return content
        except Exception as exc:
            if record_tool:
                self._record_tool_call("read_file", path=relative_path, status="error", error=str(exc))
            raise

    def read_lines(self, relative_path: str, start_line: int, end_line: int, *, record_tool: bool = True) -> str:
        try:
            if not isinstance(start_line, int) or not isinstance(end_line, int) or start_line < 1 or end_line < start_line or end_line - start_line >= 200:
                raise WorkspaceError("Line range must be 1-based and at most 200 lines")
            lines = self.read_text(relative_path, record_tool=False).splitlines(keepends=True)
            if start_line > len(lines):
                raise WorkspaceError("Start line exceeds the file length")
            chunk = "".join(lines[start_line - 1:end_line])
            if record_tool:
                self._record_tool_call("read_lines", path=relative_path, start_line=start_line, end_line=end_line, status="ok", chars=len(chunk))
                key = f"{relative_path}:{start_line}-{end_line}"
                if key not in self._seen_reads:
                    self._seen_reads.add(key)
                    self.explicit_reads.append({"path": relative_path, "type": "read_lines", "lines": f"{start_line}-{end_line}", "chars": len(chunk)})
            return chunk
        except Exception as exc:
            if record_tool:
                self._record_tool_call("read_lines", path=relative_path, start_line=start_line, end_line=end_line, status="error", error=str(exc))
            raise

    def search_files(self, query: str, relative_path: str = ".") -> str:
        try:
            if not query or len(query) > 200:
                raise WorkspaceError("Search query must be 1 to 200 characters")
            base = self.root if relative_path == "." else self.resolve(relative_path)
            paths = [base] if base.is_file() else islice(base.rglob("*.py"), 1000)
            results = []
            for path in paths:
                if not path.is_file() or self._sensitive(path):
                    continue
                rel_posix = path.relative_to(self.root).as_posix()
                if rel_posix not in self.files_searched:
                    self.files_searched.append(rel_posix)
                try:
                    for number, line in enumerate(self.read_text(rel_posix, record_tool=False).splitlines(), 1):
                        if query.lower() in line.lower():
                            results.append(f"{rel_posix}:{number}: {line[:200]}")
                            if len(results) == 50:
                                res_text = "\n".join(results) + "\n[search truncated at 50 matches]"
                                self._record_tool_call("search_files", query=query, path=relative_path, status="ok", matches=50)
                                return res_text
                except (OSError, UnicodeError, WorkspaceError):
                    continue
            if not results and query.endswith("()") and query[:-2].isidentifier():
                return self.search_files(query[:-2], relative_path)
            res_text = "\n".join(results) or "(no matches; try a shorter identifier or a path-limited search)"
            self._record_tool_call("search_files", query=query, path=relative_path, status="ok", matches=len(results))
            return res_text
        except Exception as exc:
            self._record_tool_call("search_files", query=query, path=relative_path, status="error", error=str(exc))
            raise

    def replace_text(self, relative_path: str, old: str, new: str) -> str:
        try:
            if not old or len(old.encode("utf-8")) > self.max_file_bytes:
                raise WorkspaceError("Replacement source must be nonempty and within the file size limit")
            content = self.read_text(relative_path, record_tool=False)

            # 1. Exact match
            if content.count(old) == 1:
                res = self.write_text(relative_path, content.replace(old, new, 1))
                self._record_tool_call("replace_text", path=relative_path, status="ok")
                return res

            # 2. Line ending normalized match (CRLF vs LF)
            norm_content = content.replace("\r\n", "\n")
            norm_old = old.replace("\r\n", "\n")
            norm_new = new.replace("\r\n", "\n")
            if norm_content.count(norm_old) == 1:
                updated = norm_content.replace(norm_old, norm_new, 1)
                res = self.write_text(relative_path, updated)
                self._record_tool_call("replace_text", path=relative_path, status="ok")
                return res

            # 3. Strip trailing whitespace per line match
            c_lines = norm_content.splitlines(keepends=True)
            o_lines = [l.rstrip() for l in norm_old.splitlines()]
            matches = []
            for i in range(len(c_lines) - len(o_lines) + 1):
                window = [c_lines[i + k].rstrip() for k in range(len(o_lines))]
                if window == o_lines:
                    matches.append(i)

            if len(matches) == 1:
                idx = matches[0]
                leading_lines = c_lines[:idx]
                trailing_lines = c_lines[idx + len(o_lines):]
                replacement_block = norm_new if norm_new.endswith("\n") else norm_new + "\n"
                updated = "".join(leading_lines) + replacement_block + "".join(trailing_lines)
                res = self.write_text(relative_path, updated)
                self._record_tool_call("replace_text", path=relative_path, status="ok")
                return res

            occurrences = content.count(old)
            if occurrences == 0:
                raise WorkspaceError(f"Replacement source not found in {relative_path} (0 matches)")
            else:
                raise WorkspaceError(f"Replacement source is ambiguous in {relative_path} ({occurrences} matches; must occur exactly once)")
        except Exception as exc:
            self._record_tool_call("replace_text", path=relative_path, status="error", error=str(exc))
            raise

    def replace_line(self, relative_path: str, line: int, new: str) -> str:
        try:
            if not isinstance(line, int) or isinstance(line, bool) or not isinstance(new, str) or not new.strip() or "\n" in new or "\r" in new:
                raise WorkspaceError("Replacement requires a line number and one nonempty line")
            current = self.read_text(relative_path, record_tool=False).splitlines(keepends=True)
            if line < 1 or line > len(current):
                raise WorkspaceError("Replacement line is outside the file")
            old = current[line - 1]
            indentation = old[:len(old) - len(old.lstrip())]
            replacement = new if new[:1].isspace() else indentation + new
            current[line - 1] = replacement + ("\n" if old.endswith("\n") else "")
            res = self.write_text(relative_path, "".join(current))
            self._record_tool_call("replace_line", path=relative_path, line=line, status="ok")
            return res
        except Exception as exc:
            self._record_tool_call("replace_line", path=relative_path, line=line, status="error", error=str(exc))
            raise

    def write_text(self, relative_path: str, content: str) -> str:
        try:
            target = self.resolve(relative_path)
            parts = target.relative_to(self.root).parts
            name = target.name.lower()
            if (
                any(part.lower() in {"tests", ".git", ".github", ".venv"} for part in parts)
                or self._sensitive(target)
                or name in PROTECTED_FILES
                or (name.startswith("requirements") and name.endswith(".txt"))
                or (name.startswith("test_") and name.endswith(".py"))
                or name.endswith("_test.py")
            ):
                raise WorkspaceError(f"Protected test, configuration, or credential path cannot be edited: {relative_path}")
            payload = content.encode("utf-8")
            if len(payload) > self.max_file_bytes:
                raise WorkspaceError("File exceeds the configured size limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise WorkspaceError("Writing through a symbolic link is not allowed")
            # Preserve target file's existing newline convention if present
            nl = "\n"
            if target.is_file():
                try:
                    with open(target, "rb") as ef:
                        chunk = ef.read(8192)
                    if b"\r\n" in chunk:
                        nl = "\r\n"
                    elif b"\n" in chunk:
                        nl = "\n"
                except Exception:
                    pass
            normalized_content = content.replace("\r\n", "\n")
            target.write_text(normalized_content, encoding="utf-8", newline=nl)
            norm_rel = relative_path.replace("\\", "/")
            self._changed_files.add(norm_rel)
            msg = f"Wrote {norm_rel} ({len(payload)} bytes)"
            self._record_tool_call("write_file", path=norm_rel, status="ok", bytes=len(payload))
            return msg
        except Exception as exc:
            self._record_tool_call("write_file", path=relative_path, status="error", error=str(exc))
            raise

    def list_files(self, relative_path: str = ".") -> list[str]:
        base = self.root if relative_path == "." else self.resolve(relative_path)
        if not base.is_dir():
            raise WorkspaceError(f"Directory does not exist: {relative_path}")
        found: list[str] = []
        for item in base.rglob("*"):
            try:
                item.resolve().relative_to(self.root)
            except (ValueError, OSError):
                continue
            if item.is_file():
                found.append(item.relative_to(self.root).as_posix())
            if len(found) >= 1000:
                found.append("[listing truncated at 1000 files]")
                break
        return sorted(found)

    def snapshot_bytes(self) -> dict[str, bytes]:
        res: dict[str, bytes] = {}
        for p in self.root.rglob("*"):
            if (
                p.is_file()
                and not p.is_symlink()
                and ".git" not in p.parts
                and ".pytest_cache" not in p.parts
                and "__pycache__" not in p.parts
            ):
                res[p.relative_to(self.root).as_posix()] = p.read_bytes()
        return res

    def compute_workspace_hash(self) -> str:
        snapshot = self.snapshot_bytes()
        hasher = hashlib.sha256()
        for k in sorted(snapshot.keys()):
            hasher.update(k.encode("utf-8"))
            hasher.update(hashlib.sha256(snapshot[k]).digest())
        return hasher.hexdigest()
