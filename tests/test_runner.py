import os
import sys
import time

from codeatlas import runner
from conftest import commit_all, demo


def test_bug_is_detected_and_attributed(buggy):
    res = runner.run_affected_tests(buggy)
    assert res["status"] == "failed" and (res["passed"], res["failed"]) == (2, 5)
    assert res["likely_causes"][0]["symbol"] == "shop.pricing.clamp"
    assert res["likely_causes"][0]["failing_tests"] == 5 and res["unlinked_failures"] == 0
    f = next(x for x in res["failures"] if "test_clamp_keeps" in x["test"])
    assert "assert 0 == 5" in f["message"] and "tests/test_pricing.py" in " ".join(f["locations"])
    assert f["link"] == "call-graph" and f["linked_changes"][0]["symbol"] == "shop.pricing.clamp"
    assert res["selected_test_files"] == ["tests/test_checkout.py", "tests/test_orders.py", "tests/test_pricing.py"]


def test_fixed_code_passes(buggy):
    demo.fix_bug(buggy)
    (buggy / "shop" / "pricing.py").write_text(
        (buggy / "shop" / "pricing.py").read_text().replace("Keep a value", "Keep a number"))
    res = runner.run_affected_tests(buggy)
    assert res["status"] == "passed" and res["failed"] == 0 and res["passed"] == 7


def test_clean_tree_runs_last_commit_by_fallback(buggy):
    commit_all(buggy, "bug")
    res = runner.run_affected_tests(buggy)
    assert res["status"] == "failed" and "HEAD~1..HEAD" in res["range"]
    assert runner.run_affected_tests(buggy, fallback=False)["status"] == "no_changes"


def test_no_changes(repo):
    res = runner.run_affected_tests(repo, fallback=False)
    assert res["status"] == "no_changes" and res["mode"] == "none"


def test_unmapped_change_and_run_all_option(repo):
    (repo / "shop" / "util.py").write_text("def helper(x):\n    return x * 2\n")
    res = runner.run_affected_tests(repo)
    assert res["status"] == "no_tests" and "run_all_if_unmapped" in res["hint"]
    res = runner.run_affected_tests(repo, all_if_unmapped=True)
    assert res["status"] == "passed" and res["mode"] == "all" and res["passed"] == 7


def test_config_change_runs_everything(repo):
    (repo / "pyproject.toml").write_text("[tool.x]\na = 1\n")
    res = runner.run_affected_tests(repo)
    assert res["mode"] == "all" and res["selected_test_files"] == "all" and res["status"] == "passed"


def test_baseline_separates_new_from_preexisting(buggy):
    commit_all(buggy, "bug is now in HEAD")                      # the bug pre-dates the next change
    p = buggy / "shop" / "pricing.py"
    p.write_text(p.read_text().replace("Return a discount amount", "Return the discount amount"))
    res = runner.run_affected_tests(buggy, baseline=True)
    assert res["status"] == "failed"
    assert (res["new_failures"], res["preexisting_failures"]) == (0, 5)
    assert {f["vs_base"] for f in res["failures"]} == {"pre-existing"}
    assert "worktree" in " ".join(res["notes"])
    assert demo.git(buggy, "worktree", "list").stdout.strip().count("\n") == 0   # temp worktree cleaned up


def test_baseline_marks_regressions_as_new(buggy):
    res = runner.run_affected_tests(buggy, baseline=True)       # HEAD is good, working tree is bad
    assert res["new_failures"] == 5 and res["preexisting_failures"] == 0


def test_timeout_kills_the_process_tree(repo):
    (repo / "tests" / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(30)\n")
    started = time.time()
    res = runner.run_affected_tests(repo, timeout=3)
    assert res["status"] == "timeout" and "timed out" in res["error"]
    assert time.time() - started < 20


def test_collection_error_is_reported_as_error_or_failed(repo):
    (repo / "tests" / "test_broken.py").write_text("import module_that_does_not_exist\n\ndef test_x():\n    pass\n")
    res = runner.run_affected_tests(repo)
    assert res["status"] in ("error", "failed") and (res["errors"] + res["failed"] >= 1 or res["stderr_tail"] or res["stdout_tail"])


def test_pytest_missing_in_interpreter_gives_actionable_error(repo, monkeypatch):
    fake = repo / "fakepy.sh"
    fake.write_text("#!/bin/sh\necho 'No module named pytest' >&2\nexit 1\n")
    fake.chmod(0o755)
    (repo / "shop" / "pricing.py").write_text((repo / "shop" / "pricing.py").read_text() + "\n# touch\n")
    monkeypatch.setenv("CODEATLAS_PYTHON", str(fake))
    res = runner.run_affected_tests(repo)
    assert res["status"] == "error" and "No module named pytest" in res["stderr_tail"]
    assert any("CODEATLAS_PYTHON" in n for n in res["notes"])


def test_project_venv_is_preferred(repo):
    venv_py = repo / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.symlink_to(sys.executable)
    assert runner._python_for(repo) == str(venv_py)
    assert runner._python_for(repo.parent) == sys.executable


def test_user_pytest_addopts_do_not_leak_in(buggy, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-x --maxfail=1")        # would stop after the first failure
    res = runner.run_affected_tests(buggy)
    assert res["failed"] == 5
