#!/usr/bin/env python3
"""Add new progression tests and Stockfish releases to the Regression-Tests wiki page.

Usage: regression_update.py <wiki-checkout> [page.md] [--dry-run]

Finished progression tests are found on fishtest and added as rows to the
Current Development table. New releases are found on GitHub: a point release
(e.g. 19.1) adds a marker row, a major release (e.g. 20) also moves the table
into a new Historical Information section. Charts are left to
regression_charts.py, which runs afterwards.
"""

import datetime as dt
import difflib
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

FISHTEST = "https://tests.stockfishchess.org"
GITHUB_API = "https://api.github.com/repos/official-stockfish/Stockfish"
SF_REPO = "https://github.com/official-stockfish/Stockfish"
IGNORE_RUNS = set()  # fishtest run ids of aborted or duplicate progression tests
LOOKBACK_DAYS = 30  # rescan this far before the newest row, for late 8 thread runs

SF_ICON = ('[<img src="https://official-stockfish.github.io/docs/images/'
           'icon_128x128@2x.webp" width="20px">]')
DP_ICON = ('[<img src="https://github.githubassets.com/images/icons/emoji/'
           'unicode/1f4c8.png" width="20px">]')
TABLE_HEADER = ["| `Date` | `Version` | `1 Thread` | `8 Threads` |",
                "|:---:|:---:|:---:|:---:|"]

INFO_RE = re.compile(r"^(SMP )?Progression test of ")
VS_RE = re.compile(r" vs SF_(\d+(?:\.\d+)?)\.?\s*$")
TAG_RE = re.compile(r"^sf_(\d+)(?:\.(\d+))?$")
RELEASE_DEF_RE = re.compile(r"^\[Stockfish ([\d.]+)\]:\s*\S+/commit/([0-9a-f]+)", re.M)
LINKDEF_RE = re.compile(r"^\[([^\]]+)\]:\s*(\S+)", re.M)
ROW_DATE_RE = re.compile(r"^\| (\d{4})&#8209;(\d\d)&#8209;(\d\d) \|", re.M)
SUMMARY_RE = re.compile(r"<summary><code>Stockfish \S+ Development "
                        r"\(\d{4}-\d{2}-\d{2} - (\d{4}-\d{2}-\d{2})\)</code>")

def warn(msg):
    print(f"::warning::{msg}")


# --- remote data ----------------------------------------------------------

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "regression-update"})
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as f:
        return json.load(f)


def url_exists(url):
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "regression-update"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.URLError:
        return False


def commit_info(ref):
    c = get_json(f"{GITHUB_API}/commits/{ref}")
    date = dt.datetime.fromisoformat(c["commit"]["committer"]["date"].replace("Z", "+00:00"))
    return c["sha"], date.date(), c["commit"]["message"]


def ahead_by(base, head):
    return get_json(f"{GITHUB_API}/compare/{base}...{head}")["ahead_by"]


def fetch_releases():
    """Published releases as (version tuple, name), oldest first."""
    releases = []
    for r in get_json(f"{GITHUB_API}/releases?per_page=100"):
        m = TAG_RE.match(r["tag_name"])
        if m and not r["draft"] and not r["prerelease"]:
            version = (int(m[1]), int(m[2] or 0))
            releases.append((version, r["tag_name"][3:]))
    return sorted(releases)


def fetch_progression_runs(since):
    """Progression test runs that finished on or after `since`.

    They are long time control runs, so the LTC filter keeps this to a few
    pages whoever ran them.
    """
    stamp = int(dt.datetime.combine(since, dt.time(), dt.UTC).timestamp())
    runs = {}
    for page in range(1, 101):
        batch = get_json(f"{FISHTEST}/api/finished_runs?ltc_only=1&timestamp={stamp}&page={page}")
        if not batch:
            return runs
        runs.update({i: r for i, r in batch.items() if INFO_RE.match(r["args"].get("info", ""))})
    warn("stopped after 100 pages of fishtest runs")
    return runs


def get_run(run_id):
    return get_json(f"{FISHTEST}/api/get_run/{run_id}")


# --- statistics (as shown on fishtest for pentanomial results) -------------

def stats(ptnml):
    n = sum(ptnml)
    scores = [0, 0.25, 0.5, 0.75, 1]
    mu = sum(p * s for p, s in zip(ptnml, scores)) / n
    var = sum(p * (s - mu) ** 2 for p, s in zip(ptnml, scores)) / n
    elo = lambda x: -400 * math.log10(1 / x - 1)
    se = 1.959964 * math.sqrt(var / n)
    return {
        "elo": elo(mu),
        "elo_err": (elo(mu + se) - elo(mu - se)) / 2,
        "nelo": (mu - 0.5) / math.sqrt(2 * var) * 800 / math.log(10),
        "nelo_err": 1.959964 * 800 / math.log(10) / math.sqrt(2 * n),
        "pairs": (ptnml[3] + ptnml[4]) / (ptnml[0] + ptnml[1]),
    }


# --- page editing ---------------------------------------------------------

def wiki_date(d):
    return d.isoformat().replace("-", "&#8209;")


def row_key(d):
    return d.strftime("%d%m%y")


def linkdef(key, url):
    label = f"[{key}]:"
    return label + " " * max(1, 18 - len(label)) + url


class Page:
    def __init__(self, text):
        self.lines = text.split("\n")

    @property
    def text(self):
        return "\n".join(self.lines)

    def links(self):
        return {k.lower(): v for k, v in LINKDEF_RE.findall(self.text)}

    def releases(self):
        """{name: sha} of the releases already on the page."""
        return {name: sha for name, sha in RELEASE_DEF_RE.findall(self.text)}

    def known_runs(self):
        return {url.rstrip("/").rsplit("/", 1)[-1] for url in self.links().values()
                if "tests.stockfishchess.org/tests/" in url}

    def newest_date(self):
        return max(dt.date(int(y), int(m), int(d)) for y, m, d in ROW_DATE_RE.findall(self.text))

    def last_cycle_end(self):
        return dt.date.fromisoformat(SUMMARY_RE.findall(self.text)[-1])

    def index(self, pred, start=0):
        return next(i for i in range(start, len(self.lines)) if pred(self.lines[i]))

    def current_table(self):
        """Line range of the rows of the Current Development table."""
        head = self.index(lambda l: l.startswith("## Current Development"))
        first = self.index(lambda l: l.startswith("| `Date`"), head) + 2
        end = first
        while self.lines[end].startswith("|"):
            end += 1
        return first, end

    def add_row(self, row):
        _, end = self.current_table()
        self.lines.insert(end, row)

    def add_linkdefs(self, defs, group):
        """Add link definitions before the [graph-*] block.

        Rows follow each other directly; a release forms its own group
        separated by blank lines, as on the hand-edited page.
        """
        g = self.index(lambda l: l.startswith("[graph-"))
        assert self.lines[g - 1] == ""
        after_row = re.match(r"\[\d{6}-(raw|elo|master)", self.lines[g - 2])
        block = defs + [""]
        if group or not after_row:
            block = [""] + block
        self.lines[g - 1:g] = block

    def close_cycle(self, name, start, end):
        """Move the Current Development rows into a Historical Information section."""
        first, last = self.current_table()
        rows = self.lines[first:last]
        del self.lines[first:last]
        links = self.index(lambda l: l.startswith("## External Links"))
        sep = max(i for i in range(links) if self.lines[i] == "---")
        self.lines[sep:sep] = [
            "<details>",
            f"  <summary><code>Stockfish {name} Development ({start} - {end})</code></summary><br>",
            "",
            *TABLE_HEADER,
            *rows,
            "",
            "</details>",
            "",
        ]


def release_row(page, release, prev_major):
    name, sha, date = release["name"], release["sha"], release["date"]
    bench = re.search(r"Bench: (\d+)", release["message"])[1]
    count = ahead_by(prev_major[1], sha)
    key = row_key(date)
    tag = name.replace(".", "")
    major = "." not in name
    icons = f"{SF_ICON}[SF{tag}RN]" + (f" {DP_ICON}[SF{tag}DP]" if major else "")
    page.add_row(f"| {wiki_date(date)} | [Stockfish {name}] {icons}<br><sub>`Bench: {bench}`"
                 f"<br>[\\[differences\\]][{key}-dif] `{count}`</sub> |  |  |  |")
    defs = [linkdef(f"Stockfish {name}", f"{SF_REPO}/commit/{sha}")]
    if major:
        defs.append(linkdef(f"SF{tag}DP", f'charts/sf{tag}-progress.svg "Development Progress"'))
    defs += [linkdef(f"SF{tag}RN", f'{release["url"]} "Release Notes"'),
             linkdef(f"{key}-dif", f"{SF_REPO}/compare/{prev_major[1][:10]}...{sha[:10]}")]
    page.add_linkdefs(defs, group=True)
    return date


def cell(key, threads, run):
    s = stats(run["results"]["pentanomial"])
    ptnml = ",&nbsp;".join(str(p) for p in run["results"]["pentanomial"])
    t = str(threads)
    return (f"Elo: [{s['elo']:.2f}][{key}-elo{t}] ±{s['elo_err']:.2f}<br><sub>"
            f"Ptnml:&nbsp;{ptnml}<br>nElo: {s['nelo']:.2f} ±{s['nelo_err']:.2f}<br>"
            f"PairsRatio: {s['pairs']:.2f}<br>[\\[raw statistics\\]][{key}-raw{t}]</sub>")


def test_row(page, test, base_name, base_sha):
    runs = test["runs"]
    any_run = next(iter(runs.values()))
    new_sha = any_run["args"]["resolved_new"]
    key = row_key(test["date"])
    if key + "-master" in page.links():
        warn(f"row key {key} is already used on the page; skipping {new_sha[:10]}")
        return
    count = ahead_by(base_sha, new_sha)
    msg = any_run["args"]["msg_new"].replace("|", "\\|")
    cells = [cell(key, t, runs[t]) if t in runs else "" for t in (1, 8)]
    page.add_row(f"| {wiki_date(test['date'])} | [master][{key}-master] vs [Stockfish {base_name}]"
                 f"<br><sub>`Bench: {any_run['args']['new_signature']}`<br>{msg}<br>"
                 f"[\\[differences\\]][{key}-dif] `{count}`</sub> | {cells[0]} | {cells[1]}")
    defs = [linkdef(f"{key}-dif", f"{SF_REPO}/compare/{base_sha[:10]}...{new_sha[:10]}")]
    defs += [linkdef(f"{key}-elo{t}", f"{FISHTEST}/tests/view/{runs[t]['_id']}") for t in (1, 8) if t in runs]
    defs.append(linkdef(f"{key}-master", f"{SF_REPO}/commit/{new_sha}"))
    defs += [linkdef(f"{key}-raw{t}", f"{FISHTEST}/tests/stats/{runs[t]['_id']}") for t in (1, 8) if t in runs]
    page.add_linkdefs(defs, group=False)


def check_criteria(page, run):
    text = page.text
    criteria = text[text.index("Current Testing Criteria"):text.index("<details>")]
    a = run["args"]
    base, inc = a["tc"].split("+")
    threads = f"`{a['threads']} Thread{'s' if a['threads'] > 1 else ''}`"
    tc = f"{threads} {base} seconds + {inc} seconds for {a['num_games']:,} games"
    if tc not in criteria:
        warn(f"run {run['_id']} ({tc}) is not in Current Testing Criteria")
    if a["book"] not in criteria:
        warn(f"run {run['_id']} uses book {a['book']}, not in Current Testing Criteria")


def new_tests(page, runs):
    """Group unseen finished runs by tested commit: [{date, runs: {threads: run}}]."""
    known = page.known_runs()
    by_commit = {}
    for run_id, run in runs.items():
        if run_id in known or run_id in IGNORE_RUNS:
            continue
        if run.get("deleted") or run.get("failed") or not run.get("finished"):
            continue
        by_commit.setdefault(run["args"]["resolved_new"], {})[run["args"]["threads"]] = run_id
    tests = []
    for sha, ids in by_commit.items():
        if set(ids) != {1, 8}:
            missing = "8" if 1 in ids else "1"
            warn(f"{sha[:10]}: waiting for the {missing} thread progression test")
            continue
        full = {t: get_run(i) for t, i in ids.items()}
        _, date, _ = commit_info(sha)
        tests.append({"date": date, "sha": sha, "runs": full})
    return sorted(tests, key=lambda t: t["date"])


def blog_url(name, year):
    return f"https://stockfishchess.org/blog/{year}/stockfish-{name.replace('.', '-')}/"


def update(page, releases, runs):
    """Apply new releases and tests to `page` in date order. Returns a summary."""
    done = []
    on_page = page.releases()
    majors = [(n, s) for n, s in on_page.items() if "." not in n]
    newest = max(tuple(int(x) for x in n.split(".")) + (0,) for n in on_page)[:2]
    pending = [(v, n) for v, n in releases if v > newest]

    events = [(t["date"], 1, t) for t in new_tests(page, runs)]
    for version, name in pending:
        sha, date, message = commit_info(f"sf_{name}")
        url = blog_url(name, date.year)
        if not url_exists(url):
            warn(f"Stockfish {name}: release notes {url} not found yet; will retry")
            break
        events.append((date, 0, {"name": name, "sha": sha, "date": date,
                                 "message": message, "url": url}))

    for date, kind, ev in sorted(events, key=lambda e: e[:2]):
        if kind == 0:
            name = ev["name"]
            release_row(page, ev, majors[-1])
            if "." not in name:
                page.close_cycle(name, page.last_cycle_end(), date)
                majors.append((name, ev["sha"]))
            on_page[name] = ev["sha"]
            done.append(f"Stockfish {name} release")
        else:
            # Tests run against the latest major release or one of its point
            # releases, sometimes with a fix on top, so go by the description.
            args = ev["runs"][1]["args"]
            base = args["resolved_base"]
            name = m[1] if (m := VS_RE.search(args["info"])) else None
            major = majors[-1][0]
            if name not in on_page or name.split(".")[0] != major:
                warn(f"{ev['sha'][:10]}: tested against SF_{name}, not Stockfish {major}; skipping")
                continue
            for run in ev["runs"].values():
                check_criteria(page, run)
            test_row(page, ev, name, base)
            done.append(f"regression test {date}")
    return done


def main():
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv
    path = Path(args[0]) / (args[1] if len(args) > 1 else "Regression-Tests.md")
    text = path.read_text(encoding="utf-8")
    page = Page(text)

    since = page.newest_date() - dt.timedelta(days=LOOKBACK_DAYS)
    done = update(page, fetch_releases(), fetch_progression_runs(since))

    summary = ", ".join(done) if done else "no new tests or releases"
    print(f"Added: {summary}")
    if dry_run:
        sys.stdout.writelines(difflib.unified_diff(
            text.splitlines(True), page.text.splitlines(True), "before", "after"))
    elif page.text != text:
        path.write_text(page.text, encoding="utf-8", newline="\n")
    if done and "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"message=Add {summary}\n")


if __name__ == "__main__":
    main()
