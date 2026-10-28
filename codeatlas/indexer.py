"""Parses Python files with `ast` into a SQLite graph: symbols, imports, calls, references.
Incremental: unchanged files (by content hash) are skipped. The DB lives in a cache dir, never in the repo."""
import ast
import hashlib
import os
import sqlite3
from pathlib import Path

from .security import is_test_path

IGNORE = {".git", "node_modules", "venv", ".venv", "__pycache__", ".tox", "build", "dist",
          ".mypy_cache", ".pytest_cache", "site-packages", ".eggs"}
BRANCH = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp,
          ast.comprehension, ast.Assert, ast.BoolOp)
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, hash TEXT, module TEXT, is_test INT);
CREATE TABLE IF NOT EXISTS symbols(qname TEXT, name TEXT, kind TEXT, file TEXT, line INT, end_line INT,
                                   complexity INT, parent TEXT, decorated INT);
CREATE INDEX IF NOT EXISTS sym_file ON symbols(file);
CREATE TABLE IF NOT EXISTS imports(file TEXT, target TEXT, line INT, runtime INT);
CREATE TABLE IF NOT EXISTS edges(file TEXT, caller TEXT, callee_name TEXT, callee_qname TEXT, line INT);
CREATE TABLE IF NOT EXISTS refs(file TEXT, name TEXT, line INT);
"""


SCHEMA_VERSION = "2"


def connect(repo: Path) -> sqlite3.Connection:
    home = Path(os.environ.get("CODEATLAS_HOME", Path.home() / ".cache" / "codeatlas"))
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / (hashlib.sha1(str(repo).encode()).hexdigest()[:16] + ".db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
    if not row or row["v"] != SCHEMA_VERSION:      # old cache from an earlier version: rebuild
        for t in ("files", "symbols", "imports", "edges", "refs"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute("DELETE FROM meta")
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
        conn.commit()
    return conn


def module_name(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "__init__"


def flatten(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class Parser(ast.NodeVisitor):
    def __init__(self, module: str, is_pkg: bool, modules: set[str], tree: ast.Module):
        self.module, self.is_pkg, self.modules = module, is_pkg, modules
        self.binds: dict = {}
        self.imports, self.symbols, self.edges, self.refs = [], [], [], []
        self.tc = False                      # inside `if TYPE_CHECKING:`
        self.scope: list[str] = []
        self.kinds: list[str] = []
        self.funcs: list[str] = []
        self.top = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}

    # ---- imports
    def _runtime(self) -> int:
        """1 if the import runs when the module is imported (module level, not TYPE_CHECKING)."""
        return int(not self.funcs and not self.tc)

    def visit_If(self, node):
        t = node.test
        if (isinstance(t, ast.Name) and t.id == "TYPE_CHECKING") or \
                (isinstance(t, ast.Attribute) and t.attr == "TYPE_CHECKING"):
            prev, self.tc = self.tc, True
            for n in node.body:
                self.visit(n)
            self.tc = prev
            for n in node.orelse:
                self.visit(n)
        else:
            self.generic_visit(node)

    def visit_Assign(self, node):
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets) and \
                isinstance(node.value, (ast.List, ast.Tuple)):
            for e in node.value.elts:          # names listed in __all__ count as used (public API)
                if isinstance(e, ast.Constant) and isinstance(e.value, str):
                    self.refs.append((e.value, node.lineno))
        self.generic_visit(node)

    def _abs(self, mod: str | None, level: int) -> str:
        if level == 0:
            return mod or ""
        pkg = self.module if self.is_pkg else self.module.rpartition(".")[0]
        parts = pkg.split(".") if pkg else []
        if level > 1:
            parts = parts[: max(len(parts) - (level - 1), 0)]
        base = ".".join(parts)
        return f"{base}.{mod}".strip(".") if mod else base

    def visit_Import(self, node):
        for a in node.names:
            parts = a.name.split(".")
            target = next((".".join(parts[:i]) for i in range(len(parts), 0, -1)
                           if ".".join(parts[:i]) in self.modules), None)
            if target:
                self.imports.append((target, node.lineno, self._runtime()))
                if a.asname:
                    self.binds[a.asname] = ("module", a.name)
                else:
                    self.binds[parts[0]] = ("module", parts[0])

    def visit_ImportFrom(self, node):
        base = self._abs(node.module, node.level)
        for a in node.names:
            if a.name == "*":
                continue
            self.refs.append((a.name, node.lineno))     # re-exports / imports count as uses
            sub = f"{base}.{a.name}".strip(".")
            local = a.asname or a.name
            if sub in self.modules:
                self.binds[local] = ("module", sub)
                self.imports.append((sub, node.lineno, self._runtime()))
            elif base in self.modules:
                self.binds[local] = ("symbol", base, a.name)
                self.imports.append((base, node.lineno, self._runtime()))

    # ---- definitions
    def _q(self, name: str) -> str:
        return ".".join([self.module, *self.scope, name])

    def _func(self, node):
        in_class = bool(self.kinds) and self.kinds[-1] == "class"
        qn = self._q(node.name)
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        cx = 1 + sum(isinstance(n, BRANCH) for n in ast.walk(node))
        parent = ".".join([self.module, *self.scope]) if self.scope else None
        self.symbols.append((qn, node.name, "method" if in_class else "function", start,
                             node.end_lineno, cx, parent, int(bool(node.decorator_list))))
        self.scope.append(node.name); self.kinds.append("func"); self.funcs.append(qn)
        self.generic_visit(node)
        self.scope.pop(); self.kinds.pop(); self.funcs.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _func

    def visit_ClassDef(self, node):
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        parent = ".".join([self.module, *self.scope]) if self.scope else None
        self.symbols.append((self._q(node.name), node.name, "class", start, node.end_lineno, 0, parent,
                             int(bool(node.decorator_list))))
        self.scope.append(node.name); self.kinds.append("class")
        self.generic_visit(node)
        self.scope.pop(); self.kinds.pop()

    # ---- calls and references
    def _resolve(self, dotted: str) -> str | None:
        parts = dotted.split(".")
        head = parts[0]
        if head in self.binds:
            b = self.binds[head]
            base = b[1] if b[0] == "module" else f"{b[1]}.{b[2]}"
            return ".".join([base, *parts[1:]])
        if head in ("self", "cls") and len(parts) == 2 and "class" in self.kinds:
            idx = len(self.kinds) - 1 - self.kinds[::-1].index("class")
            return ".".join([self.module, *self.scope[: idx + 1], parts[1]])
        if head in self.top:
            return f"{self.module}.{dotted}"
        return None

    def visit_Call(self, node):
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if name:
            dotted = flatten(f)
            caller = self.funcs[-1] if self.funcs else f"{self.module}.<module>"
            self.edges.append((caller, name, self._resolve(dotted) if dotted else None, node.lineno))
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.refs.append((node.id, node.lineno))

    def visit_Attribute(self, node):
        self.refs.append((node.attr, node.lineno))
        self.generic_visit(node)


def _py_files(repo: Path) -> list[str]:
    out = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in IGNORE and not d.endswith(".egg-info")]
        out += [os.path.relpath(os.path.join(root, f), repo).replace(os.sep, "/")
                for f in files if f.endswith(".py")]
    return sorted(out)


def index_repo(repo: Path, full: bool = False) -> dict:
    conn = connect(repo)
    files = _py_files(repo)
    modules = {module_name(f) for f in files}
    mod_hash = hashlib.sha1("\n".join(sorted(modules)).encode()).hexdigest()
    prev = conn.execute("SELECT v FROM meta WHERE k='mods'").fetchone()
    force = full or not prev or prev["v"] != mod_hash
    existing = {r["path"]: r["hash"] for r in conn.execute("SELECT path, hash FROM files")}
    removed = set(existing) - set(files)
    reparsed, errors = 0, []

    def purge(path):
        for t in ("files", "symbols", "imports", "edges", "refs"):
            conn.execute(f"DELETE FROM {t} WHERE {'path' if t == 'files' else 'file'}=?", (path,))

    for path in removed:
        purge(path)
    for rel in files:
        data = (repo / rel).read_bytes()
        h = hashlib.sha1(data).hexdigest()
        if not force and existing.get(rel) == h:
            continue
        purge(rel)
        mod = module_name(rel)
        try:
            tree = ast.parse(data, filename=rel)
        except (SyntaxError, ValueError):
            errors.append(rel)
            conn.execute("INSERT INTO files VALUES (?,?,?,?)", (rel, h, mod, int(is_test_path(rel))))
            continue
        p = Parser(mod, rel.endswith("__init__.py"), modules, tree)
        p.visit(tree)
        conn.execute("INSERT INTO files VALUES (?,?,?,?)", (rel, h, mod, int(is_test_path(rel))))
        conn.executemany("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?)", [(q, n, k, rel, a, b, c, d, e)
                         for q, n, k, a, b, c, d, e in p.symbols])
        conn.executemany("INSERT INTO imports VALUES (?,?,?,?)", [(rel, t, ln, rt) for t, ln, rt in p.imports])
        conn.executemany("INSERT INTO edges VALUES (?,?,?,?,?)", [(rel, *e) for e in p.edges])
        conn.executemany("INSERT INTO refs VALUES (?,?,?)", [(rel, n, ln) for n, ln in p.refs])
        reparsed += 1
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('mods', ?)", (mod_hash,))
    conn.commit()
    count = lambda t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
    return {"files": len(files), "reparsed": reparsed, "removed": len(removed), "symbols": count("symbols"),
            "calls": count("edges"), "imports": count("imports"), "parse_errors": errors,
            "full_reindex": bool(force)}
