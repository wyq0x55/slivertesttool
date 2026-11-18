#!/usr/bin/env python3
"""Scan tracked repository files (or explicit paths) for sensitive content and secrets."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]

_BINARY_SUFFIXES = frozenset(
    {
        ".7z",
        ".bin",
        ".bmp",
        ".br",
        ".db",
        ".dll",
        ".doc",
        ".docx",
        ".dylib",
        ".eot",
        ".exe",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".mp3",
        ".mp4",
        ".otf",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".pyc",
        ".pyd",
        ".pyo",
        ".pyz",
        ".rar",
        ".so",
        ".sqlite",
        ".sqlite3",
        ".tar",
        ".tgz",
        ".ttf",
        ".wasm",
        ".webm",
        ".webp",
        ".whl",
        ".woff",
        ".woff2",
        ".xls",
        ".xlsx",
        ".zip",
    }
)

_LOCKFILE_NAMES = frozenset(
    {
        "cargo.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
    }
)

_SKIP_WALK_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)

_REPORTS_DOC_RE = re.compile(r"^reports/.*\.(md|docx?|pdf|html)$", re.IGNORECASE)
_PROCUREMENT_MARKER = "DGX_Spark_" + "\u5185\u7f51\u90e8\u7f72"
_PROCUREMENT_RE = re.compile(re.escape(_PROCUREMENT_MARKER))

_CONTENT_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "private_key_header",
        "Private key header detected",
        re.compile("-----BEGIN " + "(?:RSA |EC |OPENSSH |DSA )?" + "PRIVATE KEY-----"),
    ),
    (
        "openai_secret_key",
        "Live OpenAI-style secret key pattern detected",
        re.compile(r"(?<![A-Za-z0-9])" + "sk-" + r"[A-Za-z0-9]{32,}"),
    ),
    (
        "github_classic_pat",
        "Live GitHub classic personal access token pattern detected",
        re.compile(r"(?<![A-Za-z0-9_])" + "ghp_" + r"[A-Za-z0-9]{36}(?![A-Za-z0-9])"),
    ),
    (
        "github_fine_grained_pat",
        "Live GitHub fine-grained personal access token pattern detected",
        re.compile(r"(?<![A-Za-z0-9_])" + "github_pat_" + r"[A-Za-z0-9_]{40,}"),
    ),
    (
        "aws_access_key_id",
        "Live AWS access key ID pattern detected",
        re.compile(r"(?<![A-Za-z0-9])" + "AKIA" + r"[0-9A-Z]{16}(?![0-9A-Z])"),
    ),
    (
        "internal_procurement_marker",
        "Sensitive internal procurement document marker detected",
        _PROCUREMENT_RE,
    ),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None
    rule: str
    message: str

    def format(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"{loc}: [{self.rule}] {self.message}"


def _normalize_rel_path(rel_path: str) -> str:
    norm = rel_path.replace("\\", "/")
    while norm.startswith("./") or norm.startswith("/"):
        norm = norm[2:] if norm.startswith("./") else norm[1:]
    return norm


def _should_skip_content(rel_path: str) -> bool:
    rel_lower = _normalize_rel_path(rel_path).lower()
    basename = rel_lower.rsplit("/", 1)[-1]

    if basename in _LOCKFILE_NAMES:
        return True
    if rel_lower.endswith((".umd.js", ".wasm", ".umd.js.gz", ".umd.js.br", ".wasm.gz", ".wasm.br")):
        return True
    if rel_lower.startswith("app/static/vendor/") or "/static/vendor/" in f"/{rel_lower}":
        return True
    if (
        rel_lower.startswith("tests/fixtures/")
        or "/tests/fixtures/" in f"/{rel_lower}"
        or rel_lower.startswith("frontend/tests/fixtures/")
    ):
        return True

    dot_idx = basename.rfind(".")
    if dot_idx != -1 and basename[dot_idx:] in _BINARY_SUFFIXES:
        return True
    return False


def _check_entry(full_path: Path, rel_path: str) -> list[Finding]:
    norm_rel = _normalize_rel_path(rel_path)
    findings: list[Finding] = []

    if _REPORTS_DOC_RE.match(norm_rel):
        findings.append(
            Finding(
                path=norm_rel,
                line=None,
                rule="tracked_report_artifact",
                message="Tracked report/document file under reports/ is prohibited",
            )
        )

    if _PROCUREMENT_RE.search(norm_rel):
        findings.append(
            Finding(
                path=norm_rel,
                line=None,
                rule="internal_procurement_marker",
                message="Sensitive internal procurement document marker in path",
            )
        )

    if _should_skip_content(norm_rel) or not full_path.is_file():
        return findings

    try:
        raw = full_path.read_bytes()
    except OSError as exc:
        findings.append(
            Finding(
                path=norm_rel,
                line=None,
                rule="read_error",
                message=f"Unable to read file: {exc}",
            )
        )
        return findings

    if b"\x00" in raw[:8192]:
        return findings

    text = raw.decode("utf-8", errors="replace")
    for line_no, line in enumerate(text.splitlines(), start=1):
        for rule_id, message, pattern in _CONTENT_RULES:
            if pattern.search(line):
                findings.append(
                    Finding(
                        path=norm_rel,
                        line=line_no,
                        rule=rule_id,
                        message=message,
                    )
                )
    return findings


def _git_tracked_files(root: Path) -> list[str]:
    proc = subprocess.run(
        ("git", "ls-files", "-z"),
        cwd=root,
        check=True,
        capture_output=True,
    )
    entries = proc.stdout.decode("utf-8", errors="surrogateescape").split("\x00")
    return [entry for entry in entries if entry]


def scan_repository(root: Path = ROOT) -> list[Finding]:
    resolved_root = root.resolve()
    findings: list[Finding] = []
    for rel_path in _git_tracked_files(resolved_root):
        findings.extend(_check_entry(resolved_root / rel_path, rel_path))
    return findings


def _derive_rel_path(file_path: Path, explicit_root: Path | None) -> str:
    resolved_file = file_path.resolve()
    if explicit_root is not None:
        try:
            return resolved_file.relative_to(explicit_root.resolve()).as_posix()
        except ValueError:
            pass

    try:
        return resolved_file.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        pass

    lowered_parts = [part.lower() for part in resolved_file.parts[:-1]]
    if "reports" in lowered_parts:
        idx = len(lowered_parts) - 1 - lowered_parts[::-1].index("reports")
        return "/".join(resolved_file.parts[idx:])
    return resolved_file.name


def _iter_explicit_targets(
    paths: Iterable[str | Path],
    explicit_root: Path | None,
) -> list[tuple[Path, str]]:
    targets: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    resolved_repo_root = ROOT.resolve()

    for raw_item in paths:
        candidate = Path(raw_item)
        resolved = candidate.resolve()
        if resolved.is_dir():
            dir_base: Path | None = explicit_root.resolve() if explicit_root is not None else None
            if dir_base is None:
                try:
                    resolved.relative_to(resolved_repo_root)
                    dir_base = resolved_repo_root
                except ValueError:
                    dir_base = resolved
            for walk_root, dirnames, filenames in os.walk(resolved):
                dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_WALK_DIRS)
                walk_path = Path(walk_root)
                for filename in sorted(filenames):
                    file_path = (walk_path / filename).resolve()
                    if file_path in seen:
                        continue
                    seen.add(file_path)
                    rel_path = _derive_rel_path(file_path, dir_base)
                    targets.append((file_path, rel_path))
        else:
            if resolved in seen:
                continue
            seen.add(resolved)
            rel_path = _derive_rel_path(resolved, explicit_root)
            targets.append((resolved, rel_path))
    return targets


def scan_paths(
    paths: Sequence[str | Path],
    *,
    root: Path | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    for full_path, rel_path in _iter_explicit_targets(paths, root):
        if not full_path.exists():
            findings.append(
                Finding(
                    path=_normalize_rel_path(rel_path),
                    line=None,
                    rule="missing_path",
                    message="Specified path does not exist",
                )
            )
            continue
        findings.extend(_check_entry(full_path, rel_path))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check repository files for prohibited reports, private keys, and live secrets.",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        help="explicit files or directories to scan instead of git ls-files",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="base directory for relative path evaluation",
    )
    args = parser.parse_args(argv)

    if args.paths:
        findings = scan_paths(args.paths, root=args.root)
    else:
        findings = scan_repository(args.root or ROOT)

    if findings:
        for item in findings:
            print(item.format(), file=sys.stderr)
        print(
            f"Sensitive content gate failed with {len(findings)} finding(s).",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
