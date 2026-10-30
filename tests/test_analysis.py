import pytest

from codeatlas import analysis
from conftest import commit_all, demo


def test_impact_finds_clamp_and_its_dependents(buggy):
    imp = analysis.impact(buggy)
    assert [c["symbol"] for c in imp["changed_symbols"]] == ["shop.pricing.clamp"]
    chain = [f["symbol"] for f in imp["affected_functions"] if not f["symbol"].startswith("tests.")]
    assert chain == ["shop.pricing.calculate_discount", "shop.pricing.discounted_total",
                     "shop.checkout.checkout_total", "shop.orders.create_order"]
    assert imp["affected_tests"] == ["tests/test_checkout.py", "tests/test_orders.py", "tests/test_pricing.py"]
    assert imp["total_test_files"] == 3                  # tests/__init__.py is not a test file
    assert imp["untested_changed_symbols"] == []


def test_impact_on_committed_change_via_range_and_fallback(buggy):
    commit_all(buggy)
    assert analysis.impact(buggy, "HEAD~1..HEAD")["changed_files"] == ["shop/pricing.py"]
    fb = analysis.impact(buggy)
    assert fb["fallback_used"] and fb["changed_files"] == ["shop/pricing.py"]
    assert analysis.impact(buggy, fallback=False)["changed_files"] == []


def test_review_reports_reasons_and_score(buggy):
    r = analysis.review_changes(buggy)
    assert r["risk_level"] == "low" and r["risk_score"] >= 10
    assert any("dependent modules" in x for x in r["reasons"])
    assert any("production functions call" in x for x in r["reasons"])


def test_breaking_signature_change_lists_callers(repo):
    p = repo / "shop" / "pricing.py"
    p.write_text(p.read_text().replace("def clamp(value: float, minimum: float, maximum: float)",
                                       "def clamp(value: float, low: float, high: float)")
                 .replace("max(minimum, min(value, maximum))", "max(low, min(value, high))"))
    api = analysis.impact(repo)["api_changes"]
    ch = api["signature_changed"][0]
    assert ch["symbol"] == "shop.pricing.clamp" and ch["breaking"] is True
    assert any(c["in"] == "shop.pricing.calculate_discount" for c in ch["callers"])
    assert analysis.review_changes(repo)["breaking_api_changes"] == ["shop.pricing.clamp"]


def test_non_breaking_signature_change(repo):
    p = repo / "shop" / "pricing.py"
    p.write_text(p.read_text().replace("maximum: float) -> float:", "maximum: float, strict: bool = False) -> float:"))
    ch = analysis.impact(repo)["api_changes"]["signature_changed"][0]
    assert ch["breaking"] is False


def test_removed_function_with_remaining_callers(repo):
    p = repo / "shop" / "pricing.py"
    text = p.read_text()
    p.write_text(text[: text.index("def discounted_total")])         # delete discounted_total
    api = analysis.impact(repo)["api_changes"]
    removed = {r["symbol"]: r for r in api["removed"]}
    assert "shop.pricing.discounted_total" in removed
    assert any(c["in"] == "shop.checkout.checkout_total" for c in removed["shop.pricing.discounted_total"]["callers"])
    assert api["breaking_count"] >= 1


def test_deleted_file_is_reported_with_callers(repo):
    (repo / "shop" / "pricing.py").unlink()
    imp = analysis.impact(repo)
    assert imp["deleted_files"] == ["shop/pricing.py"]
    assert {r["symbol"] for r in imp["api_changes"]["removed"]} >= {"shop.pricing.clamp", "shop.pricing.discounted_total"}


def test_untested_new_function_raises_risk(repo):
    (repo / "shop" / "util.py").write_text("def helper(x):\n    return x * 2\n")
    imp = analysis.impact(repo)
    assert "shop.util.helper" in imp["untested_changed_symbols"]
    assert any("not reached by any test" in r for r in analysis.review_changes(repo)["reasons"])


def test_config_change_is_flagged(repo):
    (repo / "pyproject.toml").write_text("[tool.x]\na = 1\n")
    assert analysis.impact(repo)["config_files_changed"] == ["pyproject.toml"]


def test_find_symbol_source_and_references(repo):
    src = analysis.symbol_source(repo, "clamp")
    assert src["matches"][0]["source"].startswith("def clamp(") and not src["matches"][0]["truncated"]
    assert "error" in analysis.symbol_source(repo, "nope")
    refs = analysis.find_references(repo, "shop.pricing.clamp")
    assert any(r["in"] == "shop.pricing.calculate_discount" for r in refs["references"])
    assert analysis.find_symbol(repo, "clamp")["matches"][0]["symbol"] == "shop.pricing.clamp"


def test_hotspots_and_health(repo):
    assert analysis.hotspots(repo)["hotspots"][0]["file"].startswith("shop/")
    assert analysis.architecture_health(repo)["import_cycles"] == []


def test_import_cycle_detected_iteratively(repo):
    (repo / "shop" / "a.py").write_text("from . import b\n\ndef fa():\n    return b.fb()\n")
    (repo / "shop" / "b.py").write_text("from . import a\n\ndef fb():\n    return a.fa()\n")
    assert analysis.architecture_health(repo)["import_cycles"] == [["shop.a", "shop.b"]]


def test_long_import_chain_does_not_hit_recursion_limit(tmp_path):
    root = demo.build(tmp_path / "chain")
    n = 1500
    for i in range(n):
        nxt = f"from . import m{i + 1}\n" if i < n - 1 else ""
        (root / "shop" / f"m{i}.py").write_text(nxt + f"def f{i}():\n    return {i}\n")
    assert analysis.architecture_health(root)["import_cycles"] == []


def test_affected_functions_truncation_flag(repo):
    imp = analysis.impact(repo)
    assert imp["affected_functions_truncated"] is False and imp["affected_functions_total"] == len(imp["affected_functions"])


@pytest.mark.parametrize("scope", ["all", "staged", "unstaged"])
def test_scopes_flow_through_impact(buggy, scope):
    if scope == "staged":
        demo.git(buggy, "add", ".")
    imp = analysis.impact(buggy, scope=scope)
    assert imp["changed_files"] == ["shop/pricing.py"] and imp["scope"] == scope
