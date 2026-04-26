#!/usr/bin/env python3
"""
Scan the entire git history for committed secrets / credentials.

Even after a secret is deleted from the working tree, it remains in git
history forever — and once any commit hits a public remote, scrapers
(GitHub, automated bots, security researchers) will have indexed it.
**Anything ever committed must be considered leaked.**

This script:
1. Walks every blob in `git log -p --all` (or only files ever touched in
   `--cached` mode for speed).
2. Greps each blob for high-confidence secret patterns (AWS keys, GitHub
   tokens, JWT tokens, Stripe keys, OpenAI keys, generic high-entropy
   long secrets, .env-style assignments).
3. Reports any match with the commit SHA, file path, and line number.

Usage:
    # Full history scan
    python scripts/audit_git_secrets.py

    # Limit to last N commits (faster)
    python scripts/audit_git_secrets.py --since 100

    # Focus on a specific path
    python scripts/audit_git_secrets.py --path '.env*'

Exit codes:
    0 — no secrets found
    1 — at least one suspected secret found
    2 — script failed (e.g. not in a git repo)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# High-confidence patterns. Tuned for low false positives — generic
# `password=` or `secret=` matches are excluded because they appear in
# templates and example files.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS Access Key ID",      re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("AWS Secret Access Key",  re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?")),
    ("GitHub Token (classic)", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("GitHub Token (fine)",    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b")),
    ("GitLab PAT",             re.compile(r"\bglpat-[A-Za-z0-9\-_]{20}\b")),
    ("Slack Bot Token",        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,72}\b")),
    ("Stripe Secret Key",      re.compile(r"\bsk_(live|test)_[A-Za-z0-9]{24,99}\b")),
    ("Stripe Publishable Key", re.compile(r"\bpk_(live|test)_[A-Za-z0-9]{24,99}\b")),
    ("OpenAI API Key",         re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("Google API Key",         re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("Telegram Bot Token",     re.compile(r"\b\d{8,11}:[A-Za-z0-9_-]{35}\b")),
    ("JWT Token",              re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("Private Key (PEM)",      re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----")),
    # EVM private keys are 64 hex chars, but so are event-topic hashes,
    # transaction hashes, Keccak digests, and a thousand other things — a bare
    # 0x...64hex literal is almost never a key in practice. Require nearby
    # context (variable name like privateKey/PRIVATE_KEY/signerKey/etc.) to
    # cut false-positive noise to a manageable level.
    ("EVM Private Key (hex)",
     re.compile(
         r"(?im)(?:private[_\-]?key|priv[_\-]?key|signer[_\-]?key|wallet[_\-]?key|pk)"
         r"[\"'\s:=]+0x[a-fA-F0-9]{64}\b"
     )),
    ("Supabase service key",   re.compile(r"(?i)supabase.{0,40}service.{0,40}role.{0,40}eyJ")),
    # .env-style assignment: STRICT — uppercase var name must contain a known
    # secret-suggestive token (KEY/TOKEN/SECRET/PASSWORD/...) AND value must be
    # 24+ chars of base64-ish high-entropy content. Without these guardrails
    # the pattern matches every UPPERCASE constant in the codebase.
    (".env-style secret assignment",
     re.compile(
         r"(?m)^\s*(?:export\s+)?"
         r"[A-Z][A-Z0-9_]*"
         r"(?:KEY|TOKEN|SECRET|PASSWORD|PASS|PWD|API|CRED|AUTH|PRIVATE)"
         r"[A-Z0-9_]*"
         r"\s*=\s*"
         r"['\"]?[A-Za-z0-9+/=_\-]{24,}['\"]?\s*$"
     )),
]

# Files we expect to contain placeholder strings; filter out obvious examples
# to keep noise down. We still scan them, but matches that look like
# placeholders are suppressed.
PLACEHOLDER_TOKENS = (
    "your-secret-key", "your_secret_key", "your-api-key", "your_api_key",
    "changeme", "change_me", "example", "REPLACE_ME", "xxxx", "placeholder",
    "<your-", "<paste-", "<token>", "***",
)

# Files that genuinely SHOULD contain example assignments (NOT real secrets).
SAFE_FILE_GLOBS = (
    ".env.example", ".env.template", ".env.sample",
    "README.md", "CHANGELOG.md", "docs/", ".github/",
)


def is_placeholder(line: str) -> bool:
    lower = line.lower()
    return any(tok in lower for tok in PLACEHOLDER_TOKENS)


def is_safe_file(path: str) -> bool:
    return any(g in path for g in SAFE_FILE_GLOBS)


def run(cmd: list[str]) -> str:
    return subprocess.run(
        cmd, capture_output=True, text=True, errors="replace",
    ).stdout


def list_commits(since: int | None) -> list[str]:
    cmd = ["git", "log", "--all", "--format=%H"]
    if since:
        cmd += [f"-{since}"]
    out = run(cmd)
    return [c for c in out.splitlines() if c]


def list_files_in_commit(sha: str) -> list[str]:
    out = run(["git", "show", "--name-only", "--format=", sha])
    return [p for p in out.splitlines() if p.strip()]


def get_file_at_commit(sha: str, path: str) -> str:
    return run(["git", "show", f"{sha}:{path}"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since", type=int, default=None,
        help="Limit scan to the last N commits (default: all history).",
    )
    parser.add_argument(
        "--path", default=None,
        help="Only inspect paths matching this substring (e.g. '.env').",
    )
    parser.add_argument(
        "--show-line", action="store_true",
        help="Print the matching line content (default: just file:line).",
    )
    args = parser.parse_args()

    # Sanity check: must be in a git repo.
    if subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    ).returncode != 0:
        print("error: not a git repository", file=sys.stderr)
        return 2

    commits = list_commits(args.since)
    print(f"Scanning {len(commits)} commit(s)...\n", file=sys.stderr)

    findings: list[tuple[str, str, str, int, str, str]] = []
    seen: set[tuple[str, int, str]] = set()  # dedupe by (path, line, label)

    for i, sha in enumerate(commits):
        if i % 50 == 0 and i > 0:
            print(f"  ... {i}/{len(commits)}", file=sys.stderr)
        for path in list_files_in_commit(sha):
            if args.path and args.path not in path:
                continue
            if is_safe_file(path):
                continue
            try:
                content = get_file_at_commit(sha, path)
            except Exception:
                continue
            if not content:
                continue

            for label, pat in PATTERNS:
                for m in pat.finditer(content):
                    # Find the line number of the match.
                    line_no = content.count("\n", 0, m.start()) + 1
                    line = content.splitlines()[line_no - 1] if line_no <= content.count("\n") + 1 else ""
                    if is_placeholder(line):
                        continue
                    key = (path, line_no, label)
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append((sha[:12], path, label, line_no, line.strip()[:200], m.group(0)[:80]))

    if not findings:
        print("OK: no high-confidence secrets found in git history.")
        return 0

    print("=" * 78)
    print(f"FOUND {len(findings)} suspected secret(s) in git history")
    print("=" * 78)
    for sha, path, label, line_no, line, match in findings:
        print(f"\n[{label}]  {path}:{line_no}  (commit {sha})")
        print(f"  match: {match}")
        if args.show_line and line:
            print(f"  line:  {line}")

    print()
    print("=" * 78)
    print("ACTION REQUIRED")
    print("=" * 78)
    print("Anything that ever appeared in git history must be considered leaked.")
    print("For each finding above:")
    print("  1. ROTATE the credential at its source (revoke the key, issue a new one).")
    print("  2. Verify no production system still uses the old credential.")
    print("  3. Consider purging history with `git filter-repo` (but rotation is")
    print("     the only true mitigation — purging just hides what scrapers may")
    print("     already have).")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(2)
