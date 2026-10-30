from collections import defaultdict, deque


class Graph:
    """In-memory view of the SQLite index with fast reverse lookups."""

    def __init__(self, conn):
        self.files = {r["path"]: dict(r) for r in conn.execute("SELECT * FROM files")}
        self.module_file = {f["module"]: p for p, f in self.files.items()}
        self.symbols = [dict(r) for r in conn.execute("SELECT * FROM symbols")]
        self.by_q = defaultdict(list)
        self.by_short = defaultdict(list)
        self.by_file = defaultdict(list)
        for s in self.symbols:
            self.by_q[s["qname"]].append(s)
            self.by_short[s["name"]].append(s)
            self.by_file[s["file"]].append(s)
        self.importers = defaultdict(set)      # module -> modules that import it
        self.runtime_imports_of = defaultdict(set)   # only imports executed at import time (for cycles)
        for r in conn.execute("SELECT file, target, runtime FROM imports"):
            if r["file"] not in self.files:
                continue
            importer = self.files[r["file"]]["module"]
            if importer != r["target"]:
                self.importers[r["target"]].add(importer)
                if r["runtime"]:
                    self.runtime_imports_of[importer].add(r["target"])
        self.calls_q = defaultdict(list)       # resolved callee qname -> edges
        self.calls_name = defaultdict(list)    # unresolved callee short name -> edges
        self.edges = []
        for r in conn.execute("SELECT * FROM edges"):
            e = dict(r)
            self.edges.append(e)
            (self.calls_q if e["callee_qname"] in self.by_q else self.calls_name)[
                e["callee_qname"] if e["callee_qname"] in self.by_q else e["callee_name"]].append(e)
        self.refs = [dict(r) for r in conn.execute("SELECT * FROM refs")]

    def is_test_module(self, module):
        f = self.files.get(self.module_file.get(module, ""))
        return bool(f and f["is_test"])

    def callers_of(self, q: str):
        out = [(e, "resolved") for e in self.calls_q.get(q, [])]
        sym = self.by_q.get(q, [{}])[0]
        if sym.get("name") == "__init__" and sym.get("parent"):
            out += [(e, "resolved") for e in self.calls_q.get(sym["parent"], [])]
        short = q.rsplit(".", 1)[-1]
        if not short.startswith("__") and 0 < len(self.by_short.get(short, [])) <= 3:
            out += [(e, "name-match") for e in self.calls_name.get(short, [])]
        return out

    def dangling_callers(self, q: str):
        """Calls that were resolved to `q` at parse time even though `q` no longer exists
        (e.g. a function that was just removed or renamed)."""
        if q in self.by_q:
            return []
        return [(e, "resolved") for e in self.edges if e["callee_qname"] == q]

    def call_closure(self, starts, max_depth: int = 6, cap: int = 300):
        seen = set(starts)
        queue = deque((s, 0) for s in starts)
        result = []
        while queue and len(result) < cap:
            q, d = queue.popleft()
            if d >= max_depth:
                continue
            for e, conf in self.callers_of(q):
                c = e["caller"]
                if c in seen:
                    continue
                seen.add(c)
                result.append({"symbol": c, "file": e["file"], "depth": d + 1, "confidence": conf})
                if not c.endswith(".<module>"):
                    queue.append((c, d + 1))
        return result

    def import_closure(self, modules):
        seen, queue = set(modules), deque(modules)
        while queue:
            m = queue.popleft()
            for imp in self.importers.get(m, ()):
                if imp not in seen:
                    seen.add(imp)
                    queue.append(imp)
        return seen

    def symbols_in_ranges(self, file: str, ranges):
        """Innermost symbols overlapping the changed line ranges; '<module>' pseudo-symbol for the rest."""
        syms = self.by_file.get(file, [])
        mod = self.files[file]["module"]
        found, module_level = {}, False
        for a, b in ranges:
            hit = [s for s in syms if s["line"] <= b and s["end_line"] >= a]
            inner = [s for s in hit if not any(o is not s and o["line"] >= s["line"] and o["end_line"] <= s["end_line"]
                                               and (o["line"], o["end_line"]) != (s["line"], s["end_line"])
                                               for o in hit)]
            for s in inner:
                found[s["qname"]] = s
            if not hit or b >= 10**8:
                module_level = True
        names = list(found)
        if module_level:
            names.append(f"{mod}.<module>")
        return names, found
