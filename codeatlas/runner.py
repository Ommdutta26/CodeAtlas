"""Run only the tests affected by a change, and report results that point back at the change."""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from . import analysis, attribution, gitutil
from .security import is_test_path

_LOC = re.compile(r"^([^\s:][^:\n]*?\.py):(\d+):", re.M)
MAX_FAILURES = 20


class _Timeout(Exception):
    def __init__(self, out: str = "", err: str = ""):
        super().__init__("timeout")
        self.out, self.err = out, err


def _python_for(repo) -> str:
    """The project's own interpreter if it has one; the server's Python otherwise.
    Override with CODEATLAS_PYTHON."""
    override = os.environ.get("CODEATLAS_PYTHON")
    if override:
        return override
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python", "venv/Scripts/python.exe", "venv/bin/python"):
        if (repo / rel).exists():
            return str(repo / rel)
    return sys.executable


def _env() -> dict:
    env = os.environ.copy()
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
        env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if os.environ.get("CODEATLAS_ISOLATE_PLUGINS") == "1":      # opt-in: breaks pytest-asyncio, -django, -mock ...
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


def _kill_tree(p: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        p.kill()


def _run(cmd, cwd, timeout, env) -> subprocess.CompletedProcess:
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", env=env, **kw)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(p)                       # kill pytest AND anything it spawned
        try:
            out, err = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = err = ""
        raise _Timeout(out or "", err or "")
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def _parse_junit(path: str):
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    failures = []
    if not os.path.exists(path):
        return counts, failures
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return counts, failures
    for tc in root.iter("testcase"):
        failure, error = tc.find("failure"), tc.find("error")
        bad = failure if failure is not None else error
        if bad is not None:
            counts["failed" if failure is not None else "errors"] += 1
            text = bad.text or ""
            locs = [f"{p.replace(chr(92), '/').removeprefix('./')}:{n}" for p, n in _LOC.findall(text)]
            failures.append({"test": f"{tc.get('classname')}::{tc.get('name')}",
                             "message": (bad.get("message") or "")[:300],
                             "locations": list(dict.fromkeys(locs))[-4:],
                             "traceback": text[-1200:]})
        elif tc.find("skipped") is not None:
            counts["skipped"] += 1
        else:
            counts["passed"] += 1
    return counts, failures


def _pytest(cwd, python, files, timeout, env):
    with tempfile.TemporaryDirectory() as tmp:
        xml = os.path.join(tmp, "r.xml")
        cmd = [python, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", f"--junitxml={xml}", *files]
        proc = _run(cmd, cwd, timeout, env)
        counts, failures = _parse_junit(xml)
    return proc, counts, failures


def _status(code: int, ran: int) -> str:
    if code == 0:
        return "passed"
    if code == 1 and ran:
        return "failed"
    if code == 5:
        return "no_tests"
    return "error"                           # pytest crashed, bad usage, collection errors, no junit


def _baseline_failures(repo, base_ref, selected, mode, python, timeout, env):
    """Failing test ids when the same tests run on the code BEFORE the change (temporary git worktree).
    Returns (set | None, note)."""
    tmp = tempfile.mkdtemp(prefix="codeatlas-base-")
    wt = os.path.join(tmp, "wt")
    try:
        r = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", wt, base_ref],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return None, f"baseline skipped: cannot check out {base_ref} ({r.stderr.strip()[-150:]})"
        files = [f for f in selected if os.path.exists(os.path.join(wt, f))] if mode == "selected" else []
        if mode == "selected" and not files:
            return set(), "baseline: none of the selected test files exist before the change"
        _, _, failures = _pytest(wt, python, files, timeout, env)
        return {f["test"] for f in failures}, (
            "baseline ran in a temporary worktree; if the project is pip-installed (editable/src layout) "
            "the baseline may import the modified code and under-report pre-existing failures")
    except _Timeout:
        return None, "baseline skipped: timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"baseline skipped: {exc}"
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", wt],
                       capture_output=True, text=True)
        shutil.rmtree(tmp, ignore_errors=True)


def run_affected_tests(repo, rev_range=None, timeout: int = 60, scope: str = "all", fallback: bool = True,
                       baseline: bool = False, all_if_unmapped: bool = False,
                       g=None, cs=None, imp=None) -> dict:
    started = time.perf_counter()
    g = g or analysis.build_graph(repo)
    cs = cs or gitutil.collect(repo, rev_range, scope, fallback)
    imp = imp or analysis.impact(repo, g=g, cs=cs)
    total = imp["total_test_files"]
    base = {"range": imp["range"], "total_test_files": total, "notes": list(imp["notes"])}

    changed_tests = [f for f in imp["changed_files"]
                     if f.endswith(".py") and is_test_path(f) and (repo / f).exists()]
    selected = sorted(set(imp["affected_tests"]) | set(changed_tests))
    mode = "selected"

    if imp["config_files_changed"]:
        selected, mode = [], "all"
        base["notes"].append(f"Config/dependency files changed ({imp['config_files_changed']}): running ALL tests.")
    elif not imp["changed_files"] and not imp["deleted_files"]:
        return {**base, "status": "no_changes", "mode": "none", "message": "No changes detected."}
    elif not selected:
        if all_if_unmapped and total:
            selected, mode = [], "all"
            base["notes"].append("No test depends on the changed code (statically); running ALL tests as requested.")
        else:
            return {**base, "status": "no_tests", "mode": "none",
                    "message": "No test files depend on the changed code.",
                    "hint": "The change is not covered by any test the call graph can see. Re-run with "
                            "run_all_if_unmapped=true to run everything."}

    python, env = _python_for(repo), _env()
    try:
        proc, counts, failures = _pytest(repo, python, selected, timeout, env)
    except _Timeout as exc:
        return {**base, "status": "timeout", "mode": mode, "stage": "execution", "python": python,
                "error": f"pytest timed out after {timeout}s (process tree killed)",
                "selected_test_files": selected if mode == "selected" else "all",
                "stdout_tail": exc.out[-2000:], "stderr_tail": exc.err[-2000:]}

    ran = sum(counts.values())
    status = _status(proc.returncode, ran)
    attr = attribution.attribute(g, imp, failures) if failures else {"likely_causes": [], "unlinked_failures": 0}

    result = {**base, "status": status, "mode": mode, "stage": "execution", "python": python,
              "selected_test_files": selected if mode == "selected" else "all",
              "saved_pct": round(100 * (1 - len(selected) / total)) if mode == "selected" and total else 0,
              "exit_code": proc.returncode, **counts,
              "likely_causes": attr["likely_causes"], "unlinked_failures": attr["unlinked_failures"]}

    if failures and baseline:
        base_ref = "HEAD" if cs.base == gitutil.INDEX else cs.base
        if base_ref:
            ids, note = _baseline_failures(repo, base_ref, selected, mode, python, timeout, env)
            result["notes"].append(note)
            if ids is not None:
                for f in failures:
                    f["vs_base"] = "pre-existing" if f["test"] in ids else "new"
                result["new_failures"] = sum(f["vs_base"] == "new" for f in failures)
                result["preexisting_failures"] = sum(f["vs_base"] == "pre-existing" for f in failures)
        else:
            result["notes"].append("baseline skipped: no commit before the change")

    result["failures"] = failures[:MAX_FAILURES]
    result["failures_truncated"] = len(failures) > MAX_FAILURES
    if status == "error" or (ran == 0 and proc.returncode != 0):
        result["stdout_tail"], result["stderr_tail"] = proc.stdout[-2000:], proc.stderr[-2000:]
        result["notes"].append("pytest did not run any tests; see stdout_tail/stderr_tail. A missing dependency "
                               f"in {python} is the usual cause (set CODEATLAS_PYTHON to the project's interpreter).")
    result["duration_s"] = round(time.perf_counter() - started, 2)
    return result
