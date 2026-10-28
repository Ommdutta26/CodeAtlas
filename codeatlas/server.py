"""CodeAtlas MCP server (stdio)."""
from mcp.server.fastmcp import FastMCP

from . import analysis, runner
from .indexer import index_repo
from .security import resolve_repo

mcp = FastMCP("codeatlas")


def _safe(fn, repo_path, *a, **kw):
    try:
        return fn(resolve_repo(repo_path), *a, **kw)
    except (ValueError, PermissionError) as e:
        return {"error": str(e)}


@mcp.tool()
def index_repository(repo_path: str, full: bool = False) -> dict:
    """Build or incrementally refresh the symbol/import/call graph of a Python repo."""
    return _safe(index_repo, repo_path, full)


@mcp.tool()
def find_symbol(repo_path: str, name: str) -> dict:
    """Locate definitions of a function/class/method with file, line, complexity and a code snippet."""
    return _safe(analysis.find_symbol, repo_path, name)


@mcp.tool()
def find_references(repo_path: str, symbol: str) -> dict:
    """List callers and references of a symbol using the call graph (with confidence labels)."""
    return _safe(analysis.find_references, repo_path, symbol)


@mcp.tool()
def impact_analysis(repo_path: str, rev_range: str | None = None) -> dict:
    """What does this change affect? Changed symbols, affected functions/modules and tests.
    rev_range like 'main..HEAD'; omit for uncommitted changes."""
    return _safe(analysis.impact, repo_path, rev_range)


@mcp.tool()
def run_affected_tests(repo_path: str, rev_range: str | None = None, timeout: int = 300) -> dict:
    """Run only the tests that depend on the changed code (all tests if config/deps changed)."""
    return _safe(runner.run_affected_tests, repo_path, rev_range, timeout)


@mcp.tool()
def review_changes(repo_path: str, rev_range: str | None = None) -> dict:
    """Risk score (low/medium/high) for a change set with reasons and suggested tests."""
    return _safe(analysis.review_changes, repo_path, rev_range)


@mcp.tool()
def hotspots(repo_path: str, days: int = 90, top: int = 10) -> dict:
    """Files that change often and are complex: where bugs are most likely."""
    return _safe(analysis.hotspots, repo_path, days, top)


@mcp.tool()
def architecture_health(repo_path: str) -> dict:
    """Import cycles, dead-code candidates and the most complex functions."""
    return _safe(analysis.architecture_health, repo_path)


@mcp.prompt()
def review_my_changes(repo_path: str) -> str:
    return (f"Review the changes in {repo_path}. Call review_changes, then impact_analysis, then "
            "run_affected_tests. Summarize risk, what could break, and which tests passed or failed.")


def main():
    mcp.run()


if __name__ == "__main__":
    main()
