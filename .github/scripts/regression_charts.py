#!/usr/bin/env python3
"""Generate the charts on the Regression-Tests wiki page from its own tables.

Usage: regression_charts.py <wiki-checkout> [page.md] [chart-url-prefix]

Reads the progression tables, writes SVG charts to <wiki-checkout>/charts/ and
points the page's [graph-*] link definitions at them.
"""

import datetime as dt
import html
import json
import re
import sys
import urllib.request
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
matplotlib.rcParams["svg.fonttype"] = "none"
matplotlib.rcParams["svg.hashsalt"] = "regression-tests"  # stable ids between runs
import matplotlib.pyplot as plt  # noqa: E402

FIRST_CHARTED = 13  # the progression charts start at Stockfish 13
DRAW_CHARTED = 17  # draw percentage charts start at Stockfish 17
FISHTEST_API = "https://tests.stockfishchess.org/api/get_run/"

# Series colours of the original Google Sheets charts, oldest release first.
PALETTE = ["#4285f4", "#ea4335", "#fbbc04", "#34a853",
           "#ff6d01", "#46bdc6", "#ff00ff", "#f07b72"]

SUMMARY_RE = re.compile(
    r"<summary><code>Stockfish (\S+) Development "
    r"\((\d{4}-\d{2}-\d{2}) - (\d{4}-\d{2}-\d{2})\)</code>")
CELL_RE = re.compile(
    r"Elo: \[(?P<elo>-?[\d.]+)\]\[[^\]]*\]\s*±\s*(?P<elo_err>[\d.]+)"
    r".*?(?:WDL:(?P<wdl>[\d,\s]+)|Ptnml:(?P<ptnml>[\d,\s]+))"
    r".*?nElo:\s*(?P<nelo>-?[\d.]+)\s*±\s*(?P<nelo_err>[\d.]+)"
    r"(?:.*?Pai?rsRatio:\s*(?P<pairs>[\d.]+))?"
    r".*?\[\\\[raw statistics\\\]\]\[(?P<raw>[^\]]+)\]")
LINKDEF_RE = re.compile(r"^\[([^\]]+)\]:\s*(\S+)", re.M)

COLUMNS = {"`1 Thread`": "1t", "`8 Threads`": "8t"}


def parse_date(text):
    return dt.date.fromisoformat(html.unescape(text).replace("\u2011", "-").strip())


def parse_cell(cell, links, line_no):
    if not cell.strip():
        return None
    m = CELL_RE.search(cell.replace("&nbsp;", " "))
    if not m:
        raise ValueError(f"line {line_no}: cannot parse cell: {cell[:120]}")
    nums = lambda s: [int(x) for x in s.replace(" ", "").split(",") if x]
    raw_url = links.get(m["raw"].lower(), "")
    return {
        "elo": float(m["elo"]), "elo_err": float(m["elo_err"]),
        "nelo": float(m["nelo"]), "nelo_err": float(m["nelo_err"]),
        "pairs": float(m["pairs"]) if m["pairs"] else None,
        "wdl": nums(m["wdl"]) if m["wdl"] else None,
        "ptnml": nums(m["ptnml"]) if m["ptnml"] else None,
        "run": raw_url.rstrip("/").rsplit("/", 1)[-1] if "/tests/" in raw_url else None,
    }


def parse_page(text):
    """Return a list of dev cycles: {name, start, rows: [{date, 1t, 8t}]}."""
    links = {k.lower(): v for k, v in LINKDEF_RE.findall(text)}
    cycles, cycle, columns = [], None, None
    for line_no, line in enumerate(text.splitlines(), 1):
        if m := SUMMARY_RE.search(line):
            cycle = {"name": m[1], "start": dt.date.fromisoformat(m[2]),
                     "end": dt.date.fromisoformat(m[3]), "rows": []}
            cycles.append(cycle)
        elif line.startswith("## Current Development"):
            cycle = {"name": None, "start": None, "rows": []}  # filled in below
            cycles.append(cycle)
        elif line.startswith("## "):
            cycle = None
        elif line.startswith("| `Date`"):
            columns = [c.strip() for c in line.strip().strip("|").split("|")]
        elif cycle is not None and line.startswith("| 20"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            row = {"date": parse_date(cells[0])}
            for name, cell in zip(columns[2:], cells[2:]):
                if name in COLUMNS:
                    row[COLUMNS[name]] = parse_cell(cell, links, line_no)
            if row.get("1t") or row.get("8t"):
                cycle["rows"].append(row)

    # The current cycle starts where the last released one ended and is
    # named after the next release number.
    current = next(c for c in cycles if c["name"] is None)
    released = [c for c in cycles if c["name"] is not None]
    last = released[-1]
    current["start"] = last["end"]
    current["name"] = str(int(last["name"]) + 1)
    cycles.remove(current)
    cycles.append(current)
    return cycles


def load_draws(cycles, cache_path):
    """Draw ratio per result: from WDL, else from fishtest (cached)."""
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    for cycle in cycles:
        for row in cycle["rows"]:
            for key in ("1t", "8t"):
                r = row.get(key)
                if not r:
                    continue
                if r["wdl"]:
                    w, d, l = r["wdl"]
                    r["draws"] = d / (w + d + l)
                    continue
                if not r["run"] or not charted(cycle, DRAW_CHARTED):
                    continue
                if r["run"] not in cache:
                    with urllib.request.urlopen(FISHTEST_API + r["run"], timeout=30) as f:
                        res = json.load(f)["results"]
                    cache[r["run"]] = [res["wins"], res["draws"], res["losses"]]
                w, d, l = cache[r["run"]]
                r["draws"] = d / (w + d + l)
    cache_path.write_text(json.dumps(cache, indent=0, sort_keys=True) + "\n",
                          newline="\n")


def charted(cycle, first):
    return cycle["name"].isdigit() and int(cycle["name"]) >= first


# --- plotting -----------------------------------------------------------

FG = "#8b949e"  # mid grey, readable on GitHub's light and dark themes
GRID = "#8b949e44"


def new_figure(title, xlabel, ylabel):
    fig, ax = plt.subplots(figsize=(9, 6.2))
    fig.patch.set_alpha(0)
    ax.set_facecolor("none")
    ax.set_title(title, color=FG, loc="left", fontsize=15, pad=14)
    ax.set_xlabel(xlabel, color=FG)
    ax.set_ylabel(ylabel, color=FG)
    ax.tick_params(colors=FG)
    ax.grid(color=GRID, linewidth=0.8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(FG)
    return fig, ax


def legend(ax, **kw):
    ax.legend(frameon=False, labelcolor=FG, **kw)


def save(fig, out, name):
    fig.tight_layout()
    # Always write LF line endings so runs on Windows match CI.
    with open(out / name, "w", encoding="utf-8", newline="\n") as f:
        fig.savefig(f, format="svg", metadata={"Date": None})
    plt.close(fig)


def colours(cycles):
    return {c["name"]: PALETTE[i % len(PALETTE)] for i, c in enumerate(cycles)}


def progression(cycles, out, key, field, title, ylabel, name, origin=0.0):
    fig, ax = new_figure(title, "Number of Days", ylabel)
    colour = colours(cycles)
    for c in cycles:
        pts = [((r["date"] - c["start"]).days, r[key][field])
               for r in c["rows"] if r.get(key) and r[key][field] is not None]
        if not pts:
            continue
        xs, ys = zip(*([(0, origin)] + pts))
        ax.plot(xs, ys, "-o", color=colour[c["name"]], markersize=5,
                linewidth=2, label=f"SF {c['name']}")
    ax.set_xlim(left=0)
    legend(ax, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    save(fig, out, name)


def thirty_day(cycles, out, key, title, name):
    fig, ax = new_figure(title, "", "Elo")
    colour = colours(cycles)
    names, values = [], []
    for c in cycles:
        rows = [r for r in c["rows"] if r.get(key)]
        if not rows:
            continue
        days = (rows[-1]["date"] - c["start"]).days
        names.append(f"SF {c['name']}")
        values.append(rows[-1][key]["elo"] / days * 30 if days else 0)
    bars = ax.bar(names, values, color=[colour[n[3:]] for n in names], width=0.65)
    ax.bar_label(bars, fmt="%.2f", color=FG, padding=3)
    ax.grid(axis="x", visible=False)
    ax.set_axisbelow(True)
    save(fig, out, name)


def draw_vs_elo(cycles, out, key, title, name):
    fig, ax = new_figure(title, "Elo", "Draw Percentage")
    colour = colours(cycles)
    for c in cycles:
        if not charted(c, DRAW_CHARTED):
            continue
        pts = [(r[key]["elo"], 100 * r[key]["draws"])
               for r in c["rows"] if r.get(key) and r[key].get("draws") is not None]
        if pts:
            xs, ys = zip(*pts)
            ax.scatter(xs, ys, color=colour[c["name"]], s=40, label=f"SF {c['name']}")
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(decimals=1))
    legend(ax, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    save(fig, out, name)


def current(cycle, out, name):
    fig, ax = new_figure(f"Stockfish {cycle['name']} Development Progress", "", "Elo")
    for key, label, col in (("1t", "1 Thread", PALETTE[0]), ("8t", "8 Threads", PALETTE[1])):
        rows = [r for r in cycle["rows"] if r.get(key)]
        if not rows and cycle["rows"]:
            continue  # e.g. no 8 thread tests before 2018
        xs = [cycle["start"]] + [r["date"] for r in rows]
        ys = [0.0] + [r[key]["elo"] for r in rows]
        err = [0.0] + [r[key]["elo_err"] for r in rows]
        ax.errorbar(xs, ys, yerr=err, fmt="-o", color=col, markersize=5,
                    linewidth=2, capsize=4, label=label)
    if not cycle["rows"]:  # just released: show an empty first month
        ax.set_xlim(cycle["start"], cycle["start"] + dt.timedelta(days=30))
        ax.set_ylim(-1, 5)
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    legend(ax, loc="upper left", ncols=2)
    save(fig, out, name)


CHARTS = {
    "graph-current": "current.svg",
    "graph-elo1": "elo-1t.svg", "graph-elo8": "elo-8t.svg",
    "graph-nelo1": "nelo-1t.svg", "graph-nelo8": "nelo-8t.svg",
    "graph-gpr1": "pairs-1t.svg", "graph-gpr8": "pairs-8t.svg",
    "graph-thirty1": "thirty-1t.svg", "graph-thirty8": "thirty-8t.svg",
    "graph-dve1": "draw-vs-elo-1t.svg", "graph-dve8": "draw-vs-elo-8t.svg",
}


def render(cycles, out):
    shown = [c for c in cycles if charted(c, FIRST_CHARTED)]
    for key, threads in (("1t", "1 Thread"), ("8t", "8 Threads")):
        progression(shown, out, key, "elo", f"Elo Progress ({threads})",
                    "Elo", f"elo-{key}.svg")
        progression(shown, out, key, "nelo", f"Normalized Elo Progress ({threads})",
                    "nElo", f"nelo-{key}.svg")
        progression(shown, out, key, "pairs", f"Game Pair Ratio Progress ({threads})",
                    "Pairs Ratio", f"pairs-{key}.svg", origin=1.0)
        thirty_day(shown, out, key,
                   f"30 Day Average Development Progress ({threads})", f"thirty-{key}.svg")
        draw_vs_elo(shown, out, key, f"Draw Percentage vs Elo ({threads})",
                    f"draw-vs-elo-{key}.svg")
    current(cycles[-1], out, "current.svg")
    for c in cycles[:-1]:  # the [SFnDP] chart of each released cycle
        current(c, out, progress_chart(c["name"]))


def progress_chart(name):
    return f"sf{name.lower()}-progress.svg"


def relink(text, base_url, released):
    """Point the [graph-*] and [SFnDP] link definitions at the generated charts."""
    def dp(m):
        if m[2] not in released:
            return m[0]
        return f'{m[1]}{base_url}/{progress_chart(m[2])} "Development Progress"'

    def sub(m):
        if m[1] in CHARTS:
            pad = " " * max(1, 18 - len(m[1]) - 3)
            return f"[{m[1]}]:{pad}{base_url}/{CHARTS[m[1]]}"
        return m[0]
    text = re.sub(r"\n?^\[graph-total\]:.*$", "", text, flags=re.M)
    text = re.sub(r"^(\[SF(\w+)DP\]:\s*).*$", dp, text, flags=re.M)
    return re.sub(r"^\[(graph-[a-z0-9]+)\]:\s*\S+", sub, text, flags=re.M)


def main():
    wiki = Path(sys.argv[1])
    page = wiki / (sys.argv[2] if len(sys.argv) > 2 else "Regression-Tests.md")
    base_url = sys.argv[3] if len(sys.argv) > 3 else "charts"
    out = wiki / "charts"
    out.mkdir(exist_ok=True)
    text = page.read_text(encoding="utf-8")
    cycles = parse_page(text)
    load_draws(cycles, out / "fishtest-cache.json")
    for c in cycles:
        print(f"SF {c['name']:>5}  start {c['start']}  rows {len(c['rows'])}")
    render(cycles, out)
    new = relink(text, base_url, {c["name"] for c in cycles[:-1]})
    if new != text:
        page.write_text(new, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
