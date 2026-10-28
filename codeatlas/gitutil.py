import re

from git import Repo
from git.exc import GitCommandError

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(repo_dir, rev_range: str | None = None):
    """Return (ranges, deleted). ranges: {file: [(start, end)]} on the NEW side.
    rev_range None = working tree vs HEAD (+ untracked files); else e.g. 'main..HEAD'."""
    repo = Repo(repo_dir)
    spec = [rev_range] if rev_range else (["HEAD"] if repo.head.is_valid() else [])
    try:
        diff = repo.git.diff("-U0", "--no-color", "--no-ext-diff", *spec)
    except GitCommandError as e:
        raise ValueError(f"git diff failed: {str(e)[:200]}")
    ranges: dict[str, list] = {}
    deleted: list[str] = []
    cur = None
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            cur = line.split(" b/")[-1]
        elif line.startswith("+++ /dev/null"):
            deleted.append(cur)
            ranges.pop(cur, None)
            cur = None
        elif cur and (m := HUNK.match(line)):
            start, n = int(m.group(1)), int(m.group(2) or 1)
            ranges.setdefault(cur, []).append((start, start + max(n, 1) - 1))
    if not rev_range:
        for f in repo.git.ls_files("--others", "--exclude-standard").splitlines():
            ranges[f] = [(1, 10**9)]
    return ranges, deleted


def churn(repo_dir, days: int = 90) -> dict[str, int]:
    repo = Repo(repo_dir)
    try:
        out = repo.git.log(f"--since={days}.days", "--name-only", "--pretty=format:")
    except GitCommandError:
        return {}
    counts: dict[str, int] = {}
    for f in out.splitlines():
        if f.strip():
            counts[f] = counts.get(f, 0) + 1
    return counts
