import sys

from . import gitutil
from .graph import Graph
from .indexer import connect, index_repo
from .security import is_test_path

CONFIG_FILES = ("conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "setup.py")


def _graph(repo):
    index_repo(repo)
    return Graph(connect(repo))


def _is_config(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in CONFIG_FILES or name.startswith("requirements") and name.endswith(".txt")


def impact(repo, rev_range=None) -> dict:
    g = _graph(repo)
    ranges, deleted = gitutil.changed_lines(repo, rev_range)
    changed_modules, changed_symbols, non_python, config = set(), {}, [], []
    for f, rs in ranges.items():
        if _is_config(f):
            config.append(f)
        if f in g.files:
            names, found = g.symbols_in_ranges(f, rs)
            changed_symbols.update({n: f for n in names})
            changed_modules.add(g.files[f]["module"])
        elif not f.endswith(".py"):
            non_python.append(f)
    for f in deleted:
        if f.endswith(".py"):
            changed_modules.add(f[:-3].replace("/", "."))
    starts = [q for q in changed_symbols if not q.endswith(".<module>")]
    callers = g.call_closure(starts)
    affected_modules = g.import_closure(changed_modules)
    affected_tests = sorted({g.module_file[m] for m in affected_modules
                             if g.is_test_module(m) and m in g.module_file})
    funcs = [q for q in starts if g.by_q[q][0]["kind"] != "class"]
    untested = [q for q in funcs if not any(g.files.get(c["file"], {}).get("is_test")
                                            for c in g.call_closure([q]))]
    return {
        "range": rev_range or "working tree vs HEAD",
        "changed_files": sorted(ranges), "deleted_files": deleted, "config_files_changed": config,
        "changed_symbols": [{"symbol": q, "file": f,
                             "complexity": g.by_q[q][0]["complexity"] if q in g.by_q else 0}
                            for q, f in changed_symbols.items()],
        "affected_functions": callers[:100],
        "affected_modules": sorted(m for m in affected_modules if m not in changed_modules),
        "affected_tests": affected_tests, "total_test_files": sum(1 for f in g.files.values() if f["is_test"]),
        "untested_changed_symbols": untested,
        "notes": ["Call resolution is static/best-effort: dynamic dispatch and getattr are not tracked.",
                  "Entries with confidence 'name-match' are matched by function name only."],
    }


def find_symbol(repo, name: str) -> dict:
    g = _graph(repo)
    hits = [s for s in g.symbols if s["qname"] == name or s["name"] == name or s["qname"].endswith("." + name)]
    out = []
    for s in hits[:20]:
        lines = (repo / s["file"]).read_text(errors="ignore").splitlines()
        out.append({"symbol": s["qname"], "kind": s["kind"], "file": s["file"], "line": s["line"],
                    "complexity": s["complexity"],
                    "snippet": "\n".join(lines[s["line"] - 1: min(s["end_line"], s["line"] + 11)])})
    return {"matches": out}


def find_references(repo, symbol: str, limit: int = 60) -> dict:
    g = _graph(repo)
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
    return {"definitions": [d["qname"] for d in defs], "count": len(refs), "references": refs[:limit]}


def hotspots(repo, days: int = 90, top: int = 10) -> dict:
    g = _graph(repo)
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
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]

    def strong(v):
        index[v] = low[v] = counter[0]; counter[0] += 1
        stack.append(v); on.add(v)
        for w in g.runtime_imports_of.get(v, ()):
            if w not in index:
                strong(w); low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop(); on.discard(w); comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(comp))
    for m in list(g.module_file):
        if m not in index:
            strong(m)
    return out


def architecture_health(repo) -> dict:
    g = _graph(repo)
    used = {r["name"] for r in g.refs}
    dead = [{"symbol": s["qname"], "file": s["file"], "line": s["line"]} for s in g.symbols
            if not g.files[s["file"]]["is_test"] and not s["decorated"] and s["kind"] != "method"
            and not s["name"].startswith(("__", "test")) and s["name"] != "main" and s["name"] not in used]
    complex_fns = sorted((s for s in g.symbols if s["kind"] != "class" and not g.files[s["file"]]["is_test"]),
                         key=lambda s: s["complexity"], reverse=True)[:5]
    return {"import_cycles": import_cycles(g), "dead_code_candidates": dead[:50],
            "most_complex_functions": [{"symbol": s["qname"], "file": s["file"], "complexity": s["complexity"]}
                                       for s in complex_fns],
            "note": "Cycles count only module-level imports (TYPE_CHECKING and function-local imports are ignored). "
                    "Dead code is a candidate list: names re-exported or listed in __all__ are skipped, "
                    "but plugins and reflection may still use these."}


def review_changes(repo, rev_range=None) -> dict:
    imp = impact(repo, rev_range)
    hot = {h["file"]: h for h in hotspots(repo, top=1000)["hotspots"]}
    top_quarter = set(list(hot)[: max(1, len(hot) // 4)]) if hot else set()
    reasons, score = [], 0
    n_mod = len(imp["affected_modules"])
    if n_mod:
        score += min(n_mod, 20); reasons.append(f"{n_mod} dependent modules may be affected")
    for f in imp["changed_files"]:
        if f in top_quarter:
            score += 5; reasons.append(f"{f} is a churn/complexity hotspot")
    if imp["untested_changed_symbols"]:
        score += 8 * len(imp["untested_changed_symbols"])
        reasons.append(f"{len(imp['untested_changed_symbols'])} changed functions are not reached by any test")
    cx = sum(c["complexity"] for c in imp["changed_symbols"])
    if cx:
        score += cx // 5; reasons.append(f"changed code has total complexity {cx}")
    if imp["config_files_changed"]:
        score += 10; reasons.append(f"config/dependency files changed: {imp['config_files_changed']}")
    level = "low" if score < 15 else "medium" if score < 35 else "high"
    return {"risk_level": level, "risk_score": score, "reasons": reasons,
            "suggested_tests": imp["affected_tests"], "changed_symbols": imp["changed_symbols"]}

