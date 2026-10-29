
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from . import analysis


def run_affected_tests(repo, rev_range=None, timeout: int = 60) -> dict:
    imp = analysis.impact(repo, rev_range)
    total = imp["total_test_files"]

    changed_tests = [
        f
        for f in imp["changed_files"]
        if f.endswith(".py")
        and analysis.is_test_path(f)
        and (repo / f).exists()
    ]

    selected = sorted(set(imp["affected_tests"]) | set(changed_tests))
    mode = "selected"

    if imp["config_files_changed"]:
        selected, mode = [], "all"
    elif not imp["changed_files"]:
        return {
            "mode": "none",
            "message": "No changes detected.",
            "total_test_files": total,
        }
    elif not selected:
        return {
            "mode": "none",
            "message": "No test files depend on the changed code.",
            "total_test_files": total,
        }
        
    
    test_env = os.environ.copy()

    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
        test_env.pop(key, None)

    test_env["PYTHONNOUSERSITE"] = "1"
    test_env["PYTHONDONTWRITEBYTECODE"] = "1"
    test_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"


    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "r.xml")

        # First, check whether pytest can collect tests.
        collect_cmd = [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-vv",
            "-p",
            "no:cacheprovider",
            *selected,
        ]

        try:
           
            collected = subprocess.run(
                collect_cmd,
                cwd=repo,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=min(timeout, 60),
                env=test_env,
            )

        except subprocess.TimeoutExpired as exc:
            return {
                "mode": mode,
                "stage": "collection",
                "error": "pytest test collection timed out",
                "selected_test_files": selected,
                "stdout_tail": str(exc.stdout or "")[-2000:],
                "stderr_tail": str(exc.stderr or "")[-2000:],
            }

        if collected.returncode != 0:
            return {
                "mode": mode,
                "stage": "collection",
                "error": "pytest could not collect tests",
                "selected_test_files": selected,
                "exit_code": collected.returncode,
                "stdout_tail": collected.stdout[-2000:],
                "stderr_tail": collected.stderr[-2000:],
            }

        # If collection succeeds, execute the selected tests.
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={out}",
            *selected,
        ]

        try:
                p = subprocess.run(
                cmd,
                cwd=repo,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=test_env,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "mode": mode,
                "stage": "execution",
                "error": f"pytest execution timed out after {timeout}s",
                "selected_test_files": selected,
                "stdout_tail": str(exc.stdout or "")[-2000:],
                "stderr_tail": str(exc.stderr or "")[-2000:],
            }

        counts = {
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
        }
        failures = []

        if os.path.exists(out):
            root = ET.parse(out).getroot()

            for tc in root.iter("testcase"):
                failure = tc.find("failure")
                error = tc.find("error")
                bad = failure if failure is not None else error

                if bad is not None:
                    counts["failed" if failure is not None else "errors"] += 1
                    failures.append({
                        "test": f"{tc.get('classname')}::{tc.get('name')}",
                        "message": (bad.get("message") or "")[:300],
                    })
                elif tc.find("skipped") is not None:
                    counts["skipped"] += 1
                else:
                    counts["passed"] += 1

    n = len(selected) if mode == "selected" else total

    return {
        "mode": mode,
        "stage": "execution",
        "selected_test_files": selected if mode == "selected" else "all",
        "total_test_files": total,
        "saved_pct": round(100 * (1 - n / total)) if total else 0,
        "exit_code": p.returncode,
        **counts,
        "failures": failures[:10],
        "log_tail": p.stdout[-2000:] if p.returncode not in (0, 1) else "",
        "stderr_tail": p.stderr[-2000:] if p.returncode not in (0, 1) else "",
    }
