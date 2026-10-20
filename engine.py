"""
Tournament engine: Swiss pairing, standings with tiebreakers, and Top Cut brackets.

Pure logic only (no database / web code), so it can be tested on its own.

A "match" here is a dict with keys:
    p1, p2            player ids (p2 is None for a bye)
    p1_wins, p2_wins  games won by each player
    draws             drawn games
    has_result        True once a result has been entered
"""
import math
import random

WIN_POINTS = 3
DRAW_POINTS = 1
TIEBREAK_FLOOR = 1 / 3  # standard 33% minimum for MW% and GW%

# Result choices. Code is "p1wins-p2wins-draws".
RESULTS_BO1 = ["1-0-0", "0-1-0", "0-0-1"]
RESULTS_BO3 = ["2-0-0", "2-1-0", "1-0-0", "1-1-0", "0-0-0", "0-1-0", "1-2-0", "0-2-0"]
RESULTS_TOPCUT = ["2-0-0", "2-1-0", "1-2-0", "0-2-0"]  # Top Cut is always Bo3, no draws


def parse_result(code):
    a, b, d = (int(x) for x in code.split("-"))
    return a, b, d


def recommended_swiss_rounds(player_count):
    """Common rule of thumb: enough rounds to leave a single undefeated player."""
    if player_count < 2:
        return 0
    return max(1, math.ceil(math.log2(player_count)))


# ---------------------------------------------------------------- standings

def compute_standings(player_ids, matches, swiss_format, names=None):
    """Return a ranked list of standing rows for Swiss matches.

    Ranking: match points, then OMW%, GW%, OGW%.
    A bye counts as a match win and 2-0 in games (1-0 in Bo1), and is
    ignored when computing opponents' percentages.
    """
    names = names or {}
    bye_games = 2 if swiss_format == "bo3" else 1
    st = {
        p: {"mp": 0, "played": 0, "gp": 0, "games": 0, "w": 0, "l": 0, "d": 0, "byes": 0, "opps": []}
        for p in player_ids
    }

    for m in matches:
        if not m.get("has_result"):
            continue
        p1, p2 = m["p1"], m["p2"]
        if p2 is None:
            if p1 in st:
                s = st[p1]
                s["mp"] += WIN_POINTS
                s["played"] += 1
                s["w"] += 1
                s["byes"] += 1
                s["gp"] += WIN_POINTS * bye_games
                s["games"] += bye_games
            continue
        a, b, d = m["p1_wins"], m["p2_wins"], m["draws"]
        for me, opp, mine, theirs in ((p1, p2, a, b), (p2, p1, b, a)):
            if me not in st:
                continue
            s = st[me]
            s["played"] += 1
            s["opps"].append(opp)
            if mine > theirs:
                s["mp"] += WIN_POINTS
                s["w"] += 1
            elif mine < theirs:
                s["l"] += 1
            else:
                s["mp"] += DRAW_POINTS
                s["d"] += 1
            s["gp"] += WIN_POINTS * mine + DRAW_POINTS * d
            s["games"] += a + b + d

    def mwp(p):
        s = st[p]
        if not s["played"]:
            return TIEBREAK_FLOOR
        return max(TIEBREAK_FLOOR, s["mp"] / (WIN_POINTS * s["played"]))

    def gwp(p):
        s = st[p]
        if not s["games"]:
            return TIEBREAK_FLOOR
        return max(TIEBREAK_FLOOR, s["gp"] / (WIN_POINTS * s["games"]))

    rows = []
    for p, s in st.items():
        opps = [o for o in s["opps"] if o in st]
        omw = sum(mwp(o) for o in opps) / len(opps) if opps else 0.0
        ogw = sum(gwp(o) for o in opps) / len(opps) if opps else 0.0
        rows.append({
            "player": p,
            "name": names.get(p, str(p)),
            "points": s["mp"],
            "wins": s["w"], "losses": s["l"], "draws": s["d"], "byes": s["byes"],
            "record": f"{s['w']}-{s['l']}-{s['d']}",
            "mwp": mwp(p), "omw": omw, "gw": gwp(p), "ogw": ogw,
        })

    rows.sort(key=lambda r: (-r["points"], -round(r["omw"], 6), -round(r["gw"], 6),
                             -round(r["ogw"], 6), r["name"].lower()))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


# ---------------------------------------------------------------- swiss pairing

class _TooHard(Exception):
    pass


def _pair_no_rematch(pool, played, budget):
    """Pair players top-down, each with the closest-ranked opponent they haven't played."""
    if not pool:
        return []
    budget[0] -= 1
    if budget[0] < 0:
        raise _TooHard()
    first = pool[0]
    for i in range(1, len(pool)):
        cand = pool[i]
        if frozenset((first, cand)) in played:
            continue
        rest = pool[1:i] + pool[i + 1:]
        sub = _pair_no_rematch(rest, played, budget)
        if sub is not None:
            return [(first, cand)] + sub
    return None


def pair_swiss_round(ranked_ids, previous_matches, rng=None, round_number=1):
    """Create pairings for the next Swiss round.

    ranked_ids: active (non-dropped) players, best first (current standings order).
    previous_matches: all earlier Swiss matches (dicts with p1/p2).
    Returns (pairs, bye_player_or_None). Pairs are ordered top table first.
    """
    rng = rng or random.Random()
    players = list(ranked_ids)
    if round_number == 1:
        rng.shuffle(players)

    played = set()
    had_bye = set()
    for m in previous_matches:
        if m["p2"] is None:
            had_bye.add(m["p1"])
        else:
            played.add(frozenset((m["p1"], m["p2"])))

    if len(players) % 2 == 0:
        bye_options = [None]
    else:
        bottom_up = list(reversed(players))
        bye_options = [p for p in bottom_up if p not in had_bye] + [p for p in bottom_up if p in had_bye]

    for bye in bye_options:
        pool = [p for p in players if p != bye]
        try:
            pairs = _pair_no_rematch(pool, played, [20000])
        except _TooHard:
            pairs = None
        if pairs is not None:
            return pairs, bye

    # No rematch-free pairing exists (tiny events / many rounds): pair in order.
    bye = bye_options[0]
    pool = [p for p in players if p != bye]
    return [(pool[i], pool[i + 1]) for i in range(0, len(pool), 2)], bye


# ---------------------------------------------------------------- top cut

TOPCUT_SIZES = [0, 2, 4, 8, 16, 32]


def bracket_seed_order(n):
    """Standard seeding: for 8 -> [1, 8, 4, 5, 2, 7, 3, 6] (1v8, 4v5, 2v7, 3v6)."""
    order = [1, 2]
    while len(order) < n:
        total = len(order) * 2 + 1
        order = [x for s in order for x in (s, total - s)]
    return order


def topcut_round_name(players_in_round):
    return {2: "Finals", 4: "Semifinals", 8: "Quarterfinals"}.get(players_in_round, f"Top {players_in_round}")


def topcut_first_round(seeded_ids):
    """seeded_ids: list where index 0 is seed 1. Returns list of (slot, p1, p2)."""
    n = len(seeded_ids)
    order = bracket_seed_order(n)
    out = []
    for slot, i in enumerate(range(0, n, 2), 1):
        out.append((slot, seeded_ids[order[i] - 1], seeded_ids[order[i + 1] - 1]))
    return out


def match_winner(m):
    if m["p2"] is None:
        return m["p1"]
    if m["p1_wins"] > m["p2_wins"]:
        return m["p1"]
    if m["p2_wins"] > m["p1_wins"]:
        return m["p2"]
    return None


def topcut_next_round(prev_round_matches, seed_of):
    """prev_round_matches sorted by slot. Winners of slots (1,2), (3,4)... meet.
    Higher seed (lower number) is listed as player 1."""
    ms = sorted(prev_round_matches, key=lambda m: m["slot"])
    out = []
    for slot, i in enumerate(range(0, len(ms), 2), 1):
        a, b = match_winner(ms[i]), match_winner(ms[i + 1])
        if seed_of[b] < seed_of[a]:
            a, b = b, a
        out.append((slot, a, b))
    return out
