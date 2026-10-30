"""CLI mirror of the MCP tools, for CI use:
  codeatlas check --repo . --range origin/main..HEAD --format markdown
Exit codes: 0 ok, 1 tests failed / verdict fail, 2 error (bad repo, bad range...)."""
import argparse
import json
import sys

from . import analysis, gitutil, report, runner, workflow
from .indexer import index_repo
from .security import resolve_repo

COMMANDS = ["index", "symbol", "source", "refs", "impact", "test", "review", "check", "diff", "hotspots", "health"]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="codeatlas")
    ap.add_argument("command", choices=COMMANDS, help="'check' = review + impact + tests with one verdict")
    ap.add_argument("name", nargs="?", help="symbol name for symbol/source/refs")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--range", dest="rev_range", help="e.g. HEAD~1..HEAD or origin/main..HEAD")
    ap.add_argument("--scope", choices=gitutil.SCOPES, default="all")
    ap.add_argument("--no-fallback", action="store_true", help="do not analyse the last commit when the tree is clean")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--baseline", action="store_true", help="also run the tests on the pre-change code")
    ap.add_argument("--all-if-unmapped", action="store_true", help="run every test if none maps to the change")
    ap.add_argument("--fail-on-unverified", action="store_true", help="exit 1 when no test covers the change")
    ap.add_argument("--format", choices=["json", "markdown"], default="json")
    a = ap.parse_args(argv)
    if a.command in ("symbol", "source", "refs") and not a.name:
        ap.error("this command needs a symbol name")
    fb = not a.no_fallback
    try:
        repo = resolve_repo(a.repo)
        result = {
            "index": lambda: index_repo(repo),
            "symbol": lambda: analysis.find_symbol(repo, a.name),
            "source": lambda: analysis.symbol_source(repo, a.name),
            "refs": lambda: analysis.find_references(repo, a.name),
            "impact": lambda: analysis.impact(repo, a.rev_range, a.scope, fb),
            "review": lambda: analysis.review_changes(repo, a.rev_range, a.scope, fb),
            "test": lambda: runner.run_affected_tests(repo, a.rev_range, a.timeout, a.scope, fb,
                                                      a.baseline, a.all_if_unmapped),
            "check": lambda: workflow.review_and_test(repo, a.rev_range, a.timeout, a.scope, fb,
                                                      a.baseline, a.all_if_unmapped),
            "diff": lambda: gitutil.diff_text(repo, a.rev_range, a.scope, fb),
            "hotspots": lambda: analysis.hotspots(repo),
            "health": lambda: analysis.architecture_health(repo),
        }[a.command]()
    except (ValueError, PermissionError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        sys.exit(2)

    if a.format == "markdown" and a.command == "check":
        print(report.render_markdown(result))
    elif a.command == "diff" and a.format == "markdown":
        print(f"```diff\n{result['diff']}\n```")
    else:
        print(json.dumps(result, indent=2))

    if a.command == "test":
        bad = result.get("status") in ("failed", "error", "timeout")
        sys.exit(1 if bad or (a.fail_on_unverified and result.get("status") == "no_tests") else 0)
    if a.command == "check":
        sys.exit(1 if result["verdict"] == "fail" or (a.fail_on_unverified and result["verdict"] == "unverified") else 0)


if __name__ == "__main__":
    main()
