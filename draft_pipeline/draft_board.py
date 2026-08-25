"""Draft geometry, pick ownership, and the draft.json document contract."""

from __future__ import annotations

import collections
import datetime as dt


#: Formats a board can be laid out for. An auction has no pick order to derive.
SUPPORTED_TYPES = ("snake", "linear")

#: Expected position-in-round for draft slot 2 before trades. The report checks the
#: documented geometry, while the live derivation check above validates made picks.
DOCUMENTED_SLOT_2_PICK_IN_ROUND = {1: 2, 2: 9, 3: 9, 4: 2, 5: 9, 6: 2, 28: 2, 29: 9}

FIELD_DEFINITIONS = {
    "pick_no": "Overall pick number, 1..pick_count. Unique, gap-free, and the array order.",
    "round": "Round number, 1..rounds.",
    "pick_in_round": (
        "Position within the round, 1..teams. Differs from draft_slot in a reversed "
        "round — in round 2 of a 10-team snake, pick_in_round 1 is draft_slot 10."
    ),
    "draft_slot": "The board column this pick belongs to, 1..teams.",
    "roster_id": (
        "The Sleeper roster that receives the player — the current owner, which is not "
        "the slot's original owner if the pick was traded."
    ),
    "user_id": "The Sleeper user that owns the pick. Null if the draft order is unpublished.",
    "username": "That user's Sleeper display name. Null if the league user list was unreadable.",
    "is_mine": "True for the configured owner's picks (see `me` in the header).",
    "status": "'made' — Sleeper has recorded a selection — or 'pending'.",
    "sleeper_id": (
        "Sleeper player id of the selection, as a string; null while pending. This is "
        "the join key back to pool.json's sleeper_id."
    ),
    "name": (
        "Sleeper's name for the selection, for reading the file by eye. Informational: "
        "the pool carries the projection provider's own name, and joins go by sleeper_id."
    ),
    "position": "Sleeper's position for the selection. Informational, as above.",
    "team": "Sleeper's NFL team for the selection. Informational, as above.",
    "is_keeper": "True when Sleeper flagged the pick as a keeper. False for a normal pick.",
}


# ---------------------------------------------------------------------------
# Board geometry
# ---------------------------------------------------------------------------


class Board:
    """Which slot, roster and user owns each pick number.

    Everything here comes from the draft object: ``settings`` for the shape,
    ``draft_order`` (user -> slot) and ``slot_to_roster_id`` for who sits where, and
    ``/traded_picks`` for the picks that have since changed hands.
    """

    def __init__(self, draft: dict, traded: list[dict] | None = None):
        settings = draft.get("settings") or {}
        self.type = draft.get("type") or "snake"
        self.teams = int(settings.get("teams") or 0)
        self.rounds = int(settings.get("rounds") or 0)
        self.reversal_round = int(settings.get("reversal_round") or 0)
        self.pick_count = self.teams * self.rounds

        self.slot_to_roster = {
            int(slot): int(roster)
            for slot, roster in (draft.get("slot_to_roster_id") or {}).items()
        }
        # draft_order maps user -> slot; a board is read the other way round.
        self.slot_to_user = {
            int(slot): user for user, slot in (draft.get("draft_order") or {}).items()
        }
        self.roster_to_user = {
            roster: self.slot_to_user.get(slot) for slot, roster in self.slot_to_roster.items()
        }
        self.traded = self._ownership(traded or [], str(draft.get("season") or ""))

    @staticmethod
    def _ownership(traded: list[dict], season: str) -> dict[tuple[int, int], int]:
        """(round, original roster) -> roster that owns that pick now.

        A traded-pick entry names the pick by its *original* owner's ``roster_id``,
        which is exactly how a slot maps onto the board, and ``owner_id`` is who holds
        it now. Entries for another season belong to a different draft in the league.
        """
        moved: dict[tuple[int, int], int] = {}
        for entry in traded:
            if season and str(entry.get("season") or season) != season:
                continue
            try:
                key = (int(entry["round"]), int(entry["roster_id"]))
                moved[key] = int(entry["owner_id"])
            except (KeyError, TypeError, ValueError):
                continue
        return moved

    def problems(self) -> list[str]:
        """Reasons a board cannot be laid out at all."""
        issues = []
        if self.type not in SUPPORTED_TYPES:
            issues.append(f"draft type {self.type!r} has no pick order to derive")
        if self.teams < 1 or self.rounds < 1:
            issues.append(f"settings give {self.teams} teams x {self.rounds} rounds")
        return issues

    def is_reversed(self, round_no: int) -> bool:
        """Does this round run slot teams..1 rather than 1..teams?

        A plain snake alternates, odd rounds forward. A reversal round repeats the
        previous round's order instead of flipping back, so from that round on the
        parity is inverted — reversal_round 3 gives forward, reverse, reverse,
        forward, reverse, forward, ...
        """
        if self.type == "linear":
            return False
        reversed_ = round_no % 2 == 0
        if self.reversal_round and round_no >= self.reversal_round:
            reversed_ = not reversed_
        return reversed_

    def locate(self, pick_no: int) -> tuple[int, int, int]:
        """(round, pick_in_round, draft_slot) for an overall pick number."""
        round_no = (pick_no - 1) // self.teams + 1
        pick_in_round = (pick_no - 1) % self.teams + 1
        slot = (
            self.teams + 1 - pick_in_round if self.is_reversed(round_no) else pick_in_round
        )
        return round_no, pick_in_round, slot

    def owner_roster(self, round_no: int, slot: int) -> int | None:
        """The roster holding this slot's pick in this round, trades applied."""
        original = self.slot_to_roster.get(slot)
        if original is None:
            return None
        return self.traded.get((round_no, original), original)


def round_pick(round_no: int, pick_in_round: int) -> str:
    """``1.02`` — how a draft slot is spoken about, and how README.md writes it."""
    return f"{round_no}.{pick_in_round:02d}"


def index_users(users: list[dict]) -> dict[str, dict]:
    """user id -> display name and team name. Both are free text a human typed, so
    they are stripped; several of this league's team names carry a trailing space."""
    return {
        str(user["user_id"]): {
            "username": (user.get("display_name") or "").strip() or None,
            "team_name": ((user.get("metadata") or {}).get("team_name") or "").strip() or None,
        }
        for user in users
        if isinstance(user, dict) and user.get("user_id")
    }


def resolve_me(username: str, board: Board, by_user: dict[str, dict]) -> dict:
    """Find the configured owner's user id, slot and roster, or say why not."""
    wanted = (username or "").strip().lower()
    match = next(
        (uid for uid, user in by_user.items() if (user["username"] or "").lower() == wanted),
        None,
    )
    if match is None:
        return {"username": username, "user_id": None, "draft_slot": None, "roster_id": None}
    slot = next((s for s, uid in board.slot_to_user.items() if uid == match), None)
    return {
        "username": by_user[match]["username"],
        "user_id": match,
        "draft_slot": slot,
        "roster_id": board.slot_to_roster.get(slot) if slot else None,
    }


def pick_rows(board: Board, picks: list[dict], by_user: dict[str, dict], my_user: str | None):
    """Every pick 1..pick_count, made ones from Sleeper and the rest derived.

    Returns (rows, checks) where checks records how the derivation compared with what
    Sleeper reported for the picks that have been made.
    """
    made = {}
    for pick in picks:
        try:
            made[int(pick["pick_no"])] = pick
        except (KeyError, TypeError, ValueError):
            continue

    mismatches: list[dict] = []
    rows: list[dict] = []

    for pick_no in range(1, board.pick_count + 1):
        round_no, pick_in_round, slot = board.locate(pick_no)
        derived_roster = board.owner_roster(round_no, slot)
        pick = made.get(pick_no)

        if pick is None:
            roster_id = derived_roster
            user_id = board.roster_to_user.get(roster_id) if roster_id else None
            player = {
                "status": "pending",
                "sleeper_id": None,
                "name": None,
                "position": None,
                "team": None,
                "is_keeper": None,
            }
        else:
            # Sleeper reported these, so they win; the derived pair is the thing
            # under test, and any gap is surfaced rather than silently preferred.
            reported_slot = pick.get("draft_slot")
            reported_roster = pick.get("roster_id")
            if (reported_slot is not None and int(reported_slot) != slot) or (
                reported_roster is not None and int(reported_roster) != derived_roster
            ):
                mismatches.append(
                    {
                        "pick_no": pick_no,
                        "reported": {"draft_slot": reported_slot, "roster_id": reported_roster},
                        "derived": {"draft_slot": slot, "roster_id": derived_roster},
                    }
                )
            slot = int(reported_slot) if reported_slot is not None else slot
            roster_id = int(reported_roster) if reported_roster is not None else derived_roster
            # picked_by is empty when the pick was made for the team, not by them.
            user_id = pick.get("picked_by") or board.roster_to_user.get(roster_id)
            meta = pick.get("metadata") or {}
            player = {
                "status": "made",
                "sleeper_id": str(pick["player_id"]) if pick.get("player_id") else None,
                "name": " ".join(
                    part for part in (meta.get("first_name"), meta.get("last_name")) if part
                )
                or None,
                "position": meta.get("position") or None,
                "team": meta.get("team") or None,
                "is_keeper": bool(pick.get("is_keeper")),
            }

        rows.append(
            {
                "pick_no": pick_no,
                "round": round_no,
                "pick_in_round": pick_in_round,
                "draft_slot": slot,
                "roster_id": roster_id,
                "user_id": user_id,
                "username": (by_user.get(str(user_id)) or {}).get("username"),
                "is_mine": bool(my_user) and str(user_id) == str(my_user),
                **player,
            }
        )

    checks = {
        "made_picks_checked": len(made),
        "slot_and_roster_agree": len(made) - len(mismatches),
        "mismatches": mismatches,
        "rounds_exercised": sorted({board.locate(n)[0] for n in made}),
    }
    return rows, checks


def pick_number_problems(picks: list[dict], board: Board) -> tuple[list[str], list[str]]:
    """Check the reported pick numbers. Returns (fatal, notable).

    Fatal means the array indexed by ``pick_no`` would silently lose a pick: a number
    outside the board, or two picks claiming the same one. A *gap* is not fatal —
    those pick numbers simply stay pending, and a keeper draft can legitimately have
    selections recorded before the picks in front of them — so it is only reported.
    """
    numbers = [pick.get("pick_no") for pick in picks]
    fatal, notable = [], []

    bad = [n for n in numbers if not isinstance(n, int) or not 1 <= n <= board.pick_count]
    if bad:
        fatal.append(f"{len(bad)} pick_no outside 1..{board.pick_count}: {bad[:5]}")
    duplicates = [n for n, count in collections.Counter(numbers).items() if count > 1]
    if duplicates:
        fatal.append(f"duplicate pick_no: {duplicates[:5]}")

    clean = sorted(n for n in numbers if isinstance(n, int))
    if clean and clean != list(range(1, len(clean) + 1)):
        missing = sorted(set(range(1, max(clean) + 1)) - set(clean))
        notable.append(
            f"picks are not a gap-free prefix — {len(missing)} earlier pick(s) unmade "
            f"behind pick {max(clean)}, first {missing[:5]}"
        )
    return fatal, notable


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


def iso(epoch_ms) -> str | None:
    """Sleeper's millisecond timestamps, as UTC ISO-8601."""
    if not epoch_ms:
        return None
    try:
        moment = dt.datetime.fromtimestamp(int(epoch_ms) / 1000, dt.timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return moment.isoformat(timespec="seconds")


def build_document(
    fetched: dict,
    board: Board,
    rows: list[dict],
    checks: dict,
    me: dict,
    api: str,
) -> dict:
    draft = fetched["draft"]
    by_user = index_users(fetched["users"])
    on_clock = next((row for row in rows if row["status"] == "pending"), None)
    mine_next = next(
        (row for row in rows if row["status"] == "pending" and row["is_mine"]), None
    )
    made = [row for row in rows if row["status"] == "made"]

    def summarize(row: dict | None) -> dict | None:
        if row is None:
            return None
        summary = {
            key: row[key]
            for key in ("pick_no", "round", "pick_in_round", "draft_slot", "user_id", "username")
        }
        summary["slot"] = round_pick(row["round"], row["pick_in_round"])
        return summary

    next_mine = summarize(mine_next)
    if next_mine and on_clock:
        next_mine["picks_away"] = mine_next["pick_no"] - on_clock["pick_no"]

    return {
        "source": f"{api}/draft/{draft['draft_id']}",
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "draft_id": str(draft["draft_id"]),
        "league_id": draft.get("league_id"),
        "league_name": (draft.get("metadata") or {}).get("name"),
        "season": draft.get("season"),
        "status": draft.get("status"),
        "started_at": iso(draft.get("start_time")),
        "last_picked_at": iso(draft.get("last_picked")),
        "format": {
            "type": board.type,
            "teams": board.teams,
            "rounds": board.rounds,
            "reversal_round": board.reversal_round or None,
            "scoring_type": (draft.get("metadata") or {}).get("scoring_type"),
        },
        "pick_count": board.pick_count,
        "picks_made": len(made),
        "picks_pending": board.pick_count - len(made),
        "on_the_clock": summarize(on_clock),
        "me": me,
        "my_next_pick": next_mine,
        "board_derivation": {
            "method": (
                f"{board.type}"
                + (f", order reverses at round {board.reversal_round}" if board.reversal_round else "")
                + "; traded picks applied"
            ),
            "checked_against_made_picks": checks["made_picks_checked"],
            "slot_and_roster_agree": checks["slot_and_roster_agree"],
            "rounds_exercised": checks["rounds_exercised"],
            "mismatches": checks["mismatches"],
        },
        "slots": [
            {
                "draft_slot": slot,
                "roster_id": board.slot_to_roster.get(slot),
                "user_id": user,
                "username": (by_user.get(str(user)) or {}).get("username"),
                "team_name": (by_user.get(str(user)) or {}).get("team_name"),
                # Both sides can be None — an unpublished draft order must not read
                # as every slot being mine.
                "is_mine": bool(me.get("user_id")) and str(user) == str(me["user_id"]),
            }
            for slot in range(1, board.teams + 1)
            for user in [board.slot_to_user.get(slot)]
        ],
        "traded_picks": fetched["traded"],
        "fields": FIELD_DEFINITIONS,
        "picks": rows,
    }
