
"""CodeAtlas MCP server (stdio)."""

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import analysis, runner
from .indexer import index_repo
from .security import resolve_repo

logging.basicConfig(
    filename=str(Path.home() / "codeatlas-mcp-debug.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("codeatlas")

mcp = FastMCP("codeatlas")


def _safe(fn, repo_path, *args, **kwargs):
    try:
        return fn(resolve_repo(repo_path), *args, **kwargs)
    except (ValueError, PermissionError) as exc:
        logger.exception("CodeAtlas request rejected: %s", exc)
        return {"error": str(exc)}


@mcp.tool()
def index_repository(repo_path: str, full: bool = False) -> dict:
    """Build or incrementally refresh the symbol/import/call graph."""
    return _safe(index_repo, repo_path, full)


@mcp.tool()
def find_symbol(repo_path: str, name: str) -> dict:
    """Locate definitions of a function, class, or method."""
    return _safe(analysis.find_symbol, repo_path, name)


@mcp.tool()
def find_references(repo_path: str, symbol: str) -> dict:
    """List callers and references of a symbol."""
    return _safe(analysis.find_references, repo_path, symbol)


@mcp.tool()
def impact_analysis(
    repo_path: str,
    rev_range: str | None = None,
) -> dict:
    """Analyze changed symbols, affected functions, modules, and tests."""
    return _safe(analysis.impact, repo_path, rev_range)


@mcp.tool()
def run_affected_tests(
    repo_path: str,
    rev_range: str | None = None,
    timeout: int = 60,
) -> dict:
    """Run tests affected by changed code."""
    logger.info(
        "run_affected_tests received: repo=%s range=%s timeout=%s",
        repo_path,
        rev_range,
        timeout,
    )

    try:
        result = _safe(
            runner.run_affected_tests,
            repo_path,
            rev_range,
            timeout,
        )
        logger.info(
            "run_affected_tests completed: mode=%s exit_code=%s error=%s",
            result.get("mode"),
            result.get("exit_code"),
            result.get("error"),
        )
        return result
    except Exception:
        logger.exception("run_affected_tests crashed")
        raise


@mcp.tool()
def review_changes(
    repo_path: str,
    rev_range: str | None = None,
) -> dict:
    """Calculate change risk and suggest tests."""
    return _safe(analysis.review_changes, repo_path, rev_range)


@mcp.tool()
def hotspots(
    repo_path: str,
    days: int = 90,
    top: int = 10,
) -> dict:
    """Find frequently changed and complex files."""
    return _safe(analysis.hotspots, repo_path, days, top)


@mcp.tool()
def architecture_health(repo_path: str) -> dict:
    """Report import cycles, dead-code candidates, and complexity."""
    return _safe(analysis.architecture_health, repo_path)


@mcp.prompt()
def review_my_changes(repo_path: str) -> str:
    return (
        f"Review the changes in {repo_path}. Call review_changes, "
        "then impact_analysis, then run_affected_tests. "
        "Summarize the risk and the test results."
    )


def main():
    mcp.run()


if __name__ == "__main__":
    main()
