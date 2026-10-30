"""One call that does review -> impact -> tests and returns a single verdict."""
from __future__ import annotations

from . import analysis, gitutil, runner


def _verdict(tests: dict) -> str:
    st = tests["status"]
    if st == "no_changes":
        return "no_changes"
    if st == "no_tests":
        return "unverified"                  # changed code that no test can see
    if st == "passed":
        return "pass"
    if st == "failed" and tests.get("failed", 0) + tests.get("errors", 0) and tests.get("new_failures", 1) == 0:
        return "pass_with_preexisting_failures"
    return "fail"


def _summary(verdict: str, tests: dict, review: dict) -> str:
    if verdict == "no_changes":
        return "No changes to analyse."
    if verdict == "unverified":
        return f"UNVERIFIED: no test covers the changed code (risk {review['risk_level']})."
    if verdict == "pass":
        return (f"PASS: {tests['passed']} affected tests passed "
                f"({tests['skipped']} skipped); static risk {review['risk_level']}.")
    if verdict == "pass_with_preexisting_failures":
        return (f"PASS (no new failures): {tests['preexisting_failures']} failures already existed before "
                f"this change; static risk {review['risk_level']}.")
    if tests["status"] == "timeout":
        return f"FAIL: tests timed out. {tests['error']}"
    if tests["status"] == "error":
        return "FAIL: pytest could not run the tests (see tests.stderr_tail)."
    n_bad = tests.get("failed", 0) + tests.get("errors", 0)
    text = f"FAIL: {n_bad} of {n_bad + tests.get('passed', 0)} affected tests failed."
    causes = tests.get("likely_causes", [])
    if causes:
        c = causes[0]
        where = f"{c['file']}:{c['line']}-{c['end_line']}" if c.get("line") else c["file"]
        text += f" Likely cause: {c['symbol']} ({where}), reached by {c['failing_tests']} failing test(s)."
    return text


def review_and_test(repo, rev_range=None, timeout: int = 120, scope: str = "all", fallback: bool = True,
                    baseline: bool = False, all_if_unmapped: bool = False) -> dict:
    g = analysis.build_graph(repo)
    cs = gitutil.collect(repo, rev_range, scope, fallback)
    imp = analysis.impact(repo, g=g, cs=cs)
    review = analysis.review_changes(repo, g=g, cs=cs, imp=imp)
    tests = runner.run_affected_tests(repo, timeout=timeout, g=g, cs=cs, imp=imp, baseline=baseline,
                                      all_if_unmapped=all_if_unmapped)
    verdict = _verdict(tests)
    extra_notes = [n for n in tests.get("notes", []) if n not in imp["notes"]]
    tests["notes"] = extra_notes                      # imp["notes"] are reported once, at the top level

    review["static_risk_level"] = review["risk_level"]
    if verdict == "fail":                                   # failing tests outrank any static score
        review["risk_level"] = "high"
        review["risk_score"] = max(review["risk_score"], 35)
        review["reasons"].append("affected tests are failing" if tests["status"] != "timeout"
                                 else "affected tests timed out")
    elif verdict == "unverified" and review["risk_level"] == "low":
        review["risk_level"] = "medium"
        review["reasons"].append("changed code is not covered by any test")

    return {"verdict": verdict, "summary": _summary(verdict, tests, review), "range": imp["range"],
            "notes": imp["notes"] + extra_notes, "review": review,
            "api_changes": imp["api_changes"], "affected_tests": imp["affected_tests"],
            "affected_modules": imp["affected_modules"], "tests": tests}
