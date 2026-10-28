import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codeatlas import analysis, runner                 # noqa: E402
from codeatlas.cli import main as cli_main             # noqa: E402
from codeatlas.indexer import index_repo               # noqa: E402
from codeatlas.security import resolve_repo            # noqa: E402

GIT = ["git", "-c", "user.name=T", "-c", "user.email=t@e.com"]
FILES = {
    "conftest.py": "",
    "app/__init__.py": "",
    "app/util.py": "def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n\n\ndef unused_helper():\n    return 1\n",
    "app/core.py": ("from app.util import clamp\n\n\ndef score(x):\n    return clamp(x, 0, 100)\n\n\n"
                    "class Engine:\n    def run(self, v):\n        return self.step(v)\n\n"
                    "    def step(self, v):\n        return score(v)\n"),
    "app/api.py": "from app import core\n\n\ndef handler(v):\n    return core.Engine().run(v)\n",
    "app/other.py": "def hello():\n    return 'hi'\n",
    "tests/test_core.py": "from app.core import score\n\n\ndef test_score():\n    assert score(150) == 100\n",
    "tests/test_api.py": "from app.api import handler\n\n\ndef test_handler():\n    assert handler(5) == 5\n",
    "tests/test_other.py": "from app.other import hello\n\n\ndef test_hello():\n    assert hello() == 'hi'\n",
}


def git(repo, *args):
    subprocess.run([*GIT, *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEATLAS_ROOTS", str(tmp_path))
    monkeypatch.setenv("CODEATLAS_HOME", str(tmp_path / "_cache"))
    r = tmp_path / "proj"
    for rel, text in FILES.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text)
    git(r, "init", "-q")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "initial")
    (r / "app/util.py").write_text(FILES["app/util.py"].replace("max(lo, min(x, hi))", "min(max(x, lo), hi)"))
    git(r, "commit", "-qam", "tweak clamp")
    return resolve_repo(str(r))


def test_index_is_incremental(repo):
    first = index_repo(repo)
    assert first["files"] == 9 and first["symbols"] >= 9
    second = index_repo(repo)
    assert second["reparsed"] == 0
    (repo / "app/other.py").write_text("def hello():\n    return 'hello'\n")
    assert index_repo(repo)["reparsed"] == 1


def test_find_symbol_and_references(repo):
    assert analysis.find_symbol(repo, "clamp")["matches"][0]["file"] == "app/util.py"
    refs = analysis.find_references(repo, "app.util.clamp")
    assert any(r["in"] == "app.core.score" for r in refs["references"])


def test_impact_selects_only_dependent_tests(repo):
    r = analysis.impact(repo, "HEAD~1..HEAD")
    assert [s["symbol"] for s in r["changed_symbols"]] == ["app.util.clamp"]
    assert r["affected_tests"] == ["tests/test_api.py", "tests/test_core.py"]
    callers = {f["symbol"] for f in r["affected_functions"]}
    assert {"app.core.score", "app.core.Engine.step", "app.core.Engine.run", "app.api.handler"} <= callers
    assert r["total_test_files"] == 3 and r["untested_changed_symbols"] == []


def test_run_affected_tests_saves_work(repo):
    r = runner.run_affected_tests(repo, "HEAD~1..HEAD")
    assert r["mode"] == "selected" and r["passed"] == 2 and r["failed"] == 0
    assert r["saved_pct"] == 33 and "tests/test_other.py" not in r["selected_test_files"]


def test_uncommitted_change_and_untested_detection(repo):
    (repo / "app/other.py").write_text("def hello():\n    return 'hi'\n\n\ndef brand_new(x):\n    return x * 2\n")
    r = analysis.impact(repo)
    syms = {s["symbol"] for s in r["changed_symbols"]}
    assert "app.other.brand_new" in syms
    assert "app.other.brand_new" in r["untested_changed_symbols"]
    review = analysis.review_changes(repo)
    assert review["risk_score"] > 0 and any("not reached by any test" in x for x in review["reasons"])


def test_config_change_runs_everything(repo):
    (repo / "pytest.ini").write_text("[pytest]\n")
    r = runner.run_affected_tests(repo)
    assert r["mode"] == "all" and r["passed"] == 3


def test_health_and_hotspots(repo):
    h = analysis.architecture_health(repo)
    assert any(d["symbol"] == "app.util.unused_helper" for d in h["dead_code_candidates"])
    assert not any(d["symbol"] == "app.other.hello" for d in h["dead_code_candidates"])
    assert analysis.hotspots(repo)["hotspots"][0]["file"] == "app/util.py"


def test_import_cycle_detected(repo):
    (repo / "app/a.py").write_text("from app import b\n\n\ndef fa():\n    return b.fb\n")
    (repo / "app/b.py").write_text("from app import a\n\n\ndef fb():\n    return a.fa\n")
    assert ["app.a", "app.b"] in analysis.architecture_health(repo)["import_cycles"]


def test_allowlist(repo):
    with pytest.raises(PermissionError):
        resolve_repo("/etc")


def test_cli(repo, capsys):
    cli_main(["impact", "--repo", str(repo), "--range", "HEAD~1..HEAD"])
    assert "app.util.clamp" in json.loads(capsys.readouterr().out)["changed_symbols"][0]["symbol"]


def test_type_checking_and_local_imports_are_not_cycles(repo):
    (repo / "app/c.py").write_text("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from app import d\n\n\ndef fc():\n    return 1\n")
    (repo / "app/d.py").write_text("from app import c\n\n\ndef fd():\n    return c.fc()\n")
    (repo / "app/e.py").write_text("def fe():\n    from app import g\n    return g\n")
    (repo / "app/g.py").write_text("from app import e\n\n\ndef fg():\n    return e.fe()\n")
    cycles = analysis.architecture_health(repo)["import_cycles"]
    assert not any({"app.c", "app.d"} <= set(c) or {"app.e", "app.g"} <= set(c) for c in cycles)


def test_public_api_not_reported_dead(repo):
    (repo / "app/pub.py").write_text("__all__ = ['exported']\n\n\ndef exported():\n    return 1\n\n\ndef hidden():\n    return 2\n")
    (repo / "app/__init__.py").write_text("from app.util import unused_helper as unused_helper\n")
    dead = {d["symbol"] for d in analysis.architecture_health(repo)["dead_code_candidates"]}
    assert "app.pub.hidden" in dead
    assert "app.pub.exported" not in dead and "app.util.unused_helper" not in dead
