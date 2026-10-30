"""Link failing tests to the changed symbols they (transitively) reach."""
from __future__ import annotations

import re

from .graph import Graph


def _qname(test_id: str) -> tuple[str, str]:
    classname, _, name = test_id.partition("::")
    name = re.sub(r"\[.*\]$", "", name)          # strip pytest parametrisation
    return classname, f"{classname}.{name}"


def attribute(g: Graph, imp: dict, failures: list) -> dict:
    """Annotate each failure with `link` and `linked_changes`; return a summary of likely causes."""
    changed = {c["symbol"]: c for c in imp["changed_symbols"] if not c["symbol"].endswith(".<module>")}
    reach: dict = {}
    for sym in changed:
        for hit in g.call_closure([sym], max_depth=8, cap=5000):
            reach.setdefault(hit["symbol"], {})[sym] = (hit["depth"], hit["confidence"])
    changed_files, affected_tests = set(imp["changed_files"]), set(imp["affected_tests"])

    causes: dict = {}
    unlinked = 0
    for f in failures:
        classname, qn = _qname(f["test"])
        links = []
        for loc in f.get("locations", []):                    # the traceback ran through changed code
            path, _, line = loc.rpartition(":")
            if path in g.files and line.isdigit():
                names, _ = g.symbols_in_ranges(path, [(int(line), int(line))])
                links += [{"symbol": n, "depth": 0, "confidence": "traceback"} for n in names if n in changed]
        for sym, (depth, conf) in reach.get(qn, {}).items():  # the test calls changed code
            links.append({"symbol": sym, "depth": depth, "confidence": conf})
        seen, uniq = set(), []
        for link in links:
            if link["symbol"] not in seen:
                seen.add(link["symbol"])
                uniq.append(link)
        f["linked_changes"] = uniq
        if uniq:
            f["link"] = "call-graph"
            for link in uniq:
                causes[link["symbol"]] = causes.get(link["symbol"], 0) + 1
            continue
        test_file = g.module_file.get(classname) or g.module_file.get(classname.rsplit(".", 1)[0], "")
        if test_file in changed_files:
            f["link"] = "test-file-changed"
        elif test_file in affected_tests:
            f["link"] = "import-only"
        else:
            f["link"] = "none"
        unlinked += 1
    likely = []
    for sym, n in sorted(causes.items(), key=lambda kv: -kv[1]):
        c = changed[sym]
        likely.append({"symbol": sym, "file": c["file"], "line": c.get("line"), "end_line": c.get("end_line"),
                       "failing_tests": n})
    return {"likely_causes": likely, "unlinked_failures": unlinked}
