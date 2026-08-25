#!/usr/bin/env python3
"""Parse the dynasty rankings table in projections.html into structured JSON.

The page is a server-rendered Alpine.js table: every value for every scoring
scheme is already present in the DOM as ``data-scoring-value-*`` attributes on
each cell, and the front-end just toggles which one is displayed. That means we
can recover all eight scoring schemes from the static HTML without running JS.

Row shape (one <tbody> per player):

    <tbody data-player-row data-key="9962" data-player-name="Josh Allen"
           data-fantasy-position="QB" data-team-id="4" data-is-rookie="false"
           data-tier-overall="1" data-tier-positional="1"
           data-percent-low="19.0" data-percent-high="16.0">
      <tr class="player-row">
        <td class="rank ...">1</td>
        <td class="player-cell ...">  <!-- name / team / positional rank -->
        <td data-attribute="dsValue" data-value="100"
            data-scoring-value="49" data-scoring-value-half-ppr="49" ...>
        ...
      </tr>
      <tr class="detail-view-row">   <!-- duplicate values, ignored -->
    </tbody>

``data-value`` holds the currently-rendered scheme, which for this export is
.5 PPR superflex, at full precision (e.g. 93.8 where the rounded cell shows 94).

Caveat on the printed ranks: this HTML was saved from a hydrated page, and the
rank numbers baked into the markup are partially stale relative to the values
sitting next to them (62 overall ranks are duplicated, 62 are missing, and one
place in the document order jumps backwards). They are preserved verbatim as
``rank_displayed``/``positional_rank_displayed``, and this module additionally
derives clean ``rank_by_3d_value``/``positional_rank_by_3d_value`` from the
.5 PPR superflex 3D values, which are self-consistent.

Usage:
    uv run parse_projections.py [input.html] [-o output.json] [--report]
"""

from __future__ import annotations

import argparse
import json
import sys
from html.parser import HTMLParser
from pathlib import Path

import paths

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

#: ``data-scoring-value*`` attribute -> canonical scheme name.
#: Four scoring styles x {1QB, superflex}. The bare attribute is standard/non-PPR.
SCORING_SCHEMES: dict[str, str] = {
    "data-scoring-value": "standard",
    "data-scoring-value-half-ppr": "half_ppr",
    "data-scoring-value-ppr": "ppr",
    "data-scoring-value-te-premium": "te_premium",
    "data-scoring-value-superflex": "standard_superflex",
    "data-scoring-value-half-ppr-superflex": "half_ppr_superflex",
    "data-scoring-value-ppr-superflex": "ppr_superflex",
    "data-scoring-value-te-premium-superflex": "te_premium_superflex",
}

#: The scheme the page was rendered in, i.e. what bare ``data-value`` reflects.
DEFAULT_SCHEME = "half_ppr_superflex"

#: ``data-attribute`` -> (output key, varies-by-scoring-scheme?)
COLUMNS: dict[str, tuple[str, bool]] = {
    "adp": ("adp", True),
    "fantasy_points": ("proj_1yr", True),
    "threeYrPts": ("proj_3yr", True),
    "fiveYrPts": ("proj_5yr", True),
    "tenYrPts": ("proj_10yr", True),
    "dsValue": ("three_d_value", True),
    "player.age": ("age", False),
    "player.team.bye": ("bye_week", False),
    "comment": ("analysis", False),
}

#: Projection horizons folded into a nested ``projections`` object.
PROJECTION_KEYS = {
    "proj_1yr": "1yr",
    "proj_3yr": "3yr",
    "proj_5yr": "5yr",
    "proj_10yr": "10yr",
}

COLUMN_DEFINITIONS = {
    "adp": "Average draft position based closely on your league scoring rules.",
    "proj_1yr": "Current-year projected fantasy points.",
    "proj_3yr": "Three-year projected fantasy points.",
    "proj_5yr": "Five-year projected fantasy points.",
    "proj_10yr": "Ten-year projected fantasy points.",
    "three_d_value": (
        "'3D Value' — the player's overall dynasty value derived from his "
        "multi-year projections, the league's scoring rules, and team needs. "
        "Scaled so the most valuable player in a scheme is 100."
    ),
    "age": "Player age in years.",
    "bye_week": "Team bye week.",
    "analysis": "Free-text dynasty analyst comment ('DS Analysis').",
    "percent_low": "Low end of the row's percentage range (undocumented in page).",
    "percent_high": "High end of the row's percentage range (undocumented in page).",
    "hidden_row": "Row carried class='hidden-row', i.e. hidden in the saved on-page view.",
    "rank_displayed": (
        "Overall rank exactly as printed in the markup. Partially stale in this "
        "snapshot — contains duplicates and gaps; prefer rank_by_3d_value."
    ),
    "rank_by_3d_value": (
        "Derived overall rank, 1..N, by .5 PPR superflex 3D value descending "
        "(ties broken by document order). Unique and gap-free."
    ),
    "positional_rank_displayed": "Positional rank as printed (e.g. 'WR12' -> 12).",
    "positional_rank_by_3d_value": (
        "Derived positional rank by .5 PPR superflex 3D value descending."
    ),
    "document_index": "0-based position of the row in the HTML, for reproducing on-page order.",
}


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def to_number(raw: str | None) -> int | float | None:
    """Coerce a cell value to int/float, or None when absent or non-numeric.

    ADP values like "1.01" stay floats; point totals stay ints.
    """
    if raw is None:
        return None
    text = raw.strip().replace(",", "")
    if not text or text in {"-", "--", "N/A"}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if value.is_integer() and "." not in text else value


def to_bool(raw: str | None) -> bool:
    return str(raw).strip().lower() == "true"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class RankingsParser(HTMLParser):
    """Streaming parser that emits one record per ``data-player-row`` tbody."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.players: list[dict] = []
        #: (player_index, attribute, precise, rounded) where data-value and the
        #: default scheme's value disagree — surfaced by --report.
        self.default_scheme_mismatches: list[tuple[int, str, object, object]] = []

        self._row: dict | None = None  # tbody-level attrs of current player
        self._cells: dict[str, dict[str, str]] = {}  # data-attribute -> attrs
        self._in_player_row = False  # inside <tr class="player-row">
        self._cell_attrs: dict[str, str] | None = None
        self._capture: str | None = None  # which buffer text goes to
        self._buf: list[str] = []
        self._text: dict[str, str] = {}  # capture name -> collected text

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {name: (value if value is not None else "") for name, value in attrs}

    @staticmethod
    def _classes(attrs: dict[str, str]) -> set[str]:
        return set(attrs.get("class", "").split())

    def _start_capture(self, name: str) -> None:
        self._capture = name
        self._buf = []

    def _end_capture(self) -> None:
        if self._capture is not None:
            self._text[self._capture] = "".join(self._buf).strip()
            self._capture = None
            self._buf = []

    # -- HTMLParser hooks -------------------------------------------------

    def handle_startendtag(self, tag, attrs):  # <img/>, <br/>
        self.handle_starttag(tag, attrs)

    def handle_starttag(self, tag, attrs):
        a = self._attrs(attrs)

        if tag == "tbody":
            if "data-player-row" in a:
                self._row = a
                self._cells = {}
                self._text = {}
            return

        if self._row is None:
            return

        if tag == "tr":
            self._in_player_row = "player-row" in self._classes(a)
            return

        if not self._in_player_row:
            return  # ignore the collapsed detail-view row (duplicate values)

        if tag == "td":
            classes = self._classes(a)
            if "data-attribute" in a:
                self._cell_attrs = a
                self._start_capture("cell")
            elif "rank" in classes:
                self._start_capture("rank")
            return

        # Player identity lives in the sticky name cell as custom elements.
        if tag == "player-name":
            self._text["first_name"] = a.get("first-name", "")
            self._text["last_name"] = a.get("last-name", "")
            self._text["player_id"] = a.get("player-id", "")
        elif tag == "pos-roster-spot":
            self._text["position_from_spot"] = a.get("pos-roster-spot", "")
            self._start_capture("positional_rank_label")
        elif tag == "a" and "player-name-responsive" in self._classes(a):
            self._text["profile_path"] = a.get("href", "")
        elif tag == "img" and "team-badge" in self._classes(a):
            self._text["team_from_badge"] = Path(a.get("src", "")).stem
        elif tag == "span" and "player-details-group__team-name" in self._classes(a):
            self._start_capture("team")

    def handle_data(self, data):
        if self._capture is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if self._row is None:
            return

        if tag in {"td", "span", "pos-roster-spot"} and self._capture is not None:
            # The name/team spans and the roster-spot element close before their
            # <td> does; cell text is flushed on </td>.
            if tag == "td" or self._capture in {"team", "positional_rank_label"}:
                self._end_capture()
                if tag == "td" and self._cell_attrs is not None:
                    self._cells[self._cell_attrs["data-attribute"]] = self._cell_attrs
                    self._cell_attrs = None

        if tag == "tr":
            self._in_player_row = False
        elif tag == "tbody":
            self.players.append(self._build_player())
            self._row = None

    # -- record assembly --------------------------------------------------

    def _build_player(self) -> dict:
        row, cells, text = self._row or {}, self._cells, self._text
        index = len(self.players)

        player: dict = {
            "document_index": index,
            "rank_displayed": to_number(text.get("rank")),
            "rank_by_3d_value": None,  # filled in by assign_derived_ranks()
            "player_id": to_number(row.get("data-key")),
            "name": row.get("data-player-name", "").strip(),
            "first_name": text.get("first_name", "").strip(),
            "last_name": text.get("last_name", "").strip(),
            "position": row.get("data-fantasy-position", ""),
            "positional_rank_label": text.get("positional_rank_label", ""),
            "positional_rank_displayed": None,
            "positional_rank_by_3d_value": None,  # filled in by assign_derived_ranks()
            "team": text.get("team") or text.get("team_from_badge", ""),
            "team_id": to_number(row.get("data-team-id")),
            "age": None,
            "bye_week": None,
            "is_rookie": to_bool(row.get("data-is-rookie")),
            "tier_overall": to_number(row.get("data-tier-overall")),
            "tier_positional": to_number(row.get("data-tier-positional")),
            "percent_low": to_number(row.get("data-percent-low")),
            "percent_high": to_number(row.get("data-percent-high")),
            "hidden_row": "hidden-row" in self._classes(row),
            "profile_path": text.get("profile_path", ""),
            "analysis": "",
        }

        # "WR12" -> 12; falls back to None if the label is missing.
        label, position = player["positional_rank_label"], player["position"]
        if label.startswith(position):
            player["positional_rank_displayed"] = to_number(label[len(position) :])

        scheme_values: dict[str, dict[str, object]] = {}

        for attribute, cell in cells.items():
            key, varies = COLUMNS.get(attribute, (attribute, False))
            raw_default = cell.get("data-value")

            if not varies:
                player[key] = raw_default.strip() if key == "analysis" else to_number(raw_default)
                continue

            per_scheme = {
                name: to_number(cell.get(data_attr))
                for data_attr, name in SCORING_SCHEMES.items()
            }
            scheme_values[key] = per_scheme

            # data-value is the rendered (default) scheme at full precision.
            precise = to_number(raw_default)
            per_scheme[f"{DEFAULT_SCHEME}_precise"] = precise
            rounded = per_scheme[DEFAULT_SCHEME]
            if precise is not None and rounded is not None and abs(precise - rounded) > 0.5001:
                self.default_scheme_mismatches.append((index, key, precise, rounded))

        # Headline figure the draft board is built around.
        three_d = scheme_values.get("three_d_value", {})
        player["half_ppr_superflex_3d_value"] = three_d.get(
            f"{DEFAULT_SCHEME}_precise", three_d.get(DEFAULT_SCHEME)
        )
        player["three_d_value"] = three_d
        player["adp"] = scheme_values.get("adp", {})
        player["projections"] = {
            horizon: scheme_values.get(key, {})
            for key, horizon in PROJECTION_KEYS.items()
        }

        return player


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def assign_derived_ranks(players: list[dict]) -> None:
    """Add overall and positional ranks derived from the .5 PPR superflex 3D value.

    The values baked into this snapshot's markup are internally inconsistent, so
    we recompute a clean ordering. Ties fall back to document order, which is the
    site's own ordering, keeping the result stable and close to the on-page view.
    """

    def sort_key(player: dict) -> tuple[float, int]:
        value = player["half_ppr_superflex_3d_value"]
        return (-(value if value is not None else float("-inf")), player["document_index"])

    for rank, player in enumerate(sorted(players, key=sort_key), start=1):
        player["rank_by_3d_value"] = rank

    positional_counter: dict[str, int] = {}
    for player in sorted(players, key=sort_key):
        position = player["position"]
        positional_counter[position] = positional_counter.get(position, 0) + 1
        player["positional_rank_by_3d_value"] = positional_counter[position]


def parse_file(path: Path) -> tuple[list[dict], RankingsParser]:
    parser = RankingsParser()
    with path.open(encoding="utf-8") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), ""):
            parser.feed(chunk)
    parser.close()
    assign_derived_ranks(parser.players)
    return parser.players, parser


def build_document(players: list[dict], source: Path) -> dict:
    return {
        "source_file": source.name,
        "default_scoring_scheme": DEFAULT_SCHEME,
        "default_scheme_note": (
            "The page was rendered in .5 PPR superflex, so each cell's bare "
            "data-value carries that scheme at full precision. It is exposed "
            "as '<scheme>_precise' alongside the rounded per-scheme values, "
            "and as the top-level 'half_ppr_superflex_3d_value'."
        ),
        "scoring_schemes": list(SCORING_SCHEMES.values()),
        "column_definitions": COLUMN_DEFINITIONS,
        "player_count": len(players),
        "players": players,
    }


def report(players: list[dict], parser: RankingsParser) -> None:
    out = sys.stderr
    print(f"players parsed: {len(players)}", file=out)

    ids = [p["player_id"] for p in players]
    print(f"unique player_ids: {len(set(ids))}/{len(ids)}", file=out)

    shown = [p["rank_displayed"] for p in players if p["rank_displayed"] is not None]
    expected = list(range(1, len(players) + 1))
    duplicates = len(shown) - len(set(shown))
    print(
        f"rank_displayed: {len(shown)} present, range {min(shown)}-{max(shown)}, "
        f"duplicates={duplicates}, gaps={len(set(expected) - set(shown))} "
        f"(stale in this snapshot — use rank_by_3d_value)",
        file=out,
    )
    derived = sorted(p["rank_by_3d_value"] for p in players)
    print(f"rank_by_3d_value: unique and gap-free 1..{len(players)}: {derived == expected}", file=out)

    values = [p["half_ppr_superflex_3d_value"] for p in players]
    regressions = sum(1 for a, b in zip(values, values[1:]) if a is not None and b is not None and a < b)
    print(
        f"document order descending by .5 PPR SF 3D value: {regressions} regression(s) "
        f"out of {len(players) - 1} adjacent pairs",
        file=out,
    )

    by_position: dict[str, int] = {}
    for player in players:
        by_position[player["position"]] = by_position.get(player["position"], 0) + 1
    print(f"positions: {dict(sorted(by_position.items()))}", file=out)
    print(
        f"rookies: {sum(p['is_rookie'] for p in players)}, "
        f"hidden rows: {sum(p['hidden_row'] for p in players)}",
        file=out,
    )

    required = ("name", "position", "team", "player_id", "half_ppr_superflex_3d_value")
    for field in required:
        missing = [p["name"] or "<unnamed>" for p in players if not p.get(field) and p.get(field) != 0]
        status = "ok" if not missing else f"MISSING {len(missing)}: {missing[:5]}"
        print(f"  {field}: {status}", file=out)

    for field in ("age", "bye_week", "analysis"):
        blank = sum(1 for p in players if p.get(field) in (None, ""))
        print(f"  {field}: {len(players) - blank}/{len(players)} populated", file=out)

    schemes = list(SCORING_SCHEMES.values())
    incomplete = sum(
        1
        for p in players
        if any(p["three_d_value"].get(s) is None for s in schemes)
        or any(p["projections"][h].get(s) is None for h in PROJECTION_KEYS.values() for s in schemes)
    )
    print(f"  all 8 schemes present for 3D value + 4 horizons: {len(players) - incomplete}/{len(players)}", file=out)

    mismatches = parser.default_scheme_mismatches
    print(
        f"  data-value vs {DEFAULT_SCHEME} rounding disagreements: {len(mismatches)}"
        + (f" e.g. {mismatches[:3]}" if mismatches else " (confirms default view is .5 PPR SF)"),
        file=out,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", default=paths.PROJECTIONS_HTML, type=Path)
    ap.add_argument("-o", "--output", default=paths.PROJECTIONS_JSON, type=Path)
    ap.add_argument("--report", action="store_true", help="print a validation summary to stderr")
    ap.add_argument("--indent", type=int, default=2, help="JSON indent; 0 for compact")
    args = ap.parse_args(argv)

    if not args.input.is_file():
        print(f"error: {args.input} not found", file=sys.stderr)
        return 1

    players, parser = parse_file(args.input)
    if not players:
        print("error: no player rows found — did the page layout change?", file=sys.stderr)
        return 1

    document = build_document(players, args.input)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=args.indent or None, ensure_ascii=False)
        handle.write("\n")

    print(f"wrote {len(players)} players to {paths.display(args.output)}", file=sys.stderr)
    if args.report:
        report(players, parser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
