"""Git helpers: what changed (line ranges), diffs, and file contents at a revision.

Scopes for "no rev_range":
  all       working tree vs HEAD (staged + unstaged + untracked)   [default]
  staged    index vs HEAD                                           (what `git add .` produced)
  unstaged  working tree vs index (+ untracked)

If nothing is uncommitted and `fallback` is on, the last commit (HEAD~1..HEAD) is analysed instead,
and the result says so in `notes`.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_REV = re.compile(r"^[\w./~^@{}:+\-]+$")
SCOPES = ("all", "staged", "unstaged")
WHOLE_FILE = 10**9
INDEX = ":"          # marker meaning "the staging area" for ChangeSet.base / .head


@dataclass
class ChangeSet:
    ranges: dict                      # {file: [(start, end)]} on the NEW side
    deleted: list
    label: str                        # human-readable description of what was compared
    base: str | None                  # revision holding the OLD side (INDEX = staging area, None = none)
    head: str | None                  # revision holding the NEW side (None = working tree, INDEX = staging area)
    diff_args: list = field(default_factory=list)   # argument lists for `git diff` reproducing this change
    untracked: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    scope: str = "all"
    rev_range: str | None = None
    fallback_used: bool = False

    @property
    def empty(self) -> bool:
        return not self.ranges and not self.deleted


def validate_rev_range(rev_range):
    """Reject anything that could be read by git as an option or contains odd characters."""
    if rev_range is None:
        return None
    r = str(rev_range).strip()
    if not r:
        return None
    if len(r) > 200 or r.startswith("-") or not _REV.match(r):
        raise ValueError(f"Invalid rev_range: {rev_range!r}")
    return r


@contextmanager
def open_repo(repo_dir):
    try:
        repo = Repo(str(repo_dir))
    except (InvalidGitRepositoryError, NoSuchPathError) as exc:
        raise ValueError(f"Not a git repository: {repo_dir}") from exc
    try:
        yield repo
    finally:
        repo.close()


def _git(repo, *args) -> str:
    return repo.git.execute(["git", "-c", "core.quotepath=false", *args])


def _path(p: str, prefix: str) -> str:
    p = p.rstrip("\t")
    if len(p) > 1 and p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    return p[len(prefix):] if p.startswith(prefix) else p


def _parse_diff(diff: str, ranges: dict, deleted: list) -> None:
    old = cur = None
    in_hunk = False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            old = cur = None
            in_hunk = False
        elif not in_hunk and line.startswith("--- "):
            old = _path(line[4:], "a/")
        elif not in_hunk and line.startswith("+++ "):
            target = line[4:].rstrip("\t")
            if target == "/dev/null":
                if old:
                    deleted.append(old)
                    ranges.pop(old, None)
                cur = None
            else:
                cur = _path(target, "b/")
        elif line.startswith("@@"):
            in_hunk = True            # from here on, '--- x' / '+++ x' are file content, not headers
            m = HUNK.match(line)
            if cur and m:
                start, n = int(m.group(1)), int(m.group(2) or 1)
                ranges.setdefault(cur, []).append((start, start + max(n, 1) - 1))


def _diff(repo, *spec) -> str:
    try:
        return _git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", *spec, "--")
    except GitCommandError as exc:
        raise ValueError(f"git diff failed: {str(exc)[:200]}") from exc


def _split_range(repo, r: str):
    """Return (base, head) revisions for a user range. head None = working tree."""
    try:
        if "..." in r:
            a, _, b = r.partition("...")
            a, b = a or "HEAD", b or "HEAD"
            return _git(repo, "merge-base", a, b).strip(), b
        if ".." in r:
            a, _, b = r.partition("..")
            return (a or "HEAD"), (b or "HEAD")
    except GitCommandError as exc:
        raise ValueError(f"Cannot resolve rev_range {r!r}: {str(exc)[:200]}") from exc
    return r, None


def collect(repo_dir, rev_range=None, scope: str = "all", fallback: bool = True) -> ChangeSet:
    """Work out what changed. See the module docstring for the meaning of scope/fallback."""
    rev_range = validate_rev_range(rev_range)
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")
    with open_repo(repo_dir) as repo:
        has_head = repo.head.is_valid()
        ranges: dict = {}
        deleted: list = []
        untracked: list = []
        notes: list = []

        if rev_range:
            base, head = _split_range(repo, rev_range)
            _parse_diff(_diff(repo, rev_range), ranges, deleted)
            notes.append("Line ranges come from the requested range; the call graph is built from the "
                         "checked-out working tree, so results are most accurate when it is checked out "
                         "at the end of the range.")
            return ChangeSet(ranges, deleted, rev_range, base, head, [[rev_range]], [], notes,
                             scope, rev_range)

        if scope == "staged":
            diff_args, label, base, head = [["--cached"]], "staged vs HEAD", "HEAD" if has_head else None, INDEX
        elif scope == "unstaged":
            diff_args, label, base, head = [[]], "unstaged (working tree vs index)", INDEX, None
        elif has_head:
            diff_args, label, base, head = [["HEAD"]], "working tree vs HEAD", "HEAD", None
        else:
            diff_args, label, base, head = [["--cached"], []], "working tree (repository has no commits yet)", None, None
        for args in diff_args:
            _parse_diff(_diff(repo, *args), ranges, deleted)

        if scope in ("all", "unstaged"):
            untracked = [f for f in _git(repo, "ls-files", "--others", "--exclude-standard").split("\n") if f]
            for f in untracked:
                ranges[f] = [(1, WHOLE_FILE)]

        cs = ChangeSet(ranges, deleted, label, base, head, diff_args, untracked, notes, scope, None)

        if cs.empty and scope == "all" and fallback and has_head and repo.head.commit.parents:
            sha = repo.head.commit.hexsha[:7]
            subject = repo.head.commit.summary
            fb = collect(repo_dir, "HEAD~1..HEAD", "all", False)
            fb.label = "HEAD~1..HEAD (last commit)"
            fb.scope, fb.fallback_used = "all", True
            fb.notes = [f"The working tree is clean (nothing uncommitted), so the last commit "
                        f"{sha} \"{subject}\" was analysed instead. Pass rev_range to choose another range."]
            return fb
        if cs.empty:
            cs.notes.append("No changes found. If you already committed, pass rev_range such as "
                            "'HEAD~1..HEAD' (last commit) or 'main..HEAD' (whole branch).")
        return cs


def read_at(repo_dir, rev, path, repo=None):
    """File content on one side of a change. rev None = working tree, ':' = index, else a revision.
    Returns None if the file does not exist there."""
    if rev is None:
        try:
            return (Path(repo_dir) / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    spec = f":{path}" if rev == INDEX else f"{rev}:{path}"

    def _show(r):
        try:
            return _git(r, "show", spec)
        except GitCommandError:
            return None
    if repo is not None:
        return _show(repo)
    with open_repo(repo_dir) as r:
        return _show(r)


def diff_text(repo_dir, rev_range=None, scope: str = "all", fallback: bool = True,
              max_chars: int = 20000) -> dict:
    cs = collect(repo_dir, rev_range, scope, fallback)
    parts = []
    with open_repo(repo_dir) as repo:
        for args in cs.diff_args:
            try:
                out = _git(repo, "diff", "-U3", "--no-color", "--no-ext-diff", "--no-renames", *args, "--")
            except GitCommandError as exc:
                raise ValueError(f"git diff failed: {str(exc)[:200]}") from exc
            if out:
                parts.append(out)
    for f in cs.untracked[:20]:
        try:
            data = (Path(repo_dir) / f).read_bytes()[:20000]
        except OSError:
            continue
        if b"\0" in data:
            parts.append(f"diff --git a/{f} b/{f}\nnew file (binary, not shown)")
            continue
        lines = data.decode("utf-8", errors="replace").split("\n")
        parts.append(f"diff --git a/{f} b/{f}\nnew file (untracked)\n--- /dev/null\n+++ b/{f}\n"
                     f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + ln for ln in lines))
    text = "\n".join(parts)
    return {"range": cs.label, "files": sorted(set(cs.ranges) | set(cs.deleted)),
            "diff": text[:max_chars], "truncated": len(text) > max_chars, "total_chars": len(text),
            "notes": cs.notes}


def churn(repo_dir, days: int = 90) -> dict:
    try:
        with open_repo(repo_dir) as repo:
            out = _git(repo, "log", f"--since={int(days)}.days", "--name-only", "--no-renames", "--pretty=format:")
    except (GitCommandError, ValueError):
        return {}
    counts: dict = {}
    for f in out.split("\n"):
        if f.strip():
            counts[f] = counts.get(f, 0) + 1
    return counts
