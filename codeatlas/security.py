import os
from pathlib import Path


def resolve_repo(path: str) -> Path:
    roots = [Path(p).expanduser().resolve()
             for p in os.environ.get("CODEATLAS_ROOTS", str(Path.cwd())).split(os.pathsep) if p]
    p = Path(path).expanduser().resolve()
    if not any(p == r or r in p.parents for r in roots):
        raise PermissionError(f"{p} is outside CODEATLAS_ROOTS")
    if not p.is_dir():
        raise ValueError(f"Not a directory: {path}")
    return p


def is_test_path(rel: str) -> bool:
    low = "/" + rel.lower().replace("\\", "/")
    name = low.rsplit("/", 1)[-1]
    return "/tests/" in low or "/test/" in low or name.startswith("test_") or name.endswith("_test.py")
