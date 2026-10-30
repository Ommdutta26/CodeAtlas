import importlib.util
import os
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "make_demo_repo", Path(__file__).resolve().parents[1] / "examples" / "make_demo_repo.py")
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Each test gets its own cache dir, log file and allowed root."""
    monkeypatch.setenv("CODEATLAS_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("CODEATLAS_LOG", str(tmp_path / "codeatlas.log"))
    monkeypatch.setenv("CODEATLAS_ROOTS", str(tmp_path))
    monkeypatch.delenv("CODEATLAS_PYTHON", raising=False)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A clean, committed demo repo."""
    return demo.build(tmp_path / "shop_repo").resolve()


@pytest.fixture
def buggy(repo) -> Path:
    """The demo repo with the clamp() bug introduced but NOT committed."""
    demo.introduce_bug(repo)
    return repo


def commit_all(repo: Path, msg: str = "change") -> None:
    demo.git(repo, "add", ".")
    demo.git(repo, "commit", "-q", "-m", msg)
