# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Keep GitHub's traffic numbers longer than the 14 days GitHub shows.

Run daily by .github/workflows/traffic.yml. Reads the traffic API and merges
it into CSV files in the given folder (the traffic-stats branch):

  views.csv      date, views, unique visitors      (one row per day)
  clones.csv     date, clones, unique cloners      (one row per day)
  referrers.csv  date fetched, site, views, unique (14-day totals on that date)
  paths.csv      date fetched, path, views, unique (14-day totals on that date)

A day seen again is overwritten: GitHub's count for a recent day can still
grow. Older days are kept as they were.

    GH_TOKEN=... python traffic.py owner/repo <folder>
"""

import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone


def api(repo: str, path: str):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/traffic/{path}",
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def read(path: str) -> list[list[str]]:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.reader(f))[1:]


def write(path: str, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="") as f:
        out = csv.writer(f)
        out.writerow(header)
        out.writerows(rows)


def merge_daily(path: str, header: list[str], days: list[dict]) -> int:
    rows = {r[0]: r for r in read(path)}
    for day in days:
        date = day["timestamp"][:10]
        rows[date] = [date, day["count"], day["uniques"]]
    write(path, header, [rows[d] for d in sorted(rows)])
    return len(days)


def merge_snapshot(path: str, header: list[str], today: str, items: list[dict], name: str) -> None:
    rows = [r for r in read(path) if r[0] != today]
    rows += [[today, item[name], item["count"], item["uniques"]] for item in items]
    write(path, header, rows)


def main(repo: str, folder: str) -> None:
    os.makedirs(folder, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    views = api(repo, "views")
    clones = api(repo, "clones")
    merge_daily(os.path.join(folder, "views.csv"), ["date", "views", "unique"], views["views"])
    merge_daily(os.path.join(folder, "clones.csv"), ["date", "clones", "unique"], clones["clones"])
    merge_snapshot(os.path.join(folder, "referrers.csv"), ["fetched", "referrer", "views", "unique"], today,
                   api(repo, "popular/referrers"), "referrer")
    merge_snapshot(os.path.join(folder, "paths.csv"), ["fetched", "path", "views", "unique"], today,
                   api(repo, "popular/paths"), "path")
    print(f"{today}: last 14 days {views['count']} views ({views['uniques']} unique), "
          f"{clones['count']} clones ({clones['uniques']} unique)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
