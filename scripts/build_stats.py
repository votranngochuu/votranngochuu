#!/usr/bin/env python3
"""Build assets/stats.svg: an animated GitHub stats card for the profile README.

Data comes from the GitHub GraphQL API. Standard library only.

Environment:
  GITHUB_TOKEN  token used for the GraphQL API (required)
  GH_USER       GitHub login (default: votranngochuu)

If the API cannot be reached or returns bad data, the script exits non-zero
and leaves the existing SVG untouched.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from html import escape
from pathlib import Path

API_URL = "https://api.github.com/graphql"
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "assets" / "stats.svg"

# Languages that are only ranked after "real" languages (still shown if fewer
# than five real languages exist).
RANK_IGNORE = {"HTML", "CSS", "SCSS", "Jupyter Notebook"}
FALLBACK_LANG_COLOR = "#8b949e"
TOP_LANGS = 5

# Visual constants (match hero.svg / highlights.svg).
SANS = "'Segoe UI', Ubuntu, 'Helvetica Neue', Arial, sans-serif"
MONO = "'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace"
BLUE, RED, YELLOW, GREEN = "#4285F4", "#EA4335", "#FBBC05", "#34A853"
WHITE, BODY, MUTED, BORDER = "#ffffff", "#c9d1d9", "#8b949e", "#21262d"
# Heatmap ramp, from empty to most active (GitHub dark greens).
LEVELS = ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"]
LEVEL_INDEX = {
    "NONE": 0,
    "FIRST_QUARTILE": 1,
    "SECOND_QUARTILE": 2,
    "THIRD_QUARTILE": 3,
    "FOURTH_QUARTILE": 4,
}
SNOW_SEED = 20241225
SNOW_COUNT = 30


class ApiError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #
def graphql(token: str, query: str, variables: dict, attempts: int = 3) -> dict:
    """POST a GraphQL query with retries and exponential backoff."""
    body = json.dumps({"query": query, "variables": variables}).encode()
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            API_URL,
            data=body,
            headers={
                "Authorization": f"bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "profile-stats-builder",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("errors"):
                messages = "; ".join(e.get("message", "?") for e in payload["errors"])
                raise ApiError(f"GraphQL errors: {messages}")
            if not payload.get("data"):
                raise ApiError("GraphQL response has no data")
            return payload["data"]
        except urllib.error.HTTPError as exc:
            # 401 means a bad token: retrying will not help.
            if exc.code == 401:
                raise ApiError("HTTP 401: token rejected") from exc
            last_error = ApiError(f"HTTP {exc.code}: {exc.reason}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ApiError) as exc:
            last_error = exc
        if attempt < attempts:
            wait = 2 ** attempt
            print(f"[stats] attempt {attempt} failed ({last_error}); retrying in {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
    raise ApiError(f"GitHub API failed after {attempts} attempts: {last_error}")


def parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


COLLECTION_FIELDS = """
  totalCommitContributions
  restrictedContributionsCount
  totalPullRequestContributions
  totalIssueContributions
  totalPullRequestReviewContributions
  contributionCalendar {
    totalContributions
    weeks { contributionDays { date contributionCount } }
  }
"""


def fetch_profile(token: str, login: str) -> dict:
    query = """
    query($login: String!) {
      user(login: $login) { createdAt followers { totalCount } }
    }"""
    user = graphql(token, query, {"login": login}).get("user")
    if not user:
        raise ApiError(f"user {login!r} not found")
    return user


def fetch_contributions(token: str, login: str, created: dt.datetime,
                        now: dt.datetime) -> tuple[dict, dict[int, dict]]:
    """Return (last-12-months collection, {year: collection}) in one query."""
    parts = [
        "lastYear: contributionsCollection { contributionCalendar { totalContributions "
        "weeks { contributionDays { date contributionCount contributionLevel } } } }"
    ]
    for year in range(created.year, now.year + 1):
        start = max(dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc), created)
        end = min(dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc), now)
        parts.append(
            f'y{year}: contributionsCollection(from: "{iso(start)}", to: "{iso(end)}") '
            f"{{{COLLECTION_FIELDS}}}"
        )
    query = "query($login: String!) { user(login: $login) { %s } }" % "\n".join(parts)
    user = graphql(token, query, {"login": login}).get("user") or {}
    if "lastYear" not in user:
        raise ApiError("missing last-year contributions")
    years = {}
    for year in range(created.year, now.year + 1):
        coll = user.get(f"y{year}")
        if coll is None:
            raise ApiError(f"missing contributions for {year}")
        years[year] = coll
    return user["lastYear"], years


def fetch_repositories(token: str, login: str) -> tuple[int, dict[str, dict]]:
    """Return (public non-fork owned repo count, {language: {size, color}})."""
    query = """
    query($login: String!, $cursor: String) {
      user(login: $login) {
        repositories(ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC,
                     first: 100, after: $cursor) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes {
            name
            languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
              edges { size node { name color } }
            }
          }
        }
      }
    }"""
    langs: dict[str, dict] = {}
    cursor = None
    total = 0
    for _ in range(50):  # hard stop: 5,000 repositories
        user = graphql(token, query, {"login": login, "cursor": cursor}).get("user")
        if not user:
            raise ApiError("missing repositories")
        repos = user["repositories"]
        total = repos["totalCount"]
        # The profile README and portfolio sites are mostly HTML/SVG; they say
        # nothing about engineering skills, so keep them out of the language mix.
        skip = {login.lower(), f"{login.lower()}.github.io", "portfolio"}
        for node in repos["nodes"] or []:
            if (node.get("name") or "").lower() in skip:
                continue
            for edge in (node.get("languages") or {}).get("edges") or []:
                name = edge["node"]["name"]
                entry = langs.setdefault(
                    name, {"size": 0, "color": edge["node"].get("color") or FALLBACK_LANG_COLOR})
                entry["size"] += edge["size"]
        if not repos["pageInfo"]["hasNextPage"]:
            break
        cursor = repos["pageInfo"]["endCursor"]
    return total, langs


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def daily_counts(collections) -> dict[dt.date, int]:
    days: dict[dt.date, int] = {}
    for coll in collections:
        for week in coll["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                date = dt.date.fromisoformat(day["date"])
                days[date] = max(days.get(date, 0), day["contributionCount"])
    return days


def streaks(days: dict[dt.date, int], today: dt.date) -> tuple[int, int]:
    """Return (current, longest) streaks of days with at least one contribution."""
    if not days:
        return 0, 0
    longest = run = 0
    day, last = min(days), max(max(days), today)
    while day <= last:
        if days.get(day, 0) > 0:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
        day += dt.timedelta(days=1)

    # Today may still be empty: the streak then counts from yesterday.
    cursor = today if days.get(today, 0) > 0 else today - dt.timedelta(days=1)
    current = 0
    while days.get(cursor, 0) > 0:
        current += 1
        cursor -= dt.timedelta(days=1)
    return current, longest


def top_languages(langs: dict[str, dict]) -> list[dict]:
    ranked = sorted(langs.items(), key=lambda kv: (kv[0] in RANK_IGNORE, -kv[1]["size"], kv[0]))
    top = [{"name": n, **v} for n, v in ranked[:TOP_LANGS] if v["size"] > 0]
    top.sort(key=lambda l: -l["size"])  # display by size once the set is chosen
    total = sum(l["size"] for l in top) or 1
    for lang in top:
        lang["share"] = lang["size"] / total
    return top


# --------------------------------------------------------------------------- #
# SVG
# --------------------------------------------------------------------------- #
W, H = 1200, 560
PAD = 40
CELL, GAP = 14, 3
STEP = CELL + GAP
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt(n: int) -> str:
    return f"{n:,}"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{fmt(n)} {word if n == 1 else (many or word + 's')}"


def css() -> str:
    return f"""
    .sans {{ font-family: {SANS}; }}
    .mono {{ font-family: {MONO}; }}
    .up {{ animation: up .7s cubic-bezier(.2,.7,.2,1) both; }}
    @keyframes up {{ from {{ opacity: 0; transform: translateY(16px); }} to {{ opacity: 1; transform: translateY(0); }} }}
    .t2 {{ animation-delay: .15s; }} .t3 {{ animation-delay: .3s; }} .t4 {{ animation-delay: .45s; }}
    .accent {{ transform-box: fill-box; transform-origin: left center; animation: grow .9s ease-out both; }}
    @keyframes grow {{ from {{ transform: scaleX(0); }} to {{ transform: scaleX(1); }} }}
    .a1 {{ animation-delay: .5s; }} .a2 {{ animation-delay: .65s; }} .a3 {{ animation-delay: .8s; }} .a4 {{ animation-delay: .95s; }}
    .col {{ animation: wave .45s ease-out both; }}
    @keyframes wave {{ from {{ opacity: 0; transform: translateY(6px); }} to {{ opacity: 1; transform: translateY(0); }} }}
    .lbar {{ transform-box: fill-box; transform-origin: left center; animation: grow 1.2s cubic-bezier(.2,.7,.2,1) 1.6s both; }}
    .fade {{ animation: fade .8s ease-out both; }}
    @keyframes fade {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
    .fall {{ animation-name: fall; animation-timing-function: linear; animation-iteration-count: infinite; }}
    @keyframes fall {{ from {{ transform: translateY(-10px); }} to {{ transform: translateY(570px); }} }}
    .sway {{ animation: sway 3s ease-in-out infinite alternate; }}
    @keyframes sway {{ from {{ transform: translateX(-25px); }} to {{ transform: translateX(25px); }} }}
    @media (prefers-reduced-motion: reduce) {{
      * {{ animation: none !important; }}
      .snow {{ display: none; }}
    }}
"""


def tile(i: int, x: int, y: int, w: int, h: int, color: str,
         value: str, label: str, sub: str) -> str:
    cls = f"up t{i}" if i > 1 else "up"
    return f"""
    <g class="{cls}">
      <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="18" fill="#161b22" fill-opacity=".55" stroke="{BORDER}" stroke-width="1.5"/>
      <rect class="accent a{i}" x="{x + 24}" y="{y + 20}" width="44" height="4" rx="2" fill="{color}"/>
      <text class="sans" x="{x + 24}" y="{y + 74}" font-size="46" font-weight="800" fill="{color}">{escape(value)}</text>
      <text class="sans" x="{x + 24}" y="{y + 100}" font-size="16" font-weight="600" fill="{WHITE}">{escape(label)}</text>
      <text class="sans" x="{x + 24}" y="{y + 120}" font-size="13" fill="{MUTED}">{escape(sub)}</text>
    </g>"""


def heatmap(last_year: dict, x0: int, y0: int) -> tuple[str, dict]:
    weeks = last_year["contributionCalendar"]["weeks"][-53:]
    out = []
    month_labels = []
    prev_month = None
    best = (0, None)
    for col, week in enumerate(weeks):
        days = week["contributionDays"]
        if not days:
            continue
        first = dt.date.fromisoformat(days[0]["date"])
        if first.month != prev_month:
            month_labels.append((col, first.month))
            prev_month = first.month
        cx = x0 + col * STEP
        delay = 0.6 + col * 0.022
        rects = []
        for day in days:
            date = dt.date.fromisoformat(day["date"])
            row = (date.weekday() + 1) % 7  # Sunday on top, like GitHub
            count = day["contributionCount"]
            level = LEVEL_INDEX.get(day.get("contributionLevel") or "", 1 if count else 0)
            if count and level == 0:
                level = 1
            if count > best[0]:
                best = (count, date)
            title = f"{count} contribution{'s' if count != 1 else ''} on {date.isoformat()}"
            rects.append(
                f'<rect x="{cx}" y="{y0 + row * STEP}" width="{CELL}" height="{CELL}" rx="3" '
                f'fill="{LEVELS[level]}"><title>{title}</title></rect>'
            )
        out.append(f'<g class="col" style="animation-delay:{delay:.3f}s">{"".join(rects)}</g>')

    # Drop a leading month label that would collide with the next one.
    if len(month_labels) > 1 and month_labels[1][0] - month_labels[0][0] < 3:
        month_labels.pop(0)
    labels = "".join(
        f'<text x="{x0 + col * STEP}" y="{y0 - 10}">{MONTHS[m - 1]}</text>'
        for col, m in month_labels
    )
    svg = (
        f'<g class="mono fade" font-size="12" fill="{MUTED}" style="animation-delay:.5s">{labels}'
        f'<text x="{x0 - 12}" y="{y0 + 1 * STEP + 11}" text-anchor="end">Mon</text>'
        f'<text x="{x0 - 12}" y="{y0 + 3 * STEP + 11}" text-anchor="end">Wed</text>'
        f'<text x="{x0 - 12}" y="{y0 + 5 * STEP + 11}" text-anchor="end">Fri</text></g>'
        + "".join(out)
    )
    return svg, {"best_count": best[0], "best_date": best[1], "columns": len(weeks)}


def languages_block(langs: list[dict], x: int, y: int, width: int) -> str:
    segs = []
    cursor = 0.0
    for lang in langs:
        seg_w = lang["share"] * width
        segs.append(f'<rect x="{x + cursor:.2f}" y="{y}" width="{seg_w + 0.5:.2f}" height="12" '
                    f'fill="{escape(lang["color"])}"/>')
        cursor += seg_w
    bar = (
        f'<clipPath id="lclip"><rect x="{x}" y="{y}" width="{width}" height="12" rx="6"/></clipPath>'
        f'<rect class="fade" x="{x}" y="{y}" width="{width}" height="12" rx="6" fill="#161b22" '
        f'style="animation-delay:1.4s"/>'
        f'<g clip-path="url(#lclip)"><g class="lbar">{"".join(segs)}</g></g>'
    )
    legend = []
    lx = x
    for i, lang in enumerate(langs):
        pct = f"{lang['share'] * 100:.1f}%"
        name = escape(lang["name"])
        legend.append(
            f'<g class="fade" style="animation-delay:{2.0 + i * 0.1:.2f}s">'
            f'<circle cx="{lx + 6}" cy="{y + 40}" r="6" fill="{escape(lang["color"])}"/>'
            f'<text class="sans" x="{lx + 20}" y="{y + 45}" font-size="15" fill="{BODY}">{name}'
            f' <tspan fill="{MUTED}">{pct}</tspan></text></g>'
        )
        # Rough text width estimate for the next legend item.
        lx += 20 + int(len(lang["name"]) * 7.9 + len(pct) * 7.4) + 30
    return bar + "".join(legend)


def snow() -> str:
    rng = random.Random(SNOW_SEED)
    flakes = []
    for _ in range(SNOW_COUNT):
        x = rng.uniform(10, W - 10)
        r = rng.uniform(0.8, 2.4)
        op = rng.uniform(0.25, 0.8)
        dur = rng.uniform(7, 16)
        delay = -rng.uniform(0, dur)
        sway_dur = rng.uniform(2.5, 5)
        sway_delay = -rng.uniform(0, sway_dur)
        flakes.append(
            f'<g class="fall" style="animation-duration:{dur:.2f}s;animation-delay:{delay:.2f}s">'
            f'<circle class="sway" cx="{x:.1f}" cy="0" r="{r:.2f}" fill="#ffffff" opacity="{op:.2f}" '
            f'style="animation-duration:{sway_dur:.2f}s;animation-delay:{sway_delay:.2f}s"/></g>'
        )
    return f'<g class="snow">{"".join(flakes)}</g>'


def render(stats: dict, last_year: dict, langs: list[dict]) -> str:
    tiles_y, tile_h = 64, 132
    tile_w = (W - 2 * PAD - 3 * 20) // 4
    tiles = "".join([
        tile(1, PAD + 0 * (tile_w + 20), tiles_y, tile_w, tile_h, BLUE,
             fmt(stats["commits"]), "All-time commits", f"since {stats['created_year']}"),
        tile(2, PAD + 1 * (tile_w + 20), tiles_y, tile_w, tile_h, RED,
             fmt(stats["last_year"]), "Contributions", "last 12 months"),
        tile(3, PAD + 2 * (tile_w + 20), tiles_y, tile_w, tile_h, YELLOW,
             fmt(stats["current_streak"]), "Current streak",
             "day" if stats["current_streak"] == 1 else "days"),
        tile(4, PAD + 3 * (tile_w + 20), tiles_y, tile_w, tile_h, GREEN,
             fmt(stats["longest_streak"]), "Longest streak",
             "day" if stats["longest_streak"] == 1 else "days"),
    ])

    # Row 2: heatmap panel.
    hm_top, hm_h = 212, 172
    hm_x0, hm_y0 = PAD + 56, hm_top + 36
    hm_svg, hm_info = heatmap(last_year, hm_x0, hm_y0)
    side_x = hm_x0 + 53 * STEP + 22
    best = hm_info["best_count"]
    best_date = hm_info["best_date"].strftime("%b %d, %Y") if hm_info["best_date"] else "-"
    legend_cells = "".join(
        f'<rect x="{side_x + 34 + i * 14}" y="{hm_y0 + 6 * STEP - 11}" width="11" height="11" rx="2" fill="{c}"/>'
        for i, c in enumerate(LEVELS)
    )
    side = f"""
    <g class="fade" style="animation-delay:1.2s">
      <text class="mono" x="{side_x}" y="{hm_y0 + 4}" font-size="12" fill="{MUTED}">best day</text>
      <text class="sans" x="{side_x}" y="{hm_y0 + 40}" font-size="32" font-weight="800" fill="{LEVELS[4]}">{fmt(best)}</text>
      <text class="sans" x="{side_x}" y="{hm_y0 + 60}" font-size="13" fill="{BODY}">{escape(best_date)}</text>
      <text class="mono" x="{side_x}" y="{hm_y0 + 6 * STEP - 2}" font-size="11" fill="{MUTED}">Less</text>
      {legend_cells}
      <text class="mono" x="{side_x + 34 + 5 * 14 + 4}" y="{hm_y0 + 6 * STEP - 2}" font-size="11" fill="{MUTED}">More</text>
    </g>"""

    # Row 3: languages panel.
    ln_top, ln_h = 400, 120
    ln_x = PAD + 24
    ln_w = W - 2 * PAD - 48
    extra = " · ".join([plural(stats["repos"], "repository", "repositories"),
                        plural(stats["prs"], "PR"), plural(stats["followers"], "follower")])
    lang_svg = languages_block(langs, ln_x, ln_top + 46, ln_w) if langs else (
        f'<text class="sans" x="{ln_x}" y="{ln_top + 70}" font-size="14" fill="{MUTED}">No language data yet</text>')

    aria = (f"GitHub stats: {fmt(stats['commits'])} all-time commits, {fmt(stats['last_year'])} "
            f"contributions in the last 12 months, current streak {stats['current_streak']} days, "
            f"longest streak {stats['longest_streak']} days. Top languages: "
            + ", ".join(f"{l['name']} {l['share'] * 100:.1f}%" for l in langs))

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(aria)}">
  <title>GitHub stats</title>
  <defs>
    <clipPath id="card"><rect width="{W}" height="{H}" rx="24"/></clipPath>
    <filter id="blur" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="70"/></filter>
    <pattern id="dots" width="28" height="28" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1.2" fill="#ffffff"/></pattern>
    <linearGradient id="gbar" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="{BLUE}"/><stop offset=".25" stop-color="{BLUE}"/>
      <stop offset=".25" stop-color="{RED}"/><stop offset=".5" stop-color="{RED}"/>
      <stop offset=".5" stop-color="{YELLOW}"/><stop offset=".75" stop-color="{YELLOW}"/>
      <stop offset=".75" stop-color="{GREEN}"/><stop offset="1" stop-color="{GREEN}"/>
    </linearGradient>
  </defs>
  <style>{css()}  </style>

  <g clip-path="url(#card)">
    <!-- Background -->
    <rect width="{W}" height="{H}" fill="#0d1117"/>
    <g filter="url(#blur)" opacity=".16">
      <circle cx="120" cy="60" r="160" fill="{BLUE}"/>
      <circle cx="1100" cy="520" r="160" fill="{GREEN}"/>
    </g>
    <rect width="{W}" height="{H}" fill="url(#dots)" opacity=".04"/>

    <!-- Header -->
    <g class="up mono" font-size="15">
      <text x="{PAD}" y="42" fill="{MUTED}">~/activity <tspan fill="{GREEN}">$</tspan> <tspan fill="{BODY}">git log --stats</tspan></text>
      <text x="{W - PAD}" y="42" fill="{MUTED}" text-anchor="end" font-size="13">updated {stats['updated']}</text>
    </g>

    <!-- Row 1: stat tiles -->{tiles}

    <!-- Row 2: contribution heatmap -->
    <rect class="fade" x="{PAD}" y="{hm_top}" width="{W - 2 * PAD}" height="{hm_h}" rx="18" fill="#161b22" fill-opacity=".55" stroke="{BORDER}" stroke-width="1.5" style="animation-delay:.4s"/>
    {hm_svg}
    {side}

    <!-- Row 3: top languages -->
    <rect class="fade" x="{PAD}" y="{ln_top}" width="{W - 2 * PAD}" height="{ln_h}" rx="18" fill="#161b22" fill-opacity=".55" stroke="{BORDER}" stroke-width="1.5" style="animation-delay:1.2s"/>
    <g class="fade" style="animation-delay:1.4s">
      <text class="mono" x="{ln_x}" y="{ln_top + 30}" font-size="13" fill="{MUTED}">top languages <tspan fill="{BODY}">by bytes</tspan></text>
      <text class="sans" x="{W - PAD - 24}" y="{ln_top + 30}" font-size="13" fill="{MUTED}" text-anchor="end">{escape(extra)}</text>
    </g>
    {lang_svg}

    <!-- Google colour bar -->
    <rect x="0" y="{H - 6}" width="{W}" height="6" fill="url(#gbar)"/>

    <!-- Snow -->
    {snow()}
  </g>
</svg>
"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def read_previous_commits() -> int | None:
    """Return the commit total recorded in the current SVG, if any."""
    try:
        text = OUTPUT.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"<!--stats:commits=(\d+)-->", text)
    return int(match.group(1)) if match else None


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    login = os.environ.get("GH_USER", "").strip() or "votranngochuu"
    if not token:
        print("[stats] GITHUB_TOKEN is not set", file=sys.stderr)
        return 1

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    try:
        profile = fetch_profile(token, login)
        created = parse_ts(profile["createdAt"])
        last_year, years = fetch_contributions(token, login, created, now)
        repo_count, raw_langs = fetch_repositories(token, login)
    except (ApiError, KeyError, TypeError, ValueError) as exc:
        print(f"[stats] giving up, existing SVG left untouched: {exc}", file=sys.stderr)
        return 1

    days = daily_counts(years.values())
    current, longest = streaks(days, now.date())
    langs = top_languages(raw_langs)
    stats = {
        # Private-repo work shows up as restricted contributions when the token cannot
        # see those repos (e.g. the Actions GITHUB_TOKEN), so add it to keep the count honest.
        "commits": sum(y["totalCommitContributions"] + y["restrictedContributionsCount"]
                       for y in years.values()),
        "restricted": sum(y["restrictedContributionsCount"] for y in years.values()),
        "prs": sum(y["totalPullRequestContributions"] for y in years.values()),
        "issues": sum(y["totalIssueContributions"] for y in years.values()),
        "reviews": sum(y["totalPullRequestReviewContributions"] for y in years.values()),
        "last_year": last_year["contributionCalendar"]["totalContributions"],
        "current_streak": current,
        "longest_streak": longest,
        "repos": repo_count,
        "followers": profile["followers"]["totalCount"],
        "created_year": created.year,
        "updated": now.date().isoformat(),
    }

    # Commit totals never shrink in reality. A big drop means this token sees less than
    # the token that produced the current card (e.g. private profile or private repos),
    # so keep the existing card instead of publishing misleading numbers.
    previous = read_previous_commits()
    if previous and stats["commits"] < previous * 0.9:
        print(f"[stats] new total {stats['commits']} is far below the current {previous}; "
              "keeping the existing SVG (add a STATS_TOKEN secret or enable private "
              "contributions to refresh it).", file=sys.stderr)
        return 0

    svg = render(stats, last_year, langs) + f"<!--stats:commits={stats['commits']}-->\n"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix(".svg.tmp")
    tmp.write_text(svg, encoding="utf-8", newline="\n")
    os.replace(tmp, OUTPUT)

    summary = {k: v for k, v in stats.items()}
    summary["languages"] = [f"{l['name']} {l['share'] * 100:.1f}%" for l in langs]
    print(json.dumps(summary, indent=2))
    print(f"[stats] wrote {OUTPUT} ({len(svg.encode()):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
