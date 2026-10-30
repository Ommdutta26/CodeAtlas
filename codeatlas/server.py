"""CodeAtlas MCP server (stdio)."""
import asyncio
import logging
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Literal

try:                                           
    from mcp.server.fastmcp import FastMCP
except ImportError:                             
    from mcp.server.mcpserver import MCPServer as FastMCP

try:
    from mcp.types import ToolAnnotations
except ImportError:                             
    ToolAnnotations = None

from . import analysis, gitutil, runner, workflow
from .indexer import index_repo
from .security import resolve_repo


logging.basicConfig(
    filename=os.environ.get("CODEATLAS_LOG", str(Path.home() / "codeatlas-mcp-debug.log")),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("codeatlas")

mcp = FastMCP("codeatlas")
Scope = Literal["all", "staged", "unstaged"]


def tool(read_only: bool = True):
    """@mcp.tool() with read-only/closed-world hints where the SDK supports them."""
    if ToolAnnotations is not None:
        try:
            return mcp.tool(annotations=ToolAnnotations(readOnlyHint=read_only, openWorldHint=False))
        except TypeError:
            pass
    return mcp.tool()


def _safe(fn, repo_path, *args, **kwargs):
    """Run an analysis function and turn every failure into a structured, explainable error."""
    try:
        return fn(resolve_repo(repo_path), *args, **kwargs)
    except (ValueError, PermissionError) as exc:
        logger.warning("CodeAtlas request rejected: %s", exc)
        return {"error": str(exc)}
    except (subprocess.SubprocessError, OSError, sqlite3.Error) as exc:
        logger.exception("CodeAtlas environment error")
        return {"error": f"{type(exc).__name__}: {exc}",
                "hint": "Check that git is installed and on PATH, the repo path exists, "
                        "and CODEATLAS_HOME is writable."}
    except Exception as exc:                    # noqa: BLE001 - the model should see the failure, not a stack trace
        logger.exception("CodeAtlas internal error in %s", getattr(fn, "__name__", fn))
        return {"error": f"internal error: {type(exc).__name__}: {exc}", "log": os.environ.get(
            "CODEATLAS_LOG", str(Path.home() / "codeatlas-mcp-debug.log"))}


async def _run(fn, repo_path, *args, **kwargs):
    """Tools are async and do their blocking work in a thread, so a long test run never freezes the server."""
    return await asyncio.to_thread(_safe, fn, repo_path, *args, **kwargs)


@tool(read_only=False)
async def index_repository(repo_path: str, full: bool = False) -> dict:
    """Build or incrementally refresh the symbol/import/call graph."""
    return await _run(index_repo, repo_path, full)


@tool()
async def find_symbol(repo_path: str, name: str) -> dict:
    """Locate definitions of a function, class, or method."""
    return await _run(analysis.find_symbol, repo_path, name)


@tool()
async def get_symbol_source(repo_path: str, name: str, max_lines: int = 120) -> dict:
    """Return the full source of a function, class, or method (so whole files need not be read)."""
    return await _run(analysis.symbol_source, repo_path, name, max_lines)


@tool()
async def find_references(repo_path: str, symbol: str, limit: int = 60) -> dict:
    """List callers and references of a symbol."""
    return await _run(analysis.find_references, repo_path, symbol, limit)


@tool()
async def get_diff(repo_path: str, rev_range: str | None = None, scope: Scope = "all",
                   fallback_to_last_commit: bool = True, max_chars: int = 20000) -> dict:
    """Show the actual diff of the change being reviewed (uncommitted work, or rev_range)."""
    return await _run(gitutil.diff_text, repo_path, rev_range, scope, fallback_to_last_commit, max_chars)


@tool()
async def impact_analysis(repo_path: str, rev_range: str | None = None, scope: Scope = "all",
                          fallback_to_last_commit: bool = True) -> dict:
    """Analyze changed symbols, affected functions, modules, tests and breaking API changes.
    With no rev_range it looks at uncommitted work (scope: all | staged | unstaged); if the working
    tree is clean it analyses the last commit and says so."""
    return await _run(analysis.impact, repo_path, rev_range, scope, fallback_to_last_commit)


@tool()
async def review_changes(repo_path: str, rev_range: str | None = None, scope: Scope = "all",
                         fallback_to_last_commit: bool = True) -> dict:
    """Calculate change risk and suggest tests (static estimate; run the tests for a real verdict)."""
    return await _run(analysis.review_changes, repo_path, rev_range, scope, fallback_to_last_commit)


@tool(read_only=False)
async def run_affected_tests(repo_path: str, rev_range: str | None = None, timeout: int = 60,
                             scope: Scope = "all", fallback_to_last_commit: bool = True,
                             baseline: bool = False, run_all_if_unmapped: bool = False) -> dict:
    """Run tests affected by changed code. Failures are linked to the changed symbols that likely caused
    them; baseline=true also runs the same tests on the pre-change code to separate new from pre-existing failures."""
    logger.info("run_affected_tests: repo=%s range=%s scope=%s timeout=%s baseline=%s",
                repo_path, rev_range, scope, timeout, baseline)
    result = await _run(runner.run_affected_tests, repo_path, rev_range, timeout, scope,
                        fallback_to_last_commit, baseline, run_all_if_unmapped)
    logger.info("run_affected_tests: status=%s exit_code=%s error=%s",
                result.get("status"), result.get("exit_code"), result.get("error"))
    return result


@tool(read_only=False)
async def review_and_test(repo_path: str, rev_range: str | None = None, timeout: int = 120,
                          scope: Scope = "all", fallback_to_last_commit: bool = True,
                          baseline: bool = False, run_all_if_unmapped: bool = False) -> dict:
    """Review risk, analyse impact and run the affected tests in ONE call, returning a single verdict
    (pass | fail | unverified | no_changes) with a plain-English summary. Prefer this for 'review my changes'."""
    return await _run(workflow.review_and_test, repo_path, rev_range, timeout, scope,
                      fallback_to_last_commit, baseline, run_all_if_unmapped)


@tool()
async def hotspots(repo_path: str, days: int = 90, top: int = 10) -> dict:
    """Find frequently changed and complex files."""
    return await _run(analysis.hotspots, repo_path, days, top)


@tool()
async def architecture_health(repo_path: str) -> dict:
    """Report import cycles, dead-code candidates, and complexity."""
    return await _run(analysis.architecture_health, repo_path)


@mcp.prompt()
def review_my_changes(repo_path: str, rev_range: str = "") -> str:
    target = f" (range {rev_range})" if rev_range else ""
    return (f"Review the changes in {repo_path}{target}. Call review_and_test, then, if there are failures or "
            "breaking API changes, call get_diff and get_symbol_source on the likely cause. "
            "Summarize the verdict, the risk, the likely cause and the fix.")


@mcp.prompt()
def review_last_commit(repo_path: str) -> str:
    return (f"Review the last commit in {repo_path}: call review_and_test with rev_range='HEAD~1..HEAD'. "
            "Summarize the verdict, the risk and any failing tests with their likely cause.")


def main():
    mcp.run()


if __name__ == "__main__":
    main()
