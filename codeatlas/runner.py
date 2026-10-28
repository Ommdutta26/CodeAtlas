import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from . import analysis


def run_affected_tests(repo, rev_range=None, timeout: int = 300) -> dict:
    imp = analysis.impact(repo, rev_range)
    total = imp["total_test_files"]
    changed_tests = [f for f in imp["changed_files"] if f.endswith(".py") and
                     analysis.is_test_path(f) and (repo / f).exists()]
    selected = sorted(set(imp["affected_tests"]) | set(changed_tests))
    mode = "selected"
    if imp["config_files_changed"]:
        selected, mode = [], "all"
    elif not imp["changed_files"]:
        return {"mode": "none", "message": "No changes detected.", "total_test_files": total}
    elif not selected:
        return {"mode": "none", "message": "No test files depend on the changed code.", "total_test_files": total}
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "r.xml")
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={out}", *selected]
        try:
            p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        except subprocess.TimeoutExpired:
            return {"mode": mode, "error": f"timed out after {timeout}s"}
        counts, failures = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}, []
        if os.path.exists(out):
            for tc in ET.parse(out).getroot().iter("testcase"):
                bad = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
                if bad is not None:
                    counts["failed" if tc.find("failure") is not None else "errors"] += 1
                    failures.append({"test": f"{tc.get('classname')}::{tc.get('name')}",
                                     "message": (bad.get("message") or "")[:300]})
                else:
                    counts["skipped" if tc.find("skipped") is not None else "passed"] += 1
    n = len(selected) if mode == "selected" else total
    return {"mode": mode, "selected_test_files": selected if mode == "selected" else "all",
            "total_test_files": total,
            "saved_pct": round(100 * (1 - n / total)) if total else 0,
            "exit_code": p.returncode, **counts, "failures": failures[:10],
            "log_tail": p.stdout[-800:] if p.returncode not in (0, 1) else ""}
