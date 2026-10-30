"""Static impact analysis on top of the SQLite call/import graph."""
from __future__ import annotations

import ast
import threading

from . import gitutil
from .graph import Graph
from .indexer import connect, index_repo, module_name
from .security import is_test_path  # noqa: F401  (re-exported for older callers)

CONFIG_FILES = ("conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "setup.py")
MAX_AFFECTED = 100

_LOCKS: dict[str, threading.Lock] = {}


def build_graph(repo) -> Graph:
    """Refresh the index (incremental) and load it. Serialised per repo so parallel tool calls
    cannot fight over the SQLite cache."""
    lock = _LOCKS.setdefault(str(repo), threading.Lock())
    with lock:
        index_repo(repo)
        conn = connect(repo)
        try:
            return Graph(conn)
        finally:
            conn.close()


_graph = build_graph


def _is_config(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in CONFIG_FILES or (name.startswith("requirements") and name.endswith(".txt"))


def _match(g: Graph, name: str) -> list:
    return [s for s in g.symbols
            if s["qname"] == name or s["name"] == name or s["qname"].endswith("." + name)]


# --------------------------------------------------------------------------- API change detection
def _params(a: ast.arguments) -> list:
    """[(name, has_default, kind)] for every parameter."""
    pos = [*a.posonlyargs, *a.args]
    n_def = len(a.defaults)
    out = [(p.arg, i >= len(pos) - n_def, "pos") for i, p in enumerate(pos)]
    if a.vararg:
        out.append(("*" + a.vararg.arg, True, "var"))
    out += [(p.arg, d is not None, "kw") for p, d in zip(a.kwonlyargs, a.kw_defaults)]
    if a.kwarg:
        out.append(("**" + a.kwarg.arg, True, "var"))
    return out


def _signatures(source: str):
    """{local qualified name: info} for every function/class in a source text; None on syntax error."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    out: dict = {}

    def walk(node, scope):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = ".".join([*scope, ch.name])
                sig = f"({ast.unparse(ch.args)})" + (f" -> {ast.unparse(ch.returns)}" if ch.returns else "")
                out[q] = {"kind": "function", "params": _params(ch.args), "sig": sig}
                walk(ch, [*scope, ch.name])
            elif isinstance(ch, ast.ClassDef):
                q = ".".join([*scope, ch.name])
                out[q] = {"kind": "class", "params": [], "sig": ""}
                walk(ch, [*scope, ch.name])
            else:
                walk(ch, scope)

    walk(tree, [])
    return out


def _breaking(old: list, new: list) -> bool:
    old_names, new_names = [p[0] for p in old], [p[0] for p in new]
    if any(n not in new_names for n in old_names):                        # removed or renamed parameter
        return True
    if any(not p[1] and p[0] not in old_names for p in new):              # new required parameter
        return True
    old_default = {p[0]: p[1] for p in old}
    if any(p[0] in old_default and old_default[p[0]] and not p[1] for p in new):   # default removed
        return True
    old_pos = [p[0] for p in old if p[2] == "pos"]
    new_pos = [p[0] for p in new if p[2] == "pos" and p[0] in old_pos]
    return new_pos != old_pos                                             # positional order changed


def _callers(g: Graph, q: str, gone_files: set) -> list:
    seen, out = set(), []
    for e, conf in [*g.callers_of(q), *g.dangling_callers(q)]:
        key = (e["file"], e["line"])
        if key in seen or e["file"] in gone_files:
            continue
        seen.add(key)
        out.append({"file": e["file"], "line": e["line"], "in": e["caller"], "confidence": conf})
    return out[:10]


def _api_changes(repo, g: Graph, cs: gitutil.ChangeSet) -> dict:
    paths = list(dict.fromkeys([f for f in cs.ranges if f.endswith(".py")][:200]
                               + [f for f in cs.deleted if f.endswith(".py")]))
    gone = set(cs.deleted)
    removed, added, changed = [], [], []
    with gitutil.open_repo(repo) as r:
        for path in paths:
            mod = module_name(path)
            old_src = gitutil.read_at(repo, cs.base, path, repo=r) if cs.base is not None else None
            new_src = None if path in gone else gitutil.read_at(repo, cs.head, path, repo=r)
            old = {} if old_src is None else _signatures(old_src)
            new = {} if new_src is None else _signatures(new_src)
            if old is None or new is None:          # a syntax error on one side: nothing reliable to compare
                continue
            for local, o in old.items():
                q = f"{mod}.{local}"
                n = new.get(local)
                if n is None:
                    removed.append({"symbol": q, "kind": o["kind"], "callers": _callers(g, q, gone)})
                elif o["kind"] == n["kind"] == "function" and o["sig"] != n["sig"]:
                    changed.append({"symbol": q, "old": o["sig"], "new": n["sig"],
                                    "breaking": _breaking(o["params"], n["params"]),
                                    "callers": _callers(g, q, gone)})
            added += [f"{mod}.{local}" for local in new if local not in old]
    added_short = {}
    for q in added:
        added_short.setdefault(q.rsplit(".", 1)[-1], []).append(q)
    for item in removed:                           # a function moved to another module is not "gone"
        moved = added_short.get(item["symbol"].rsplit(".", 1)[-1])
        if moved:
            item["possibly_moved_to"] = moved[:3]
    breaking = [c for c in changed if c["breaking"]] + [r for r in removed if r["callers"]]
    return {"removed": removed[:50], "signature_changed": changed[:50], "added": added[:100],
            "breaking_count": len(breaking), "truncated": len(removed) > 50 or len(changed) > 50 or len(added) > 100}


# --------------------------------------------------------------------------- impact / review
def impact(repo, rev_range=None, scope: str = "all", fallback: bool = True,
           g: Graph | None = None, cs: gitutil.ChangeSet | None = None) -> dict:
    g = g or build_graph(repo)
    cs = cs or gitutil.collect(repo, rev_range, scope, fallback)
    changed_modules, changed_symbols, other_files, config = set(), {}, [], []
    for f, rs in cs.ranges.items():
        if _is_config(f):
            config.append(f)
        if f in g.files:
            names, _ = g.symbols_in_ranges(f, rs)
            changed_symbols.update({n: f for n in names})
            changed_modules.add(g.files[f]["module"])
        elif not f.endswith(".py"):
            other_files.append(f)
    for f in cs.deleted:
        if f.endswith(".py"):
            changed_modules.add(module_name(f))
    starts = [q for q in changed_symbols if not q.endswith(".<module>")]
    closures = {q: g.call_closure([q], max_depth=6, cap=1000) for q in starts}
    merged: dict = {}
    for closure in closures.values():
        for c in closure:
            cur = merged.get(c["symbol"])
            if cur is None or c["depth"] < cur["depth"]:
                merged[c["symbol"]] = c
    callers = sorted(merged.values(), key=lambda c: (c["depth"], c["symbol"]))
    affected_modules = g.import_closure(changed_modules)
    affected_tests = sorted({g.module_file[m] for m in affected_modules
                             if g.is_test_module(m) and m in g.module_file})
    funcs = [q for q in starts if q in g.by_q and g.by_q[q][0]["kind"] != "class"]
    untested = [q for q in funcs
                if not any(g.files.get(c["file"], {}).get("is_test") for c in closures.get(q, []))]

    def sym_info(q, f):
        s = g.by_q[q][0] if q in g.by_q else None
        return {"symbol": q, "file": f, "complexity": s["complexity"] if s else 0,
                **({"line": s["line"], "end_line": s["end_line"]} if s else {})}

    notes = [*cs.notes,
             "Call resolution is static/best-effort: dynamic dispatch and getattr are not tracked.",
             "Entries with confidence 'name-match' are matched by function name only."]
    return {
        "range": cs.label, "scope": cs.scope, "fallback_used": cs.fallback_used,
        "changed_files": sorted(cs.ranges), "deleted_files": cs.deleted, "config_files_changed": config,
        "other_files_changed": other_files,
        "changed_symbols": [sym_info(q, f) for q, f in changed_symbols.items()],
        "affected_functions": callers[:MAX_AFFECTED], "affected_functions_total": len(callers),
        "affected_functions_truncated": len(callers) > MAX_AFFECTED,
        "affected_modules": sorted(m for m in affected_modules if m not in changed_modules),
        "affected_tests": affected_tests,
        "total_test_files": sum(1 for p, f in g.files.items()
                                if f["is_test"] and not p.endswith(("__init__.py", "conftest.py"))),
        "untested_changed_symbols": untested,
        "api_changes": _api_changes(repo, g, cs),
        "notes": notes,
    }


def review_changes(repo, rev_range=None, scope: str = "all", fallback: bool = True,
                   g: Graph | None = None, cs: gitutil.ChangeSet | None = None,
                   imp: dict | None = None) -> dict:
    g = g or build_graph(repo)
    cs = cs or gitutil.collect(repo, rev_range, scope, fallback)
    imp = imp or impact(repo, g=g, cs=cs)
    hot = {h["file"]: h for h in hotspots(repo, top=1000, g=g)["hotspots"]}
    top_quarter = set(list(hot)[: max(1, len(hot) // 4)]) if hot else set()
    reasons, score = [], 0

    n_mod = len(imp["affected_modules"])
    if n_mod:
        score += min(n_mod, 20)
        reasons.append(f"{n_mod} dependent modules may be affected")
    for f in imp["changed_files"]:
        if f in top_quarter:
            score += 5
            reasons.append(f"{f} is a churn/complexity hotspot")
    if imp["untested_changed_symbols"]:
        score += 8 * len(imp["untested_changed_symbols"])
        reasons.append(f"{len(imp['untested_changed_symbols'])} changed functions are not reached by any test")
    cx = sum(c["complexity"] for c in imp["changed_symbols"])
    if cx:
        score += cx // 5
        reasons.append(f"changed code has total complexity {cx}")
    prod_callers = sum(1 for c in imp["affected_functions"] if not g.files.get(c["file"], {}).get("is_test"))
    if prod_callers:
        score += min(prod_callers, 30) // 3
        reasons.append(f"{prod_callers} production functions call the changed code (transitively)")
    api = imp["api_changes"]
    broken = [c["symbol"] for c in api["signature_changed"] if c["breaking"]] + \
             [r["symbol"] for r in api["removed"] if r["callers"]]
    if broken:
        score += min(12 * len(broken), 36)
        reasons.append(f"{len(broken)} breaking API change(s): {', '.join(broken[:5])}")
    if imp["config_files_changed"]:
        score += 10
        reasons.append(f"config/dependency files changed: {imp['config_files_changed']}")

    level = "low" if score < 15 else "medium" if score < 35 else "high"
    return {"risk_level": level, "risk_score": score, "reasons": reasons, "range": imp["range"],
            "suggested_tests": imp["affected_tests"], "changed_symbols": imp["changed_symbols"],
            "breaking_api_changes": broken, "notes": imp["notes"]}


# --------------------------------------------------------------------------- lookups
def find_symbol(repo, name: str, g: Graph | None = None) -> dict:
    g = g or build_graph(repo)
    out = []
    for s in _match(g, name)[:20]:
        lines = (repo / s["file"]).read_text(encoding="utf-8", errors="replace").splitlines()
        out.append({"symbol": s["qname"], "kind": s["kind"], "file": s["file"], "line": s["line"],
                    "complexity": s["complexity"],
                    "snippet": "\n".join(lines[s["line"] - 1: min(s["end_line"], s["line"] + 11)])})
    return {"matches": out}


def symbol_source(repo, name: str, max_lines: int = 120, g: Graph | None = None) -> dict:
    """Full source of a function/class, so a caller never needs to read whole files."""
    g = g or build_graph(repo)
    out = []
    for s in _match(g, name)[:5]:
        lines = (repo / s["file"]).read_text(encoding="utf-8", errors="replace").splitlines()
        seg = lines[s["line"] - 1: s["end_line"]]
        out.append({"symbol": s["qname"], "kind": s["kind"], "file": s["file"], "line": s["line"],
                    "end_line": s["end_line"], "source": "\n".join(seg[:max_lines]),
                    "truncated": len(seg) > max_lines})
    return {"matches": out} if out else {"error": f"Symbol '{name}' not found", "matches": []}


def find_references(repo, symbol: str, limit: int = 60, g: Graph | None = None) -> dict:
    g = g or build_graph(repo)
    defs = [s for s in g.symbols if s["qname"] == symbol or s["qname"].endswith("." + symbol) or s["name"] == symbol]
    if not defs:
        return {"error": f"Symbol '{symbol}' not found", "references": []}
    refs, seen = [], set()
    for d in defs:
        for e, conf in g.callers_of(d["qname"]):
            key = (e["file"], e["line"])
            if key not in seen:
                seen.add(key)
                refs.append({"file": e["file"], "line": e["line"], "in": e["caller"], "kind": "call", "confidence": conf})
    names = {d["name"] for d in defs}
    for r in g.refs:
        key = (r["file"], r["line"])
        if r["name"] in names and key not in seen and 0 < len(g.by_short[r["name"]]) <= 3:
            seen.add(key)
            refs.append({"file": r["file"], "line": r["line"], "kind": "reference", "confidence": "name-match"})
    return {"definitions": [d["qname"] for d in defs], "count": len(refs), "references": refs[:limit],
            "truncated": len(refs) > limit}


def hotspots(repo, days: int = 90, top: int = 10, g: Graph | None = None) -> dict:
    g = g or build_graph(repo)
    counts = gitutil.churn(repo, days)
    rows = []
    for f, n in counts.items():
        if f in g.files and not g.files[f]["is_test"]:
            cx = sum(s["complexity"] for s in g.by_file[f])
            rows.append({"file": f, "commits": n, "complexity": cx, "score": round(n * (1 + cx / 10), 1)})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return {"window_days": days, "hotspots": rows[:top],
            "meaning": "score = commits x (1 + total complexity / 10): files that change often AND are complex."}


def import_cycles(g: Graph) -> list[list[str]]:
    """Strongly connected components of the runtime import graph (iterative Tarjan: no recursion limit)."""
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = 0
    for root in g.module_file:
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        work = [(root, iter(sorted(g.runtime_imports_of.get(root, ()))))]
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(g.runtime_imports_of.get(w, ())))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))
    return out


def architecture_health(repo, g: Graph | None = None) -> dict:
    g = g or build_graph(repo)
    used = {r["name"] for r in g.refs}
    dead = [{"symbol": s["qname"], "file": s["file"], "line": s["line"]} for s in g.symbols
            if not g.files[s["file"]]["is_test"] and not s["decorated"] and s["kind"] != "method"
            and not s["name"].startswith(("__", "test")) and s["name"] != "main" and s["name"] not in used]
    complex_fns = sorted((s for s in g.symbols if s["kind"] != "class" and not g.files[s["file"]]["is_test"]),
                         key=lambda s: s["complexity"], reverse=True)[:5]
    return {"import_cycles": import_cycles(g), "dead_code_candidates": dead[:50],
            "dead_code_total": len(dead),
            "most_complex_functions": [{"symbol": s["qname"], "file": s["file"], "complexity": s["complexity"]}
                                       for s in complex_fns],
            "note": "Cycles count only module-level imports (TYPE_CHECKING and function-local imports are ignored). "
                    "Dead code is a candidate list: names re-exported or listed in __all__ are skipped, "
                    "but plugins and reflection may still use these."}
