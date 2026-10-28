"""CLI mirror of the MCP tools, for CI use:  codeatlas test --repo . --range origin/main..HEAD"""
import argparse
import json
import sys

from . import analysis, runner
from .indexer import index_repo
from .security import resolve_repo


def main(argv=None):
    ap = argparse.ArgumentParser(prog="codeatlas")
    ap.add_argument("command", choices=["index", "symbol", "refs", "impact", "test", "review", "hotspots", "health"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--range", dest="rev_range")
    a = ap.parse_args(argv)
    repo = resolve_repo(a.repo)
    if a.command in ("symbol", "refs") and not a.name:
        ap.error("this command needs a symbol name")
    result = {"index": lambda: index_repo(repo), "symbol": lambda: analysis.find_symbol(repo, a.name),
              "refs": lambda: analysis.find_references(repo, a.name),
              "impact": lambda: analysis.impact(repo, a.rev_range),
              "test": lambda: runner.run_affected_tests(repo, a.rev_range),
              "review": lambda: analysis.review_changes(repo, a.rev_range),
              "hotspots": lambda: analysis.hotspots(repo), "health": lambda: analysis.architecture_health(repo)}[a.command]()
    print(json.dumps(result, indent=2))
    if a.command == "test":
        sys.exit(1 if result.get("failed") or result.get("errors") or result.get("error") else 0)


if __name__ == "__main__":
    main()
