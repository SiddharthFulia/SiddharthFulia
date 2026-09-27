#!/usr/bin/env python3
"""
Build a skill-radar SVG from GitHub language usage across all repos
(public + private for the token owner).

Output: assets/skill-radar.svg

Env:
  GITHUB_TOKEN   (required) - a GitHub token with repo read access
  USERNAME       (required) - GitHub login to aggregate (e.g. SiddharthFulia)

Exits 1 if GraphQL returns no data, so CI fails visibly rather than
committing a broken SVG.
"""
from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.request

GRAPHQL_URL = "https://api.github.com/graphql"

# Languages we never want on the radar (build files, docs, config noise,
# or notebook JSON that inflates byte counts artificially).
EXCLUDE = {
    "Dockerfile",
    "Shell",
    "Batchfile",
    "Makefile",
    "CMake",
    "Jupyter Notebook",   # notebook JSON bloats — misrepresents actual code volume
    "Text",
    "Roff",
    "M4",
    "Nix",
    "Vim Script",
    "Vim Snippet",
    "Emacs Lisp",
    "Gnuplot",
    "Awk",
    "Sed",
    "PowerShell",
    "Rich Text Format",
    "TeX",
    "MDX",
    "Astro",
    "SCSS",
    "Less",
    "PostCSS",
}

TOP_N = 9  # matches the reference radar density

# Per-repo bytes get sqrt-normalised before summing so a single huge repo
# (e.g. a portfolio with megabytes of bundled JS) can't drown out breadth
# across many repos. Result: mix reads as "breadth of use", not "size of
# biggest project".
def normalise_bytes(size: int) -> float:
    return math.sqrt(max(0, size))

QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    repositories(
      first: 100,
      after: $after,
      ownerAffiliations: [OWNER],
      isFork: false,
      privacy: null
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        isArchived
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
          edges {
            size
            node { name }
          }
        }
      }
    }
  }
}
"""


def gh_graphql(token: str, query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "skill-radar-builder",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_language_bytes(token: str, login: str) -> dict[str, float]:
    """Aggregate sqrt(bytes) per repo per language. sqrt-per-repo pre-sum
    flattens single-repo dominance (one 100 MB bundle contributes 10× a
    1 MB script, not 100×) so the radar reflects breadth-of-use across
    the portfolio rather than the size of the biggest single project."""
    totals: dict[str, float] = {}
    cursor = None
    while True:
        payload = gh_graphql(token, QUERY, {"login": login, "after": cursor})
        if "errors" in payload:
            print("GraphQL errors:", json.dumps(payload["errors"], indent=2), file=sys.stderr)
            sys.exit(1)
        data = payload.get("data") or {}
        user = data.get("user")
        if not user:
            print("No user data returned", file=sys.stderr)
            sys.exit(1)
        repos = user["repositories"]
        for node in repos["nodes"]:
            for edge in node["languages"]["edges"]:
                name = edge["node"]["name"]
                totals[name] = totals.get(name, 0.0) + normalise_bytes(edge["size"])
        page = repos["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]
    return totals


def pick_languages(totals: dict[str, float], top_n: int = TOP_N) -> list[tuple[str, float]]:
    filtered: list[tuple[str, float]] = []
    for name, weight in totals.items():
        if name in EXCLUDE:
            continue
        filtered.append((name, weight))
    filtered.sort(key=lambda kv: kv[1], reverse=True)
    return filtered[:top_n]


def build_svg(items: list[tuple[str, int]]) -> str:
    # Percentages relative to sum of picked languages so radar fills nicely.
    total = sum(size for _, size in items) or 1
    entries = [(name, size, size / total * 100.0) for name, size in items]

    # Sort so largest points sit at the top and neighbors are visually balanced.
    # Alternate large-small around the circle for a more organic silhouette.
    entries.sort(key=lambda t: t[2], reverse=True)
    balanced: list[tuple[str, int, float]] = []
    left, right = 0, len(entries) - 1
    take_left = True
    while left <= right:
        if take_left:
            balanced.append(entries[left])
            left += 1
        else:
            balanced.append(entries[right])
            right -= 1
        take_left = not take_left

    entries = balanced

    W = 720
    H = 720
    cx, cy = W / 2, H / 2 + 10  # nudge down to leave room for title
    R = 220  # max axis radius

    n = len(entries)
    # Start axis at top (-90deg), go clockwise
    def angle(i: int) -> float:
        return -math.pi / 2 + (2 * math.pi * i / n)

    # Build the 4 grid rings
    grid_svg = []
    for ring in range(1, 5):
        r = R * ring / 4
        pts = []
        for i in range(n):
            a = angle(i)
            pts.append(f"{cx + r*math.cos(a):.1f},{cy + r*math.sin(a):.1f}")
        grid_svg.append(
            f'<polygon points="{" ".join(pts)}" fill="none" stroke="rgba(255,255,255,0.10)" stroke-width="1" stroke-dasharray="3 3" />'
        )

    # Axis spokes
    spokes_svg = []
    for i in range(n):
        a = angle(i)
        x2 = cx + R * math.cos(a)
        y2 = cy + R * math.sin(a)
        spokes_svg.append(
            f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="rgba(255,255,255,0.08)" stroke-width="1" />'
        )

    # Data polygon — scale each vertex by its percentage / max-pct
    max_pct = max(pct for _, _, pct in entries) or 1
    data_pts = []
    vertex_dots = []
    for i, (_, _, pct) in enumerate(entries):
        a = angle(i)
        r = R * (pct / max_pct)
        x = cx + r * math.cos(a)
        y = cy + r * math.sin(a)
        data_pts.append(f"{x:.1f},{y:.1f}")
        vertex_dots.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="#22c55e" />'
        )

    data_poly = (
        f'<polygon points="{" ".join(data_pts)}" '
        f'fill="rgba(34,197,94,0.25)" stroke="#22c55e" stroke-width="2" '
        f'stroke-linejoin="round" />'
    )

    # Labels around outside
    labels_svg = []
    label_r = R + 34
    for i, (name, _, pct) in enumerate(entries):
        a = angle(i)
        lx = cx + label_r * math.cos(a)
        ly = cy + label_r * math.sin(a)

        # anchor decision based on x offset
        dx = math.cos(a)
        if dx > 0.15:
            anchor = "start"
        elif dx < -0.15:
            anchor = "end"
        else:
            anchor = "middle"

        # vertical offset for two lines
        line1_dy = 0
        line2_dy = 16
        # if near top/bottom, tighten so labels don't collide with radar
        dy_shift = 0
        if math.sin(a) < -0.85:
            dy_shift = -8  # near top: push both lines up
            line1_dy += dy_shift
            line2_dy += dy_shift
        elif math.sin(a) > 0.85:
            dy_shift = 6  # near bottom: push down slightly
            line1_dy += dy_shift
            line2_dy += dy_shift

        labels_svg.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" '
            f'font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" '
            f'font-size="13" fill="#e5e5e5" dy="{line1_dy}">{escape(name)}</text>'
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" '
            f'font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" '
            f'font-size="12" font-weight="700" fill="#22c55e" dy="{line2_dy}">{pct:.1f}%</text>'
        )

    title = (
        f'<text x="{W/2:.0f}" y="46" text-anchor="middle" '
        f'font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" '
        f'font-size="18" fill="#22c55e" font-weight="700">skill radar</text>'
        f'<text x="{W/2:.0f}" y="70" text-anchor="middle" '
        f'font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" '
        f'font-size="11" fill="rgba(229,229,229,0.55)">language mix across all my repos</text>'
    )

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Skill radar chart of language usage across all repos">
  <rect x="0" y="0" width="{W}" height="{H}" fill="#0a0a0e" />
  {title}
  <g>
    {chr(10).join(grid_svg)}
    {chr(10).join(spokes_svg)}
  </g>
  {data_poly}
  {chr(10).join(vertex_dots)}
  <g>
    {chr(10).join(labels_svg)}
  </g>
</svg>
'''
    return svg


def escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    username = os.environ.get("USERNAME")
    if not token or not username:
        print("GITHUB_TOKEN and USERNAME env vars required", file=sys.stderr)
        return 1

    totals = fetch_language_bytes(token, username)
    if not totals:
        print("No language data returned from GraphQL", file=sys.stderr)
        return 1

    picked = pick_languages(totals)
    if len(picked) < 3:
        print(f"Not enough languages to draw a radar (got {len(picked)})", file=sys.stderr)
        return 1

    # Log picked mix so CI logs show what we drew
    total_bytes = sum(size for _, size in picked)
    print("Picked languages:")
    for name, size in picked:
        pct = size / total_bytes * 100.0
        print(f"  {name:<15} {size:>12,}  {pct:5.1f}%")

    svg = build_svg(picked)
    out_dir = os.path.join(os.path.dirname(__file__), "..", "assets")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "skill-radar.svg")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"Wrote {out_path} ({len(svg)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
