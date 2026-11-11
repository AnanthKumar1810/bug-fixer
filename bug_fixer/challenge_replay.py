"""Export a self-contained interactive replay from recorded challenge evidence."""
import argparse
from html import escape
import json
from pathlib import Path


def render(directory: Path) -> str:
    record = json.loads((directory / "challenge.json").read_text(encoding="utf-8"))
    checks = record["checks"]
    columns = ["base_outcome", "workaround_outcome", "candidate_outcome"]

    def row(check):
        cells = "".join(f"<td>{escape(str(check.get(key) or 'unavailable'))}</td>" for key in columns)
        return f"<tr><th>{escape(check['description'])}<small>{escape(check['test_id'])}</small></th>{cells}</tr>"

    def table(items):
        return ("<table><thead><tr><th>Check</th><th>Original</th><th>Workaround</th>"
                "<th>Candidate</th></tr></thead><tbody>" + "".join(map(row, items)) + "</tbody></table>")

    first = table(c for c in checks if c["role"] == "reproducer")
    rest = table(c for c in checks if c["role"] != "reproducer")
    patch = escape((directory / "candidate.patch").read_text(encoding="utf-8"))
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bug Fixer · Patch challenge</title>
<style>*{{box-sizing:border-box}}body{{font:17px/1.5 system-ui;color:#14283a;background:#f1f5f7;margin:auto;max-width:960px;padding:4vw}}
h1{{font-size:clamp(30px,5vw,52px);line-height:1.1}}section,details{{background:white;padding:1.3em;margin:1em 0;border:1px solid #cbd6db;border-radius:8px;overflow:auto}}
th,td{{text-align:left;padding:.65em;border-bottom:1px solid #dee6ea}}table{{border-collapse:collapse;width:100%}}small{{display:block;font-weight:400;color:#536774}}summary{{cursor:pointer;font-weight:700;color:#125941}}pre{{overflow:auto}}</style>
<main><p>Bug Fixer / Recorded Docker challenge</p><h1>Which patch fixes the bug?</h1>
<p>Compare both patches on the original report. Then reveal what happens for another caller and input.</p>
<section><h2>The reported failure</h2>{first}</section>
<details><summary>Challenge both patches on independent checks</summary>{rest}</details>
<details><summary>Inspect the candidate diff</summary><pre>{patch}</pre></details>
<details><summary>Inspect the evidence identity</summary><p>Run: {escape(record['run_id'])}</p>
<p>Candidate tree before: {escape(record['candidate_tree_before'])}<br>Candidate tree after: {escape(record['candidate_tree_after'])}</p></details>
<p>Offline replay of saved outcomes. Open the run in Bug Fixer to repeat validation.</p></main></html>"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(render(args.artifacts), encoding="utf-8")
