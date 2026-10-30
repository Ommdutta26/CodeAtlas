import pytest

from codeatlas import gitutil
from conftest import commit_all, demo


def test_uncommitted_change_is_found(buggy):
    cs = gitutil.collect(buggy)
    assert "shop/pricing.py" in cs.ranges and not cs.fallback_used
    assert cs.label == "working tree vs HEAD"


def test_clean_tree_falls_back_to_last_commit(buggy):
    commit_all(buggy, "bug")
    cs = gitutil.collect(buggy)
    assert cs.fallback_used and "shop/pricing.py" in cs.ranges
    assert "last commit" in cs.label and "clean" in cs.notes[0]


def test_fallback_can_be_disabled(buggy):
    commit_all(buggy, "bug")
    cs = gitutil.collect(buggy, fallback=False)
    assert cs.empty and "rev_range" in cs.notes[0]


def test_explicit_range(buggy):
    commit_all(buggy, "bug")
    cs = gitutil.collect(buggy, "HEAD~1..HEAD")
    assert set(cs.ranges) == {"shop/pricing.py"} and cs.base == "HEAD~1" and cs.head == "HEAD"


def test_scopes_staged_vs_unstaged(buggy):
    assert "shop/pricing.py" in gitutil.collect(buggy, scope="unstaged").ranges
    assert gitutil.collect(buggy, scope="staged", fallback=False).empty
    demo.git(buggy, "add", ".")                       # what `git add .` does
    assert "shop/pricing.py" in gitutil.collect(buggy, scope="staged").ranges
    assert gitutil.collect(buggy, scope="unstaged").empty
    assert "shop/pricing.py" in gitutil.collect(buggy, scope="all").ranges


def test_untracked_and_deleted_files(repo):
    (repo / "shop" / "new.py").write_text("def f():\n    return 1\n")
    (repo / "shop" / "orders.py").unlink()
    cs = gitutil.collect(repo)
    assert cs.ranges["shop/new.py"] == [(1, gitutil.WHOLE_FILE)]
    assert cs.deleted == ["shop/orders.py"]


def test_removed_line_that_looks_like_a_diff_header_is_not_a_deletion(repo):
    (repo / "notes.txt").write_text("keep\n-- sql comment\nkeep2\n")
    commit_all(repo, "notes")
    (repo / "notes.txt").write_text("keep\nkeep2\n")      # removes a line that renders as '--- sql comment'
    cs = gitutil.collect(repo)
    assert cs.deleted == [] and "notes.txt" in cs.ranges


@pytest.mark.parametrize("bad", ["--output=/tmp/x", "-p", "a b", "HEAD;rm -rf", "x" * 300, "$(id)"])
def test_rev_range_is_validated(repo, bad):
    with pytest.raises(ValueError):
        gitutil.collect(repo, bad)


@pytest.mark.parametrize("ok", ["HEAD~1..HEAD", "main..HEAD", "origin/main...HEAD", "abc1234", "HEAD@{1}", ""])
def test_valid_rev_ranges_pass_validation(ok):
    gitutil.validate_rev_range(ok)


def test_unknown_revision_is_a_clean_error(repo):
    with pytest.raises(ValueError):
        gitutil.collect(repo, "no_such_branch..HEAD")


def test_not_a_git_repo(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(ValueError, match="Not a git repository"):
        gitutil.collect(tmp_path / "plain")


def test_diff_text_and_truncation(buggy):
    out = gitutil.diff_text(buggy, max_chars=60)
    assert out["truncated"] and out["files"] == ["shop/pricing.py"]
    full = gitutil.diff_text(buggy)
    assert "min(minimum, max(value, maximum))" in full["diff"]


def test_diff_text_includes_untracked(repo):
    (repo / "shop" / "new.py").write_text("x = 1\n")
    assert "+x = 1" in gitutil.diff_text(repo)["diff"]


def test_read_at_sides(buggy):
    assert "max(minimum, min(" in gitutil.read_at(buggy, "HEAD", "shop/pricing.py")
    assert "min(minimum, max(" in gitutil.read_at(buggy, None, "shop/pricing.py")
    assert gitutil.read_at(buggy, "HEAD", "nope.py") is None
    assert demo.BAD_CLAMP.strip() not in gitutil.read_at(buggy, gitutil.INDEX, "shop/pricing.py")


def test_paths_with_spaces_and_unicode(repo):
    (repo / "shop" / "my module.py").write_text("def f():\n    return 1\n")
    (repo / "shop" / "módulo.py").write_text("def g():\n    return 2\n")
    commit_all(repo, "odd names")
    (repo / "shop" / "my module.py").write_text("def f():\n    return 11\n")
    (repo / "shop" / "módulo.py").write_text("def g():\n    return 22\n")
    cs = gitutil.collect(repo)
    assert set(cs.ranges) == {"shop/my module.py", "shop/módulo.py"}


def test_rename_is_reported_as_delete_plus_add(repo):
    demo.git(repo, "mv", "shop/orders.py", "shop/purchases.py")
    cs = gitutil.collect(repo)
    assert cs.deleted == ["shop/orders.py"] and "shop/purchases.py" in cs.ranges


def test_new_file_has_no_old_side(repo):
    (repo / "shop" / "fresh.py").write_text("def h():\n    return 3\n")
    demo.git(repo, "add", ".")
    cs = gitutil.collect(repo, scope="staged")
    assert cs.ranges["shop/fresh.py"] == [(1, 2)]
    assert gitutil.read_at(repo, cs.base, "shop/fresh.py") is None


def test_repository_without_commits(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    demo.git(root, "init", "-q")
    (root / "a.py").write_text("def a():\n    return 1\n")
    demo.git(root, "add", ".")
    (root / "b.py").write_text("x = 1\n")
    cs = gitutil.collect(root)
    assert set(cs.ranges) == {"a.py", "b.py"} and cs.base is None
