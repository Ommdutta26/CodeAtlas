import asyncio
import json

import pytest

from codeatlas import cli, report, server, workflow
from conftest import commit_all, demo


def test_review_and_test_fails_on_the_bug(buggy):
    res = workflow.review_and_test(buggy)
    assert res["verdict"] == "fail"
    assert res["review"]["risk_level"] == "high" and res["review"]["static_risk_level"] == "low"
    assert "shop.pricing.clamp" in res["summary"] and "5 of 7" in res["summary"]
    assert res["tests"]["failed"] == 5 and res["notes"].count(res["notes"][0]) == 1


def test_review_and_test_passes_when_fixed(buggy):
    demo.fix_bug(buggy)
    p = buggy / "shop" / "pricing.py"
    p.write_text(p.read_text().replace("Keep a value", "Keep a number"))
    res = workflow.review_and_test(buggy)
    assert res["verdict"] == "pass" and res["summary"].startswith("PASS: 7")


def test_unverified_when_nothing_covers_the_change(repo):
    (repo / "shop" / "util.py").write_text("def helper(x):\n    return x * 2\n")
    res = workflow.review_and_test(repo)
    assert res["verdict"] == "unverified" and res["review"]["risk_level"] == "medium"


def test_no_changes_verdict(repo):
    assert workflow.review_and_test(repo, fallback=False)["verdict"] == "no_changes"


def test_preexisting_failures_do_not_fail_the_verdict(buggy):
    commit_all(buggy, "bug in HEAD")
    p = buggy / "shop" / "pricing.py"
    p.write_text(p.read_text().replace("Return a discount amount", "Return the discount amount"))
    res = workflow.review_and_test(buggy, baseline=True)
    assert res["verdict"] == "pass_with_preexisting_failures"


def test_markdown_report(buggy):
    md = report.render_markdown(workflow.review_and_test(buggy))
    assert md.startswith("## ❌ CodeAtlas: FAIL") and "`shop.pricing.clamp`" in md and "| `tests.test_pricing" in md


# ----------------------------------------------------------------------------- MCP server
def test_all_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names >= {"index_repository", "find_symbol", "get_symbol_source", "find_references", "get_diff",
                     "impact_analysis", "review_changes", "run_affected_tests", "review_and_test",
                     "hotspots", "architecture_health"}


def test_tools_are_async_and_return_dicts(buggy):
    res = asyncio.run(server.review_and_test(repo_path=str(buggy)))
    assert res["verdict"] == "fail"
    assert asyncio.run(server.get_diff(repo_path=str(buggy), max_chars=50))["truncated"] is True
    assert asyncio.run(server.impact_analysis(repo_path=str(buggy), scope="staged", fallback_to_last_commit=False)
                       )["changed_files"] == []


def test_server_errors_are_structured_not_raised(repo, tmp_path):
    out = asyncio.run(server.review_changes(repo_path=str(repo), rev_range="--output=/tmp/x"))
    assert "Invalid rev_range" in out["error"]
    out = asyncio.run(server.review_changes(repo_path="/etc"))
    assert "outside CODEATLAS_ROOTS" in out["error"]
    out = asyncio.run(server.review_changes(repo_path=str(tmp_path / "missing")))
    assert "error" in out
    out = asyncio.run(server.review_changes(repo_path=str(repo), rev_range="nope..HEAD"))
    assert "error" in out


def test_empty_string_rev_range_means_none(buggy):
    res = asyncio.run(server.impact_analysis(repo_path=str(buggy), rev_range=""))
    assert res["changed_files"] == ["shop/pricing.py"]


def test_parallel_calls_do_not_corrupt_the_index(buggy):
    async def go():
        return await asyncio.gather(*[server.impact_analysis(repo_path=str(buggy)) for _ in range(6)])
    assert all(r["changed_files"] == ["shop/pricing.py"] for r in asyncio.run(go()))


def test_prompts_exist():
    assert "review_and_test" in server.review_my_changes("/x", "HEAD~1..HEAD")
    assert "HEAD~1..HEAD" in server.review_last_commit("/x")


# ----------------------------------------------------------------------------- CLI
def test_cli_check_exit_codes(buggy, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["check", "--repo", str(buggy)])
    assert e.value.code == 1 and json.loads(capsys.readouterr().out)["verdict"] == "fail"
    demo.fix_bug(buggy)
    with pytest.raises(SystemExit) as e:
        cli.main(["check", "--repo", str(buggy), "--no-fallback"])
    assert e.value.code == 0


def test_cli_markdown_and_errors(buggy, capsys):
    with pytest.raises(SystemExit):
        cli.main(["check", "--repo", str(buggy), "--format", "markdown"])
    assert "CodeAtlas: FAIL" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["impact", "--repo", str(buggy), "--range=--evil"])
    assert e.value.code == 2 and "Invalid rev_range" in capsys.readouterr().out


def test_cli_read_only_commands(repo, capsys):
    cli.main(["symbol", "clamp", "--repo", str(repo)])
    assert "shop.pricing.clamp" in capsys.readouterr().out
    cli.main(["source", "clamp", "--repo", str(repo)])
    assert "def clamp(" in capsys.readouterr().out


def test_cli_diff_and_no_symbol_arg(buggy, capsys):
    cli.main(["diff", "--repo", str(buggy)])
    assert "min(minimum, max(value, maximum))" in json.loads(capsys.readouterr().out)["diff"]
    with pytest.raises(SystemExit) as e:
        cli.main(["symbol", "--repo", str(buggy)])
    assert e.value.code == 2
