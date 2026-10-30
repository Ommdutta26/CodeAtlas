"""Markdown rendering for PR comments / CI logs."""
from __future__ import annotations

_ICON = {"pass": "✅", "pass_with_preexisting_failures": "✅", "fail": "❌", "unverified": "⚠️", "no_changes": "➖"}


def render_markdown(result: dict) -> str:
    """Render the output of workflow.review_and_test."""
    review, tests = result["review"], result["tests"]
    lines = [f"## {_ICON.get(result['verdict'], '')} CodeAtlas: {result['verdict'].upper()}",
             "", result["summary"], "",
             f"- **Compared:** `{result['range']}`",
             f"- **Risk:** {review['risk_level']} (score {review['risk_score']})"]
    if review.get("static_risk_level") != review["risk_level"]:
        lines[-1] += f", static estimate was {review['static_risk_level']}"
    for r in review["reasons"]:
        lines.append(f"  - {r}")
    if review["changed_symbols"]:
        lines += ["", "### Changed symbols"]
        lines += [f"- `{c['symbol']}` (`{c['file']}`, complexity {c['complexity']})" for c in review["changed_symbols"][:15]]
    api = result["api_changes"]
    if api["signature_changed"] or api["removed"]:
        lines += ["", "### API changes"]
        for c in api["signature_changed"]:
            lines.append(f"- {'**breaking** ' if c['breaking'] else ''}`{c['symbol']}`: `{c['old']}` → `{c['new']}`"
                         f" ({len(c['callers'])} caller(s))")
        for r in api["removed"]:
            lines.append(f"- removed `{r['symbol']}` ({len(r['callers'])} remaining caller(s))")
    if tests.get("status") in ("passed", "failed", "error"):
        lines += ["", "### Tests",
                  f"{tests.get('passed', 0)} passed, {tests.get('failed', 0)} failed, "
                  f"{tests.get('errors', 0)} errors, {tests.get('skipped', 0)} skipped "
                  f"({tests.get('duration_s', '?')}s, ran {len(tests['selected_test_files']) if isinstance(tests.get('selected_test_files'), list) else 'all'} "
                  f"of {tests['total_test_files']} test files)"]
        if tests.get("failures"):
            lines += ["", "| Test | Message | Vs base |", "|---|---|---|"]
            for f in tests["failures"]:
                msg = f["message"].replace("|", "\\|").replace("\n", " ")[:120]
                lines.append(f"| `{f['test']}` | {msg} | {f.get('vs_base', '')} |")
    for n in result["notes"][:3]:
        lines.append(f"\n> {n}")
    return "\n".join(lines) + "\n"
