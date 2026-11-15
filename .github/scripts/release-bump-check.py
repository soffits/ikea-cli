#!/usr/bin/env python3
"""Fail when a release version bumps less than its commit types require.

Usage: release-bump-check.py [vX.Y.Z]  (default $GITHUB_REF_NAME; run it before tagging too)
Compares with the highest plain X.Y.Z tag below the new version, reading commits up to HEAD.
Below 1.0: feat or breaking -> minor, fix/perf -> patch.
From 1.0: breaking -> major, feat -> minor, fix/perf -> patch.
"""

import os
import re
import subprocess
import sys

SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
HEADER = re.compile(r"^(\w+)(\([^)]*\))?(!)?:")
LEVELS = ["none", "patch", "minor", "major"]


def git(*args):
    out = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    return out.stdout


def parse(tag):
    m = SEMVER.match(tag)
    return tuple(int(x) for x in m.groups()) if m else None


def is_breaking(m, body):
    if m and m.group(3):
        return True
    return "BREAKING CHANGE" in body or "BREAKING-CHANGE" in body


def required_bump(version, prev_tag):
    fmt = "--format=%s%x1e%b%x1f"
    commits = git("log", f"{prev_tag}..HEAD", fmt).split("\x1f")
    level = 0
    for commit in commits:
        if not commit.strip():
            continue
        subject, _, body = commit.strip().partition("\x1e")
        m = HEADER.match(subject)
        kind = m.group(1) if m else ""
        if is_breaking(m, body):
            level = max(level, 2 if version[0] == 0 else 3)
        elif kind == "feat":
            level = max(level, 2)
        elif kind in ("fix", "perf"):
            level = max(level, 1)
    return level


def actual_bump(old, new):
    if new[0] != old[0]:
        return 3
    if new[1] != old[1]:
        return 2
    return 1 if new[2] != old[2] else 0


def main():
    new_tag = sys.argv[1] if len(sys.argv) > 1 else ""
    new_tag = new_tag or os.environ.get("GITHUB_REF_NAME", "")
    new = parse(new_tag)
    if not new:
        print(f"{new_tag!r} is not a plain X.Y.Z version; nothing to check")
        return 0
    tags = {parse(t): t for t in git("tag", "--list").split() if parse(t)}
    older = sorted(v for v in tags if v < new)
    if not older:
        print("no earlier release; nothing to check")
        return 0
    prev_tag = tags[older[-1]]
    need = required_bump(new, prev_tag)
    got = actual_bump(older[-1], new)
    summary = f"{prev_tag} -> {new_tag}: need {LEVELS[need]}, got {LEVELS[got]}"
    print(summary)
    if got < need:
        print(f"::error::{summary} (version bump too small for the commits)")
        return 1
    return 0


sys.exit(main())
