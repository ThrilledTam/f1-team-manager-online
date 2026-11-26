"""Core career logic — clean rebuild of Main.py's ideas, no globals, no input().

Sources (verified against ~/workspace/f1-manager/Main.py, Race.py, Rules.py):
- 2026 grid, 12 rounds/season, 30 seasons, points top 10.
- Turn flow is SIMULTANEOUS in v0 (task spec): every human acts at once and
  the race runs when all humans have advanced. This replaces Main.py's
  standings-order pass-and-play loop.
- Money flows per round: salaries (salary/12 per driver), race prize money
  (1.95 - 0.025*pos per driver, Race.py), per-race gains EPRG (Main.py
  Finances estimates, applied for real in v0), research/develop spending.
- Season end: champion, prize money (12 for 1st, 11 - 0.65*pos otherwise),
  post-season injection EPSCI, retirements, DSUS ageing, salary recalc,
  contracts decrement, expired contracts become free agents, F2 champion
  promoted, reg change, winter break to refill empty seats.
"""
import copy
import json
import random
import uuid
import zlib
from pathlib import Path

from . import race as race_mod
from . import rules as rules_mod
from .data import (
    GRID_2026, CHAMPION_HISTORY_2010_2025, PREV_SEASON_DRIVER_ORDER,
    PREV_SEASON_TEAM_ORDER, TR_2026, ACADEMY_2026, CAREER_STATS,
    circuit_for_round,
)
from .models import make_driver, make_team, new_career_dict
from .rules import SEASON_LENGTH, MAX_SEASONS
from .rules import (
    ACADEMY_FINE_FREE_COUNT, ACADEMY_LOYALTY_BOOST, ACADEMY_MAX_JOIN_AGE,
    DRIVER_REP_CHAMPION_BONUS, DRIVER_REP_CHAMPION_BONUS_NON_TOP,
    DRIVER_REP_F2_CHAMPION_BONUS, DRIVER_REP_SEASON_CAP,
    DRIVER_REP_SEASON_RATE, ILLNESS_CHANCE, ILLNESS_MAX_ROUNDS,
    ILLNESS_MIN_ROUNDS, REG_ADAPT_COST_PER_POINT, REG_OPENS_ROUND,
    REG_MAJOR_TARGET, REG_MINOR_TARGET, TR_FINE_FREE_COUNT,
    TR_RESEARCH_BONUS, WINTER_BREAK_ROUNDS, add_perf_gain, add_rel_gain,
    extra_driver_fine, finance_estimates, grow_rating, is_retiring,
    reg_change, reg_change_v2, retirement_chance, update_points_dist,
)

CUSTOM_TEAM_MONEY = 6.0
CUSTOM_TEAM_REL = 0.90  # v0 fix: Main.py passed rel=6 (a bug) for custom teams

# v0.6: team relationships (supplier / customer / junior).
# v0.6.6: the unilateral break fee is $2.0M per remaining contract
# year (was $1.0M), paid when the break STARTS, and the break takes
# effect after a 3-round notice period (winter-break rounds count
# as rounds) — during the notice the relation stays fully active.
RELATION_BREAK_FEE_PER_YEAR = 2.0  # $M per remaining contract year
RELATION_BREAK_NOTICE_ROUNDS = 3  # rounds between starting a break
# and it taking effect (winter-break rounds count as rounds)
RELATION_START_YEARS = 2  # contract years for the 2026 starting relations
MAX_WANTED = 3  # WANTED marks a parent may have on each supplied team
# v0.6.6: the junior income share — after each race a junior team
# pays 15% of its RACE PRIZE money (per-car prize only; not EPRG,
# not the season-end payout) to its parent.
JUNIOR_INCOME_SHARE = 0.15
BOT_POOR_MONEY = 5.0  # below this a bot team counts as "poor" (it wants
# a supplier, and suppliers want to help it)
BOT_RICH_MONEY = 20.0  # above this a bot supplier donates to a poor team
BOT_PERF_WINDOW = 60.0  # "close" in performance for junior / renewal rules

# v0.7.0: hidden team reputation (0-100, starts at 50 for every team).
# It bends how bot teams treat a team — donation requests, winter-break
# offers, relation proposals — never by a hard refusal, only by a quiet
# probability / ranking drop (Matthew's rule: no hard caps). The number
# itself NEVER leaves the server (see public_state / my_team_public).
TEAM_REP_START = 50.0
TEAM_REP_MIN = 0.0
TEAM_REP_MAX = 100.0
TEAM_REP_WIN_BONUS = 0.3       # per race win, at race time
TEAM_REP_DONATION_RATE = 0.2   # rep points per $M given / received
TEAM_REP_DONATION_CAP = 1.0    # max rep gain per single donation
TEAM_REP_PROPOSAL_FLOOR = 25.0    # below this, bots cool on proposals
TEAM_REP_PROPOSAL_MAX_CUT = 0.6   # at rep 0 a bot's acceptance is x0.4
TEAM_REP_QUIET_DROP_STEP = 15.0   # rep points below 50 per +1 offer drop
TEAM_REP_QUIET_DROP_MAX = 3


def _load_real_standings():
    """v0.7.0: the real 2010-2025 drivers' + constructors' standings
    (Jolpica/Ergast API data, baked in as static data — no live
    fetching at runtime), so the real champion years are clickable
    too. Returns {} when the file is missing — the game still runs,
    the real years just have no detail."""
    try:
        with open(Path(__file__).with_name("real_standings.json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# {year: {drivers: [{pos, name, team, points, wins}], constructors:
# [{pos, name, points, wins}]}} — pos may be null (unclassified),
# points may be .5 (shortened races).
REAL_STANDINGS = _load_real_standings()


def team_rep_of(team):
    """A team's hidden reputation (0-100); 50 when the field is
    missing (an old career that has not been migrated)."""
    if not team:
        return TEAM_REP_START
    try:
        return float(team.get("team_rep", TEAM_REP_START))
    except (TypeError, ValueError):
        return TEAM_REP_START


def adjust_team_rep(team, delta):
    """Move a team's hidden reputation, clamped to [0, 100]."""
    if not team:
        return
    team["team_rep"] = min(TEAM_REP_MAX,
                           max(TEAM_REP_MIN, team_rep_of(team) + delta))


def wb_rep_drop(team_rep):
    """v0.7.0: the QUIET DROP for winter-break offer resolution — a
    low-rep team counts as this many constructors' places lower when
    a driver picks between offers. Rep 50+ -> 0, 35 -> 1, 20 -> 2,
    5 -> 3 (capped at +3)."""
    if team_rep >= TEAM_REP_START:
        return 0
    return min(TEAM_REP_QUIET_DROP_MAX,
               int((TEAM_REP_START - team_rep) // TEAM_REP_QUIET_DROP_STEP))


def proposal_rep_factor(proposer_rep):
    """v0.7.0: the multiplier on a bot's acceptance of a
    customer/junior proposal — 1.0 at rep >= 25, sliding to 0.4 at
    rep 0 (a 60% cut)."""
    if proposer_rep >= TEAM_REP_PROPOSAL_FLOOR:
        return 1.0
    return 1.0 - ((TEAM_REP_PROPOSAL_FLOOR - proposer_rep)
                  / TEAM_REP_PROPOSAL_FLOOR * TEAM_REP_PROPOSAL_MAX_CUT)


def driver_initials(name):
    """Top-3 initials for the race history grid, like Matthew's PyCharm
    version: first initial + '.' + first 3 surname letters, uppercased
    (Lewis Hamilton -> L.HAM, Kimi Antonelli -> K.ANT)."""
    parts = str(name or "").strip().split()
    if not parts:
        return "?.???"
    return f"{parts[0][0].upper()}.{parts[-1][:3].upper()}"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def build_new_career(name, host_nickname, chart_on=True):
    """A fresh 2026 career: 11 teams, 22 F1 drivers, 33 free agents, 22 F2,
    5 queued rookies, 68 pool rookies (all from GRID_2026).

    v0.3: the season opens in the real 2025 finishing order — the
    driver standings tie-breaker (prev_driver_order) is the 2025
    order, and the teams list is in 2025 constructors order, so at
    0 points both standings show last season's order, like real F1.
    The champion history is pre-filled with the real 2010–2025 table."""
    career = new_career_dict(name, host_nickname, chart_on)
    teams_by_name = {}
    for tdata in GRID_2026["teams"]:
        team = make_team(tdata)
        career["teams"].append(team)
        teams_by_name[team["name"]] = team
    # suppliers (by name, resolved after all teams exist)
    for team, tdata in zip(career["teams"], GRID_2026["teams"]):
        if tdata.get("supplier"):
            sup = teams_by_name.get(tdata["supplier"])
            team["supplier_id"] = sup["id"] if sup else None
    for team, tdata in zip(career["teams"], GRID_2026["teams"]):
        for key, slot in (("d1", "driver1_id"), ("d2", "driver2_id")):
            d = make_driver(tdata[key], status="f1", team_id=team["id"])
            career["drivers"].append(d)
            team[slot] = d["id"]
    for ddata in GRID_2026["free_agents"]:
        career["drivers"].append(make_driver(ddata, status="free"))
    for ddata in GRID_2026["f2"]:
        career["drivers"].append(make_driver(ddata, status="f2"))
    for ddata in GRID_2026["queued"]:
        career["drivers"].append(make_driver(ddata, status="queued"))
    for ddata in GRID_2026["pool"]:
        career["drivers"].append(make_driver(ddata, status="pool"))
    # v0.4.2: the 2026 start re-homes the CSV's Test & Reserve
    # drivers (Attachment #1, status "Test & Reserve") to their
    # teams — 22 drivers, McLaren and Mercedes with 4 each — and
    # puts the CSV's F2 juniors with a team (status "F2" + a
    # current_team) into that team's Driver Academy. CSV age /
    # salary / rating override the GRID_2026 copies where they
    # differ (Westcott $1.4M, Kirakawa 84, Pourchaire 25, and the
    # academy F2 salaries). T&R drivers start on 2-year contracts
    # (the CSV has no contract length for them; 2 is the default
    # signing length). A driver missing from the pool is skipped —
    # the career still builds.
    by_name = {d["name"]: d for d in career["drivers"]}
    for entry in TR_2026:
        d = by_name.get(entry["name"])
        team = teams_by_name.get(entry["team"])
        if not d or not team:
            continue
        d["age"] = int(entry["age"])
        d["salary"] = float(entry["salary"])
        d["mv"] = float(entry["salary"])
        d["perf"] = float(entry["perf"])
        d["status"] = "tr"
        d["team_id"] = team["id"]
        d["contract_left"] = 2
    for entry in ACADEMY_2026:
        d = by_name.get(entry["name"])
        team = teams_by_name.get(entry["team"])
        if not d or not team:
            continue
        d["age"] = int(entry["age"])
        d["salary"] = float(entry["salary"])
        d["mv"] = float(entry["salary"])
        d["perf"] = float(entry["perf"])
        d["academy_team_id"] = team["id"]
    # v0.8.0: seed the happiness/loyalty relations for the 2026
    # grid — every driver with a team (main, T&R) or an academy
    # starts neutral (3.0) with medium loyalty (3.0: these are
    # established relations, not fresh signings — a brand-new
    # signing starts at loyalty 1.0 instead). Free agents and
    # unaffiliated F2 drivers keep empty relation maps and sit at
    # neutral until signed.
    for d in career["drivers"]:
        rel_team = (d.get("team_id")
                    if d.get("status") in ("f1", "tr")
                    else d.get("academy_team_id"))
        if rel_team:
            d["happiness_by_team"][rel_team] = 3.0
            d["loyalty_by_team"][rel_team] = 3.0
            d["happiness"] = 3.0
            d["loyalty"] = 3.0
    # v0.5: career Grand Prix totals — real totals at the end of the
    # 2025 season (data.CAREER_STATS). These persist across seasons
    # and team changes, and drive the development contribution.
    # Drivers not in the table (F2 juniors, pool rookies, fictional
    # drivers) keep the template default of (0, 0).
    for d in career["drivers"]:
        starts, wins = CAREER_STATS.get(d["name"], (0, 0))
        d["gp_starts"] = starts
        d["gp_wins"] = wins
    # v0.3: season 1 opens in the real 2025 order. The three 2026-grid
    # drivers who did not race in F1 in 2025 (Pérez, Bottas, Lindblad)
    # go after the 2025-ordered drivers, ordered by perf — the
    # driver_standings() tie-break (best finish, then -perf) does
    # exactly that for drivers with no previous order.
    by_name = {d["name"]: d for d in career["drivers"]}
    ordered = [by_name[n] for n in PREV_SEASON_DRIVER_ORDER if n in by_name]
    career["prev_driver_order"] = [d["id"] for d in ordered]
    # v0.3: teams list in 2025 constructors order (Audi in Sauber's
    # slot, Cadillac last as a new team) — constructor_standings() is
    # a stable sort, so at 0 points the list order is the order shown.
    rank = {n: i for i, n in enumerate(PREV_SEASON_TEAM_ORDER)}
    career["teams"].sort(key=lambda t: rank.get(t["name"], len(rank)))
    # v0.4: the constructors' carry-over order, so the new
    # constructor_standings() sort (points, wins, best finish,
    # earliest, then last season's order) opens season 1 in the real
    # 2025 constructors' order — the list sort above alone is not
    # enough any more, because the sort key falls through to the team
    # name when every team is tied on 0 points.
    by_team_name = {t["name"]: t for t in career["teams"]}
    career["prev_team_order"] = [by_team_name[n]["id"]
                                 for n in PREV_SEASON_TEAM_ORDER
                                 if n in by_team_name]
    # v0.3: champion history starts with the real 2010–2025 table;
    # in-game seasons append as they complete (see end_of_season).
    career["champion_history"] = [dict(r) for r in CHAMPION_HISTORY_2010_2025]
    career["next_reg_year"] = 2027
    career["next_reg_kind"] = "Minor"
    set_circuit_for_round(career)
    return career


def set_circuit_for_round(career):
    """Point the career at the current round's Grand Prix (v0.1)."""
    gp = circuit_for_round(career["round"])
    career["circuit"] = gp["name"]
    career["circuit_laps"] = gp["laps"]


def result_row(d, team, pos, gap, points=None, gl=None):
    """One classification row (v0.1): gaps only, NEVER absolute times.
    Server keeps qual_time/race_time on the driver dicts for ordering, but
    those times are never put in a results payload."""
    row = {
        "pos": pos,
        "driver": d["name"],
        "driver_id": d["id"],
        "team": team["name"],
        "team_id": team["id"],
        "cha": d["cha"],
        "gap": gap,
    }
    if points is not None:
        row["points"] = points
    if gl is not None:
        row["gl"] = gl
    return row


def get_team(career, team_id):
    return next((t for t in career["teams"] if t["id"] == team_id), None)


def get_driver(career, driver_id):
    return next((d for d in career["drivers"] if d["id"] == driver_id), None)


def team_drivers(career, team):
    out = []
    for did in (team["driver1_id"], team["driver2_id"]):
        if did:
            out.append(get_driver(career, did))
    return [d for d in out if d]


def team_tr_drivers(career, team):
    """v0.4: a team's Test & Reserve drivers — status 'tr', affiliated
    with this team. They do not race and never appear in the F1
    standings, but they are paid their full salary and add +25% each
    to research gained. v0.4.2: there is NO hard cap any more — a
    team with more than 2 T&R drivers pays an exponential fine per
    race instead (see tr_fine_per_race). v0.7.1: this includes an
    ill main driver sitting in T&R while they recover — use
    active_tr_drivers() where an ill driver should not count
    (research bonus, T&R fine)."""
    return [d for d in career["drivers"]
            if d.get("status") == "tr" and d.get("team_id") == team["id"]]


def active_tr_drivers(career, team):
    """v0.7.1: the team's T&R drivers who are FIT — an ill main
    driver sitting in T&R while they recover neither boosts research
    nor counts toward the exponential T&R fine (they are not
    available to test or train)."""
    return [d for d in team_tr_drivers(career, team)
            if not d.get("ill_until_round")]


def team_academy_drivers(career, team):
    """v0.4.2: a team's Driver Academy juniors — drivers whose
    academy_team_id is this team. They keep their status ("f2" or
    "free") and still race in F2 / sit in the free-agent pool; the
    academy is an affiliation on top. The team pays 1/3 of each
    academy driver's salary per season."""
    return [d for d in career["drivers"]
            if d.get("academy_team_id") == team["id"]]


def tr_fine_per_race(career, team):
    """v0.4.2: the exponential T&R fine, per race — $0.5M for the
    3rd T&R driver, $1.5M for 3rd+4th, $3.5M for 3rd+4th+5th
    (the first 2 are free). Charged after each race, on top of the
    T&R drivers' full salaries."""
    return extra_driver_fine(len(active_tr_drivers(career, team)),
                             TR_FINE_FREE_COUNT)


def academy_fine_per_race(career, team):
    """v0.4.2: the same exponential fine for the academy — the
    first 3 academy drivers are free; the 4th costs $0.5M/race,
    the 5th $1.5M/race in total, and so on."""
    return extra_driver_fine(len(team_academy_drivers(career, team)),
                             ACADEMY_FINE_FREE_COUNT)


def academy_cost_per_race(career, team):
    """v0.4.2: what the academy costs per race — 1/3 of each
    academy driver's salary per season, paid salary/36 after
    each race."""
    total = sum(d["salary"] for d in team_academy_drivers(career, team))
    return round(total / 3.0 / SEASON_LENGTH, 4)


def extra_charges_per_race(career, team):
    """v0.4.2: everything a team pays after each race on top of
    main + T&R salaries — the T&R fine, the academy stipends, and
    the academy fine. {tr_fine, academy_cost, academy_fine, total}."""
    tr_fine = tr_fine_per_race(career, team)
    academy_cost = academy_cost_per_race(career, team)
    academy_fine = academy_fine_per_race(career, team)
    return {"tr_fine": tr_fine, "academy_cost": academy_cost,
            "academy_fine": academy_fine,
            "total": round(tr_fine + academy_cost + academy_fine, 4)}


def research_multiplier(career, team):
    """v0.4: each T&R driver adds +25% to research gained from an
    investment — 1 T&R -> x1.25, 2 T&R -> x1.50. Applies to the
    Performance / Reliability research pools (car-parts research);
    regulation research is a separate pool and is not boosted."""
    return 1.0 + TR_RESEARCH_BONUS * len(active_tr_drivers(career, team))


def driver_dev_contrib(driver):
    """v0.5: how much ONE driver contributes to developing the car.
    The more F1 Grands Prix they have started, the more they have
    won, and the higher their rating, the bigger the contribution:
        0.02 + 0.02*(starts/(starts+100)) + 0.02*(wins/(wins+10))
             + 0.02*clamp((perf-70)/30, 0, 1)
    Range [0.02, 0.08]. Hamilton (~0.065) > Leclerc (~0.052);
    Norris (~0.052) > Piastri (~0.046); Gasly (~0.044) > Aron
    (~0.029). This is THE single helper for the contribution — the
    develop path (do_develop, bot R&D) and the display path
    (server.my_team_public) both call it, so they can never drift
    apart. Academy drivers never contribute (they are F2 juniors),
    so callers only pass main + T&R drivers."""
    starts = driver.get("gp_starts", 0) or 0
    wins = driver.get("gp_wins", 0) or 0
    perf = driver.get("perf") or 0
    perf_term = max(0.0, min(1.0, (perf - 70.0) / 30.0))
    return (0.02
            + 0.02 * (starts / (starts + 100.0))
            + 0.02 * (wins / (wins + 10.0))
            + 0.02 * perf_term)


def team_dev_factor(career, team):
    """v0.5: the team's development factor — 1 + the summed
    contributions of the main 2 + T&R drivers (academy juniors do
    not contribute). Applied to the Develop PERF gain only
    (reliability is unchanged). The T&R research-pool bonus
    (research_multiplier) is a separate mechanic and is untouched."""
    drivers = team_drivers(career, team) + team_tr_drivers(career, team)
    return 1.0 + sum(driver_dev_contrib(d) for d in drivers)


def free_agents(career):
    return [d for d in career["drivers"] if d["status"] == "free"]


def f2_drivers(career):
    """v0.7.3: drivers racing in F2 — status "f2", plus a driver
    holding an F1 T&R contract while still racing F2 (f2_tr)."""
    return [d for d in career["drivers"]
            if d["status"] == "f2" or d.get("f2_tr")]


def constructor_pos(career, team_id):
    """1-based constructor position (teams list is kept in standings order)."""
    for i, t in enumerate(career["teams"], start=1):
        if t["id"] == team_id:
            return i
    return len(career["teams"])


def salary_per_round(career, team):
    """Salary cost after each race: salary/12 for every driver on the
    team — the two main drivers AND any T&R drivers. v0.4: T&R
    drivers are paid their FULL salary even though they do not race."""
    total = sum(d["salary"] for d in team_drivers(career, team))
    total += sum(d["salary"] for d in team_tr_drivers(career, team))
    return total / SEASON_LENGTH


# ---------------------------------------------------------------------------
# Team selection / custom teams
# ---------------------------------------------------------------------------

def choose_team(career, team_id, player_id, allow_takeover=False):
    """A human takes over an AI team (new career pick, or mid-career takeover).

    v0.4.1: taking over also clears any auto-generated winter-break
    offers for the team — those were made by the bot manager while
    the team was AI (e.g. when a career was loaded and every team
    was still flagged AI), and the human must choose their own
    offers. The team is also taken out of wb_ready, so a bot's
    readiness never counts as the human's.
    v0.6.2: allow_takeover lets the server reassign a team whose
    human owner disconnected and is past the takeover grace period
    (the server validates the grace period first — game code never
    sees the room's player state). Without it, an owned team is
    never reassigned."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    if team["owner_player_id"] is not None and not allow_takeover:
        return False, "That team is already taken."
    team["is_ai"] = False
    team["owner_player_id"] = player_id
    if career.get("phase") == "winter_break":
        (career.get("wb_offers") or {}).pop(team["id"], None)
        if team["id"] in (career.get("wb_ready") or []):
            career["wb_ready"].remove(team["id"])
    return True, f"You took over {team['name']} ✓"


def leave_team(career, player_id):
    """v0.6.6: leave the team you picked on the setup screen (before
    the career starts) so you can pick a different one — nobody has
    to remake the room. The team goes back to being AI (a custom team
    a player created and then leaves stays on the grid as an AI team
    rather than being deleted mid-setup, so its two drafted drivers
    are never stranded). The server gates this to pre-start lobbies;
    after the career starts the mid-career picker covers takeovers."""
    team = next((t for t in career["teams"]
                 if t.get("owner_player_id") == player_id), None)
    if team is None:
        return False, "You don't have a team to leave."
    team["is_ai"] = True
    team["owner_player_id"] = None
    if career.get("phase") == "winter_break":
        if team["id"] in (career.get("wb_ready") or []):
            career["wb_ready"].remove(team["id"])
    return True, f"You left {team['name']} — pick another team."


def create_custom_team(career, player_id, name, colour, d1_id, d2_id):
    """From Main.py create_custom_team(): pick 2 drivers from the free/F2
    pool (1-year contracts), perf = (max + 5*min) / 6, money $6M. The team is
    APPENDED to the grid, as in the original."""
    name = (name or "").strip()
    if not name:
        return False, "Give your team a name.", None
    if any(t["name"].lower() == name.lower() for t in career["teams"]):
        return False, "A team with that name already exists.", None
    if d1_id == d2_id:
        return False, "Pick two different drivers.", None
    picked = []
    for did in (d1_id, d2_id):
        d = get_driver(career, did)
        if not d or d["status"] not in ("free", "f2"):
            return False, "Pick drivers from the free-agent / F2 pool.", None
        picked.append(d)
    perfs = sorted([t["perf"] for t in career["teams"]], reverse=True)
    start_perf = (perfs[0] + 5 * perfs[-1]) / 6.0
    team = make_team({
        "name": name, "colour": colour or "#888888", "money": CUSTOM_TEAM_MONEY,
        "c_wins": 0, "perf": start_perf, "rel": CUSTOM_TEAM_REL,
        "supplier": None, "sup_stage": 0,
    })
    team["is_ai"] = False
    team["owner_player_id"] = player_id
    career["teams"].append(team)
    for d, slot in zip(picked, ("driver1_id", "driver2_id")):
        d["status"] = "f1"
        d["team_id"] = team["id"]
        d["contract_left"] = 1
        # v0.4.2: a picked academy junior leaves the academy when
        # they sign a main contract (same as any other signing).
        d["academy_team_id"] = None
        team[slot] = d["id"]
    return True, f"Custom team {name} created ✓", team


# ---------------------------------------------------------------------------
# Human actions during a round
# ---------------------------------------------------------------------------

def do_research(career, team_id, priority, amount):
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return False, "Amount must be a number."
    if amount <= 0:
        return False, "Research amount must be positive."
    if amount > team["money"]:
        return False, "Not enough money."
    if priority not in ("perf", "reliability"):
        return False, "Priority must be Performance or Reliability."
    # From Main.py: Performance adds PerfRes and -(invest/3) ReliRes;
    # Reliability adds ReliRes and -(invest/5) PerfRes.
    # v0.4: each T&R driver adds +25% to the research POOL gained
    # (x1.25 with one T&R driver, x1.50 with two). The cross-penalty
    # is not boosted — it is a cost of the investment, not a gain.
    mult = research_multiplier(career, team)
    if priority == "perf":
        team["perf_res"] += amount * mult
        team["reli_res"] -= amount / 3.0
    else:
        team["perf_res"] -= amount / 5.0
        team["reli_res"] += amount * mult
    team["money"] -= amount
    label = "Performance" if priority == "perf" else "Reliability"
    pool = team["perf_res"] if priority == "perf" else team["reli_res"]
    bonus = f" (x{mult:.2f} with T&R drivers)" if mult > 1.0 else ""
    return True, f"Invested ${amount:.2f}M in {label} research ✓ (pool now {pool:.2f}){bonus}"


def do_develop(career, team_id, chart_on=True):
    """From Main.py: Develop Parts costs $1M, converts banked research into
    car performance / reliability (Team.py addPerf / addRel).

    v0.5: the PERF gain is multiplied by the team's driver
    development factor (1 + the main 2 + T&R drivers' contributions
    — experience, wins, rating). The RELIABILITY gain is unchanged:
    drivers contribute to performance development, not reliability.
    The T&R research-pool bonus is a separate mechanic and is
    untouched (it boosts the pool an investment buys, not the
    develop step).
    v0.7.3: the pop-up respects the perf chart toggle — when the
    chart is OFF, the message shows the success and the $1M spent
    (and the reliability gain, which is a visible stat) but NOT the
    perf gained, since perf is hidden with the chart off. Chart on
    shows the current message, perf gained included."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    if team["money"] < 1.0:
        return False, "Need at least $1M to develop parts."
    dev_factor = team_dev_factor(career, team)
    perf_gain = add_perf_gain(team["perf"], team["perf_res"]) * dev_factor
    rel_gain = add_rel_gain(team["reli_res"])
    team["perf"] += perf_gain
    team["rel"] += rel_gain
    # v0.6.1: the reliability cap is 1.0 (was 0.99), so a team that
    # fully invests in reliability CAN reach "all good" — 0% DNF chance.
    team["rel"] = max(0.5, min(1.0, team["rel"]))
    team["perf_res"] = 0.0
    team["reli_res"] = 0.0
    team["money"] -= 1.0
    if chart_on:
        return True, (f"Parts developed ✓ — Perf {team['perf']:.1f} (+{perf_gain:.1f}), "
                      f"Reliability {team['rel']:.2f} (+{rel_gain:.2f}), $1M spent"
                      f" (driver development ×{dev_factor:.2f})")
    return True, (f"Parts developed ✓ — Reliability {team['rel']:.2f} "
                  f"(+{rel_gain:.2f}), $1M spent"
                  f" (driver development ×{dev_factor:.2f})")


def do_tuning(career, team_id, value):
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    try:
        value = int(value)
    except (TypeError, ValueError):
        return False, "Tuning must be a whole number."
    if not -3 <= value <= 3:
        return False, "Tuning must be between -3 and 3."
    team["tuning"] = value
    return True, f"Tuning set to {value:+d} ✓"


def reg_open_for(career, team):
    """v0.4 (feature 7): regulation research (adaptation + research
    for next season's regulations) is open from the start of round 7,
    and stays open during the winter break while a reg change is
    pending. Before round 7 the control is greyed out."""
    if career.get("phase") == "winter_break":
        return bool(team.get("reg_pending"))
    return career.get("phase") == "round" and career.get("round", 1) >= REG_OPENS_ROUND


def init_reg_state(career):
    """v0.4 (feature 7): at the start of round 7, initialise every
    team's regulation state for next season's regulations, from the
    car perf at round 7. Minor year: unadapted = own perf - bottom
    team's perf (the top team adapts the most, the bottom team is
    fully adapted and can research straight away). Major year: ALL
    parts are unadapted (unadapted = the full car perf)."""
    if not career["teams"]:
        return
    major = career.get("next_reg_kind") == "Major"
    bottom = min(t["perf"] for t in career["teams"])
    # Record which change this state is FOR — the change into the
    # season after this one. start_next_season() applies it with
    # the right target (minor 12 / major 35).
    career["pending_reg_year"] = career.get("next_reg_year")
    career["pending_reg_kind"] = career.get("next_reg_kind")
    for team in career["teams"]:
        total = float(team["perf"])
        unadapted = total if major else max(0.0, total - bottom)
        team["reg_total"] = total
        team["reg_unadapted"] = round(unadapted, 2)
        team["reg_adapted"] = round(total - unadapted, 2)
        team["reg_pending"] = True
        # reg_research is NOT reset here — it is already 0 for a team
        # that has never researched, and any pool built up before
        # round 7 (bots only) is kept.


def reg_view(career, team):
    """v0.4 (feature 7): the regulation progress a player is allowed
    to see — percentages and money ONLY, never the true car
    performance value. Returns {open, adapted_pct, unadapted_pct,
    adapt_cost, research, kind, year}."""
    total = float(team.get("reg_total") or 0.0)
    adapted = float(team.get("reg_adapted") or 0.0)
    unadapted = float(team.get("reg_unadapted") or 0.0)
    if total > 0:
        adapted_pct = round(100.0 * adapted / total, 1)
        unadapted_pct = round(100.0 * unadapted / total, 1)
    else:
        adapted_pct = 100.0 if not unadapted else 0.0
        unadapted_pct = 100.0 - adapted_pct
    return {
        "open": reg_open_for(career, team),
        "adapted_pct": adapted_pct,
        "unadapted_pct": unadapted_pct,
        "adapt_cost": round(unadapted * REG_ADAPT_COST_PER_POINT, 2),
        "research": round(float(team.get("reg_research") or 0.0), 2),
        "kind": career.get("next_reg_kind") or "Minor",
        "year": career.get("next_reg_year"),
    }


def do_reg_adapt(career, team_id, points=None):
    """v0.4 (feature 7): spend money to adapt unadapted car parts to
    next season's regulations — $0.30M per point of car perf.
    points=None adapts everything still unadapted; otherwise adapt
    up to that many points. Only while reg research is open."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    if not reg_open_for(career, team):
        return False, "Regulation research opens at round 7."
    unadapted = float(team.get("reg_unadapted") or 0.0)
    if unadapted <= 0:
        return False, "Your car is already fully adapted to the new regulations."
    if points is None:
        want = unadapted
    else:
        try:
            want = float(points)
        except (TypeError, ValueError):
            return False, "Adapt amount must be a number."
        if want <= 0:
            return False, "Adapt amount must be positive."
    adapt = min(want, unadapted)
    cost = round(adapt * REG_ADAPT_COST_PER_POINT, 2)
    if cost > team["money"]:
        affordable = int(team["money"] / REG_ADAPT_COST_PER_POINT)
        if affordable <= 0:
            return False, f"Need ${cost:.2f}M to adapt {adapt:.1f} pts."
        adapt = float(affordable)
        cost = round(adapt * REG_ADAPT_COST_PER_POINT, 2)
    team["reg_unadapted"] = round(unadapted - adapt, 2)
    team["reg_adapted"] = round(float(team.get("reg_adapted") or 0.0) + adapt, 2)
    team["money"] -= cost
    left = float(team.get("reg_unadapted") or 0.0)
    done = " — fully adapted ✓, regulation research is now open" if left <= 0 else ""
    return True, (f"Adapted {adapt:.1f} pts to the new regulations ✓ "
                  f"(${cost:.2f}M, {left:.1f} pts still to adapt){done}")


def do_reg_research(career, team_id, amount):
    """Reg Research (v0.4): a separate pool from normal research.
    Money in now is carried to the next reg change at season start.
    Only available once the car is 100% adapted to the new
    regulations, and only while reg research is open (round 7+, or
    winter break with a pending reg change)."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    if not reg_open_for(career, team):
        return False, "Regulation research opens at round 7."
    if float(team.get("reg_unadapted") or 0.0) > 0:
        return False, ("Adapt your car to the new regulations first — "
                       "regulation research opens once you are 100% adapted.")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return False, "Amount must be a number."
    if amount <= 0:
        return False, "Amount must be positive."
    if amount > team["money"]:
        return False, "Not enough money."
    team["reg_research"] += amount
    team["money"] -= amount
    return True, (f"Invested ${amount:.2f}M in regulation research ✓ "
                  f"(total ${team['reg_research']:.2f}M)")


def rounds_remaining_for(career, driver):
    """v0.7.3: rounds left on a driver's contract, shared by
    break_fee_for_driver and the academy poach fee — in the winter
    break: contract years x 12 rounds; in season: the rest of this
    season plus the full seasons left after it."""
    if career.get("phase") == "winter_break":
        return driver["contract_left"] * SEASON_LENGTH
    return ((SEASON_LENGTH - career["round"] + 1)
            + max(0, driver["contract_left"] - 1) * SEASON_LENGTH)


def break_fee_for_driver(career, driver):
    """v0.4: the contract-break fee for one driver, from the v0.3
    formula — rounds_remaining x salary/12 + $3.0M (v0.7.3: the
    flat was $2.0M), where rounds_remaining is
    rounds_remaining_for() above. Used for the occupant of a seat
    being replaced AND for poaching a driver out of another team's
    contract. It is the shared formula, so the $3.0M flat also
    raises the seat-occupant break fee and release fees."""
    rounds_remaining = rounds_remaining_for(career, driver)
    return round(rounds_remaining * driver["salary"] / SEASON_LENGTH + 3.0, 2)


def academy_poach_fee(career, driver):
    """v0.7.3: poaching an academy junior from another team's Driver
    Academy costs half the salary term plus a $1.0M flat fee —
    rounds_remaining x salary/12 x 0.5 + $1.0M. Academy drivers were
    free to poach before; signing your OWN academy junior is free
    (sign_cost only charges this when the academy belongs to
    another team)."""
    rounds_remaining = rounds_remaining_for(career, driver)
    return round(rounds_remaining * driver["salary"] / SEASON_LENGTH * 0.5 + 1.0, 2)


def sign_cost(career, team_id, driver_id, target):
    """v0.4: the total cost of signing a driver, computed the exact
    way sign_driver() / sign_tr_driver() charge it — the single
    source of truth, so the number shown in the Driver Market is
    always the number charged.

    target is ("seat", 1 | 2) for a main seat or ("tr", None) for a
    T&R slot. The cost is:
      break fee for the occupant of the target main seat (0 for an
        empty seat, and 0 for a T&R slot — it is a new affiliation)
    + break fee for the driver being signed, IF they are currently
        under contract with another team (poaching, main or T&R),
        or half the salary term + $1.0M IF they are another team's
        academy junior (v0.7.3 academy poach fee).
    Signing itself is FREE — a driver's MV is their salary per
    season, paid salary/12 after each race, never upfront (v0.3).

    Returns {total, break_fee, poach_fee, affordable} or None when
    the team/driver/target is invalid."""
    team = get_team(career, team_id)
    if not team:
        return None
    d = get_driver(career, driver_id)
    if not d:
        return None
    if target[0] == "seat":
        if target[1] not in (1, 2):
            return None
    elif target[0] != "tr":
        return None
    break_fee = 0.0
    if target[0] == "seat":
        slot = "driver1_id" if target[1] == 1 else "driver2_id"
        current = get_driver(career, team[slot]) if team[slot] else None
        if current:
            break_fee = break_fee_for_driver(career, current)
    poach_fee = 0.0
    # Poaching: the driver is under contract with another team
    # (a main driver there, or a T&R driver there).
    if d.get("status") in ("f1", "tr") and d.get("team_id") and d["team_id"] != team["id"]:
        poach_fee = break_fee_for_driver(career, d)
    # v0.7.3: poaching an academy junior from ANOTHER team's Driver
    # Academy costs half the salary term + $1.0M (see
    # academy_poach_fee). Your own academy junior is free.
    elif d.get("academy_team_id") and d["academy_team_id"] != team["id"]:
        poach_fee = academy_poach_fee(career, d)
    total = round(break_fee + poach_fee, 2)
    return {"total": total, "break_fee": break_fee, "poach_fee": poach_fee,
            "affordable": total <= team["money"]}


def break_cost(career, team_id, driver_id, seat):
    """Cost of signing a free-agent / F2 driver into a main seat —
    the v0.3 helper, kept for the market UI and the old tests. It is
    sign_cost() for ("seat", seat) with a free/F2 driver (no poaching
    fee, since the driver is not under contract with another team).
    Returns {total, break_fee, affordable} or None."""
    if seat not in (1, 2):
        return None
    d = get_driver(career, driver_id)
    if not d or d["status"] not in ("free", "f2"):
        return None
    quote = sign_cost(career, team_id, driver_id, ("seat", seat))
    if quote is None:
        return None
    return {"total": quote["total"], "break_fee": quote["break_fee"],
            "affordable": quote["affordable"]}


def _sign_driver_common(career, team, d, contract_years, as_tr=False):
    """Shared tail of a signing: the driver joins the team with a
    fresh per-team tally (championship points/wins/best finish follow
    the driver; an F2 driver starts at 0 — their F2 points must not
    carry into F1).
    v0.7.3: an F2 driver joining as T&R KEEPS racing F2 — they keep
    their F2 tally in the plain fields (f2_tr) and their F1 results
    go in the f1_* fields (see race.f1_tally). An F2 driver joining
    a MAIN seat leaves F2: the F2 tally is cleared, f2_tr is
    dropped, and their F1 tally starts at 0. (Replaces the v0.7.1
    freeze — _freeze_f2_tally is gone; a promoted driver no longer
    has their F2 points frozen.) as_tr tells the function which
    role the driver is taking."""
    if d["status"] == "f2" or d.get("f2_tr"):
        if as_tr:
            # Staying in F2 as well as holding the T&R contract.
            d["f2_tr"] = True
            d.setdefault("f1_points", 0)
            d.setdefault("f1_wins", 0)
            d.setdefault("f1_podiums", 0)
            d.setdefault("f1_best_pos", None)
            d.setdefault("f1_best_finish_round", None)
        else:
            # Leaving F2 for a main seat — the F2 tally must not
            # carry into F1 (v0.2.1), and the driver stops racing F2.
            d["f2_tr"] = False
            d["points"] = 0
            d["season_wins"] = 0
            d["season_podiums"] = 0
            d["season_qual_h2h"] = 0
            d["best_pos"] = None
            d["best_finish_round"] = None
            d["finish_sum"] = 0
            d["finish_count"] = 0
            d["f1_points"] = 0
            d["f1_wins"] = 0
            d["f1_podiums"] = 0
            d["f1_best_pos"] = None
            d["f1_best_finish_round"] = None
            # v0.4.4: their F2 starts do not count as F1 races — a
            # promoted driver is not registered in the F1 standings
            # until they actually race in F1.
    # A free agent keeps championship points/wins/best finish from an
    # earlier stint; either way the per-team tally restarts at 0.
    d["team_points"] = 0
    d["last_team_id"] = None
    # v0.7.1: a signing takes a fresh slot, so the slot they held
    # on their old team no longer applies (an expiry sets it again
    # when they leave).
    d["last_slot"] = None
    d["contract_left"] = contract_years
    # v0.4.2: signing a main or T&R contract ends the academy
    # affiliation, wherever it was — a driver cannot be an academy
    # junior and a contracted driver at the same time, and the
    # academy slot frees up.
    d["academy_team_id"] = None
    # v0.8.0: the happiness/loyalty relation with the new team —
    # reset to neutral for a brand-new team, averaged with the last
    # stint for a team the driver has been at before.
    apply_signing_relation(career, d, team["id"])


def _release_driver(career, driver, team):
    """A driver leaves a team (replaced mid-season or contract ended):
    the points they scored FOR this team stay with the team (banked
    into points_kept — real F1), their last team is recorded so the
    standings can still show it, and their per-team tally is cleared
    for wherever they go next. Championship points/wins/best finish
    are NOT touched — they follow the driver (v0.2, revised).
    v0.7.1: the slot they held ("seat1" / "seat2" / "tr") is also
    recorded, so the winter-break Drivers tab can offer the expired
    driver for re-signing into the slot they held."""
    if team is not None:
        driver["last_team_id"] = team["id"]
        if team.get("driver1_id") == driver["id"]:
            driver["last_slot"] = "seat1"
        elif team.get("driver2_id") == driver["id"]:
            driver["last_slot"] = "seat2"
        elif driver.get("status") == "tr":
            driver["last_slot"] = "tr"
        team["points_kept"] = team.get("points_kept", 0) + driver.get("team_points", 0)
    driver["team_points"] = 0
    driver["team_id"] = None
    driver["contract_left"] = 0


def sign_driver(career, team_id, driver_id, seat, contract_years=2):
    """Sign a driver into a main seat (1 or 2).

    v0.4: the driver may come from free agency, F2, or another team
    (poaching — main driver or T&R driver, with the break fee from
    sign_cost()). The signed driver takes the seat offered. During
    the winter break, signings go through the offer system instead —
    use winter offers, not this direct signing. v0.7.3: mid-season,
    an F2 driver (or a T&R driver still racing in F2) can only join
    as Test & Reserve — a main seat opens to them once the F2 season
    is over (the winter break).
    v0.3: signing is FREE — no upfront fee. A driver's MV is their
    salary per season, paid salary/12 after each race (see
    run_race_weekend). The only charge is breaking the current
    driver's contract when the seat is occupied, plus the poaching
    break fee when the driver is under contract elsewhere.
    The new driver gets the contract length the signing manager
    picked: an integer 1–4 years.

    v0.2.1 (real F1, per Matthew's correction): the outgoing driver
    KEEPS their championship points and stays in the driver standings,
    and the points they scored FOR this team stay with the team
    (banked). A free agent who already has F1 points from an earlier
    stint this season KEEPS them when signed by a new team — only
    their per-team tally restarts at 0, so the new team's total is
    never inflated by points scored elsewhere. An F2 driver starts
    at 0: their F2 championship points must not carry into F1.
    """
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    if career.get("phase") == "winter_break":
        return False, "Winter break: sign drivers with offers, not directly."
    if seat not in (1, 2):
        return False, "Seat must be 1 or 2."
    try:
        contract_years = int(contract_years)
    except (TypeError, ValueError):
        return False, "Contract length must be a whole number of years (1–4)."
    if not 1 <= contract_years <= 4:
        return False, "Contract length must be 1–4 years."
    d = get_driver(career, driver_id)
    if not d or d["status"] not in ("free", "f2", "f1", "tr"):
        return False, "That driver is not available to sign."
    # v0.7.3: mid-season, an F2 driver can ONLY join an F1 team as
    # Test & Reserve — they keep racing F2, and a main seat only
    # opens to them once the F2 season is over (the winter break).
    # This covers a plain F2 driver and a T&R driver still racing
    # in F2 (f2_tr). The winter break goes through offers instead
    # (sign_driver_wb), where main seats are allowed.
    if (d["status"] == "f2" or d.get("f2_tr")) \
            and career.get("phase") != "winter_break":
        return False, (f"{d['name']} is still racing in F2 — they can "
                       f"only join as Test & Reserve until the F2 "
                       f"season ends (main seats open in the winter "
                       f"break).")
    # v0.7.1: an ill driver is recovering, not available to sign —
    # their seat is being kept for them at their own team.
    if d.get("ill_until_round"):
        return False, (f"{d['name']} is {d.get('ill_label') or 'ill'} and "
                       f"cannot be signed while they recover.")
    if d.get("status") in ("f1", "tr") and d.get("team_id") == team["id"]:
        return False, "That driver already races for your team."
    slot = "driver1_id" if seat == 1 else "driver2_id"
    current = get_driver(career, team[slot]) if team[slot] else None
    quote = sign_cost(career, team_id, driver_id, ("seat", seat))
    if quote is None:
        return False, "That signing is not possible."
    cost = quote["total"]
    if cost > team["money"]:
        return False, f"Need ${cost:.2f}M to sign {d['name']}."
    # v0.8.0: poaching needs a secret meeting first — an agreed
    # meeting for this team, driver and seat. The meeting's bribe is
    # paid on signing, on top of the poaching costs.
    meeting = None
    bribe = 0.0
    if is_poach(career, team_id, d):
        meeting = agreed_meeting(career, team_id, driver_id, ("seat", seat))
        if meeting is None:
            return False, (f"You need a secret meeting with {d['name']} "
                           f"before you can sign them — request one from "
                           f"the Driver Market.")
        bribe = float(meeting.get("bribe") or 0.0)
        if cost + bribe > team["money"]:
            return False, (f"Need ${cost + bribe:.2f}M to sign {d['name']} "
                           f"(${cost:.2f}M fees + ${bribe:.2f}M bribe).")
    # The losing team: the driver's current team (a main/T&R poach)
    # or their academy team (an academy poach — _sign_driver_common
    # clears the academy link, so it is captured here).
    academy_team = None
    if d.get("academy_team_id") and d["academy_team_id"] != team["id"]:
        academy_team = get_team(career, d["academy_team_id"])
    # Poaching: the driver leaves their current team first (their
    # points scored FOR that team stay there, banked — real F1).
    source_team = None
    if d.get("status") in ("f1", "tr") and d.get("team_id"):
        source_team = get_team(career, d["team_id"])
        if source_team is not None:
            if source_team["driver1_id"] == d["id"]:
                source_team["driver1_id"] = None
            if source_team["driver2_id"] == d["id"]:
                source_team["driver2_id"] = None
            _release_driver(career, d, source_team)
            _recompute_team_points(career, source_team)
            log_transfer(career, 
                f"{d['name']} left {source_team['name']} (poached by {team['name']})")
    if academy_team is not None:
        log_transfer(career, 
            f"{d['name']} left {academy_team['name']}'s academy "
            f"(poached by {team['name']})")
    team["money"] -= cost
    if current:
        _release_driver(career, current, team)
        current["status"] = "free"
        log_transfer(career, 
            f"{current['name']} left {team['name']} (contract broken)")
    _sign_driver_common(career, team, d, contract_years)
    d["status"] = "f1"
    d["team_id"] = team["id"]
    team[slot] = d["id"]
    # Team points are the current drivers' per-team points plus any
    # points banked from drivers who scored for this team and have
    # since left (apply_race_results does the same after a race).
    _recompute_team_points(career, team)
    log_transfer(career, f"{d['name']} signed for {team['name']}")
    # v0.6: a WANTED signing pays the parent's reward to the junior team.
    _pay_wanted_reward(career, team, d)
    # v0.8.0: the agreed meeting is consumed, the bribe is paid, and
    # the losing team is notified in three places — the Transfers
    # feed (above), a Media entry, and a notice in their Drivers tab.
    if meeting is not None:
        career["meetings"] = [m for m in career.get("meetings") or []
                              if m is not meeting]
        if bribe > 0:
            team["money"] = round(team["money"] - bribe, 2)
        losing_team = source_team or academy_team
        if losing_team is not None:
            add_media_entry(
                career,
                f"{d['name']} has left {losing_team['name']} for "
                f"{team['name']} — a secret meeting sealed the move.",
                "transfer")
            add_team_notice(
                career, losing_team["id"],
                f"{d['name']} left your team for {team['name']}.")
    if cost > 0 or bribe > 0:
        extra = (f" + ${bribe:.2f}M bribe" if bribe > 0 else "")
        return True, (f"{d['name']} signed for {team['name']} ✓ "
                      f"(Seat {seat}, {contract_years}-year contract) "
                      f"— ${cost:.2f}M break fee{extra}")
    return True, (f"{d['name']} signed for {team['name']} ✓ "
                  f"(Seat {seat}, {contract_years}-year contract) — Free")


def sign_tr_driver(career, team_id, driver_id, contract_years=2):
    """v0.4: sign a driver as a Test & Reserve driver for a team.
    The driver may come from free agency, F2, or another team
    (poaching — with the break fee). v0.4.2: there is NO hard cap
    any more — a team may sign any number of T&R drivers, but every
    driver beyond the first 2 costs an exponential fine per race
    ($0.5M for the 3rd, $1.5M/race for 3rd+4th — see
    tr_fine_per_race). The fine is shown in the market before
    signing. T&R drivers are paid their FULL salary (salary/12
    after each race) even though they do not race, and each adds
    +25% to research gained. During the winter break, T&R signings
    go through the offer system instead."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    if career.get("phase") == "winter_break":
        return False, "Winter break: sign drivers with offers, not directly."
    try:
        contract_years = int(contract_years)
    except (TypeError, ValueError):
        return False, "Contract length must be a whole number of years (1–4)."
    if not 1 <= contract_years <= 4:
        return False, "Contract length must be 1–4 years."
    d = get_driver(career, driver_id)
    if not d or d["status"] not in ("free", "f2", "f1", "tr"):
        return False, "That driver is not available to sign."
    # v0.7.1: an ill driver is recovering, not available to sign —
    # their seat is being kept for them at their own team.
    if d.get("ill_until_round"):
        return False, (f"{d['name']} is {d.get('ill_label') or 'ill'} and "
                       f"cannot be signed while they recover.")
    if d.get("status") in ("f1", "tr") and d.get("team_id") == team["id"]:
        return False, "That driver already races for your team."
    quote = sign_cost(career, team_id, driver_id, ("tr", None))
    if quote is None:
        return False, "That signing is not possible."
    cost = quote["total"]
    if cost > team["money"]:
        return False, f"Need ${cost:.2f}M to sign {d['name']}."
    # v0.8.0: poaching needs a secret meeting first — an agreed
    # meeting for this team, driver and the T&R slot (this is also
    # the mutual-exclusion gate: an academy junior of another team
    # cannot be signed as T&R without a meeting). The meeting's
    # bribe is paid on signing, on top of the poaching costs.
    meeting = None
    bribe = 0.0
    if is_poach(career, team_id, d):
        meeting = agreed_meeting(career, team_id, driver_id, ("tr", None))
        if meeting is None:
            return False, (f"You need a secret meeting with {d['name']} "
                           f"before you can sign them — request one from "
                           f"the Driver Market.")
        bribe = float(meeting.get("bribe") or 0.0)
        if cost + bribe > team["money"]:
            return False, (f"Need ${cost + bribe:.2f}M to sign {d['name']} "
                           f"(${cost:.2f}M fees + ${bribe:.2f}M bribe).")
    # The losing team: the driver's current team (a main/T&R poach)
    # or their academy team (an academy poach — _sign_driver_common
    # clears the academy link, so it is captured here).
    academy_team = None
    if d.get("academy_team_id") and d["academy_team_id"] != team["id"]:
        academy_team = get_team(career, d["academy_team_id"])
    source_team = None
    if d.get("status") in ("f1", "tr") and d.get("team_id"):
        source_team = get_team(career, d["team_id"])
        if source_team is not None:
            if source_team["driver1_id"] == d["id"]:
                source_team["driver1_id"] = None
            if source_team["driver2_id"] == d["id"]:
                source_team["driver2_id"] = None
            _release_driver(career, d, source_team)
            _recompute_team_points(career, source_team)
            log_transfer(career, 
                f"{d['name']} left {source_team['name']} (poached by {team['name']})")
    if academy_team is not None:
        log_transfer(career, 
            f"{d['name']} left {academy_team['name']}'s academy "
            f"(poached by {team['name']})")
    team["money"] -= cost
    _sign_driver_common(career, team, d, contract_years, as_tr=True)
    d["status"] = "tr"
    d["team_id"] = team["id"]
    log_transfer(career, 
        f"{d['name']} signed for {team['name']} as Test & Reserve")
    # v0.6: a WANTED signing pays the parent's reward to the junior team.
    _pay_wanted_reward(career, team, d)
    # v0.8.0: the agreed meeting is consumed, the bribe is paid, and
    # the losing team is notified in three places — the Transfers
    # feed (above), a Media entry, and a notice in their Drivers tab.
    if meeting is not None:
        career["meetings"] = [m for m in career.get("meetings") or []
                              if m is not meeting]
        if bribe > 0:
            team["money"] = round(team["money"] - bribe, 2)
        losing_team = source_team or academy_team
        if losing_team is not None:
            add_media_entry(
                career,
                f"{d['name']} has left {losing_team['name']} for "
                f"{team['name']} — a secret meeting sealed the move.",
                "transfer")
            add_team_notice(
                career, losing_team["id"],
                f"{d['name']} left your team for {team['name']}.")
    if cost > 0 or bribe > 0:
        extra = (f" + ${bribe:.2f}M bribe" if bribe > 0 else "")
        return True, (f"{d['name']} signed for {team['name']} as Test & Reserve ✓ "
                      f"({contract_years}-year contract) — ${cost:.2f}M break fee{extra}")
    return True, (f"{d['name']} signed for {team['name']} as Test & Reserve ✓ "
                  f"({contract_years}-year contract) — Free")


# ---------------------------------------------------------------------------
# v0.8.0: driver happiness + loyalty, activities, bribes, secret
# meetings, media rumours (Matthew's locked answers, 2026-11-25).
# Happiness and loyalty are floats 1.0-5.0 per (driver, team)
# relation (rules.happiness_level: 1 very unhappy .. 5 very happy).
# They are HIDDEN — never in the public broadcast, never in the
# Driver Market; a team's own drivers carry their current values
# in the private my_team message only (server.py).
# ---------------------------------------------------------------------------

# v0.8.0: a secret meeting costs $0.5M to set up, deducted when
# requested (whether the driver accepts or declines).
MEETING_COST = 0.5
# v0.8.0: the leak chance on any meeting (accepted or declined).
MEETING_LEAK_CHANCE = 0.15
# v0.8.0: the leak's costs — the approaching team's hidden rep
# drops 2.0, the driver's hidden rep drops 0.1, the leaking
# driver's happiness/loyalty with their current team drop 0.5 each,
# and the OTHER driver's loyalty to that team rises 0.3.
MEETING_LEAK_TEAM_REP_LOSS = 2.0
MEETING_LEAK_DRIVER_REP_LOSS = 0.1
MEETING_LEAK_HAPPINESS_DROP = 0.5
MEETING_LEAK_LOYALTY_DROP = 0.5
MEETING_LEAK_OTHER_LOYALTY_GAIN = 0.3
# v0.8.0: re-signing declines — an unhappy driver turns down their
# own team's re-signing offer 70% of the time, a very unhappy one
# 90% (re-signing only; seat swaps are the team's call).
RESIGN_DECLINE_UNHAPPY = 0.70
RESIGN_DECLINE_VERY_UNHAPPY = 0.90
# v0.8.0: a happy driver messages their manager after a race with
# a 15% chance per race (at most 2 messages per team per race).
DRIVER_MESSAGE_CHANCE = 0.15
# v0.8.1: the share of media rumours that are plausible-but-wrong
# speculation instead of grounded truth (about 1 in 4). The
# speculation is always close to reality — an unhappy or
# out-of-contract driver linked to a plausible destination — never
# a claim that contradicts the state.
MEDIA_SPECULATION_CHANCE = 0.25
# v0.8.1: activity decline chances — an unhappy driver turns down
# an activity 60% of the time, a very unhappy one 85%. Neutral and
# above always accept (unless ill). A declined activity costs
# nothing.
ACTIVITY_DECLINE_UNHAPPY = 0.60
ACTIVITY_DECLINE_VERY_UNHAPPY = 0.85
# v0.8.0: loyalty growth per race — +0.04 base, x2 when happy,
# x3 when very happy.
LOYALTY_GROWTH_PER_RACE = 0.04
# v0.8.0: long T&R benching — happiness starts eroding after 12
# races benched (-0.02/race), faster after 24 races (-0.05/race,
# about two seasons).
TR_BENCH_EROSION_START = 12
TR_BENCH_EROSION_FAST = 24
TR_BENCH_EROSION_SLOW = 0.02
TR_BENCH_EROSION_FAST_RATE = 0.05

# v0.8.0: the activities a manager can do with an at-least-happy
# driver (level >= 4) — one per driver per round. Realistic F1-team
# activities, each with a money cost and a happiness/loyalty effect.
# (Max's design, correctable by Matthew.)
ACTIVITIES = [
    {"id": "simulator", "name": "Simulator session", "cost": 0.3,
     "happiness": 0.15, "loyalty": 0.05},
    {"id": "team_dinner", "name": "Team dinner", "cost": 0.2,
     "happiness": 0.15, "loyalty": 0.10},
    {"id": "media_day", "name": "Media day together", "cost": 0.1,
     "happiness": 0.10, "loyalty": 0.05},
    {"id": "fitness_camp", "name": "Fitness camp", "cost": 0.4,
     "happiness": 0.10, "loyalty": 0.10},
    {"id": "factory_tour", "name": "Factory tour with the engineers",
     "cost": 0.2, "happiness": 0.15, "loyalty": 0.05},
    {"id": "sponsor_event", "name": "Sponsor event", "cost": 0.1,
     "happiness": 0.05, "loyalty": 0.10},
    {"id": "karting", "name": "Karting day", "cost": 0.3,
     "happiness": 0.20, "loyalty": 0.05},
    {"id": "charity", "name": "Charity visit", "cost": 0.2,
     "happiness": 0.15, "loyalty": 0.10},
    {"id": "principal_chat", "name": "One-on-one with the team principal",
     "cost": 0.1, "happiness": 0.20, "loyalty": 0.15},
    {"id": "home_gp", "name": "Home Grand Prix hospitality", "cost": 0.5,
     "happiness": 0.25, "loyalty": 0.15},
    {"id": "esports", "name": "Esports / sim-racing night", "cost": 0.1,
     "happiness": 0.10, "loyalty": 0.05},
    {"id": "altitude_camp", "name": "Altitude training camp", "cost": 0.6,
     "happiness": 0.15, "loyalty": 0.20},
    {"id": "photoshoot", "name": "Team photoshoot", "cost": 0.2,
     "happiness": 0.10, "loyalty": 0.05},
    {"id": "golf", "name": "Golf day", "cost": 0.3,
     "happiness": 0.15, "loyalty": 0.10},
]

# v0.8.0: happy-driver messages to their own manager (shown at
# the top of the dashboard and in the Drivers tab, own team only).
DRIVER_MESSAGES = [
    "Really enjoying my time with the team — the atmosphere in the garage is brilliant.",
    "I love working with this group. The car is coming to me more every weekend.",
    "The team has been fantastic with me. I want to repay them on track.",
    "I feel at home here. Let's keep pushing — there's more in this car.",
    "The support from the factory has been unreal. I'm proud to drive for this team.",
    "Every weekend I feel more part of this team. The belief is there.",
]

# v0.8.1: PlanetF1-style media rumour templates. {d} = driver,
# {t} = their team, {r} = a rival team with a REAL link (a vacancy
# or a plausible move — never random), {c} = circuit. EVERY rumour
# must trace to a verified real condition (see
# generate_media_rumours): an unhappy/happy driver, a contract that
# really is expiring, a seat that really is vacant, or recent form
# that really happened (scoreless / sliding / climbing / winning).
# The SPECULATION set is the only one that may be wrong — and only
# in the "linked with" sense: the driver really is unhappy or out
# of contract, and the rival really is a plausible destination. It
# never contradicts the state (never a championship leader to a
# backmarker, never a scoreless claim about a team that scored).
MEDIA_UNHAPPY_TEMPLATES = [
    "Tensions are said to be growing between {d} and {t} behind closed doors.",
    "{d} was spotted deep in conversation with {t}'s team principal after the race.",
    "All is not well at {t} — {d} cut a frustrated figure in the paddock.",
]
MEDIA_HAPPY_TEMPLATES = [
    "{d} has praised {t}: 'I love it here — the team feels like family.'",
    "{d} says {t} are 'the best group I have ever worked with'.",
]
MEDIA_SCORELESS_TEMPLATES = [
    "Questions are being asked at {t} after a run of weekends without points.",
]
MEDIA_PRESSURE_TEMPLATES = [
    "The pressure is mounting on {t} — the board wants results, and fast.",
]
MEDIA_CORNER_TEMPLATES = [
    "{t} have turned a corner — the garage is buzzing after their recent run.",
]
MEDIA_ON_THE_MOVE_TEMPLATES = [
    "{t} are the team on the move — rivals are starting to take notice.",
]
MEDIA_WINNER_TEMPLATES = [
    "{d} took the chequered flag at {c} for {t} — the garage is buzzing.",
]
MEDIA_CONTRACT_TEMPLATES = [
    "{d}'s deal with {t} runs out at the end of the season — {r} are among those watching.",
    "{d}'s deal with {t} runs out at the end of the season — rivals are keeping a close eye on the situation.",
]
MEDIA_VACANCY_TEMPLATES = [
    "{d} has been linked with {t}'s vacant seat.",
]
MEDIA_SPECULATION_TEMPLATES = [
    "{d} has been linked with a shock move to {r} — though {t} insist they are going nowhere.",
    "Paddock whispers: {r} have asked about {d}'s availability at {t}.",
    "{d} is understood to be unsettled at {t} — {r} are said to be monitoring the situation.",
]


def happiness_with(d, team_id):
    """v0.8.0: a driver's stored happiness (1.0-5.0) with one team —
    3.0 (neutral) when there is no relation with that team."""
    if not d or not team_id:
        return 3.0
    return float((d.get("happiness_by_team") or {}).get(team_id, 3.0))


def loyalty_with(d, team_id):
    """v0.8.0: a driver's stored loyalty (1.0-5.0) with one team —
    3.0 (medium) when there is no relation with that team."""
    if not d or not team_id:
        return 3.0
    return float((d.get("loyalty_by_team") or {}).get(team_id, 3.0))


def _bump_relation(d, team_id, dh, dl):
    """v0.8.0: move a driver's happiness/loyalty with one team by
    the deltas, clamped to [1.0, 5.0]. The stored relation moves;
    when this is the driver's current team, the current value moves
    too."""
    hb = d.setdefault("happiness_by_team", {})
    lb = d.setdefault("loyalty_by_team", {})
    cur_h = float(hb.get(team_id, d.get("happiness", 3.0)))
    cur_l = float(lb.get(team_id, d.get("loyalty", 3.0)))
    new_h = max(1.0, min(5.0, cur_h + dh))
    new_l = max(1.0, min(5.0, cur_l + dl))
    hb[team_id] = round(new_h, 2)
    lb[team_id] = round(new_l, 2)
    if d.get("team_id") == team_id or d.get("academy_team_id") == team_id:
        d["happiness"] = round(new_h, 2)
        d["loyalty"] = round(new_l, 2)


def apply_signing_relation(career, d, team_id):
    """v0.8.0: set a driver's happiness/loyalty when they sign with
    a team. A team they have never been at: reset to neutral (3.0)
    with fresh loyalty (1.0 — they just arrived). A team they were
    at before: average the current happiness with the happiness
    from the last stint there (.5 rounds to nearest, halves up);
    loyalty the same way. Called from _sign_driver_common (every
    market / winter-break / auto-fill signing) and _attach_driver
    (junior promotions). Same-team moves never call it."""
    hb = d.setdefault("happiness_by_team", {})
    lb = d.setdefault("loyalty_by_team", {})
    if team_id in hb:
        cur_h = float(d.get("happiness", 3.0))
        cur_l = float(d.get("loyalty", 3.0))
        new_h = float(int((cur_h + float(hb[team_id])) / 2.0 + 0.5))
        new_l = float(int((cur_l + float(lb.get(team_id, 3.0))) / 2.0 + 0.5))
    else:
        new_h, new_l = 3.0, 1.0
    d["happiness"] = new_h
    d["loyalty"] = new_l
    hb[team_id] = new_h
    lb[team_id] = new_l
    # A fresh relation starts with no tuning streak / bench time.
    d["tuning_against_streak"] = 0
    d["tr_bench_races"] = 0


def is_poach(career, team_id, d):
    """v0.8.0: True when signing this driver means taking them from
    another team — a main/T&R driver under contract elsewhere, or
    an academy junior of another team. Your own academy junior,
    free agents and F2 drivers are NOT poaches."""
    if d.get("status") in ("f1", "tr") and d.get("team_id") \
            and d["team_id"] != team_id:
        return True
    if d.get("academy_team_id") and d["academy_team_id"] != team_id:
        return True
    return False


def bribe_want_for(career, offering_team_id, d):
    """v0.8.0: the bribe ($M) this driver wants from the offering
    team (rules.bribe_want). For a poach it is based on the
    happiness/loyalty with the driver's CURRENT team (or academy
    team); for a free agent / own academy junior, on the relation
    with the offering team."""
    if d.get("status") in ("f1", "tr") and d.get("team_id") \
            and d["team_id"] != offering_team_id:
        rel = d["team_id"]
    elif d.get("academy_team_id") \
            and d["academy_team_id"] != offering_team_id:
        rel = d["academy_team_id"]
    else:
        rel = offering_team_id
    return rules_mod.bribe_want(happiness_with(d, rel),
                                loyalty_with(d, rel))


def agreed_meeting(career, team_id, driver_id, target):
    """v0.8.0: the team's AGREED meeting with this driver for this
    target (("seat", 1|2) or ("tr", None)), or None. A meeting for
    Seat 1 does not cover Seat 2 — the driver agreed to a seat."""
    for m in career.get("meetings") or []:
        if m.get("requester_team_id") != team_id:
            continue
        if m.get("driver_id") != driver_id:
            continue
        if m.get("target") != target[0]:
            continue
        if target[0] == "seat" and m.get("seat") != target[1]:
            continue
        return m
    return None


def meeting_cooldown_left(career, team_id, driver_id):
    """v0.8.0: rounds left before this team may try this driver
    again after a declined meeting (0 = free to try)."""
    return int((career.get("meeting_cooldowns") or {}).get(
        f"{team_id}:{driver_id}", 0))


def tick_meeting_cooldowns(career):
    """v0.8.0: one round passes — every meeting cooldown ticks
    down by 1 (winter-break rounds count as rounds)."""
    cds = career.get("meeting_cooldowns") or {}
    for key in list(cds):
        cds[key] = int(cds[key]) - 1
        if cds[key] <= 0:
            del cds[key]


def meeting_accept_probability(career, team, d, target, bribe=0.0):
    """v0.8.0: the driver's chance of accepting a secret meeting
    (and the move it leads to). Base 0.5, then:
      happiness with their current team: very unhappy +0.30,
        unhappy +0.20, neutral +0.05, happy -0.20, very happy -0.30
      competitiveness: the approaching team's constructors' place
        vs the current team's, +0.05 per place better (cap +0.25),
        -0.05 per place worse (cap -0.25); a star (rep >= 4) will
        not join a bottom-3 team (-0.15)
      seat: a main seat +0.15, a T&R slot -0.15 (drivers always
        prefer a main seat over a T&R role)
      loyalty with their current team: -0.08 per point above 3,
        +0.08 per point below 3
      team reputation: (approaching rep - current rep) x 0.002,
        clamped to +/-0.10
      bribe: if the driver is happy and the bribe is below their
        want, the chance halves (a happy driver without a bribe is
        much harder to lure); at or above the want, no penalty.
    Clamped to [0.02, 0.98]. A happy driver approached by an
    ADJACENT team refuses outright before this roll — see
    request_meeting."""
    rel_team_id = d.get("team_id") or d.get("academy_team_id")
    cur_team = get_team(career, rel_team_id) if rel_team_id else None
    hap = happiness_with(d, rel_team_id)
    loy = loyalty_with(d, rel_team_id)
    lvl = rules_mod.happiness_level(hap)
    p = 0.5
    p += {1: 0.30, 2: 0.20, 3: 0.05, 4: -0.20, 5: -0.30}[lvl]
    if cur_team is not None:
        cur_pos = constructor_pos(career, cur_team["id"])
        app_pos = constructor_pos(career, team["id"])
        p += max(-0.25, min(0.25, 0.05 * (cur_pos - app_pos)))
        if d.get("rep", 2.5) >= 4.0 and app_pos >= len(career["teams"]) - 2:
            p -= 0.15
    p += 0.15 if target[0] == "seat" else -0.15
    p += -0.08 * (loy - 3.0)
    if cur_team is not None:
        p += max(-0.10, min(0.10,
                            (team_rep_of(team) - team_rep_of(cur_team)) * 0.002))
    want = bribe_want_for(career, team["id"], d)
    if lvl >= 4 and want > 0 and (bribe or 0.0) < want:
        p *= 0.5
    return max(0.02, min(0.98, p))


def _meeting_leak(career, team, d, rel_team_id):
    """v0.8.0: a secret meeting leaked — a Media entry, reputation
    loss for the approaching team (-2.0) and the driver (-0.1 rep),
    the leaking driver's happiness/loyalty with their current team
    drop 0.5 each, the OTHER driver's loyalty to that team rises
    0.3, and a BOT current team shifts its tuning in favour of the
    other driver automatically (a human team sees the leak in Media
    and sets tuning themselves)."""
    cur_team = get_team(career, rel_team_id) if rel_team_id else None
    cur_name = cur_team["name"] if cur_team else "their team"
    add_media_entry(
        career,
        f"LEAKED: {team['name']} held a secret meeting with {d['name']} "
        f"({cur_name}) — the paddock is talking.", "leak")
    adjust_team_rep(team, -MEETING_LEAK_TEAM_REP_LOSS)
    race_mod.adjust_driver_rep(d, -MEETING_LEAK_DRIVER_REP_LOSS)
    if cur_team is None:
        return
    _bump_relation(d, rel_team_id, -MEETING_LEAK_HAPPINESS_DROP,
                   -MEETING_LEAK_LOYALTY_DROP)
    mains = [x for x in team_drivers(career, cur_team)
             if x and x["id"] != d["id"]]
    if not mains:
        return
    other = mains[0]
    _bump_relation(other, rel_team_id, 0.0, MEETING_LEAK_OTHER_LOYALTY_GAIN)
    if cur_team.get("is_ai"):
        cur = int(cur_team.get("tuning") or 0)
        if other["id"] == cur_team.get("driver1_id"):
            cur_team["tuning"] = min(3, cur + 1)
        elif other["id"] == cur_team.get("driver2_id"):
            cur_team["tuning"] = max(-3, cur - 1)


def request_meeting(career, team_id, driver_id, target, bribe=0.0, rng=None):
    """v0.8.0: request a secret meeting with a driver under contract
    (or in an academy) with another team, BEFORE poaching them.
    Costs $0.5M to set up, deducted when requested. The driver
    accepts or declines on RNG (see meeting_accept_probability). A
    happy driver approached by an ADJACENT team (immediately above
    or below in the constructors' standings) refuses outright — no
    meeting, no charge. Any meeting (either outcome) may leak (15%):
    a Media entry + reputation loss + the current-team reaction
    (see _meeting_leak). A failed meeting means a 1-5 round
    cooldown before that team may try that driver again (happier /
    more loyal -> longer). An agreed meeting is stored until the
    signing consumes it (or the season ends)."""
    rng = rng or random
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    # v0.8.0: meetings are allowed in the winter break too — the
    # break is the only window where an academy junior can be
    # poached (main/T&R drivers under contract cannot be offered
    # at all), so the meeting that gates the poach must be
    # requestable there. A meeting agreed in the break is consumed
    # by the signing, like any other.
    d = get_driver(career, driver_id)
    if not d:
        return False, "No such driver."
    if not is_poach(career, team_id, d):
        return False, (f"{d['name']} is not under contract with another "
                       f"team — no meeting needed, just sign them.")
    if not (isinstance(target, (tuple, list)) and len(target) == 2):
        return False, "Pick a main seat or a T&R slot for the meeting."
    if target[0] == "seat":
        if target[1] not in (1, 2):
            return False, "A main-seat meeting needs seat 1 or 2."
    elif target[0] != "tr":
        return False, "A meeting must target a main seat or a T&R slot."
    left = meeting_cooldown_left(career, team_id, driver_id)
    if left > 0:
        return False, (f"{d['name']} is not taking your calls — try again "
                       f"in {left} round{'s' if left != 1 else ''}.")
    if agreed_meeting(career, team_id, driver_id, target):
        return False, (f"You already have an agreed meeting with "
                       f"{d['name']} — sign them.")
    try:
        bribe = float(bribe or 0.0)
    except (TypeError, ValueError):
        return False, "The bribe must be a number."
    if bribe < 0:
        return False, "The bribe cannot be negative."
    if bribe > team["money"]:
        return False, (f"Not enough money for a ${bribe:.2f}M bribe — "
                       f"you have ${team['money']:.2f}M.")
    rel_team_id = d.get("team_id") or d.get("academy_team_id")
    # A happy driver turns down an approach from a neighbouring team
    # outright — the meeting never takes place, so nothing is charged.
    if rel_team_id and rules_mod.happiness_level(
            happiness_with(d, rel_team_id)) >= 4:
        cur_pos = constructor_pos(career, rel_team_id)
        app_pos = constructor_pos(career, team_id)
        if abs(cur_pos - app_pos) == 1:
            cur_team = get_team(career, rel_team_id)
            return False, (f"{d['name']} is happy at "
                           f"{cur_team['name'] if cur_team else 'their team'} "
                           f"and will not even take a meeting with a "
                           f"neighbouring team.")
    if team["money"] < MEETING_COST:
        return False, (f"Not enough money for a secret meeting — it costs "
                       f"${MEETING_COST:.2f}M.")
    team["money"] = round(team["money"] - MEETING_COST, 2)
    p = meeting_accept_probability(career, team, d, target, bribe)
    if rng.random() < p:
        career.setdefault("meetings", []).append({
            "requester_team_id": team_id, "driver_id": driver_id,
            "target": target[0],
            "seat": target[1] if target[0] == "seat" else None,
            "bribe": round(bribe, 2),
            "year": career.get("year"), "round": career.get("round")})
        outcome = (f"{d['name']} agreed to the secret meeting ✓ — "
                   f"you can sign them now")
        if bribe > 0:
            outcome += (f" (a ${bribe:.2f}M bribe will be paid on "
                        f"signing)")
    else:
        hap = happiness_with(d, rel_team_id)
        loy = loyalty_with(d, rel_team_id)
        wait = 1 + int(round(2.0 * (hap - 1.0) / 4.0
                             + 2.0 * (loy - 1.0) / 4.0))
        wait = max(1, min(5, wait))
        career.setdefault("meeting_cooldowns", {})[
            f"{team_id}:{driver_id}"] = wait
        outcome = (f"{d['name']} turned the meeting down — you cannot "
                   f"try them again for {wait} "
                   f"round{'s' if wait != 1 else ''}.")
    if rng.random() < MEETING_LEAK_CHANCE:
        _meeting_leak(career, team, d, rel_team_id)
        outcome += " (The meeting leaked — the paddock is talking.)"
    return True, outcome


def _activity_round_key(career):
    """v0.8.0: the round key for the one-activity-per-driver-per-round
    limit — a racing round is "2029R3", a winter-break round is
    "2029WB2" (so the break's three rounds each allow an activity)."""
    if career.get("phase") == "winter_break":
        return f"{career.get('year')}WB{career.get('wb_round', 1)}"
    return f"{career.get('year')}R{career.get('round', 1)}"


def do_activity(career, team_id, driver_id, activity_id, rng=None):
    """v0.8.1: do an activity with one of your drivers (main, T&R or
    academy) — at ANY happiness level. An unhappy driver turns it
    down 60% of the time, a very unhappy one 85%; neutral and above
    always accept (unless ill). A declined activity costs nothing
    and does not use up the one-per-driver-per-round slot. One
    activity per driver per round, and the team must afford the
    cost. Raises that driver's happiness and loyalty with the team."""
    rng = rng or random
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    d = get_driver(career, driver_id)
    if not d:
        return False, "No such driver."
    if d.get("team_id") != team_id and d.get("academy_team_id") != team_id:
        return False, f"{d['name']} is not one of your drivers."
    act = next((a for a in ACTIVITIES if a["id"] == activity_id), None)
    if not act:
        return False, "No such activity."
    if d.get("ill_until_round"):
        return False, (f"{d['name']} is ill — no activities until "
                       f"they recover.")
    key = _activity_round_key(career)
    if d.get("activity_round") == key:
        return False, (f"You already did an activity with {d['name']} "
                       f"this round.")
    if act["cost"] > team["money"]:
        return False, (f"Not enough money — {act['name']} costs "
                       f"${act['cost']:.2f}M.")
    lvl = rules_mod.happiness_level(happiness_with(d, team_id))
    if lvl <= 1 and rng.random() < ACTIVITY_DECLINE_VERY_UNHAPPY:
        return False, (f"{d['name']} turned the activity down — they "
                       f"are very unhappy with the team right now.")
    if lvl == 2 and rng.random() < ACTIVITY_DECLINE_UNHAPPY:
        return False, (f"{d['name']} turned the activity down — they "
                       f"are unhappy with the team right now.")
    team["money"] = round(team["money"] - act["cost"], 2)
    _bump_relation(d, team_id, act["happiness"], act["loyalty"])
    d["activity_round"] = key
    return True, (f"{act['name']} with {d['name']} ✓ — they are happier "
                  f"({rules_mod.happiness_word(happiness_with(d, team_id))})")


def log_transfer(career, text):
    """v0.8.1: append one entry to the Driver transfers feed. Entries
    are structured dicts (not plain strings) so the UI can show when
    each one happened — a round label like "R8 2026", or "WB2 2026"
    for winter-break moves — and can style the driver/team names.
    Kept to the newest 100 (a season's real moves fit comfortably;
    the cap only trims multi-season spam)."""
    entries = career.setdefault("transfers", [])
    year = career.get("year")
    if career.get("phase") == "winter_break":
        label = f"WB{career.get('wb_round') or 1} {year}" if year else None
        rnd = career.get("round")
    else:
        label = f"R{career.get('round')} {year}" if year else None
        rnd = career.get("round")
    entries.append({"text": text, "year": year, "round": rnd,
                    "label": label})
    if len(entries) > 100:
        del entries[:len(entries) - 100]


def add_media_entry(career, text, kind="rumour"):
    """v0.8.0: append one entry to the Media & Rumours feed (newest
    last in storage, newest first in the UI). Kept to the newest 40."""
    entries = career.setdefault("media_rumours", [])
    entries.append({"text": text, "kind": kind,
                    "year": career.get("year"), "round": career.get("round")})
    if len(entries) > 40:
        del entries[:len(entries) - 40]


def add_team_notice(career, team_id, text):
    """v0.8.0: a notice for one team's Drivers tab (the losing team
    after a poach). Kept to the newest 40 across all teams."""
    notes = career.setdefault("team_notices", [])
    notes.append({"team_id": team_id, "text": text,
                  "year": career.get("year"), "round": career.get("round")})
    if len(notes) > 40:
        del notes[:len(notes) - 40]


def _constructors_positions_by_round(career):
    """v0.8.0: per-round constructors' positions, derived from
    race_history — for each race entry (in order), the cumulative
    team points within that season, ranked (points desc, team name
    for stability). Returns one {team_id: pos} dict per race, in
    chronological order. Short history = short list (the trend uses
    whatever is available)."""
    series = []
    cum = {}
    cur_year = None
    for entry in career.get("race_history") or []:
        year = entry.get("year")
        if year != cur_year:
            cur_year = year
            cum = {}
        for row in entry.get("race") or []:
            tid = row.get("team_id")
            if not tid:
                continue
            cum[tid] = cum.get(tid, 0) + (row.get("points") or 0)
        ranked = sorted(career["teams"],
                        key=lambda t: (-cum.get(t["id"], 0), t["name"]))
        series.append({t["id"]: i + 1 for i, t in enumerate(ranked)})
    return series


def _teammate_gap_12(career, d, team):
    """v0.8.0: (points_gap, finish_gap) for a main driver vs their
    teammate over the last 12 races — positive means the driver is
    BEHIND (teammate scored more / finished better on average).
    From race_history: per-round points and finish positions by
    driver id. The teammate is the driver's CURRENT teammate (their
    points over the window count wherever they raced them)."""
    entries = (career.get("race_history") or [])[-12:]
    if not entries:
        return (0, 0.0)
    mate_id = team["driver2_id"] if team.get("driver1_id") == d["id"] \
        else team.get("driver1_id")
    if not mate_id:
        return (0, 0.0)
    my_pts = mate_pts = 0
    my_fin = mate_fin = 0
    my_n = mate_n = 0
    for entry in entries:
        for row in entry.get("race") or []:
            if row.get("driver_id") == d["id"]:
                my_pts += row.get("points") or 0
                if isinstance(row.get("pos"), int):
                    my_fin += row["pos"]
                    my_n += 1
            elif row.get("driver_id") == mate_id:
                mate_pts += row.get("points") or 0
                if isinstance(row.get("pos"), int):
                    mate_fin += row["pos"]
                    mate_n += 1
    finish_gap = 0.0
    if my_n and mate_n:
        finish_gap = (my_fin / my_n) - (mate_fin / mate_n)
    return (mate_pts - my_pts, finish_gap)


def _season_positions_by_round(career):
    """v0.8.1: per-round constructors' positions for the CURRENT
    season only (like _constructors_positions_by_round, but a new
    season restarts the list instead of continuing it) — for the
    media rumours' recent-form checks."""
    year = career.get("year")
    series = []
    cum = {}
    for entry in career.get("race_history") or []:
        if entry.get("year") != year:
            continue
        for row in entry.get("race") or []:
            tid = row.get("team_id")
            if not tid:
                continue
            cum[tid] = cum.get(tid, 0) + (row.get("points") or 0)
        ranked = sorted(career["teams"],
                        key=lambda t: (-cum.get(t["id"], 0), t["name"]))
        series.append({t["id"]: i + 1 for i, t in enumerate(ranked)})
    return series


def _team_points_last_n(career, team_id, n):
    """v0.8.1: points a team scored in the last n races of the
    current season (0 if fewer than n races have been run)."""
    total = 0
    count = 0
    for entry in reversed(career.get("race_history") or []):
        if entry.get("year") != career.get("year"):
            break
        for row in entry.get("race") or []:
            if row.get("team_id") == team_id:
                total += row.get("points") or 0
        count += 1
        if count >= n:
            break
    return total


def _team_wins_last_n(career, team_id, n):
    """v0.8.1: race wins for a team in the last n races of the
    current season."""
    wins = 0
    count = 0
    for entry in reversed(career.get("race_history") or []):
        if entry.get("year") != career.get("year"):
            break
        for row in entry.get("race") or []:
            if row.get("team_id") == team_id and row.get("pos") == 1:
                wins += 1
        count += 1
        if count >= n:
            break
    return wins


def _plausible_rival(career, d):
    """v0.8.1: a team it is PLAUSIBLE for driver d to move to — a
    team with a genuinely vacant main seat first, else a team in the
    frontrunner band (top 4) or within 3 constructors' positions of
    d's team. A championship leader's team is P1, so their rivals
    are P1–P4 only — never a backmarker. Returns the team or None."""
    teams = career["teams"]
    own = get_team(career, d.get("team_id"))
    own_pos = constructor_pos(career, d.get("team_id")) if own else None
    cands = []
    for t in teams:
        if not own or t["id"] == own["id"]:
            continue
        pos = constructor_pos(career, t["id"])
        vacant = not t.get("driver1_id") or not t.get("driver2_id")
        if vacant or pos <= 4 or (own_pos is not None
                                 and abs(pos - own_pos) <= 3):
            cands.append((0 if vacant else 1, abs(pos - (own_pos or pos)),
                          t))
    if not cands:
        return None
    cands.sort(key=lambda c: (c[0], c[1], c[2]["name"]))
    return cands[0][2]


def generate_media_rumours(career, rng=None):
    """v0.8.1: two PlanetF1-style rumours after every race — every
    one traces to a verified real condition in the game state (the
    v0.8.0 random pairing is gone). Grounded sources: an unhappy
    driver (tension), a happy driver (praise), a contract that really
    expires at season end, a seat that really is vacant, and recent
    form that really happened — scoreless in the last 3 races,
    dropped 2+ places in the last 4, climbed 2+ places, won 2+ of
    the last 3, or won the race just run. About 1 in 4 rumours is
    plausible-but-wrong speculation instead: a driver who really is
    unhappy or out of contract, linked to a team that really is a
    plausible destination (a vacancy or a team in range) — the move
    itself has not happened and may never happen, but it is close to
    reality. Never a championship leader to a backmarker, never a
    scoreless/struggling claim about a team that scored or climbed."""
    rng = rng or random
    drivers = [d for d in career["drivers"]
               if d.get("status") in ("f1", "tr") and d.get("team_id")]
    teams = career["teams"]
    if not drivers or not teams:
        return

    def _team_name(tid):
        t = get_team(career, tid)
        return t["name"] if t else "a rival team"

    def _fmt(tmpl, d=None, t=None, r=None, c=None):
        return tmpl.format(d=d["name"] if d else "",
                           t=t["name"] if t else "",
                           r=r["name"] if r else "",
                           c=c or "")

    grounded = []       # (text, kind)
    speculative = []    # (text, kind) — plausible but not real
    unhappy = [d for d in drivers
               if rules_mod.happiness_level(
                   happiness_with(d, d.get("team_id"))) <= 2]
    happy = [d for d in drivers
             if rules_mod.happiness_level(
                 happiness_with(d, d.get("team_id"))) >= 4]
    for d in unhappy:
        grounded.append((_fmt(rng.choice(MEDIA_UNHAPPY_TEMPLATES),
                              d=d, t=get_team(career, d["team_id"])),
                         "unhappy"))
        rival = _plausible_rival(career, d)
        if rival is not None:
            speculative.append((_fmt(
                rng.choice(MEDIA_SPECULATION_TEMPLATES), d=d,
                t=get_team(career, d["team_id"]), r=rival),
                "speculation"))
    for d in happy:
        grounded.append((_fmt(rng.choice(MEDIA_HAPPY_TEMPLATES),
                              d=d, t=get_team(career, d["team_id"])),
                         "happy"))
    # Contracts really expiring at season end.
    for d in drivers:
        if (d.get("contract_left") is not None
                and d.get("contract_left") <= 1):
            rival = _plausible_rival(career, d)
            if rival is not None:
                grounded.append((_fmt(
                    MEDIA_CONTRACT_TEMPLATES[0], d=d,
                    t=get_team(career, d["team_id"]), r=rival),
                    "transfer"))
                speculative.append((_fmt(
                    rng.choice(MEDIA_SPECULATION_TEMPLATES), d=d,
                    t=get_team(career, d["team_id"]), r=rival),
                    "speculation"))
            else:
                grounded.append((_fmt(
                    MEDIA_CONTRACT_TEMPLATES[1], d=d,
                    t=get_team(career, d["team_id"])), "transfer"))
    # A seat that really is vacant, and a driver actually available.
    vacant = [t for t in teams
              if not t.get("driver1_id") or not t.get("driver2_id")]
    available = [d for d in career["drivers"]
                 if d.get("status") in ("free", "f2")]
    if vacant and available:
        t = rng.choice(vacant)
        d = rng.choice(available)
        grounded.append((_fmt(rng.choice(MEDIA_VACANCY_TEMPLATES),
                              d=d, t=t), "transfer"))
    # Recent form — only claims the state actually supports.
    series = _season_positions_by_round(career)
    season_races = [e for e in (career.get("race_history") or [])
                    if e.get("year") == career.get("year")]
    for t in teams:
        if len(season_races) >= 3 and \
                _team_points_last_n(career, t["id"], 3) == 0:
            grounded.append((_fmt(rng.choice(MEDIA_SCORELESS_TEMPLATES),
                                  t=t), "performance"))
        if len(series) >= 2:
            back = series[-4] if len(series) >= 4 else series[0]
            move = back.get(t["id"], series[-1][t["id"]]) \
                - series[-1][t["id"]]  # positive = climbed
            if move <= -2:
                grounded.append((_fmt(
                    rng.choice(MEDIA_PRESSURE_TEMPLATES), t=t),
                    "performance"))
            elif move >= 2:
                grounded.append((_fmt(
                    rng.choice(MEDIA_CORNER_TEMPLATES), t=t),
                    "performance"))
        if _team_wins_last_n(career, t["id"], 3) >= 2:
            grounded.append((_fmt(
                rng.choice(MEDIA_ON_THE_MOVE_TEMPLATES), t=t),
                "performance"))
    # The race just run — the winner is always a true, grounded line
    # (and guarantees at least one candidate every race).
    last = (career.get("race_history") or [])
    if last:
        entry = last[-1]
        winners = [r for r in entry.get("race") or []
                   if r.get("pos") == 1]
        if winners:
            w = winners[0]
            d = next((x for x in career["drivers"]
                      if x["id"] == w.get("driver_id")), None)
            if d:
                grounded.append((_fmt(
                    rng.choice(MEDIA_WINNER_TEMPLATES), d=d,
                    t=get_team(career, d.get("team_id")),
                    c=entry.get("circuit") or f"Round {entry.get('round')}"),
                    "performance"))
    # Two rumours: about 1 in 4 is the plausible speculation, the
    # rest grounded — never the same line twice.
    picked = []
    seen = set()
    for _ in range(2):
        pool = None
        if speculative and rng.random() < MEDIA_SPECULATION_CHANCE:
            pool = speculative
        if pool is None or not [c for c in pool if c[0] not in seen]:
            pool = grounded
        cands = [c for c in pool if c[0] not in seen]
        if not cands:
            cands = [c for c in grounded + speculative
                     if c[0] not in seen]
        if not cands:
            break
        text, kind = rng.choice(cands)
        seen.add(text)
        picked.append((text, kind))
    for text, kind in picked:
        add_media_entry(career, text, kind=kind)


def update_driver_happiness(career, rng=None):
    """v0.8.0: after every race, every driver associated with a team
    updates their happiness (and loyalty) with that team:
      (a) performance trend — the team's constructors' position
          change over 12 races (weight 0.75) and 48 races (0.25),
          +/-0.03 per place, clamped to +/-0.15/race, rep-adjusted:
          a high-rep team declining is an upset (x1.5), a low-rep
          team improving is an upset (x1.5); the expected direction
          is x0.5, the middle of the grid x1.0;
      (b) tuning — the disadvantaged driver loses happiness, scaled
          by |tuning| and by how long the disadvantage has run:
          -0.03 x |tuning| x (1 + 0.12 x streak) per race, so tune 1
          drifts slowly (about -0.64 over a full season, faster
          after) and tune 3 makes a driver unhappy within ~6 races;
          the favoured driver gains +0.015 x |tuning| per race;
      (c) kept in a main seat despite poor performances — the points
          + finishing gap to the teammate over 12 races, bigger gap
          = happier (gratitude for being kept): up to +0.08/race,
          rep-adjusted (a higher-rep driver behind their teammate
          is an upset, so the gratitude is bigger);
      (d) long T&R benching — after 12 races benched, -0.02/race;
          after 24 races (about two seasons), -0.05/race.
    Loyalty grows with tenure: +0.04/race, x2 when happy, x3 when
    very happy. All values clamp to [1.0, 5.0]. Happy drivers (level
    >= 4) may also message their manager (15% per race, at most 2
    per team per race)."""
    rng = rng or random
    series = _constructors_positions_by_round(career)
    n_teams = len(career["teams"])
    # The "expected" constructors' order from the teams' main
    # drivers' average hidden rep — for the rep adjustment.
    rep_rank = {}
    for team in career["teams"]:
        mains = [d for d in team_drivers(career, team) if d]
        rep_rank[team["id"]] = (sum(d.get("rep", 2.5) for d in mains)
                                / len(mains)) if mains else 2.5
    rep_order = sorted(career["teams"], key=lambda t: -rep_rank[t["id"]])
    expected_pos = {t["id"]: i + 1 for i, t in enumerate(rep_order)}
    raced_ids = {r.get("driver_id")
                 for r in (career.get("last_race") or [])}
    for team in career["teams"]:
        team_id = team["id"]
        # --- (a) trend delta for this team ---
        trend_delta = 0.0
        if series:
            now = series[-1].get(team_id, n_teams)

            def _pos_back(n):
                idx = max(0, len(series) - 1 - n)
                return series[idx].get(team_id, now)

            t12 = _pos_back(12) - now  # positive = improved
            t48 = _pos_back(48) - now
            combined = 0.75 * t12 + 0.25 * t48
            exp = expected_pos.get(team_id, now)
            if combined > 0:
                mult = 1.5 if exp >= n_teams - 2 else \
                    (0.5 if exp <= 3 else 1.0)
            elif combined < 0:
                mult = 1.5 if exp <= 3 else \
                    (0.5 if exp >= n_teams - 2 else 1.0)
            else:
                mult = 1.0
            trend_delta = max(-0.15, min(0.15, combined * 0.03 * mult))
        mains = [d for d in team_drivers(career, team) if d]
        tuning = int(team.get("tuning") or 0)
        assoc = list(mains)
        assoc += team_tr_drivers(career, team)
        assoc += team_academy_drivers(career, team)
        messages_this_race = 0
        for d in assoc:
            hap = happiness_with(d, team_id)
            loy = loyalty_with(d, team_id)
            lvl = rules_mod.happiness_level(hap)
            # Loyalty grows with tenure, faster when happy.
            growth = LOYALTY_GROWTH_PER_RACE * (
                3.0 if lvl >= 5 else (2.0 if lvl >= 4 else 1.0))
            new_loy = max(1.0, min(5.0, loy + growth))
            delta = trend_delta
            if d in mains:
                # --- (b) tuning ---
                is_d1 = d["id"] == team.get("driver1_id")
                disadvantaged = (tuning > 0 and not is_d1) or \
                    (tuning < 0 and is_d1)
                favoured = (tuning > 0 and is_d1) or \
                    (tuning < 0 and not is_d1)
                if disadvantaged:
                    streak = int(d.get("tuning_against_streak") or 0) + 1
                    d["tuning_against_streak"] = streak
                    delta += -0.03 * abs(tuning) * (1 + 0.12 * streak)
                else:
                    d["tuning_against_streak"] = 0
                    if favoured:
                        delta += 0.015 * abs(tuning)
                # --- (c) kept despite poor performances ---
                # v0.8.1: NO gratitude for a driver disadvantaged by
                # tuning — their poor performances are at least partly
                # the team's own doing (the team handicaps them, then
                # they would thank the team for keeping them). A
                # behind driver who is NOT disadvantaged still gets
                # the gratitude as before.
                mate = next((m for m in mains if m["id"] != d["id"]), None)
                if mate is not None and not disadvantaged:
                    points_gap, finish_gap = _teammate_gap_12(
                        career, d, team)
                    if points_gap > 0 or finish_gap > 0:
                        rep_diff = d.get("rep", 2.5) - mate.get("rep", 2.5)
                        mult = max(0.3, 1.0 + 0.3 * rep_diff)
                        delta += min(0.08, 0.003 * points_gap
                                     + 0.008 * finish_gap) * mult
            if d.get("status") == "tr":
                # --- (d) long T&R benching ---
                if d["id"] in raced_ids:
                    d["tr_bench_races"] = 0
                else:
                    bench = int(d.get("tr_bench_races") or 0) + 1
                    d["tr_bench_races"] = bench
                    if bench > TR_BENCH_EROSION_FAST:
                        delta += -TR_BENCH_EROSION_FAST_RATE
                    elif bench > TR_BENCH_EROSION_START:
                        delta += -TR_BENCH_EROSION_SLOW
            new_hap = max(1.0, min(5.0, hap + delta))
            hb = d.setdefault("happiness_by_team", {})
            lb = d.setdefault("loyalty_by_team", {})
            hb[team_id] = round(new_hap, 2)
            lb[team_id] = round(new_loy, 2)
            d["happiness"] = round(new_hap, 2)
            d["loyalty"] = round(new_loy, 2)
            # Happy drivers sometimes message their manager.
            if rules_mod.happiness_level(new_hap) >= 4 \
                    and messages_this_race < 2 \
                    and rng.random() < DRIVER_MESSAGE_CHANCE:
                career.setdefault("driver_messages", []).append({
                    "driver": d["name"], "team_id": team_id,
                    "text": rng.choice(DRIVER_MESSAGES),
                    "year": career.get("year"),
                    "round": career.get("round")})
                if len(career.get("driver_messages") or []) > 30:
                    del career["driver_messages"][:len(
                        career["driver_messages"]) - 30]
                messages_this_race += 1


def advance_blockers(career, team_id=None):
    """v0.8.0: the advance gate — a team with fewer than 2 valid
    main-seat drivers cannot advance to a race weekend. Returns the
    list of blocker strings (empty = clear). With team_id, only that
    team's seats are checked (the Advance button); without it, every
    team is checked (the host override). A seat counts as filled
    when a driver occupies it — an ill driver still occupies their
    seat (a substitute covers it), so illness never blocks."""
    out = []
    teams = [get_team(career, team_id)] if team_id else list(career["teams"])
    for team in teams:
        if not team:
            continue
        for seat, slot in ((1, "driver1_id"), (2, "driver2_id")):
            if not team.get(slot):
                out.append(f"{team['name']} Seat {seat}")
    return out


# ---------------------------------------------------------------------------
# v0.6: team relationships (supplier / customer / junior)
# ---------------------------------------------------------------------------

def supplier_of(career, team):
    """The team supplying this one (or None)."""
    if not team or not team.get("supplier_id"):
        return None
    return get_team(career, team["supplier_id"])


def supplied_teams(career, team):
    """Teams this team supplies (customers and junior)."""
    if not team:
        return []
    return [t for t in career["teams"] if t.get("supplier_id") == team["id"]]


def junior_team_of(career, team):
    """The junior team this team supplies (at most one), or None."""
    for t in supplied_teams(career, team):
        if t.get("sup_stage") == 2:
            return t
    return None


def relation_between(career, team_a_id, team_b_id):
    """The relation between two teams — (supplier, customer) — or None
    when neither supplies the other."""
    a = get_team(career, team_a_id)
    b = get_team(career, team_b_id)
    if not a or not b:
        return None
    if a.get("supplier_id") == b["id"]:
        return (b, a)
    if b.get("supplier_id") == a["id"]:
        return (a, b)
    return None


def relation_label(team):
    """'Customer' / 'Junior' / None, from a team's sup_stage."""
    return {1: "Customer", 2: "Junior"}.get(team.get("sup_stage") or 0)


def donate(career, donor_team_id, recipient_team_id, amount):
    """v0.6: donate money along a supply tie — down to a team you
    supply (customer or junior), or v0.6.5 up to your supplier
    (parent): a customer/junior team can now donate to the team
    that supplies it. Any amount up to the donor's money, any time,
    no fee. The donor loses it and the recipient gains it."""
    donor = get_team(career, donor_team_id)
    recipient = get_team(career, recipient_team_id)
    if not donor or not recipient:
        return False, "No such team."
    if donor["id"] == recipient["id"]:
        return False, "A team cannot donate to itself."
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return False, "Amount must be a number."
    if amount <= 0:
        return False, "Donation must be more than $0M."
    if amount > donor["money"]:
        return False, f"Not enough money — you have ${donor['money']:.2f}M."
    # v0.6.5: either direction along the same supply tie — the
    # recipient is supplied by the donor, or the donor is supplied
    # by the recipient (donating up to your parent).
    if recipient.get("supplier_id") != donor["id"] \
            and donor.get("supplier_id") != recipient["id"]:
        return False, (f"{donor['name']} and {recipient['name']} have no "
                       f"supply relation — donations only go along a "
                       f"supply tie, to a team you supply or to your "
                       f"supplier.")
    donor["money"] = round(donor["money"] - amount, 2)
    recipient["money"] = round(recipient["money"] + amount, 2)
    # v0.7.0: giving helps your reputation — +0.2 per $M, capped at
    # +1.0 per transfer. (v0.8.0: donation REQUESTS are gone entirely
    # — a gift is now the only way money moves between teams, so
    # there is no requester-reputation cost any more.)
    adjust_team_rep(donor, min(TEAM_REP_DONATION_RATE * amount,
                                TEAM_REP_DONATION_CAP))
    log_transfer(career, 
        f"{donor['name']} donated ${amount:.2f}M to {recipient['name']}")
    return True, f"Donated ${amount:.2f}M to {recipient['name']} ✓"


def _end_relation(career, customer):
    """v0.6: clear a relation from the customer team's side — the
    supplier link, the stage, the remaining years, and the junior
    mechanics that only exist inside a relation (WANTED marks). The
    ex-supplied team keeps its money, drivers and points; only the
    tie is gone. Also clears any pending break notice on the deal
    and any pending donation requests between the two teams.
    v0.6.6: the research cap is gone entirely (Matthew: 'mehhh') —
    there is nothing to clear for it any more."""
    supplier_id = customer.get("supplier_id")
    customer["supplier_id"] = None
    customer["supplier_name"] = None
    customer["sup_stage"] = 0
    customer["relation_years"] = 0
    customer["wanted"] = []
    customer["break_notice"] = None


def _apply_relation_change(career, proposal):
    """v0.6: carry out an accepted proposal. Returns the log line."""
    supplier = get_team(career, proposal["supplier_team_id"])
    customer = get_team(career, proposal["customer_team_id"])
    kind = proposal["kind"]
    years = proposal.get("years") or 0
    if kind == "customer":
        customer["supplier_id"] = supplier["id"]
        customer["supplier_name"] = supplier["name"]
        customer["sup_stage"] = 1
        customer["relation_years"] = years
        line = (f"{customer['name']} is now a customer of "
                f"{supplier['name']} ({years}-year deal)")
    elif kind == "junior":
        customer["supplier_id"] = supplier["id"]
        customer["supplier_name"] = supplier["name"]
        customer["sup_stage"] = 2
        customer["relation_years"] = years
        line = (f"{customer['name']} is now the junior team of "
                f"{supplier['name']} ({years}-year deal)")
    elif kind == "end":
        label = relation_label(customer) or "partner"
        _end_relation(career, customer)
        line = (f"{customer['name']} ended its {label} deal with "
                f"{supplier['name']}")
    elif kind == "downgrade":
        # A step down keeps the remaining contract years — it is a
        # stage change, not a new deal. v0.6.6: WANTED marks SURVIVE
        # the step down — WANTED now works for customer teams too,
        # with the same rules, so the parent's marks stay live.
        customer["sup_stage"] = 1
        line = (f"{customer['name']} stepped down from junior to "
                f"customer of {supplier['name']}")
    elif kind == "renew":
        customer["relation_years"] = years
        line = (f"{customer['name']} renewed its "
                f"{(relation_label(customer) or 'supplier').lower()} deal "
                f"with {supplier['name']} ({years} more years)")
    else:
        line = ""
    if line:
        log_transfer(career, line)
    return line


def _proposal_still_valid(career, proposal):
    """v0.6: re-check a pending proposal against the current state
    before it is accepted — a customer who has since taken another
    supplier, or a supplier who has since taken another junior, can
    no longer accept."""
    supplier = get_team(career, proposal["supplier_team_id"])
    customer = get_team(career, proposal["customer_team_id"])
    if not supplier or not customer:
        return False, "One of the teams is gone."
    kind = proposal["kind"]
    if kind == "customer":
        # v0.7.3: the SUPPLIER side is re-checked too — a proposal
        # sent before the would-be supplier took a supplier of
        # their own can no longer be accepted (a team is either a
        # parent or a child, never both — no supply chains).
        if supplier.get("supplier_id"):
            return False, (f"{supplier['name']} already has a supplier now.")
        if customer.get("sup_stage", 0) != 0:
            return False, f"{customer['name']} already has a supplier now."
        # v0.7.2: the customer must not have become a supplier since
        # the proposal was sent (a team is either a parent or a
        # child, never both).
        if supplied_teams(career, customer):
            return False, (f"{customer['name']} already supplies other "
                           f"teams now.")
    elif kind == "junior":
        # v0.7.3: same supplier-side re-check as for a customer
        # formation (see above).
        if supplier.get("supplier_id"):
            return False, (f"{supplier['name']} already has a supplier now.")
        if not (customer.get("sup_stage") == 1
                and customer.get("supplier_id") == supplier["id"]):
            return False, (f"{customer['name']} is no longer a customer "
                           f"of {supplier['name']}.")
        # v0.7.2: same mirror rule — a junior-to-be that has since
        # taken on supplied teams of its own cannot be a junior.
        if supplied_teams(career, customer):
            return False, (f"{customer['name']} already supplies other "
                           f"teams now.")
        other_junior = junior_team_of(career, supplier)
        if other_junior is not None and other_junior["id"] != customer["id"]:
            return False, f"{supplier['name']} already has a junior team."
    elif kind in ("end", "downgrade", "renew"):
        if relation_between(career, supplier["id"], customer["id"]) is None:
            return False, "Those teams no longer have a relation."
        # v0.6.6: a deal with a pending break notice is already on
        # its way out — no end / downgrade / renew on top of it.
        notice = customer.get("break_notice")
        if notice:
            return False, (f"That deal is already being broken — it "
                           f"ends in {notice.get('rounds_left', 0)} round(s).")
        if kind == "downgrade" and customer.get("sup_stage") != 2:
            return False, f"{customer['name']} is no longer a junior team."
        if kind == "renew" and (customer.get("relation_years") or 0) > 0:
            return False, "That deal is no longer expired."
    return True, "ok"


def propose_relation(career, proposer_team_id, target_team_id, kind,
                     supplier_team_id=None, years=2, rng=None):
    """v0.6: propose a relation change in the winter break. Either
    team may propose; the other side must accept (see
    respond_relation). Roles, as approved: for a FORMATION
    (customer / junior) the proposer is the would-be SUPPLIER and
    the target is the would-be customer/junior — 'I propose to
    supply you'. (A team that wants a supplier proposes with the
    other team as the supplier — the proposal names the supplier
    explicitly, so either side can start one.) For end / downgrade /
    renew the proposal concerns the existing relation between the two
    teams, and either side may propose it. Progression is one step at
    a time up (nothing -> customer -> junior; a direct nothing ->
    junior is rejected) and down (junior -> customer -> nothing, or
    junior -> nothing directly). A team has at most one supplier; a
    supplier has several customers but at most one junior. If the
    target is a bot team it decides on the spot by the simple rule in
    bot_accepts_proposal; a human target's proposal stays pending for
    them to answer in the Relationships tab (proposals expire when
    the next season starts)."""
    proposer = get_team(career, proposer_team_id)
    target = get_team(career, target_team_id)
    if not proposer or not target:
        return False, "No such team."
    if career.get("phase") != "winter_break":
        return False, "Relation changes happen in the winter break."
    if proposer["id"] == target["id"]:
        return False, "A team cannot form a relation with itself."
    if kind not in ("customer", "junior", "end", "downgrade", "renew"):
        return False, "Unknown relation change."
    # One pending proposal at a time between any two teams.
    for p in career.get("relation_proposals") or []:
        if {p["proposer_team_id"], p["target_team_id"]} == {proposer["id"], target["id"]}:
            return False, "There is already a pending proposal between these teams."
    if kind in ("customer", "junior", "renew"):
        try:
            years = int(years)
        except (TypeError, ValueError):
            return False, "Contract length must be a whole number of years (1–4)."
        if not 1 <= years <= 4:
            return False, "Contract length must be 1–4 years."
    else:
        years = 0
    if kind in ("customer", "junior"):
        supplier = get_team(career, supplier_team_id) if supplier_team_id else proposer
        if not supplier or supplier["id"] not in (proposer["id"], target["id"]):
            return False, "The supplier must be one of the two teams."
        customer = target if supplier["id"] == proposer["id"] else proposer
        # v0.6.5 (Matthew's rule): a team that already HAS a supplier
        # — a customer or junior team — cannot offer supply contracts
        # to other teams. It can still receive, end, downgrade or
        # renew its own deal, but it cannot be anyone's supplier.
        if supplier.get("supplier_id"):
            return False, (f"{supplier['name']} already has a supplier — "
                           f"a team that is a customer or junior cannot "
                           f"offer supply contracts to other teams.")
        # v0.7.2 (Matthew's rule, the mirror of the above): a team
        # that already SUPPLIES other teams cannot become a customer
        # or junior — it cannot be anyone's customer. Together the
        # two rules mean a team is either a parent or a child, never
        # both, and there are no supply chains (e.g. Aston cannot
        # propose to supply Mercedes while Mercedes supplies Alpine,
        # McLaren, Audi and Williams).
        supplied = supplied_teams(career, customer)
        if supplied:
            names = ", ".join(t["name"] for t in supplied)
            return False, (f"{customer['name']} already supplies {names} — "
                           f"a team that supplies other teams cannot "
                           f"become a customer or junior team.")
        if kind == "customer":
            if customer.get("sup_stage", 0) != 0:
                return False, f"{customer['name']} already has a supplier."
        else:  # junior — one step up from customer of THIS supplier
            if not (customer.get("sup_stage") == 1
                    and customer.get("supplier_id") == supplier["id"]):
                return False, (f"{customer['name']} must be a customer of "
                               f"{supplier['name']} first — relations go "
                               f"one step at a time.")
            if junior_team_of(career, supplier) is not None:
                return False, (f"{supplier['name']} already has a junior "
                               f"team (one junior max).")
    else:
        rel = relation_between(career, proposer["id"], target["id"])
        if not rel:
            return False, "Those teams have no relation to change."
        supplier, customer = rel
        # v0.6.6: a deal with a pending break notice is already on
        # its way out — no end / downgrade / renew on top of it.
        notice = customer.get("break_notice")
        if notice:
            return False, (f"That deal is already being broken — it "
                           f"ends in {notice.get('rounds_left', 0)} round(s).")
        if kind == "downgrade" and customer.get("sup_stage") != 2:
            return False, "Only a junior team can step down to customer."
        if kind == "renew" and (customer.get("relation_years") or 0) > 0:
            return False, (f"The {customer['name']}–{supplier['name']} deal "
                           f"still has {customer['relation_years']} year(s) "
                           f"left — renewal is for expired deals.")
    proposal = {
        "id": "rp-" + uuid.uuid4().hex[:10],
        "proposer_team_id": proposer["id"],
        "target_team_id": target["id"],
        "kind": kind,
        "supplier_team_id": supplier["id"],
        "customer_team_id": customer["id"],
        "years": years,
    }
    career.setdefault("relation_proposals", []).append(proposal)
    # A bot target decides on the spot; a human target answers in the UI.
    if target.get("is_ai"):
        if bot_accepts_proposal(career, proposal, rng=rng):
            line = _apply_relation_change(career, proposal)
            career["relation_proposals"] = [
                p for p in career.get("relation_proposals") or []
                if p["id"] != proposal["id"]]
            return True, f"{target['name']} accepted the proposal ✓ — {line}"
        career["relation_proposals"] = [
            p for p in career.get("relation_proposals") or []
            if p["id"] != proposal["id"]]
        return True, f"{target['name']} declined the proposal."
    return True, f"Proposal sent to {target['name']} — waiting for their answer."


def respond_relation(career, responder_team_id, proposal_id, accept):
    """v0.6: answer a pending proposal — only the target may answer,
    and only in the winter break. Accepting applies the change
    (re-validated against the current state: a customer who has
    since taken another supplier can no longer accept)."""
    responder = get_team(career, responder_team_id)
    if not responder:
        return False, "No such team."
    if career.get("phase") != "winter_break":
        return False, "Relation changes happen in the winter break."
    proposals = career.get("relation_proposals") or []
    proposal = next((p for p in proposals if p["id"] == proposal_id), None)
    if not proposal:
        return False, "That proposal is gone (it may have expired)."
    if proposal["target_team_id"] != responder["id"]:
        return False, "Only the team the proposal was sent to can answer it."
    career["relation_proposals"] = [p for p in proposals if p["id"] != proposal_id]
    if not accept:
        proposer = get_team(career, proposal["proposer_team_id"])
        return True, (f"Declined the proposal from "
                      f"{proposer['name'] if proposer else 'that team'}.")
    ok, why = _proposal_still_valid(career, proposal)
    if not ok:
        return False, why
    line = _apply_relation_change(career, proposal)
    return True, line + " ✓"


def break_relation(career, breaker_team_id, other_team_id):
    """v0.6.6: break a relation EARLY, unilaterally — the price of
    skipping agreement. Costs $2.0M per remaining contract year,
    paid by the team that breaks it WHEN THE BREAK STARTS; blocked
    when the breaker cannot afford it. The break takes effect after
    a 3-round notice period (winter-break rounds count as rounds —
    see _tick_relation_notices), and the relation stays FULLY active
    during the notice (donations, WANTED, the 15% junior share and
    promotions all still work) — the notice exists so the parent
    can organise its lineups, e.g. test a junior driver in a parent
    main seat, before the tie ends. Free and immediate once the
    contract has expired (0 years left) — that is a lapse, not a
    break. A mutual 'end it' (proposal) or a natural expiry has NO
    notice — just a dashboard message (see _apply_relation_change /
    start_next_season). Either side of the relation may break it (a
    supplier with several customers names which one).
    v0.7.3: team contracts are winter-break-only — the break can
    only START in the winter break. A notice started in the winter
    break may run into the season (the relation stays active during
    the notice, as before). Donations are NOT contracts and stay
    any-time."""
    breaker = get_team(career, breaker_team_id)
    other = get_team(career, other_team_id)
    if not breaker or not other:
        return False, "No such team."
    if career.get("phase") != "winter_break":
        return False, ("Team contracts can only be changed in the "
                       "winter break — including breaking a deal.")
    rel = relation_between(career, breaker["id"], other["id"])
    if not rel:
        return False, "Those teams have no relation to break."
    supplier, customer = rel
    notice = customer.get("break_notice")
    if notice:
        return False, (f"That deal is already being broken — it ends "
                       f"in {notice.get('rounds_left', 0)} round(s).")
    years = customer.get("relation_years") or 0
    fee = round(years * RELATION_BREAK_FEE_PER_YEAR, 2)
    if fee > breaker["money"]:
        return False, (f"Need ${fee:.2f}M to break the deal "
                       f"({years} year(s) left × $2.0M) — you have "
                       f"${breaker['money']:.2f}M.")
    label = relation_label(customer) or "partner"
    if years <= 0:
        # Expired: a lapse, not a break — immediate, no fee, no notice.
        _end_relation(career, customer)
        line = (f"{breaker['name']} ended its expired {label} deal "
                f"with {other['name']} (no fee — the contract had expired)")
        log_transfer(career, line)
        return True, line + " ✓"
    breaker["money"] = round(breaker["money"] - fee, 2)
    customer["break_notice"] = {
        "rounds_left": RELATION_BREAK_NOTICE_ROUNDS,
        "breaker_id": breaker["id"],
    }
    line = (f"{breaker['name']} started breaking its {label} deal with "
            f"{other['name']} (${fee:.2f}M break fee paid — the deal "
            f"ends in {RELATION_BREAK_NOTICE_ROUNDS} rounds)")
    log_transfer(career, line)
    return True, line + " ✓"


def _tick_relation_notices(career):
    """v0.6.6: advance every pending unilateral-break notice by one
    round. A notice that reaches 0 ends the relation (the break
    takes effect). Called once per round (complete_race_weekend) and
    once per winter-break round (resolve_winter_break_round) —
    winter-break rounds count as rounds, like season rounds, so a
    break started in winter-break round 1 ends at the season start,
    and one started in winter-break round 3 ends after season round 2."""
    for team in list(career["teams"]):
        notice = team.get("break_notice")
        if not notice:
            continue
        notice["rounds_left"] = int(notice.get("rounds_left") or 0) - 1
        if notice["rounds_left"] <= 0:
            supplier = supplier_of(career, team)
            label = relation_label(team) or "partner"
            _end_relation(career, team)
            log_transfer(career, 
                f"{team['name']}'s {label} deal with "
                f"{supplier['name'] if supplier else 'its supplier'} ended "
                f"(break notice served)")


def _wanted_target(career, parent, target_team_id=None):
    """v0.6.6: the supplied team a WANTED mark (or its removal) is
    for. A parent supplies customers and at most one junior; the
    mark goes on the named team, or — when no team is named — on
    the parent's only supplied team (the junior when there are
    several, which keeps the old junior-only callers working)."""
    if target_team_id:
        t = get_team(career, target_team_id)
        if t and t.get("supplier_id") == parent["id"]:
            return t
        return None
    supplied = supplied_teams(career, parent)
    if len(supplied) == 1:
        return supplied[0]
    return junior_team_of(career, parent)


def set_wanted(career, parent_team_id, driver_id, reward, target_team_id=None):
    """v0.6: mark a market driver WANTED for a team you supply, with
    a cash reward that team gets if they sign them (main or T&R).
    v0.6.6: WANTED now works for CUSTOMER teams too, with the same
    rules as juniors (Matthew) — the parent picks which supplied
    team the mark is for (target_team_id); at most 3 marks per
    supplied team. Any time; only free agents and F2 drivers; the
    reward is the parent's choice, > 0 and <= the parent's money at
    marking time — the money is NOT held in escrow, it is paid on
    signing (see _pay_wanted_reward). Marking a driver who is
    already marked updates their reward. The reward never changes
    the driver's rep or rating — it is a cash bounty, and the team
    judges the driver on their real stats (Matthew, 2026-11-12)."""
    parent = get_team(career, parent_team_id)
    if not parent:
        return False, "No such team."
    target = _wanted_target(career, parent, target_team_id)
    if target is None:
        if supplied_teams(career, parent):
            return False, ("You supply several teams — pick which team "
                           "the WANTED mark is for.")
        return False, "You don't supply a customer or junior team — WANTED marks are for teams you supply."
    d = get_driver(career, driver_id)
    if not d or d["status"] not in ("free", "f2"):
        return False, "Only free agents and F2 drivers can be marked WANTED."
    try:
        reward = float(reward)
    except (TypeError, ValueError):
        return False, "Reward must be a number."
    if reward <= 0:
        return False, "Reward must be more than $0M."
    if reward > parent["money"]:
        return False, f"Not enough money — you have ${parent['money']:.2f}M."
    wanted = target.setdefault("wanted", [])
    existing = next((w for w in wanted if w.get("driver_id") == d["id"]), None)
    if existing is not None:
        existing["reward"] = round(reward, 2)
        return True, f"WANTED reward for {d['name']} updated to ${reward:.2f}M ✓"
    if len(wanted) >= MAX_WANTED:
        return False, f"At most {MAX_WANTED} WANTED drivers at a time."
    wanted.append({"driver_id": d["id"], "reward": round(reward, 2)})
    log_transfer(career, 
        f"{parent['name']} marked {d['name']} WANTED "
        f"(${reward:.2f}M reward for {target['name']})")
    return True, (f"{d['name']} is WANTED ✓ — {target['name']} gets "
                  f"${reward:.2f}M if they sign them")


def remove_wanted(career, parent_team_id, driver_id, target_team_id=None):
    """v0.6: remove a WANTED mark from a team you supply. Any time.
    v0.6.6: the mark's team is the named supplied team (or the
    parent's only supplied team / junior, as in set_wanted)."""
    parent = get_team(career, parent_team_id)
    if not parent:
        return False, "No such team."
    target = _wanted_target(career, parent, target_team_id)
    if target is None:
        if supplied_teams(career, parent):
            return False, ("You supply several teams — pick which team "
                           "the WANTED mark is for.")
        return False, "You don't supply a customer or junior team."
    wanted = target.get("wanted") or []
    if not any(w.get("driver_id") == driver_id for w in wanted):
        return False, "That driver is not marked WANTED."
    target["wanted"] = [w for w in wanted if w.get("driver_id") != driver_id]
    d = get_driver(career, driver_id)
    return True, f"Removed the WANTED mark on {d['name'] if d else 'that driver'} ✓"


def _pay_wanted_reward(career, team, d):
    """v0.6: if the team just signed a driver their parent marked
    WANTED, the parent pays the reward to the team and the mark is
    consumed. v0.6.6: works for CUSTOMER teams too, same rules —
    only the supplied team of the marking parent is paid; any other
    team signing the driver leaves the mark in place (the spec).
    The money was NOT held in escrow at marking time, so if the
    parent's bank has since dropped below the reward, the team gets
    whatever the parent still has and the mark is still consumed
    (the parent's money never goes below zero)."""
    if not team.get("sup_stage"):
        return
    parent = supplier_of(career, team)
    if not parent:
        return
    wanted = team.get("wanted") or []
    mark = next((w for w in wanted if w.get("driver_id") == d["id"]), None)
    if not mark:
        return
    reward = float(mark.get("reward") or 0.0)
    paid = min(reward, max(0.0, parent["money"]))
    parent["money"] = round(parent["money"] - paid, 2)
    team["money"] = round(team["money"] + paid, 2)
    team["wanted"] = [w for w in wanted if w.get("driver_id") != d["id"]]
    line = (f"{parent['name']} paid ${paid:.2f}M WANTED reward to "
            f"{team['name']} for signing {d['name']}")
    if paid < reward:
        line += (f" (short of the ${reward:.2f}M reward — "
                 f"the parent's bank was low)")
    log_transfer(career, line)


def _wanted_pick(career, team, candidates):
    """v0.6: a supplied team's WANTED drivers get preference when a
    bot picks a signing — a WANTED driver within 1.0 rep of the best
    candidate wins the pick. v0.6.6: works for CUSTOMER teams too,
    same rules. A preference, not a hard rule: the reward is a cash
    bounty (it never changes the driver's rep or rating), so the bot
    still judges the driver on their real stats and only prefers a
    WANTED driver when they are a reasonable fit (Matthew,
    2026-11-12)."""
    if not team.get("sup_stage") or not candidates:
        return None
    wanted_ids = {w.get("driver_id") for w in (team.get("wanted") or [])}
    wanted = [d for d in candidates if d["id"] in wanted_ids]
    if not wanted:
        return None
    best_rep = max(d["rep"] for d in candidates)
    fits = [d for d in wanted if d["rep"] >= best_rep - 1.0]
    if not fits:
        return None
    fits.sort(key=lambda d: (-d["rep"], d["salary"], d["name"]))
    return fits[0]


def _is_threat(career, supplier, customer):
    """v0.7.3: a customer is 'becoming a threat' to its supplier
    when it is within 40 constructors' points of the supplier, OR
    within 1 constructors' position, OR in the frontrunner band
    (top 4). Standings-visible info only — never car perf (players
    can't see it unless the chart is on)."""
    cus_pos = constructor_pos(career, customer["id"])
    sup_pos = constructor_pos(career, supplier["id"])
    if cus_pos <= 4:
        return True
    if abs(cus_pos - sup_pos) <= 1:
        return True
    if abs(customer["points"] - supplier["points"]) <= 40:
        return True
    return False


def _junior_customer_factor(career, customer):
    """v0.8.1: how willing a customer is to become a junior team —
    from their constructors' position and recent trend (standings-
    visible only, never car perf). A top-4 customer basically never
    becomes a junior (x0.05); the factor rises the further back they
    finish (x0.2 at P5 up to x1.0 at P9+). A customer climbing 2+
    places over the last 4 races is even less likely (they are on the
    way up). Used by the bot's acceptance and by the supplier's
    offer gate, so the same rule decides both ends."""
    pos = constructor_pos(career, customer["id"])
    factor = max(0.05, min(1.0, (pos - 4) * 0.2))
    series = _season_positions_by_round(career)
    if len(series) >= 2:
        back = series[-4] if len(series) >= 4 else series[0]
        climb = back.get(customer["id"], series[-1][customer["id"]]) \
            - series[-1][customer["id"]]
        if climb >= 2:
            factor *= 1.0 / (1.0 + 0.25 * climb)
    return factor


def bot_accepts_proposal(career, proposal, rng=None):
    """v0.6: a bot target's decision on a pending relation proposal.
    The spec's bot rule, with the roles made explicit (see
    propose_relation): for a formation the proposer is the would-be
    supplier and the target is the would-be customer/junior, and the
    target decides as whichever side it would be.
    v0.7.3: a formation decision is a PROBABILITY built from
    standings-visible info only — never car perf (players can't see
    it unless the chart is on), and never a hard threshold:
    - as the customer-to-be: less likely to accept being supplied
      when you are BEATING the supplier in the constructors'
      (the further ahead, the less likely), when the supplier is
      not a frontrunner (top 4 — the further back, the less
      likely), or when the supplier has a bad team reputation
      (proposal_rep_factor: 1.0 at rep 25, 0.4 at rep 0). A poor
      customer-to-be (under $5M) is more likely to accept the help.
    - as the supplier: less likely to accept supplying when the
      customer is becoming a threat (see _is_threat) or has a bad
      team reputation. A poor customer is more likely to be taken
      on (they need the help).
    - end / downgrade / renew keep the old rule (expired, or the
      other side much weaker)."""
    target = get_team(career, proposal["target_team_id"])
    supplier = get_team(career, proposal["supplier_team_id"])
    customer = get_team(career, proposal["customer_team_id"])
    if not target or not supplier or not customer:
        return False
    kind = proposal["kind"]
    poor = lambda t: t["money"] < BOT_POOR_MONEY
    much_weaker = lambda other, me: other["perf"] < me["perf"] - BOT_PERF_WINDOW
    rng = rng or random
    if kind in ("customer", "junior"):
        if kind == "junior" and junior_team_of(career, supplier) is not None:
            return False
        if target["id"] == customer["id"]:
            # Deciding as the customer-to-be.
            cus_pos = constructor_pos(career, customer["id"])
            sup_pos = constructor_pos(career, supplier["id"])
            p = 0.75
            if cus_pos < sup_pos:
                # Beating the supplier: the further ahead, the less
                # likely (a 1-place lead halves the chance, a
                # 6-place lead cuts it to a quarter).
                p *= 1.0 / (1.0 + 0.5 * (sup_pos - cus_pos))
            if sup_pos > 4:
                # Supplier not a frontrunner (top 4): the further
                # back they sit, the less attractive the deal.
                p *= 1.0 / (1.0 + 0.25 * (sup_pos - 4))
            p *= proposal_rep_factor(team_rep_of(supplier))
            if poor(target):
                p = min(0.95, p * 1.25)
            if kind == "junior":
                # v0.8.1: a frontrunner customer basically never
                # becomes a junior team — the further back they
                # finish (and the worse their trend), the more
                # likely they accept. Applied last so it dominates
                # the poverty boost too.
                p *= _junior_customer_factor(career, customer)
        else:
            # Deciding as the supplier.
            p = 0.75
            if _is_threat(career, supplier, customer):
                p *= 0.35
            p *= proposal_rep_factor(team_rep_of(customer))
            if poor(customer):
                p = min(0.95, p * 1.25)
        return rng.random() < p
    if kind in ("end", "downgrade"):
        expired = (customer.get("relation_years") or 0) <= 0
        other = supplier if target["id"] == customer["id"] else customer
        return expired or much_weaker(other, target)
    if kind == "renew":
        within = lambda a, b: abs(a["perf"] - b["perf"]) <= BOT_PERF_WINDOW
        if target["id"] == customer["id"]:
            return within(supplier, target) or poor(target)
        return within(customer, supplier)
    return False


def bot_spontaneous_donations(career, rng=None):
    """v0.7.3: a bot parent may SPONTANEOUSLY donate to a supplied
    team that is struggling (bottom half of the constructors') and
    has a good reputation — no request needed. A small per-round
    probability; the amount scales with the customer's need and the
    parent's surplus (the parent keeps at least $5M). Reputation
    tiers for the CUSTOMER: low rep (under 30) → the parent will
    not willingly donate (probability ~0); mid rep (30-69) → low
    chance; high rep (70+) → low chance plus a slight donation-size
    boost on top of the amount the need/surplus system computes.
    Returns the list of (parent, customer, amount) donations made.
    Called once per race weekend, after bot_post_race — it does NOT
    replace the existing ai_act poverty donation (a rich parent
    giving $1-2M to its poorest supplied team under $5M)."""
    rng = rng or random
    n = len(career["teams"])
    if n < 2:
        return []
    made = []
    for parent in career["teams"]:
        if not parent.get("is_ai"):
            continue
        supplied = supplied_teams(career, parent)
        if not supplied:
            continue
        surplus = parent["money"] - BOT_POOR_MONEY
        if surplus <= 0:
            continue
        for customer in supplied:
            pos = constructor_pos(career, customer["id"])
            if pos <= n // 2:
                continue  # only a struggling customer (bottom half)
            rep = team_rep_of(customer)
            if rep < 30:
                continue  # low rep: the parent will not willingly donate
            # Need: how far below $5M the customer sits (0..1).
            need = max(0.0, min(1.0, (BOT_POOR_MONEY - customer["money"])
                                 / BOT_POOR_MONEY))
            if need <= 0:
                continue
            # Small per-round chance, higher when the customer is
            # needier and better reputed (mid rep ~0.06, high rep
            # ~0.12 at full need).
            rep_factor = 0.5 if rep < 70 else 1.0
            p = 0.12 * need * rep_factor
            if rng.random() >= p:
                continue
            amount = round(min(3.0, surplus * 0.05) * need, 2)
            if rep >= 70:
                amount = round(amount * 1.1, 2)  # high rep: slight boost
            amount = min(amount, surplus)
            if amount < 0.5:
                continue
            ok, _why = donate(career, parent["id"], customer["id"], amount)
            if ok:
                made.append((parent["id"], customer["id"], amount))
    return made


def bot_winter_relations(career, team, rng=None):
    """v0.6 bot parity for relationships, run for AI teams when a
    winter-break round opens (start_winter_break_round). Round 1
    only — proposals are made once per break, not every round:
    - renew a due relation the bot wants to keep (one per break);
    - a poor bot with no supplier asks the fastest team to supply it;
    - a bot supplier upgrades a close customer to junior when it has
      no junior yet;
    - a bot parent marks its best available market driver WANTED
      ($1.0M) for its junior team — or for a customer when it has no
      junior (v0.6.6: WANTED works for customers too, same rules).
    Proposals to a bot target are decided on the spot (see
    propose_relation); proposals to a human stay pending for the
    human to answer. (v0.6.6: the research cap is gone entirely —
    bot parents no longer set one.)"""
    rng = rng or random
    if career.get("wb_round") != 1:
        return
    # Renewals — the first due relation this team is in, if the bot
    # wants to keep it (partner within 60 perf, or the customer is poor).
    rels = []
    if team.get("sup_stage", 0) > 0:
        supplier = supplier_of(career, team)
        if supplier:
            rels.append((supplier, team))
    for supplied in supplied_teams(career, team):
        rels.append((team, supplied))
    for supplier, customer in rels:
        if (customer.get("relation_years") or 0) <= 0:
            keep = (abs(supplier["perf"] - customer["perf"]) <= BOT_PERF_WINDOW
                    or customer["money"] < BOT_POOR_MONEY)
            if keep:
                other = supplier if team["id"] == customer["id"] else customer
                propose_relation(career, team["id"], other["id"], "renew",
                                 supplier_team_id=supplier["id"], years=2,
                                 rng=rng)
                break
    # A poor bot with no supplier asks the fastest team to supply it.
    if team.get("sup_stage", 0) == 0 and team["money"] < BOT_POOR_MONEY:
        others = [t for t in career["teams"] if t["id"] != team["id"]]
        if others:
            best = max(others, key=lambda t: (t["perf"], t["name"]))
            propose_relation(career, team["id"], best["id"], "customer",
                             supplier_team_id=best["id"], years=2, rng=rng)
    # A bot supplier upgrades a customer to junior (one junior max).
    # v0.8.1: a top-4 customer is never offered junior status, and a
    # customer climbing 2+ places over the last 4 races is skipped
    # too — the offer weighs the customer's constructors' position
    # and trend, not just how close their car is. Among the eligible
    # customers the pick is a weighted random draw (worse position =
    # more likely), so different careers promote different teams
    # instead of always the same one (McLaren to Mercedes, Haas to
    # Ferrari).
    if junior_team_of(career, team) is None:
        series = _season_positions_by_round(career)
        eligible = []
        for t in supplied_teams(career, team):
            if t.get("sup_stage") != 1:
                continue
            if abs(t["perf"] - team["perf"]) > BOT_PERF_WINDOW:
                continue
            if constructor_pos(career, t["id"]) < 5:
                continue
            if len(series) >= 2:
                back = series[-4] if len(series) >= 4 else series[0]
                climb = back.get(t["id"], series[-1][t["id"]]) \
                    - series[-1][t["id"]]
                if climb >= 2:
                    continue
            eligible.append(t)
        if eligible:
            weights = [constructor_pos(career, t["id"]) - 4
                       for t in eligible]
            total = sum(weights)
            pick = eligible[-1]
            roll = rng.random() * total
            acc = 0.0
            for t, w in zip(eligible, weights):
                acc += w
                if roll < acc:
                    pick = t
                    break
            propose_relation(career, team["id"], pick["id"], "junior",
                             supplier_team_id=team["id"], years=2, rng=rng)
    # Parent duties: a WANTED mark for the junior team — or for a
    # customer when there is no junior (v0.6.6: WANTED works for
    # customers too, same rules).
    junior = junior_team_of(career, team)
    mark_target = junior
    if mark_target is None:
        customers = [t for t in supplied_teams(career, team)
                     if t.get("sup_stage") == 1]
        if customers:
            customers.sort(key=lambda t: (-t["perf"], t["name"]))
            mark_target = customers[0]
    if mark_target is not None:
        wanted = mark_target.get("wanted") or []
        if len(wanted) < MAX_WANTED and team["money"] >= 1.0:
            marked = {w.get("driver_id") for w in wanted}
            pool = [d for d in career["drivers"]
                    if d["status"] in ("free", "f2") and d["id"] not in marked]
            if pool:
                pool.sort(key=lambda d: (-d["rep"], d["salary"], d["name"]))
                set_wanted(career, team["id"], pool[0]["id"], 1.0,
                           mark_target["id"])


def add_academy_driver(career, team_id, driver_id):
    """v0.4.2: add an unsigned driver (free agent or F2, aged 25 or
    under) to a team's Driver Academy. There is NO hard cap on
    academy size — the first 3 are free, and every driver beyond
    that costs an exponential fine per race (see
    academy_fine_per_race). The team pays 1/3 of the driver's
    salary per season (salary/36 after each race). The driver keeps
    their status (they still race in F2 / sit in the pool); the
    academy is an affiliation on top. Allowed between races and
    during the winter break. A driver already in an academy (any
    team's) cannot be added to another."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    d = get_driver(career, driver_id)
    if not d or d["status"] not in ("free", "f2"):
        return False, "Only unsigned drivers (free agents or F2) can join an academy."
    if d.get("academy_team_id"):
        other = get_team(career, d["academy_team_id"])
        return False, (f"{d['name']} is already in "
                       f"{other['name'] if other else 'an'}'s academy.")
    if d["age"] > ACADEMY_MAX_JOIN_AGE:
        return False, (f"Academy drivers must be {ACADEMY_MAX_JOIN_AGE} or under "
                       f"to join — {d['name']} is {d['age']}.")
    d["academy_team_id"] = team["id"]
    cost = round(d["salary"] / 3.0, 2)
    fine = academy_fine_per_race(career, team)
    note = ""
    if fine > 0:
        n = len(team_academy_drivers(career, team))
        note = (f" — this is academy driver #{n}, so the team now pays "
                f"a ${fine:.2f}M/race academy fine on top.")
    log_transfer(career, 
        f"{d['name']} joined {team['name']}'s Driver Academy")
    return True, (f"{d['name']} joined {team['name']}'s Driver Academy ✓ "
                  f"(costs ${cost:.2f}M/season — 1/3 of salary){note}")


def release_academy_driver(career, team_id, driver_id):
    """v0.4.2: release a driver from a team's Driver Academy — free,
    no fee. The driver keeps their status (F2 / free agent) and can
    be signed by anyone afterwards, academy-free."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    d = get_driver(career, driver_id)
    if not d or d.get("academy_team_id") != team["id"]:
        return False, "That driver is not in your academy."
    d["academy_team_id"] = None
    log_transfer(career, 
        f"{d['name']} left {team['name']}'s Driver Academy")
    return True, f"{d['name']} released from {team['name']}'s academy ✓"


def release_driver(career, team_id, driver_id):
    """v0.4.3: release (drop) a main or T&R driver — breaking their
    contract costs EXACTLY what breaking a main driver's contract
    costs (break_fee_for_driver: rounds_remaining x salary/12 +
    $2.0M, where rounds_remaining = rounds left this season +
    (years left after this season x 12)). The fee is shown upfront
    in the Drivers tab and charged on confirm. The driver becomes a
    free agent and KEEPS their championship points/wins/best finish;
    the points they scored FOR this team stay with the team (banked
    into points_kept — the same real-F1 rule as a replacement, see
    _release_driver). A main driver's seat becomes empty; a T&R
    driver just leaves the roster. Logged in Transfers. v0.6.5:
    allowed in the winter break too (Matthew: teams could not drop
    a driver there at all) — the fee uses the winter-break
    rounds_remaining (contract_left x 12 rounds, the full remaining
    contract), shown upfront the same way. An emptied main seat can
    then be filled with winter-break offers, or auto-fills at season
    start like any other empty seat."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    if career.get("phase") not in ("round", "winter_break"):
        return False, "You can only release drivers between races."
    d = get_driver(career, driver_id)
    if not d or d.get("team_id") != team["id"] \
            or d.get("status") not in ("f1", "tr"):
        return False, "That driver is not one of your main or T&R drivers."
    # v0.7.1: an ill driver cannot be released while they recover —
    # their seat is being kept for them (their substitute covers it).
    if d.get("ill_until_round"):
        return False, (f"{d['name']} is {d.get('ill_label') or 'ill'} and "
                       f"cannot be released while they recover.")
    fee = break_fee_for_driver(career, d)
    if fee > team["money"]:
        return False, f"Need ${fee:.2f}M to release {d['name']}."
    was_main = d["status"] == "f1"
    team["money"] -= fee
    if was_main:
        if team["driver1_id"] == d["id"]:
            team["driver1_id"] = None
        if team["driver2_id"] == d["id"]:
            team["driver2_id"] = None
    _release_driver(career, d, team)
    d["status"] = "free"
    _recompute_team_points(career, team)
    log_transfer(career, 
        f"{d['name']} released by {team['name']} (${fee:.2f}M break fee)")
    return True, f"{d['name']} released ✓ — ${fee:.2f}M break fee"


def swap_main_tr(career, team_id, seat, tr_driver_id):
    """v0.4: swap a main driver with a T&R driver — free, instant,
    between races (not during a race weekend). The two simply swap
    roles: the main driver becomes T&R (keeping their contract and
    championship points), the T&R driver takes the main seat.
    Points rule (same as v0.2.1): the outgoing main driver's
    team_points are banked into the team's points_kept; the incoming
    driver starts with team_points 0 for this team; championship
    points/wins/best finish always follow the driver."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    if career.get("phase") not in ("round", "winter_break"):
        return False, "You can only swap drivers between races."
    if seat not in (1, 2):
        return False, "Seat must be 1 or 2."
    slot = "driver1_id" if seat == 1 else "driver2_id"
    main = get_driver(career, team[slot]) if team[slot] else None
    if not main:
        return False, f"Seat {seat} is empty — promote a T&R driver instead."
    tr = get_driver(career, tr_driver_id)
    if not tr or tr.get("status") != "tr" or tr.get("team_id") != team["id"]:
        return False, "That driver is not one of your T&R drivers."
    # v0.7.1: an ill driver sitting in T&R cannot be swapped into a
    # race seat — they are recovering, not available to race.
    if tr.get("ill_until_round"):
        return False, (f"{tr['name']} is {tr.get('ill_label') or 'ill'} "
                       f"and cannot race until Round {tr['ill_until_round'] + 1}.")
    # v0.7.4: a T&R driver still racing in F2 (f2_tr) can only take
    # a main seat once their F2 season is over (the winter break) —
    # EXCEPT as an illness substitute: standing in for an ill driver
    # is the one in-season F1 race they may do. Here the seat is
    # occupied, so the swap is a stand-in only when the outgoing
    # main driver is themselves standing in for an ill driver
    # (same rule as promote_tr_to_main, v0.7.3).
    is_ill_standin = bool(main.get("ill_sub_for"))
    if tr.get("f2_tr") and career.get("phase") != "winter_break" \
            and not is_ill_standin:
        return False, (f"{tr['name']} is still racing in F2 — they can "
                       f"only take a main seat once the F2 season ends "
                       f"(the winter break), unless they are standing in "
                       f"for an ill driver.")
    if tr.get("f2_tr") and career.get("phase") == "winter_break":
        # Leaving F2 for the main seat: the F2 tally must not carry
        # into F1 (same reset as promote_tr_to_main).
        tr["f2_tr"] = False
        tr["points"] = 0
        tr["season_wins"] = 0
        tr["season_podiums"] = 0
        tr["best_pos"] = None
        tr["best_finish_round"] = None
        tr["finish_sum"] = 0
        tr["finish_count"] = 0
        tr["f1_points"] = 0
        tr["f1_wins"] = 0
        tr["f1_podiums"] = 0
        tr["f1_best_pos"] = None
        tr["f1_best_finish_round"] = None
    # Outgoing main driver: bank their per-team points (real F1),
    # keep their contract and championship tally, become T&R.
    team["points_kept"] = team.get("points_kept", 0) + main.get("team_points", 0)
    main["team_points"] = 0
    main["last_team_id"] = None
    main["status"] = "tr"
    # Incoming T&R driver: take the seat, fresh per-team tally.
    tr["team_points"] = 0
    tr["last_team_id"] = None
    if not tr.get("f2_tr"):
        # A normal T&R driver becomes a main driver. An illness
        # stand-in still racing F2 stays a T&R driver (status "tr",
        # f2_tr) and only holds the seat while the ill driver is
        # out — restore_ill_driver sends them back (same pattern as
        # promote_tr_to_main).
        tr["status"] = "f1"
    team[slot] = tr["id"]
    # v0.7.1: if the outgoing main driver was a substitute standing
    # in for an ill driver, the stand-in role passes to the incoming
    # driver — the ill driver still needs someone in their seat.
    sub_for = main.get("ill_sub_for")
    sub_from = main.get("ill_sub_from")
    if sub_for:
        main["ill_sub_for"] = None
        main["ill_sub_from"] = None
        tr["ill_sub_for"] = sub_for
        tr["ill_sub_from"] = sub_from
        ill = get_driver(career, sub_for)
        if ill:
            ill["ill_sub_id"] = tr["id"]
    _recompute_team_points(career, team)
    log_transfer(career, 
        f"{tr['name']} swapped with {main['name']} at {team['name']}")
    if tr.get("f2_tr"):
        # An illness stand-in still racing F2: they hold the seat
        # only while the ill driver is out.
        return True, (f"{tr['name']} swapped with {main['name']} ✓ "
                      f"({tr['name']} is standing in for Seat {seat}, "
                      f"{main['name']} is Test & Reserve)")
    return True, (f"{tr['name']} swapped with {main['name']} ✓ "
                  f"({tr['name']} is now Seat {seat}, {main['name']} is Test & Reserve)")


def promote_tr_to_main(career, team_id, tr_driver_id, seat):
    """v0.4: promote one of the team's own T&R drivers into an empty
    main seat — free, keeps their contract. Used by the winter-break
    auto-fill (and by a manager with an empty seat mid-season)."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    if seat not in (1, 2):
        return False, "Seat must be 1 or 2."
    slot = "driver1_id" if seat == 1 else "driver2_id"
    if team[slot]:
        return False, f"Seat {seat} is not empty."
    tr = get_driver(career, tr_driver_id)
    if not tr or tr.get("status") != "tr" or tr.get("team_id") != team["id"]:
        return False, "That driver is not one of your T&R drivers."
    # v0.7.1: an ill driver sitting in T&R cannot be promoted into
    # a race seat — they are recovering, not available to race.
    if tr.get("ill_until_round"):
        return False, (f"{tr['name']} is {tr.get('ill_label') or 'ill'} "
                       f"and cannot race until Round {tr['ill_until_round'] + 1}.")
    # v0.7.3: a T&R driver still racing in F2 (f2_tr) can only take
    # a main seat once their F2 season is over (the winter break) —
    # EXCEPT as an illness substitute: standing in for an ill driver
    # is the one in-season F1 race they may do. The seat is empty
    # here (checked above), so any ill driver on this team who owns
    # this seat makes the promotion a stand-in — even when their
    # recorded substitute is stale (released or poached away).
    is_ill_standin = any(
        d.get("team_id") == team["id"] and d.get("ill_until_round")
        and d.get("ill_seat") == seat
        for d in career["drivers"])
    if tr.get("f2_tr") and career.get("phase") != "winter_break" \
            and not is_ill_standin:
        return False, (f"{tr['name']} is still racing in F2 — they can "
                       f"only take a main seat once the F2 season ends "
                       f"(the winter break), unless they are standing in "
                       f"for an ill driver.")
    if tr.get("f2_tr") and career.get("phase") == "winter_break":
        # Leaving F2 for the main seat: the F2 tally must not carry
        # into F1 (it was already reset at the rollover, but clear
        # it here so the promotion is safe on its own).
        tr["f2_tr"] = False
        tr["points"] = 0
        tr["season_wins"] = 0
        tr["season_podiums"] = 0
        tr["best_pos"] = None
        tr["best_finish_round"] = None
        tr["finish_sum"] = 0
        tr["finish_count"] = 0
        tr["f1_points"] = 0
        tr["f1_wins"] = 0
        tr["f1_podiums"] = 0
        tr["f1_best_pos"] = None
        tr["f1_best_finish_round"] = None
    tr["team_points"] = 0
    tr["last_team_id"] = None
    if not tr.get("f2_tr"):
        # A normal T&R driver becomes a main driver. An illness
        # stand-in still racing F2 stays a T&R driver (status "tr",
        # f2_tr) and only holds the seat while the ill driver is
        # out — restore_ill_driver sends them back. Their F1
        # results go in the f1_* fields.
        tr["status"] = "f1"
    team[slot] = tr["id"]
    # v0.7.1: if this empty seat belongs to an ill driver whose
    # substitute is not yet recorded (an empty ill seat — e.g. their
    # substitute was released), the promoted driver becomes the
    # stand-in, so the ill driver's return still finds them.
    for d in career["drivers"]:
        if (d.get("team_id") == team["id"] and d.get("ill_until_round")
                and d.get("ill_seat") == seat and not d.get("ill_sub_id")):
            d["ill_sub_id"] = tr["id"]
            tr["ill_sub_for"] = d["id"]
            tr["ill_sub_from"] = "tr"
            break
    _recompute_team_points(career, team)
    log_transfer(career, 
        f"{tr['name']} promoted to a race seat at {team['name']}")
    return True, f"{tr['name']} promoted to Seat {seat} at {team['name']} ✓"


def _driver_slot(career, team, d):
    """The slot a driver holds on a team: ("seat", 1 | 2) for a main
    driver, ("tr", None) for a Test & Reserve driver, ("academy",
    None) for an academy junior — or None when the driver holds no
    slot on that team."""
    if d.get("team_id") == team["id"]:
        if d.get("status") == "f1":
            if team.get("driver1_id") == d["id"]:
                return ("seat", 1)
            if team.get("driver2_id") == d["id"]:
                return ("seat", 2)
        elif d.get("status") == "tr":
            return ("tr", None)
    if d.get("academy_team_id") == team["id"] and d.get("status") in ("free", "f2"):
        return ("academy", None)
    return None


def _detach_driver(career, d, team, slot):
    """Take a driver out of their slot on a team, without putting
    them anywhere yet. A driver leaving a main seat banks the points
    they scored FOR this team (real F1, like _release_driver) and
    their last team is recorded; a T&R driver just leaves the
    roster; an academy junior leaves the academy. Championship
    points/wins/best finish always follow the driver."""
    if slot is None:
        return
    kind = slot[0]
    if kind == "seat":
        team["points_kept"] = team.get("points_kept", 0) + d.get("team_points", 0)
        d["team_points"] = 0
        d["last_team_id"] = team["id"]
        # v0.7.1: record the slot they held, for the winter-break
        # re-sign offer.
        d["last_slot"] = "seat1" if team.get("driver1_id") == d["id"] else "seat2"
        if team.get("driver1_id") == d["id"]:
            team["driver1_id"] = None
        if team.get("driver2_id") == d["id"]:
            team["driver2_id"] = None
    elif kind == "tr":
        d["team_points"] = 0
        d["last_team_id"] = team["id"]
        # v0.7.1: record the slot they held, for the winter-break
        # re-sign offer.
        d["last_slot"] = "tr"
    elif kind == "academy":
        d["academy_team_id"] = None
    d["team_id"] = None


def _attach_driver(career, d, team, slot):
    """Put a driver into a slot on a team: ("seat", 1 | 2), ("tr",
    None) or ("academy", None). A driver entering a main seat or
    T&R starts with a fresh per-team tally; an academy junior
    promoted to a race seat signs a 2-year contract (the default
    signing length) on their existing salary. Signing a main or T&R
    contract ends the academy affiliation, wherever it was."""
    kind = slot[0]
    if kind in ("seat", "tr"):
        # v0.7.3: an F2 driver joining as T&R keeps racing F2
        # (f2_tr — their F2 tally stays in the plain fields); an F2
        # driver joining a MAIN seat leaves F2 (the F2 tally is
        # cleared and must not carry into F1). Callers must refuse
        # a mid-season main-seat move first (see
        # promote_junior_driver) — this only handles the state.
        if d.get("status") == "f2" or d.get("f2_tr"):
            if kind == "tr":
                d["f2_tr"] = True
                d.setdefault("f1_points", 0)
                d.setdefault("f1_wins", 0)
                d.setdefault("f1_podiums", 0)
                d.setdefault("f1_best_pos", None)
                d.setdefault("f1_best_finish_round", None)
            else:
                d["f2_tr"] = False
                d["points"] = 0
                d["season_wins"] = 0
                d["season_podiums"] = 0
                d["best_pos"] = None
                d["best_finish_round"] = None
                d["finish_sum"] = 0
                d["finish_count"] = 0
                d["f1_points"] = 0
                d["f1_wins"] = 0
                d["f1_podiums"] = 0
                d["f1_best_pos"] = None
                d["f1_best_finish_round"] = None
    if kind == "seat":
        team["driver1_id" if slot[1] == 1 else "driver2_id"] = d["id"]
        d["status"] = "f1"
        d["team_id"] = team["id"]
        d["team_points"] = 0
        d["last_team_id"] = None
        # v0.7.1: a fresh slot, so the slot they held on their old
        # team no longer applies.
        d["last_slot"] = None
        d["academy_team_id"] = None
        if not d.get("contract_left"):
            d["contract_left"] = 2
    elif kind == "tr":
        d["status"] = "tr"
        d["team_id"] = team["id"]
        d["team_points"] = 0
        d["last_team_id"] = None
        # v0.7.1: a fresh slot, so the slot they held on their old
        # team no longer applies.
        d["last_slot"] = None
        d["academy_team_id"] = None
        if not d.get("contract_left"):
            d["contract_left"] = 2
    elif kind == "academy":
        d["status"] = "free"
        d["team_id"] = None
        d["last_team_id"] = team["id"]
        d["academy_team_id"] = team["id"]
    # v0.8.0: a seat/T&R move is a team change (junior promotions
    # move a driver between the parent and the junior team) — the
    # happiness/loyalty relation with the new team resets or
    # averages, like a signing. Academy moves are affiliations, not
    # contracts, so they do not touch the relation.
    if kind in ("seat", "tr"):
        apply_signing_relation(career, d, team["id"])


def promote_junior_driver(career, parent_team_id, junior_driver_id,
                          target, swap_driver_id=None):
    """v0.6.6: the parent team takes a driver from its junior team
    into a main seat or T&R, any time (season or winter break),
    with NO contract break fee. Two modes:
    - STRAIGHT: the target main seat is empty (or the target is T&R,
      which has no seat limit) — the junior driver moves straight in
      and their old slot on the junior team empties.
    - SWAP: the target main seat is occupied — the occupant (or the
      parent driver named by swap_driver_id, a main or T&R driver)
      goes to the junior team and takes the junior driver's slot.
    The junior driver may be a main driver, a T&R driver, or an
    academy junior of the junior team — the parent effectively has
    four main seats instead of two. Contracts move with the driver;
    a driver leaving a main seat banks their per-team points with
    the old team (real F1, like swap_main_tr); a driver entering a
    main seat starts at 0 for the new team. target is ("seat", 1|2)
    or ("tr", None)."""
    parent = get_team(career, parent_team_id)
    if not parent:
        return False, "No such team."
    junior = junior_team_of(career, parent)
    if junior is None:
        return False, "You don't supply a junior team — promotions are for your junior team's drivers."
    if career.get("phase") not in ("round", "winter_break"):
        return False, "You can only promote drivers between races."
    if not (isinstance(target, (tuple, list)) and len(target) == 2):
        return False, "Pick a main seat or T&R for the driver."
    kind, seat = target[0], target[1]
    if kind == "seat":
        if seat not in (1, 2):
            return False, "Seat must be 1 or 2."
    elif kind != "tr":
        return False, "Target must be a main seat or T&R."
    d = get_driver(career, junior_driver_id)
    if not d:
        return False, "No such driver."
    # v0.7.1: an ill driver is recovering, not available to be
    # promoted — their seat is being kept for them at their own team.
    if d.get("ill_until_round"):
        return False, (f"{d['name']} is {d.get('ill_label') or 'ill'} and "
                       f"cannot be promoted while they recover.")
    junior_slot = _driver_slot(career, junior, d)
    if junior_slot is None:
        return False, "That driver is not one of your junior team's drivers."
    # The swap partner (only when the target seat is occupied).
    swap = None
    if kind == "seat":
        slot = "driver1_id" if seat == 1 else "driver2_id"
        occupant = get_driver(career, parent[slot]) if parent[slot] else None
        if occupant is not None:
            if swap_driver_id:
                swap = get_driver(career, swap_driver_id)
                if (not swap or swap.get("team_id") != parent["id"]
                        or swap.get("status") not in ("f1", "tr")):
                    return False, "The swap driver must be one of your main or T&R drivers."
                if swap["id"] == d["id"]:
                    return False, "A driver cannot swap with themself."
            else:
                swap = occupant
    elif swap_driver_id:
        return False, "T&R has no seat limit — no swap is needed to move a driver there."
    # v0.7.3: a junior driver still racing in F2 (status "f2", or a
    # T&R driver still racing F2 — f2_tr) can only take a main seat
    # once their F2 season is over (the winter break); mid-season
    # they may only move to T&R. The same applies to the swap
    # partner going the other way into the junior team's main seat.
    if kind == "seat" and (d.get("status") == "f2" or d.get("f2_tr")) \
            and career.get("phase") != "winter_break":
        return False, (f"{d['name']} is still racing in F2 — they can "
                       f"only join {parent['name']} as Test & Reserve "
                       f"until the F2 season ends (main seats open in "
                       f"the winter break).")
    if (swap is not None and junior_slot[0] == "seat"
            and (swap.get("status") == "f2" or swap.get("f2_tr"))
            and career.get("phase") != "winter_break"):
        return False, (f"{swap['name']} is still racing in F2 — they "
                       f"cannot take a main seat at {junior['name']} "
                       f"until the F2 season ends.")
    # Move the junior driver out of their old slot.
    _detach_driver(career, d, junior, junior_slot)
    # The swap partner takes the junior driver's old slot.
    if swap is not None:
        swap_slot = _driver_slot(career, parent, swap)
        _detach_driver(career, swap, parent, swap_slot)
        _attach_driver(career, swap, junior, junior_slot)
    # The junior driver takes the target slot on the parent team.
    _attach_driver(career, d, parent,
                   ("seat", seat) if kind == "seat" else ("tr", None))
    _recompute_team_points(career, parent)
    _recompute_team_points(career, junior)
    target_label = f"Seat {seat}" if kind == "seat" else "Test & Reserve"
    if swap is not None:
        log_transfer(career, 
            f"{d['name']} swapped with {swap['name']} — "
            f"{d['name']} to {parent['name']} ({target_label}), "
            f"{swap['name']} to {junior['name']}")
        return True, (f"{d['name']} swapped with {swap['name']} ✓ "
                      f"({d['name']} is now {target_label} at "
                      f"{parent['name']})")
    log_transfer(career, 
        f"{d['name']} promoted from {junior['name']} to "
        f"{parent['name']} ({target_label})")
    return True, (f"{d['name']} promoted to {target_label} at "
                  f"{parent['name']} ✓")


def _recompute_team_points(career, team):
    """Team points: the points its current drivers scored FOR this
    team (team_points), plus points banked from departed drivers who
    scored for this team. A driver's championship total never counts
    here — it may include points scored for another team earlier in
    the season. The team's best finish is NOT recomputed here: it is
    the team's season record, tracked at race time (race.py) — a
    driver's best finish may have been scored for another team
    earlier in the season, and it must not inflate this team's
    record."""
    pts = team.get("points_kept", 0)
    for did in (team["driver1_id"], team["driver2_id"]):
        dd = get_driver(career, did) if did else None
        if dd:
            pts += dd.get("team_points", 0)
    team["points"] = pts


# ---------------------------------------------------------------------------
# AI — always results-based (points / position / points share / rep / titles).
# It never reads team perf, so it behaves identically whether the perf chart
# is on or off. This is the explicit chart-off requirement.
# ---------------------------------------------------------------------------

def _results_tier(career, team):
    """Front / mid / back by constructor position thirds (not perf)."""
    pos = constructor_pos(career, team["id"])
    n = len(career["teams"])
    if pos <= max(1, n // 3):
        return "front"
    if pos <= max(2, (2 * n) // 3):
        return "mid"
    return "back"


def ai_visible_score(driver, career):
    """From ContractOffer.choose_best_candidate's visible_score, simplified
    to results only: points share within the team, rep, championships, points,
    best finish. No perf anywhere."""
    pts = driver["points"]
    best = driver["best_pos"] if driver["best_pos"] is not None else 99
    rep = driver["rep"] + (driver["cha"] / 4.0)
    team = get_team(career, driver["team_id"]) if driver.get("team_id") else None
    if team and team["points"] > 0:
        share = pts / team["points"]
        overall = (share * 2.0) + (rep * 0.5) + (pts / 20.0)
    else:
        overall = 0.5 + (rep * 1.0)
    return (overall, pts, -best)


def ai_best_candidate(career, team, candidates):
    """Pick a signing target the way ContractOffer does, but on results:
    rank by rep/championships + source bonus (free agent 4, F2 2), then the
    tier picks from the top of the list."""
    if not candidates:
        return None
    tier = _results_tier(career, team)

    def exp_score(d):
        score = d["rep"] * 1.0 + (d["cha"] / 7.0)
        if d["status"] in ("free", "f2"):
            pass
        if d.get("team_id") == team["id"]:
            score *= 1.25
        return score

    def source_bonus(d):
        if d["status"] == "free":
            return 4
        if d["status"] == "f2":
            return 2
        return 0

    ranked = sorted(candidates, key=lambda d: (exp_score(d), source_bonus(d)),
                    reverse=True)
    if tier == "front":
        picks = [ranked[0]]
    elif tier == "mid":
        picks = [ranked[0], ranked[0]] + ranked[1:3]
    else:
        picks = ranked[:3]
    return random.choice(picks)


def _ai_season_plan(career, team, kind):
    """v0.8.1: whether a bot team plans a T&R ("tr") or academy
    ("academy") signing this season — a stable per-(team, year)
    coin flip, ~40% of teams each season. The bots used to sign a
    T&R driver AND an academy junior every single season, which is
    what made the transfers list essay-length. The plan is a crc32
    of team+year+kind (not the career rng), so it needs no new saved
    field and behaves the same for old saves."""
    key = f"{team['id']}:{career.get('year')}:{kind}".encode()
    return zlib.crc32(key) % 10 < 4


def ai_act(career, team, rng=None):
    """One AI turn for a round, from Main.py's AI block.

    Spends the remainder after salaries on research, develops when the
    banked research is large enough, sets tuning from results, and puts a
    share into reg research. Uses money / points / position only.
    """
    rng = rng or random
    if team["money"] is None:
        return
    # v0.6 bot parity: a supplier with > $20M donates $1–2M to its
    # poorest supplied team with < $5M, at most once per round
    # (ai_act runs once per team per round). Done first, so the
    # research budget below is planned on the post-donation bank.
    poor_supplied = [t for t in supplied_teams(career, team)
                     if t["money"] < BOT_POOR_MONEY]
    if team["money"] > BOT_RICH_MONEY and poor_supplied:
        poor_supplied.sort(key=lambda t: (t["money"], t["name"]))
        donate(career, team["id"], poor_supplied[0]["id"],
               round(rng.uniform(1.0, 2.0), 2))
    salaries = salary_per_round(career, team)
    # v0.4.2: the funds a bot plans with are what's left after
    # salaries AND the per-race extra charges (T&R fine, academy
    # stipends, academy fine) — otherwise a team with 4 T&R drivers
    # would spend money the fine then takes away.
    charges = extra_charges_per_race(career, team)["total"]
    temp_funds = team["money"] - salaries - charges
    # v0.4 bot parity: fill an empty main seat before the race, from
    # the best available free agent (highest reputation). An empty
    # seat scores nothing, so this is the first priority. v0.7.3:
    # mid-season, an F2 driver can only join as T&R — a main seat
    # is filled from free agents only (see _best_available_driver).
    for seat in (1, 2):
        slot = "driver1_id" if seat == 1 else "driver2_id"
        if not team[slot]:
            best = _best_available_driver(career, statuses=("free",),
                                            team_id=team["id"])
            if best is not None:
                # v0.6: a junior team prefers a WANTED driver when
                # signing — the reward is a cash bounty, and the
                # driver is still judged on their real stats (see
                # _wanted_pick). v0.7.3: the WANTED pool for a main
                # seat is free agents only mid-season, like the
                # best-available pick above.
                pool = [d for d in career["drivers"]
                        if d["status"] == "free"
                        and not (d.get("academy_team_id")
                                 and d["academy_team_id"] != team["id"])]
                wanted = _wanted_pick(career, team, pool)
                if wanted is not None:
                    best = wanted
                sign_driver(career, team["id"], best["id"], seat,
                            contract_years=ai_contract_years(best))
    # v0.4 bot parity: optional T&R signing, preferring young, cheap
    # talent — one T&R driver, signed early in the season, for the
    # +25% research bonus. Only when the team can comfortably afford
    # it (salaries + a buffer), and only once per season. v0.7.3:
    # the pick is probability-weighted by reputation (see
    # _rep_weighted_pick) — bots prefer the high-rep / top-of-F2
    # drivers for promotion now, not the cheapest youngster.
    # v0.8.1: only ~40% of bot teams plan a T&R signing in a given
    # season (see _ai_season_plan) — the transfers list was drowning
    # in one-per-team-per-season T&R signings.
    if (not team_tr_drivers(career, team)
            and _ai_season_plan(career, team, "tr")
            and career.get("round", 1) <= 6
            and team["money"] > salaries + charges + 10):
        young = [d for d in career["drivers"]
                 if d["status"] in ("free", "f2") and d["age"] <= 25
                 and not (d.get("academy_team_id")
                          and d["academy_team_id"] != team["id"])]
        if young:
            # v0.6: a junior team prefers a WANTED driver as a T&R
            # signing too (same cash-bounty preference as above).
            pick = _wanted_pick(career, team, young) \
                or _rep_weighted_pick(young, rng=rng)
            sign_tr_driver(career, team["id"], pick["id"],
                           contract_years=ai_contract_years(pick))
    # v0.4.2 bot parity: an AI team may add one young unsigned
    # driver to its Driver Academy per season (youngest cheapest,
    # rounds 1-6), when it can comfortably afford it. Bots never
    # exceed 3 academy drivers — they avoid the exponential academy
    # fine entirely. v0.8.1: only ~40% of bot teams plan an academy
    # signing in a given season (see _ai_season_plan).
    if (not team.get("ai_academy_signed")
            and _ai_season_plan(career, team, "academy")
            and len(team_academy_drivers(career, team)) < ACADEMY_FINE_FREE_COUNT
            and career.get("round", 1) <= 6
            and team["money"] > salaries + charges + 10):
        young = [d for d in career["drivers"]
                 if d["status"] in ("free", "f2")
                 and d["age"] <= ACADEMY_MAX_JOIN_AGE
                 and not d.get("academy_team_id")]
        if young:
            young.sort(key=lambda d: (d["salary"], d["age"], d["name"]))
            ok, _why = add_academy_driver(career, team["id"], young[0]["id"])
            if ok:
                team["ai_academy_signed"] = True
    if temp_funds >= 10:
        round_funds = (temp_funds - 1) * (rng.random() * 0.35 + 0.2)
    else:
        round_funds = (temp_funds - 1) * (rng.random() * 0.35)
    if round_funds > 0.5:
        # v0.4: the T&R research bonus applies to bots too — each
        # T&R driver adds +25% to the research POOL gained, for the
        # same money spent (the pool is what the investment buys).
        mult = research_multiplier(career, team)
        # v0.5.3: bots put most of their research into Performance
        # (50-75%, was 0-50%) — the old split starved the perf pool,
        # so the car barely developed. Reliability gets the rest.
        # v0.6.6: the research cap is gone entirely — no cap clamp
        # on a bot junior's research spend any more.
        perf_money = round_funds * (0.50 + rng.random() * 0.25)
        reli_money = round_funds - perf_money
        team["perf_res"] += perf_money * mult
        team["reli_res"] += reli_money * mult
        team["money"] -= round_funds
        # v0.5.3: bots develop whenever the banked research is worth
        # converting (combined pool > 2.0) — the old 50% coin flip
        # meant they often sat on a good pool and never developed.
        if (team["perf_res"] + team["reli_res"]) > 2.0:
            # v0.5: bots develop with the same driver factor as humans
            # (their main + T&R drivers contribute) — bot parity.
            team["perf"] += (add_perf_gain(team["perf"], team["perf_res"])
                             * team_dev_factor(career, team))
            team["perf_res"] = 0.0
            # v0.6.1: the reliability cap is 1.0 (was 0.99), so a bot
            # team can reach "all good" too — 0% DNF chance.
            team["rel"] = max(0.5, min(1.0, team["rel"] + add_rel_gain(team["reli_res"])))
            team["reli_res"] = 0.0
            team["money"] -= 1
    # Tuning from results: if the lead driver scored >70% of team points.
    if career["round"] >= 3 and team["points"] > 0:
        drivers = team_drivers(career, team)
        lead = max((d["points"] for d in drivers), default=0)
        if lead / team["points"] > 0.70:
            team["tuning"] = 1 if constructor_pos(career, team["id"]) >= 4 else 3
        else:
            team["tuning"] = 0
    else:
        team["tuning"] = 0
    # v0.4 bot parity: regulation research from round 7 onward.
    # Adapt first (the unadapted part of the car), then research once
    # fully adapted. Bots spend a modest share of what is left over
    # after salaries — the same share the old code spent on reg
    # research, now routed through the adaptation model.
    if reg_open_for(career, team) and temp_funds > 5 \
            and team["money"] > salaries + 1:
        budget = round(min(0.10 * temp_funds, team["money"] - salaries - 1), 2)
        unadapted = float(team.get("reg_unadapted") or 0.0)
        if unadapted > 0 and budget > 0:
            points = min(unadapted, budget / REG_ADAPT_COST_PER_POINT)
            ok, _why = do_reg_adapt(career, team["id"], points=points)
            if ok:
                budget = round(budget - points * REG_ADAPT_COST_PER_POINT, 2)
        if budget > 0 and float(team.get("reg_unadapted") or 0.0) <= 0:
            do_reg_research(career, team["id"], budget)


def _rep_weighted_pick(candidates, rng=None):
    """v0.7.3: pick one candidate probability-weighted by hidden
    reputation — high-rep drivers are favoured, but a lower-rep
    driver can still be picked (no hard cutoff). Weight = rep^2,
    so a rep-4.5 driver weighs ~20x a rep-1.0 driver. Bots only —
    players never see rep. This is the fix for bots targeting
    bottom-of-F2 drivers for promotion/signing: they now prefer
    the drivers leading or near the top of F2."""
    rng = rng or random
    if not candidates:
        return None
    weights = [max(0.1, d.get("rep", 2.0)) ** 2 for d in candidates]
    return rng.choices(candidates, weights=weights, k=1)[0]


def _best_available_driver(career, statuses=("free", "f2"), team_id=None):
    """Best free-agent / F2 driver by reputation (bots may use rep;
    players never see it). Ties: cheaper salary, then name. None when
    nobody is available. v0.7.3: statuses limits the pool — a main
    seat mid-season is filled from free agents only (an F2 driver
    can only join as T&R until the F2 season ends). v0.8.0: with
    team_id, another team's academy junior is excluded — a bot
    cannot hold the secret meeting a poach needs, so the signing
    would fail and the seat would stay empty."""
    cands = [d for d in career["drivers"]
             if d["status"] in statuses
             and not (team_id and d.get("academy_team_id")
                      and d["academy_team_id"] != team_id)]
    if not cands:
        return None
    cands.sort(key=lambda d: (-d["rep"], d["salary"], d["name"]))
    return cands[0]


def bot_post_race(career, rng=None):
    """v0.4 bot parity: dropping a driver for really bad performance,
    and using the swap. After each race from round 6 onward, if a bot
    team's main driver has 0 points this season AND an average finish
    of 18th or worse, the bot acts:
      - if the team has a T&R driver, SWAP the underperforming main
        driver with the (highest-rep) T&R driver (free, same points
        rules as a player swap);
      - otherwise DROP the driver (contract broken — the team pays
        break_cost(); if it cannot afford the break fee, it does not
        drop) and the seat becomes empty to fill via the market /
        winter break.
    v0.4.3: the same rule can also RELEASE a T&R driver — a T&R
    driver never races, so only one with race stats from an earlier
    main-seat stint this season (swapped out) can trigger it. The
    team pays the same break fee as for a main driver.
    Never in the last 2 rounds of a season (round >= 11). At most one
    action per team per race (the worst underperformer). Applies to
    AI teams only, not to disconnected humans' teams. Every action is
    logged in the Transfers log (History & Rumours)."""
    rng = rng or random
    if career.get("round", 1) < 6 or career.get("round", 1) >= SEASON_LENGTH - 1:
        return
    for team in career["teams"]:
        if not team["is_ai"]:
            continue
        worst = None
        worst_avg = 0.0
        for seat in (1, 2):
            slot = "driver1_id" if seat == 1 else "driver2_id"
            d = get_driver(career, team[slot]) if team[slot] else None
            if not d:
                continue
            # v0.7.1: a substitute standing in for an ill driver is
            # temporary cover — the bot never drops them for poor
            # results (the ill driver's seat is being kept for them).
            if d.get("ill_sub_for"):
                continue
            count = d.get("finish_count", 0)
            if count == 0:
                continue
            avg = d.get("finish_sum", 0) / count
            if d.get("points", 0) == 0 and avg >= 18.0:
                if worst is None or avg > worst_avg:
                    worst, worst_avg = (seat, d), avg
        if worst is None:
            # v0.4.3 bot parity: a T&R driver who raced earlier this
            # season (swapped out of a main seat) and meets the SAME
            # bad-performance rule — 0 points, average finish 18th
            # or worse — can be released outright. A T&R driver
            # never races, so only one with race stats from an
            # earlier main-seat stint can trigger it. The team pays
            # the same break fee as for a main driver; if it cannot
            # afford it, it does not release. (One action per team
            # per race, as above — this only runs when no main
            # driver was acted on.)
            bad_tr = None
            bad_tr_avg = 0.0
            # v0.7.1: only FIT T&R drivers — an ill driver recovering
            # in T&R is never released for poor results.
            for tr in active_tr_drivers(career, team):
                count = tr.get("finish_count", 0)
                if count == 0:
                    continue
                avg = tr.get("finish_sum", 0) / count
                if tr.get("points", 0) == 0 and avg >= 18.0:
                    if bad_tr is None or avg > bad_tr_avg:
                        bad_tr, bad_tr_avg = tr, avg
            if bad_tr is None:
                continue
            fee = break_fee_for_driver(career, bad_tr)
            if fee > team["money"]:
                continue  # cannot afford the break fee: do not release
            team["money"] -= fee
            _release_driver(career, bad_tr, team)
            bad_tr["status"] = "free"
            _recompute_team_points(career, team)
            log_transfer(career, 
                f"{bad_tr['name']} released by {team['name']} "
                f"(poor results, ${fee:.2f}M break fee)")
            continue
        seat, d = worst
        # v0.7.1: only FIT T&R drivers — an ill driver recovering in
        # T&R is never swapped into a race seat.
        trs = active_tr_drivers(career, team)
        if trs:
            trs.sort(key=lambda x: (-x["rep"], x["name"]))
            # swap_main_tr logs the swap in the Transfers log.
            swap_main_tr(career, team["id"], seat, trs[0]["id"])
            continue
        fee = break_fee_for_driver(career, d)
        if fee > team["money"]:
            continue  # cannot afford the break fee: do not drop
        slot = "driver1_id" if seat == 1 else "driver2_id"
        team["money"] -= fee
        _release_driver(career, d, team)
        d["status"] = "free"
        team[slot] = None
        _recompute_team_points(career, team)
        log_transfer(career, 
            f"{d['name']} dropped by {team['name']} (poor results, ${fee:.2f}M break fee)")


# ---------------------------------------------------------------------------
# v0.7.1: random driver illness / injury
# ---------------------------------------------------------------------------

def _ill_driver_out_of_seat(career, team, ill):
    """Move an ill driver out of their race seat: the points they
    scored FOR this team stay banked with the team (real F1, like a
    swap) and they become a Test & Reserve driver while they
    recover — paid their full salary, still under contract. Only
    runs when they actually hold the seat."""
    seat = ill.get("ill_seat") or 1
    slot = "driver1_id" if seat == 1 else "driver2_id"
    if team.get(slot) != ill["id"]:
        return
    team["points_kept"] = team.get("points_kept", 0) + ill.get("team_points", 0)
    ill["team_points"] = 0
    team[slot] = None
    ill["status"] = "tr"


def _place_ill_substitute(career, team, ill, rng):
    """Put a substitute in the ill driver's seat for the rounds they
    miss, racing as a normal main driver. The stand-in comes from
    the team's Test & Reserve drivers — the highest-reputation fit
    one is the provisional pick (a human may swap them for another
    T&R driver for free with the normal swap); with no T&R drivers,
    the seat is filled the same way empty main seats are already
    auto-filled (one of the five worst-reputation free agents, at
    random, on a 1-year contract — see autofill_winter_break). The
    ill driver keeps their seat: they return when they recover.
    Returns True when a substitute was placed."""
    seat = ill.get("ill_seat") or 1
    slot = "driver1_id" if seat == 1 else "driver2_id"
    occupant = get_driver(career, team[slot]) if team[slot] else None
    if occupant is not None and occupant is not ill:
        return False  # the seat is held by someone else — not ours to fill
    trs = active_tr_drivers(career, team)
    if trs:
        trs.sort(key=lambda x: (-x["rep"], x["name"]))
        sub = trs[0]
        _ill_driver_out_of_seat(career, team, ill)
        ok, _why = promote_tr_to_main(career, team["id"], sub["id"], seat)
        if not ok:
            return False
        ill["ill_sub_id"] = sub["id"]
        sub["ill_sub_for"] = ill["id"]
        sub["ill_sub_from"] = "tr"
        return True
    free = [d for d in career["drivers"] if d["status"] == "free"]
    if not free:
        return False
    free.sort(key=lambda d: (d["rep"], d["salary"], d["name"]))
    pick = rng.choice(free[:5]) if len(free) > 1 else free[0]
    _ill_driver_out_of_seat(career, team, ill)
    _sign_driver_common(career, team, pick, 1)
    pick["status"] = "f1"
    pick["team_id"] = team["id"]
    team[slot] = pick["id"]
    _recompute_team_points(career, team)
    ill["ill_sub_id"] = pick["id"]
    pick["ill_sub_for"] = ill["id"]
    pick["ill_sub_from"] = "free"
    log_transfer(career, 
        f"{pick['name']} signed for {team['name']} "
        f"(standing in for {ill['name']})")
    return True


def roll_illness(career, rng=None):
    """v0.7.1: at the start of a round, each fit F1 main driver has
    a 0.5% chance of falling ill or injured (ILLNESS_CHANCE) — about
    one incident a season across the whole 22-driver grid. The
    driver misses 1-2 races (at random), the substitute fills the
    seat, and the illness is announced in the Transfers log (the
    dashboard shows the notice before qualifying). Both main
    drivers can fall ill at once — both seats get substitutes by
    the same rules. In-season rounds only."""
    rng = rng or random
    if career.get("phase") != "round":
        return []
    newly = []
    for team in career["teams"]:
        for seat in (1, 2):
            slot = "driver1_id" if seat == 1 else "driver2_id"
            d = get_driver(career, team[slot]) if team[slot] else None
            if not d or d.get("ill_until_round") is not None:
                continue
            if rng.random() >= ILLNESS_CHANCE:
                continue
            duration = rng.randint(ILLNESS_MIN_ROUNDS, ILLNESS_MAX_ROUNDS)
            last = min(career["round"] + duration - 1, SEASON_LENGTH)
            d["ill_until_round"] = last
            d["ill_label"] = rng.choice(["ill", "injured"])
            d["ill_seat"] = seat
            placed = _place_ill_substitute(career, team, d, rng)
            missed = last - career["round"] + 1
            sub = (get_driver(career, d.get("ill_sub_id"))
                   if d.get("ill_sub_id") else None)
            note = (f"{d['name']} is {d['ill_label']} and will miss "
                    f"the next {missed} race{'s' if missed != 1 else ''}")
            if placed and sub:
                note += f" — {sub['name']} is standing in for them"
            elif not placed:
                note += " — no substitute available, the seat is empty"
            log_transfer(career, note)
            newly.append(d)
    return newly


def restore_ill_driver(career, ill):
    """The ill driver's absence is over: they go back in their race
    seat and the substitute returns — to Test & Reserve if they came
    from there, or back to the free-agent pool if they were a
    free-agent fill. If the seat is now held by someone else (a
    mid-season signing took it), the returning driver joins Test &
    Reserve instead of displacing them. The contract is untouched
    throughout."""
    team = get_team(career, ill.get("team_id")) if ill.get("team_id") else None
    sub = (get_driver(career, ill.get("ill_sub_id"))
           if ill.get("ill_sub_id") else None)
    seat = None
    if team and sub:
        if team.get("driver1_id") == sub["id"]:
            seat = 1
        elif team.get("driver2_id") == sub["id"]:
            seat = 2
    if team and seat is None:
        # The substitute is gone (or was never in a seat): the first
        # empty seat, if any.
        for s in (1, 2):
            if not team.get("driver1_id" if s == 1 else "driver2_id"):
                seat = s
                break
    label = ill.get("ill_label") or "illness"
    if team and seat is not None:
        slot = "driver1_id" if seat == 1 else "driver2_id"
        if sub and team.get(slot) == sub["id"]:
            # Send the substitute back.
            if sub.get("ill_sub_from") == "free":
                _release_driver(career, sub, team)
                sub["status"] = "free"
                sub["contract_left"] = 0
            else:
                team["points_kept"] = (team.get("points_kept", 0)
                                       + sub.get("team_points", 0))
                sub["team_points"] = 0
                sub["status"] = "tr"
            sub["ill_sub_for"] = None
            sub["ill_sub_from"] = None
            team[slot] = None
        elif sub:
            # The substitute no longer holds the seat (poached or
            # dropped in the meantime) — their stand-in markers are
            # stale, so clear them.
            sub["ill_sub_for"] = None
            sub["ill_sub_from"] = None
        team[slot] = ill["id"]
        ill["status"] = "f1"
        ill["team_points"] = 0
        ill["last_team_id"] = None
        _recompute_team_points(career, team)
        log_transfer(career, 
            f"{ill['name']} is back from {label} at {team['name']}")
    elif team:
        # No seat free — the returning driver joins Test & Reserve.
        ill["status"] = "tr"
        if sub:
            sub["ill_sub_for"] = None
            sub["ill_sub_from"] = None
        log_transfer(career, 
            f"{ill['name']} is back from {label} — no race seat free "
            f"at {team['name']}, so they join Test & Reserve")
    ill["ill_until_round"] = None
    ill["ill_label"] = None
    ill["ill_seat"] = None
    ill["ill_sub_id"] = None
    return team is not None


def restore_ill_drivers(career, force=False):
    """Bring back every driver whose absence has ended (the current
    round is past their last missed round). At the season rollover
    every ill driver comes back (force=True) — no illness carries
    across a season."""
    for d in [x for x in career["drivers"]
              if x.get("ill_until_round") is not None]:
        if force or career["round"] > d["ill_until_round"]:
            restore_ill_driver(career, d)


def ensure_ill_substitutes(career, rng=None):
    """Safety net at the start of a race weekend: any ill driver
    whose seat is not held by their substitute (the substitute was
    released, dropped or poached away in the meantime) gets a fresh
    one by the same rules. Guards (release/sign refuse ill drivers)
    should prevent it, but the race must never run with an ill
    driver's seat held by nobody."""
    rng = rng or random
    for team in career["teams"]:
        for d in [x for x in career["drivers"]
                  if x.get("team_id") == team["id"]
                  and x.get("ill_until_round") is not None]:
            seat = d.get("ill_seat") or 1
            slot = "driver1_id" if seat == 1 else "driver2_id"
            sub = (get_driver(career, d.get("ill_sub_id"))
                   if d.get("ill_sub_id") else None)
            if sub and team.get(slot) == sub["id"]:
                continue
            if not team.get(slot):
                _place_ill_substitute(career, team, d, rng)


def open_new_round(career, rng=None):
    """A new round opens: bring back drivers whose illness has run
    its course, then roll illness for the round about to be raced."""
    restore_ill_drivers(career)
    return roll_illness(career, rng=rng)


# ---------------------------------------------------------------------------
# Simultaneous advance + race weekend
# ---------------------------------------------------------------------------

def human_teams(career):
    return [t for t in career["teams"] if t["owner_player_id"] is not None]


def all_humans_advanced(career):
    """True when EVERY human team has advanced — connected or not.
    v0.6.4: a disconnected player counts as NOT advanced (pending),
    never as advanced and never excluded from the count. The weekend
    must not start until every human has advanced (or the host uses
    the host override — see host_skip_advance)."""
    for team in human_teams(career):
        if team["id"] not in career["advanced"]:
            return False
    return True


def advance_team(career, team_id, rng=None):
    """A human hits Advance. Returns (advanced_ok, race_ran, message)."""
    team = get_team(career, team_id)
    if not team:
        return False, False, "No such team."
    if career["phase"] != "round":
        return False, False, "Not in a racing round right now."
    # v0.8.0: the advance gate — a team with an empty main seat
    # cannot advance (the server turns this into a pop-up naming
    # the empty seat).
    blockers = advance_blockers(career, team_id)
    if blockers:
        return False, False, ("Cannot advance yet — " + ", ".join(blockers)
                              + " is empty. Fill the seat first.")
    if team["id"] not in career["advanced"]:
        career["advanced"].append(team["id"])
    if all_humans_advanced(career):
        run_race_weekend(career, rng=rng)
        return True, True, "Qualifying complete — results are in."
    return True, False, "Waiting for the other teams to advance."


def host_skip_advance(career, player_id, is_host, rng=None):
    """v0.6.4: the host's override for the Advance — starts the race
    weekend now even though not every human has advanced. This is the
    escape hatch when a human has disconnected and is not coming
    back (the advance itself waits for ALL humans, connected or
    not). Host-only, like host_skip_results. v0.8.0: the advance
    gate blocks the host too — no team with an empty main seat can
    start a weekend."""
    if not is_host:
        return False, False, "Only the host can start the weekend."
    if career["phase"] != "round":
        return False, False, "Not in a racing round right now."
    blockers = advance_blockers(career)
    if blockers:
        return False, False, ("Cannot advance yet — " + ", ".join(blockers)
                              + " is empty. Fill the seat first.")
    run_race_weekend(career, rng=rng)
    return True, True, "Host started the race weekend — qualifying is in."


def results_voters(career):
    """Player ids that must vote NEXT at the current results stage:
    the owners of ALL human teams, connected or not. v0.6.4: a
    disconnected player counts as NOT voted (pending), never as voted
    and never excluded from the denominator — the vote must not
    complete until every human has voted (or the host skips it).
    Bots never vote."""
    return [t["owner_player_id"] for t in human_teams(career)]


def all_results_voted(career):
    """True when every required voter has voted. With no human teams
    at all (an all-AI career) there is nobody to wait for, so the
    stage advances — the same as before."""
    voters = results_voters(career)
    voted = set(career.get("results_vote") or [])
    return all(v in voted for v in voters)


def advance_results_stage(career, rng=None):
    """Move one results stage forward. qual -> race results page;
    race -> back to dashboards (next round, or winter break at season end).
    Returns True if the stage actually advanced."""
    if career.get("phase") == "qual_results":
        career["phase"] = "race_results"
        career["results_stage"] = "race"
        career["results_vote"] = []
        return True
    if career.get("phase") == "race_results":
        career["results_vote"] = []
        career["results_stage"] = None
        complete_race_weekend(career, rng=rng)
        return True
    return False


def check_results_vote(career, rng=None):
    """Advance the results stage if the vote is complete. Returns True if
    it advanced. Called after every vote and host skip. v0.6.4: a
    disconnect no longer completes the vote — the disconnected voter
    is still pending, so this is now a no-op when anyone is missing."""
    if career.get("phase") not in ("qual_results", "race_results"):
        return False
    if all_results_voted(career):
        return advance_results_stage(career, rng=rng)
    return False


def vote_next(career, player_id, rng=None):
    """A human votes NEXT on the current results page. Returns
    (ok, advanced, message). Votes are stored by player_id, so a
    reconnecting player keeps their vote."""
    if career.get("phase") not in ("qual_results", "race_results"):
        return False, False, "There is no results vote right now."
    if player_id not in results_voters(career):
        return False, False, "You have no team in this career, so you cannot vote."
    if player_id in (career.get("results_vote") or []):
        return True, False, "Your NEXT vote is already recorded."
    career.setdefault("results_vote", []).append(player_id)
    advanced = check_results_vote(career, rng=rng)
    total = len(results_voters(career))
    voted = len(career.get("results_vote") or [])
    if advanced:
        return True, True, "Everyone voted NEXT — moving on."
    return True, False, f"NEXT vote recorded ({voted}/{total}) ✓"


def host_skip_results(career, player_id, is_host, rng=None):
    """The host's override: advances the results stage immediately. Only
    the host may call it (the button is only ever sent to the host).
    v0.6.4: the server enforces this too — a non-host gets an error
    and the stage does not move."""
    if not is_host:
        return False, False, "Only the host can skip the vote."
    if career.get("phase") not in ("qual_results", "race_results"):
        return False, False, "There is no results vote to skip right now."
    advanced = advance_results_stage(career, rng=rng)
    return True, advanced, "Host skipped the vote — moving on."


def complete_race_weekend(career, rng=None):
    """After the race-results vote: next round, or season end.
    v0.6.6: the just-finished race counts as a round for pending
    contract-break notices — the tick happens here, once per round."""
    _tick_relation_notices(career)
    # v0.8.0: a finished round also ticks the secret-meeting cooldowns.
    tick_meeting_cooldowns(career)
    career["advanced"] = []
    career["results_vote"] = []
    career["results_stage"] = None
    if career["round"] >= SEASON_LENGTH:
        end_of_season(career, rng=rng)
    else:
        career["round"] += 1
        career["phase"] = "round"
        set_circuit_for_round(career)
        # v0.4: regulation research opens at the start of round 7 —
        # initialise every team's adaptation state from the car perf
        # at round 7 (before any round-7 research or development).
        if career["round"] == REG_OPENS_ROUND:
            init_reg_state(career)
        # v0.7.1: a new round opens — bring back drivers whose
        # illness has run its course, then roll illness for the
        # round about to be raced.
        open_new_round(career, rng=rng)


def run_race_weekend(career, rng=None):
    """AI acts, then qualifying, then the race, then money. v0.1: the round
    does NOT advance here — the room goes to the qualifying results page
    first (phase 'qual_results'), and only the NEXT votes move it on."""
    rng = rng or random
    # v0.7.1: before anything acts, make sure every ill driver's
    # seat is covered — a substitute released, dropped or poached
    # away since the illness started gets a fresh one by the same
    # rules. This must run BEFORE ai_act: a bot team with an empty
    # ill seat would otherwise fill it with the best available
    # driver, not the illness stand-in.
    ensure_ill_substitutes(career, rng=rng)
    # v0.6.4: the bot AI acts ONLY for teams that are genuinely AI
    # (no human owner). A human-owned team is NEVER bot-driven just
    # because its player is temporarily disconnected — the bot used
    # to set its Tuning and spend its research money while the
    # player was away (Tuning 1 -> 3, mystery research pools).
    for team in career["teams"]:
        if team["is_ai"] or not team.get("owner_player_id"):
            ai_act(career, team, rng=rng)
    # Constructor order at the start of the weekend (for EPRG).
    standings_before = race_mod.constructor_standings(career)
    pos_before = {t["id"]: i + 1 for i, t in enumerate(standings_before)}
    points_dist = update_points_dist(len(career["drivers"]) and
                                     len([d for d in career["drivers"]
                                          if d["status"] == "f1"]))
    gp = circuit_for_round(career["round"])
    qual = race_mod.simulate_qual(career, rng=rng)
    # v0.4: qualifying head-to-head for the winter-break review — the
    # driver who out-qualified their teammate in this session gains
    # one. Tracked per season (reset at season end).
    for team in career["teams"]:
        d1 = get_driver(career, team["driver1_id"]) if team["driver1_id"] else None
        d2 = get_driver(career, team["driver2_id"]) if team["driver2_id"] else None
        if d1 and d2 and d1.get("qual_pos") is not None and d2.get("qual_pos") is not None:
            if d1["qual_pos"] < d2["qual_pos"]:
                d1["season_qual_h2h"] = d1.get("season_qual_h2h", 0) + 1
            elif d2["qual_pos"] < d1["qual_pos"]:
                d2["season_qual_h2h"] = d2.get("season_qual_h2h", 0) + 1
            # v0.7.1 (a): the teammate comparison for QUALIFYING,
            # Elo-style (K half the race weight). Both drivers
            # qualified, so both have a qual_pos — no DNFs to skip
            # here. The expectation uses the reps going into the
            # weekend (no rep change has run yet).
            race_mod.apply_teammate_elo(
                d1, d2, d1["qual_pos"], d2["qual_pos"],
                rules_mod.DRIVER_REP_K_QUALI)
    leader = qual[0][0]["qual_time"] if qual else 0
    career["last_qual"] = [
        result_row(d, t, i + 1,
                   "LEADER" if i == 0 else f"+{d['qual_time'] - leader:.3f}s")
        for i, (d, t) in enumerate(qual)]
    race_entries, dnf_entries = race_mod.simulate_race(career, laps=gp["laps"], rng=rng)
    race_mod.apply_race_results(career, race_entries, points_dist, dnf_entries)
    # v0.7.0: a race win lifts the winning team's hidden reputation
    # (+0.3) — the paddock notices winners, and it is the only
    # in-race rep movement (the season-end updates happen in
    # end_of_season).
    if race_entries:
        adjust_team_rep(race_entries[0][1], TEAM_REP_WIN_BONUS)
    # v0.6.6: the junior income share — after each race a junior
    # team pays 15% of its RACE PRIZE money (per-car prize only:
    # 1.95 - 0.025*pos per finishing car — not EPRG, not the
    # season-end payout) to its parent team. The prize was just paid
    # inside apply_race_results, so it is recomputed here from the
    # finishers. The last-race figures reset every race (a junior
    # with no finishers pays 0); the season totals accumulate.
    race_prizes = {}
    for pos, (d, t) in enumerate(race_entries, start=1):
        race_prizes[t["id"]] = round(
            race_prizes.get(t["id"], 0.0) + 1.95 - 0.025 * pos, 2)
    for team in career["teams"]:
        team["junior_share_last_race"] = 0.0
        team["junior_share_received_last_race"] = 0.0
    for team in career["teams"]:
        if team.get("sup_stage") != 2:
            continue
        parent = supplier_of(career, team)
        if not parent:
            continue
        share = round(race_prizes.get(team["id"], 0.0) * JUNIOR_INCOME_SHARE, 2)
        team["junior_share_last_race"] = share
        team["junior_share_season"] = round(
            float(team.get("junior_share_season") or 0.0) + share, 2)
        parent["junior_share_received_last_race"] = share
        parent["junior_share_received_season"] = round(
            float(parent.get("junior_share_received_season") or 0.0) + share, 2)
        if share > 0:
            team["money"] = round(team["money"] - share, 2)
            parent["money"] = round(parent["money"] + share, 2)
            # v0.8.1: the money moves, but the payment is NOT a
            # transfer — it used to spam the transfers list once per
            # race per junior team. The Finances tab shows the share.
    leader_time = race_entries[0][0]["race_time"] if race_entries else 0
    career["last_race"] = [
        result_row(d, t, i + 1,
                   "LEADER" if i == 0 else f"+{d['race_time'] - leader_time:.3f}s",
                   points=points_dist[i] if i < len(points_dist) else 0,
                   gl=(d.get("qual_pos") or 0) - (i + 1))
        for i, (d, t) in enumerate(race_entries)]
    # v0.6.1: DNF drivers are listed after the finishers — "DNF"
    # instead of a position/gap, 0 points, best-qualified first.
    dnf_entries = sorted(dnf_entries, key=lambda e: e[0].get("qual_pos") or 99)
    career["last_race"] += [
        result_row(d, t, "DNF", "DNF", points=0)
        for d, t in dnf_entries]
    # F2 race in parallel.
    f2_order = race_mod.simulate_f2(career, rng=rng)
    for pos, d in enumerate(f2_order, start=1):
        # v0.4: F2 drivers track wins / best finish / best-finish
        # round too, so the F2 standings use the same sort as F1.
        if d.get("best_pos") is None or pos < d["best_pos"]:
            d["best_pos"] = pos
            d["best_finish_round"] = career["round"]
        if pos == 1:
            d["season_wins"] = d.get("season_wins", 0) + 1
        if pos <= 3:
            d["season_podiums"] = d.get("season_podiums", 0) + 1
        d["finish_sum"] = d.get("finish_sum", 0) + pos
        d["finish_count"] = d.get("finish_count", 0) + 1
        if pos <= len(points_dist):
            d["points"] += points_dist[pos - 1]
        new_rep = ((2 - (pos / (len(f2_order) * 2)) * 1) * 1 + d["rep"] * 11) / 12
        d["rep"] = new_rep
        d["race_pos"] = pos
    # v0.4 bot parity: after each race, a bot team may drop or swap
    # a main driver for really bad performance (0 pts, avg finish
    # 18th or worse, from round 6, never in the last 2 rounds).
    bot_post_race(career, rng=rng)
    # v0.7.3: a bot parent may spontaneously donate to a struggling,
    # well-reputed supplied team (no request needed) — see
    # bot_spontaneous_donations. Small per-round chance, amount by
    # need and surplus.
    bot_spontaneous_donations(career, rng=rng)
    # Money: salaries out, per-race gains (EPRG) in. v0.4.2: the
    # per-race extra charges go out too — the T&R fine, the academy
    # stipends (1/3 salary per season), and the academy fine.
    for team in career["teams"]:
        team["money"] -= salary_per_round(career, team)
        team["money"] -= extra_charges_per_race(career, team)["total"]
        team["money"] += finance_estimates(pos_before[team["id"]])["eprg"]
    # Sort drivers within each team by points scored FOR this team
    # (Main.py swaps D1/D2) and re-sort the team list into
    # constructor order. v0.2.1: team_points, not championship points
    # — a newcomer must not become Driver 1 on points scored elsewhere.
    for team in career["teams"]:
        d1 = get_driver(career, team["driver1_id"]) if team["driver1_id"] else None
        d2 = get_driver(career, team["driver2_id"]) if team["driver2_id"] else None
        if d1 and d2 and d2.get("team_points", 0) > d1.get("team_points", 0):
            team["driver1_id"], team["driver2_id"] = team["driver2_id"], team["driver1_id"]
            # v0.8.1: tuning follows the DRIVER, not the seat — flip
            # the tuning sign so it keeps favouring the same driver
            # (and the disadvantaged driver's tuning_against_streak
            # keeps accumulating instead of resetting). Without this,
            # a flip-flopping seat order silently moved the tuning
            # between drivers and neither driver's happiness moved.
            team["tuning"] = -int(team.get("tuning") or 0)
    career["teams"] = race_mod.constructor_standings(career)
    # Full Grand Prix record for history (revisit any previous GP).
    if career["last_race"]:
        top = career["last_race"][:3]
        career["race_history"].append({
            "year": career["year"], "round": career["round"],
            "circuit": gp["name"], "laps": gp["laps"],
            "qual": list(career["last_qual"]),
            "race": list(career["last_race"]),
            # v0.3: team_id too, so the history grid can colour each
            # finisher by the team they raced for at the time.
            "top3": [{"driver": r["driver"], "team": r["team"],
                      "team_id": r.get("team_id")} for r in top],
        })
    # To the qualifying results page — the NEXT votes take it from here.
    # v0.8.0: after every race, driver happiness/loyalty update and
    # the media rumours are written (both read the race_history that
    # was just appended above).
    update_driver_happiness(career, rng=rng)
    generate_media_rumours(career, rng=rng)
    career["phase"] = "qual_results"
    career["results_stage"] = "qual"
    career["results_vote"] = []


# ---------------------------------------------------------------------------
# Season rollover + winter break
# ---------------------------------------------------------------------------

def end_of_season(career, rng=None):
    """Champion, prize money, EPSCI, retirements, ageing, contracts, F2
    promotion. Leaves the career in winter_break — the 3-round offer
    flow (feature 6) — so humans AND bots fill empty seats one round
    at a time; nobody signs instantly at season end any more.

    v0.4: the regulation change is NOT applied here any more. The
    adaptation/research state was initialised at round 7 and stays
    pending through the winter break (teams can still adapt/research
    during it); start_next_season() applies it, so the winter break
    "last-minute R&D" has something to act on. career["next_reg_year"]
    / ["next_reg_kind"] already point at the change AFTER the season
    about to start (e.g. 2030 Major at the end of 2028); the pending
    change (into the new season) is recorded in pending_reg_* by
    init_reg_state at round 7.
    """
    rng = rng or random
    # v0.7.1: no illness carries across a season — every ill driver
    # comes back and their seat is theirs again, before any of the
    # standings / retirements / contracts below read the lineups.
    restore_ill_drivers(career, force=True)
    standings = race_mod.constructor_standings(career)
    # v0.2: driver_standings() also lists replaced drivers (free agents
    # with points), and v0.4.4: any driver who raced in F1 this season
    # (0 points included) — but the champion / retirements / ageing
    # below are for F1 drivers only.
    drivers = [d for d in race_mod.driver_standings(career)
               if d["status"] == "f1"]
    # v0.2: record this season's final driver order BEFORE the points
    # reset below, so the next season opens in this order (all on 0).
    career["prev_driver_order"] = [d["id"] for d in drivers]
    # v0.4: record this season's final constructors' order too — the
    # next season opens in this order, and the winter-break offer
    # resolution processes teams in this order (champion first).
    # v0.7.2: keep the PREVIOUS season's order first — the retirement
    # chance below needs each team's trajectory (places gained vs
    # last season), and this assignment overwrites it.
    prev_order = list(career.get("prev_team_order") or [])
    new_pos = {t["id"]: i + 1 for i, t in enumerate(standings)}
    old_pos = {tid: i + 1 for i, tid in enumerate(prev_order)}
    traj_by_team = {tid: old_pos.get(tid, new_pos[tid]) - new_pos[tid]
                    for tid in new_pos}
    top_perf = max((t.get("perf", 0.0) or 0.0 for t in career["teams"]),
                   default=0.0)
    career["prev_team_order"] = [t["id"] for t in standings]
    # v0.4: snapshot the season just finished for the winter-break
    # review (stats, final standings, head-to-head) — taken BEFORE
    # the points reset below, so it shows the real final numbers.
    # v0.7.3: the snapshot saves the FULL registered driver list —
    # every driver in the season's standings (main drivers, free
    # agents with points, substitutes and mid-season movers — anyone
    # who raced in F1 or scored), not just the main seats. Points /
    # wins / podiums / best finish come from the F1 tally (a T&R
    # driver still racing in F2 reads their f1_* fields, never
    # their F2 points), and the team is their last team. The
    # champion / retirements / ageing below still use the main-seat
    # list (drivers) only.
    all_drivers = race_mod.driver_standings(career)
    season_rows = []
    for d in all_drivers:
        tally = race_mod.f1_tally(d)
        season_rows.append({
            "id": d["id"], "name": d["name"],
            "team": (get_team(career, d.get("team_id")
                              or d.get("last_team_id")) or {}).get("name", ""),
            "team_id": d.get("team_id") or d.get("last_team_id"),
            "points": tally["points"],
            "wins": tally["wins"],
            "podiums": tally["podiums"],
            "best_pos": tally["best_pos"],
            "qual_h2h": d.get("season_qual_h2h", 0),
            "avg_finish": round(d.get("finish_sum", 0) / d["finish_count"], 1)
                          if d.get("finish_count") else None,
        })
    career["last_season"] = {
        "year": career["year"],
        "drivers": season_rows,
        "teams": [{
            "id": t["id"], "name": t["name"], "points": t["points"],
            "wins": t.get("season_wins", 0), "best_pos": t.get("best_pos"),
        } for t in standings],
    }
    if drivers:
        champ = drivers[0]
        champ["cha"] += 1
        champ_team = get_team(career, champ["team_id"])
        # v0.3: the champion-history row records age at the START of
        # the season just finished (ages tick up later in this
        # function), the champion's cumulative trophy count, and the
        # constructors' champion — which may differ from the driver's
        # team (e.g. 2021 Verstappen/Red Bull, Mercedes won the
        # constructors').
        constructors_champ = standings[0]["name"] if standings else ""
        career["champion_history"].append({
            "year": career["year"], "driver": champ["name"],
            "age": champ["age"],
            "team": champ_team["name"] if champ_team else "",
            "points": champ["points"],
            "trophies": champ["cha"],
            "winning_team": constructors_champ,
        })
        if champ_team:
            champ_team["c_wins"] += 1
    # v0.7.0: append the season snapshot for the clickable champion
    # years (season_snapshots, same shape as last_season minus the
    # per-driver stats the detail view does not need) — before the
    # points reset below, so it records the real final numbers.
    # v0.7.2: the live career is passed so the snapshot also captures
    # this season's relationships as they stood at season end.
    career.setdefault("season_snapshots", []).append(
        season_snapshot(career["last_season"], career))
    # v0.7.0: hidden team reputation moves at the season rollover.
    # Constructors' position: P1 +5, P6 0, P11 -5. Driver quality:
    # the average hidden rep of the team's two main drivers, centred
    # on 2.5 and halved (a 4.4-average lineup gains +0.95, a
    # 1.5-average one loses -0.5). One empty seat uses the filled
    # driver's rep; two empty seats skip the quality term. Teams
    # are read from the final standings order, before retirements
    # change the lineups below.
    for pos, team in enumerate(standings, start=1):
        adjust_team_rep(team, (6 - pos) * 1.0)
        mains = [d for d in team_drivers(career, team) if d]
        if mains:
            avg_rep = sum(d.get("rep", 2.0) for d in mains) / len(mains)
            adjust_team_rep(team, (avg_rep - 2.5) * 0.5)
    # v0.7.1 (c): driver reputation at the season rollover —
    # standings vs team, plus the championship bonus. Positions are
    # the FINAL standings: the full drivers' list (free agents with
    # points sort in too, so a replaced driver's position counts)
    # and the final constructors' order. A driver finishing well
    # above their team's place gains; well below loses (capped at
    # +/-0.15). The champion gains +0.5 — or +0.75 when their team
    # did NOT win the constructors' championship. This runs once,
    # here, on top of the per-race components (a) and (b) — no
    # double-counting.
    full_standings = race_mod.driver_standings(career)
    dpos = {d["id"]: i + 1 for i, d in enumerate(full_standings)}
    tpos = {t["id"]: i + 1 for i, t in enumerate(standings)}
    for d in drivers:
        tp = tpos.get(d.get("team_id"), len(standings))
        dp = dpos.get(d["id"], len(full_standings))
        delta = (tp - dp) * DRIVER_REP_SEASON_RATE
        delta = max(-DRIVER_REP_SEASON_CAP,
                    min(DRIVER_REP_SEASON_CAP, delta))
        race_mod.adjust_driver_rep(d, delta)
    if drivers:
        champ = drivers[0]
        champ_team = get_team(career, champ["team_id"])
        won_constructors = bool(
            standings and champ_team and standings[0]["id"] == champ_team["id"])
        bonus = (DRIVER_REP_CHAMPION_BONUS if won_constructors
                 else DRIVER_REP_CHAMPION_BONUS_NON_TOP)
        race_mod.adjust_driver_rep(champ, bonus)
    # F2 champion is promoted to the free-agent pool.
    # v0.7.3: a driver holding an F1 T&R contract while still
    # racing F2 (f2_tr) STAYS eligible for the F2 title — they can
    # win it while a T&R. The champion is the best driver still in
    # F2 (status "f2" or f2_tr). The champion also gets a +0.5 rep
    # boost, on top of the F2 race-result rep earned during the
    # season. A champion who is an f2_tr driver is forced OUT of F2
    # at the rollover but KEEPS their T&R contract (status stays
    # "tr", team kept); a plain F2 champion joins the free-agent
    # pool as before.
    f2_sorted = [d for d in race_mod.f2_standings(career)
                 if d["status"] == "f2" or d.get("f2_tr")]
    if f2_sorted:
        f2_champ = f2_sorted[0]
        race_mod.adjust_driver_rep(f2_champ, DRIVER_REP_F2_CHAMPION_BONUS)
        was_f2_tr = bool(f2_champ.get("f2_tr"))
        if was_f2_tr:
            f2_champ["f2_tr"] = False
            f2_champ["last_team_id"] = None
        else:
            f2_champ["status"] = "free"
            f2_champ["team_id"] = None
            f2_champ["last_team_id"] = None
        f2_champ["points"] = 0
        f2_champ["team_points"] = 0  # v0.2.1: F2 points never carry to F1
        f2_champ["season_wins"] = 0
        f2_champ["season_podiums"] = 0
        f2_champ["season_qual_h2h"] = 0
        f2_champ["best_pos"] = None
        f2_champ["best_finish_round"] = None
        f2_champ["finish_sum"] = 0
        f2_champ["finish_count"] = 0
        log_transfer(career, 
            f"{f2_champ['name']} won F2 and joins the free-agent pool"
            if not was_f2_tr else
            f"{f2_champ['name']} won F2 and leaves the feeder series "
            f"(stays Test & Reserve)")
    # Prize money (Main.py): 1st $12M, others 11 - 0.65*pos. Plus EPSCI.
    for pos, team in enumerate(standings, start=1):
        team["money"] += 12.0 if pos == 1 else (11.0 - (0.65 * pos))
        team["money"] += finance_estimates(pos)["epsci"]
    # Retirements (F1 drivers, then T&R drivers, then free agents).
    # v0.7.2: the chance now weighs the whole situation —
    # performance, car, reputation and the team's trajectory — and
    # there is no maximum age. v0.7.3: T&R drivers roll too, with
    # their F1 team's context (a T&R driver still racing in F2
    # reads their F1 tally — f1_tally — never their F2 points).
    # v0.7.5: custom retirements — when the master toggle is ON,
    # ONLY the toggled drivers retire (forced) and the random rolls
    # are skipped entirely. When it is OFF, the random rolls run as
    # before and every toggle is cleared.
    retired = []
    if career.get("custom_retirements"):
        for d in list(drivers):
            if d.get("retire_at_season_end"):
                retired.append(d)
        for team in career["teams"]:
            for d in team_tr_drivers(career, team):
                if d.get("retire_at_season_end"):
                    retired.append(d)
        for d in list(free_agents(career)):
            if d.get("retire_at_season_end"):
                retired.append(d)
        # v0.8.0: F2 drivers retire too when toggled — the custom
        # branch is the only retirement path that covers them
        # (random retirements never touch F2; the cleanup below
        # retires them through the same status/tallies reset).
        # An f2_tr driver is status "tr", so they are already in
        # the T&R loop above — this loop only adds plain F2 drivers.
        for d in list(f2_drivers(career)):
            if d.get("retire_at_season_end"):
                retired.append(d)
    else:
        for d in list(drivers):
            team = get_team(career, d["team_id"]) if d.get("team_id") else None
            pos = constructor_pos(career, d["team_id"]) if team else len(career["teams"])
            team_pts = team["points"] if team else 0
            if is_retiring(d["age"], d["cha"], pos, d["points"], team_pts,
                           rep=d.get("rep", 2.5),
                           wins=d.get("season_wins", 0) or 0,
                           trajectory=traj_by_team.get(d.get("team_id"), 0),
                           perf_gap=(top_perf - team["perf"]) if team else 0.0,
                           rng=rng):
                retired.append(d)
        for team in career["teams"]:
            for d in team_tr_drivers(career, team):
                tally = race_mod.f1_tally(d)
                if is_retiring(d["age"], d["cha"],
                               constructor_pos(career, team["id"]),
                               tally["points"], team["points"],
                               rep=d.get("rep", 2.5),
                               wins=tally["wins"] or 0,
                               trajectory=traj_by_team.get(team["id"], 0),
                               perf_gap=top_perf - team["perf"],
                               rng=rng):
                    retired.append(d)
        for d in list(free_agents(career)):
            # No team (worst position: last constructors' place), no
            # forced retirement at any age — the same gentle curve.
            chance = retirement_chance(d["age"], d["cha"], len(career["teams"]),
                                       d["points"], 0,
                                       rep=d.get("rep", 2.5),
                                       wins=d.get("season_wins", 0) or 0)
            if chance > 0 and rng.random() * 100 <= chance:
                retired.append(d)
        # v0.7.5: custom retirements off — every toggle resets.
        for d in career["drivers"]:
            d["retire_at_season_end"] = False
    for d in retired:
        team = get_team(career, d["team_id"]) if d.get("team_id") else None
        if team:
            if team["driver1_id"] == d["id"]:
                team["driver1_id"] = None
            if team["driver2_id"] == d["id"]:
                team["driver2_id"] = None
            d["last_team_id"] = team["id"]  # v0.2.1: record last team
        d["status"] = "retired"
        d["team_id"] = None
        # v0.4.2: a retired driver leaves their academy too.
        d["academy_team_id"] = None
    career["retired_this_season"] = [d["name"] for d in retired]
    # v0.7.3: EVERY retired driver is cleaned off the next season's
    # active standings, whatever they were at retirement — main,
    # T&R or free agent. Their season tallies are zeroed here, AFTER
    # the snapshot above (so they stay in the previous season's
    # history) and BEFORE the ageing loops below. The leak was the
    # free-agent loop skipping retired drivers (so their points and
    # f1_races stayed stale and driver_standings kept listing them
    # next season) and T&R retirees never being reset at all — a
    # retired main driver was reset by the main loop, which is why
    # grid retirees retired cleanly and the others did not.
    for d in retired:
        d["points"] = 0
        d["team_points"] = 0
        d["season_wins"] = 0
        d["season_podiums"] = 0
        d["season_qual_h2h"] = 0
        d["best_pos"] = None
        d["best_finish_round"] = None
        d["finish_sum"] = 0
        d["finish_count"] = 0
        d["f1_races"] = 0
        d["f1_points"] = 0
        d["f1_wins"] = 0
        d["f1_podiums"] = 0
        d["f1_best_pos"] = None
        d["f1_best_finish_round"] = None
        # v0.7.5: a retired driver's toggle is spent.
        d["retire_at_season_end"] = False
    # Ageing (v0.4.2: table-driven grow_rating, replacing DSUS),
    # salary recalc, contracts tick down, points reset.
    for i, d in enumerate(drivers):
        old_age = d["age"]
        d["age"] += 1
        d["contract_left"] = max(0, d["contract_left"] - 1)
        team = get_team(career, d["team_id"]) if d.get("team_id") else None
        team_pos = constructor_pos(career, d["team_id"]) if team else len(career["teams"])
        age_val = (d["age"] / 20) * 3.5
        pos_val = (len(drivers) - i) / len(drivers) * 8 if drivers else 0
        val = (d["salary"] * 0.6) + (age_val * 0.2) + (pos_val * 0.2)
        d["salary"] = max(0.5, min(10.0, val))
        d["mv"] = d["salary"]
        d["perf"] = grow_rating(old_age, d["perf"],
                                is_academy=bool(d.get("academy_team_id")),
                                rng=rng)
        d["points"] = 0
        d["team_points"] = 0  # v0.2.1: per-team tally resets with the season
        if d["status"] == "f1":
            # Still racing: last team is their current team (recorded
            # on departure instead). Retired drivers keep theirs.
            d["last_team_id"] = None
        d["season_wins"] = 0
        d["season_podiums"] = 0
        d["season_qual_h2h"] = 0
        d["best_pos"] = None
        d["best_finish_round"] = None
        d["finish_sum"] = 0
        d["finish_count"] = 0
        d["f1_races"] = 0
    for d in free_agents(career):
        if d not in retired:
            old_age = d["age"]
            d["age"] += 1
            d["perf"] = grow_rating(old_age, d["perf"],
                                    is_academy=bool(d.get("academy_team_id")),
                                    rng=rng)
            # v0.2: free agents' points reset with the season, like F1/F2
            # drivers — otherwise a replaced driver's stale points would
            # show in next season's standings.
            d["points"] = 0
            d["team_points"] = 0  # v0.2.1
            d["season_wins"] = 0
            d["season_podiums"] = 0
            d["season_qual_h2h"] = 0
            d["best_pos"] = None
            d["best_finish_round"] = None
            d["finish_sum"] = 0
            d["finish_count"] = 0
            d["f1_races"] = 0
    for d in f2_drivers(career):
        old_age = d["age"]
        d["age"] += 1
        d["perf"] = grow_rating(old_age, d["perf"],
                                is_academy=bool(d.get("academy_team_id")),
                                rng=rng)
        d["points"] = 0
        d["team_points"] = 0  # v0.2.1
        d["season_wins"] = 0
        d["season_podiums"] = 0
        d["season_qual_h2h"] = 0
        d["best_pos"] = None
        d["best_finish_round"] = None
        d["finish_sum"] = 0
        d["finish_count"] = 0
        d["f1_races"] = 0
    # v0.4: T&R drivers age and their contracts tick down too. An
    # expired T&R contract ends the affiliation — the driver becomes
    # a free agent (and is then available for winter-break offers).
    # Their salary is unchanged (no race results to re-price on).
    # Their season tallies reset too: a T&R driver may have raced
    # earlier in the season (then been swapped out), and a stale
    # best finish would otherwise sort them above every carried-over
    # driver when they return to a race seat next season.
    # v0.7.3: a T&R driver still racing in F2 (f2_tr) has their F1
    # tally (f1_*) reset here too — the F2 loop above resets the
    # plain (F2) fields. If their T&R contract expires, they go
    # back to being a plain F2 driver (not a free agent).
    for team in career["teams"]:
        for d in team_tr_drivers(career, team):
            old_age = d["age"]
            d["age"] += 1
            d["contract_left"] = max(0, d["contract_left"] - 1)
            d["perf"] = grow_rating(old_age, d["perf"],
                                    is_academy=bool(d.get("academy_team_id")),
                                    rng=rng)
            d["points"] = 0
            d["team_points"] = 0
            d["season_wins"] = 0
            d["season_podiums"] = 0
            d["season_qual_h2h"] = 0
            d["best_pos"] = None
            d["best_finish_round"] = None
            d["finish_sum"] = 0
            d["finish_count"] = 0
            d["f1_races"] = 0
            d["f1_points"] = 0
            d["f1_wins"] = 0
            d["f1_podiums"] = 0
            d["f1_best_pos"] = None
            d["f1_best_finish_round"] = None
            if d["contract_left"] <= 0:
                if d.get("f2_tr"):
                    d["f2_tr"] = False
                    d["status"] = "f2"
                else:
                    d["status"] = "free"
                d["team_id"] = None
                d["last_team_id"] = team["id"]
                # v0.7.1: record the slot they held, for the
                # winter-break re-sign offer.
                d["last_slot"] = "tr"
                log_transfer(career, 
                    f"{d['name']} left {team['name']} (T&R contract ended)")
    # Contracts that expired: the driver leaves the team and becomes a free agent.
    for team in career["teams"]:
        for slot in ("driver1_id", "driver2_id"):
            did = team[slot]
            if not did:
                continue
            d = get_driver(career, did)
            if d and d["status"] == "f1" and d["contract_left"] <= 0 and d not in retired:
                # v0.2.1: leaving at contract end also records the last
                # team (and banks the per-team points, though the season
                # totals are reset just below anyway).
                _release_driver(career, d, team)
                d["status"] = "free"
                team[slot] = None
                log_transfer(career, 
                    f"{d['name']} left {team['name']} (contract ended)")
        if team["driver1_id"] is None and team["driver2_id"] is not None:
            team["driver1_id"], team["driver2_id"] = team["driver2_id"], None
        team["points"] = 0
        team["points_kept"] = 0  # v0.2: banked points reset with the season
        team["best_pos"] = None
        team["best_finish_round"] = None
        team["season_wins"] = 0
        team["tuning"] = 0
        # v0.4.2: the bot academy quota resets with the season.
        team["ai_academy_signed"] = False
    # v0.6: relation contracts tick down one year at season end — a
    # 2-year deal covers exactly two seasons, and is due for renewal
    # in the winter break that follows its last season (see
    # start_next_season, where an unrenewed due deal lapses).
    for team in career["teams"]:
        if team.get("sup_stage", 0) > 0 and team.get("relation_years", 0) > 0:
            team["relation_years"] -= 1
    # Refill the F2 grid from queued / pool rookies.
    refill_f2(career, rng=rng)
    # v0.8.0: agreed secret meetings expire at season end — a
    # meeting is for this season's move, not next year's. Cooldowns
    # expire too (a new season is a fresh start).
    career["meetings"] = []
    career["meeting_cooldowns"] = {}
    # v0.4: the regulation change into the new year is NOT applied
    # here — it waits for start_next_season(), after the winter
    # break's last-minute R&D. Record the new year and the NEXT
    # change (the one after the season about to start) for the UI.
    new_year = career["year"] + 1
    career["year"] = new_year
    career["season_index"] += 1
    career["next_reg_year"] = new_year + 1
    career["next_reg_kind"] = "Major" if (new_year + 1) % 4 == 2 else "Minor"
    career["round"] = SEASON_LENGTH
    # Winter break: 3 rounds of offers (feature 6). Round 1 starts
    # now — bot offers for round 1 are generated when the round opens.
    career["wb_round"] = 1
    career["wb_offers"] = {}
    career["wb_ready"] = []
    career["wb_done"] = False
    if career["season_index"] >= MAX_SEASONS:
        career["phase"] = "finished"
    else:
        career["phase"] = "winter_break"
        start_winter_break_round(career, rng=rng)


def refill_f2(career, rng=None):
    """Top the F2 grid back up to 22 from queued, then pool rookies."""
    rng = rng or random
    f2 = f2_drivers(career)
    while len(f2) < 22:
        nxt = next((d for d in career["drivers"] if d["status"] == "queued"), None)
        if nxt is None:
            nxt = next((d for d in career["drivers"] if d["status"] == "pool"), None)
        if nxt is None:
            break
        nxt["status"] = "f2"
        nxt["team_id"] = None
        f2.append(nxt)


def ai_contract_years(driver, rng=None):
    """v0.3: a bot team signing a driver also picks a contract length
    1–4 years. Rule (documented): bots commit longer to drivers they
    rate highly (rep is the bots' judgement of who's good) — rep 4+
    gets 4 years, rep 3+ gets 2–4, rep 2+ gets 1–3, below that 1–2.
    The draw uses the passed rng so tests stay deterministic."""
    rng = rng or random
    rep = driver.get("rep", 2.0)
    if rep >= 4.0:
        return 4
    if rep >= 3.0:
        return rng.randint(2, 4)
    if rep >= 2.0:
        return rng.randint(1, 3)
    return rng.randint(1, 2)


def empty_seats(career, team):
    return [s for s in (1, 2)
            if not team["driver1_id" if s == 1 else "driver2_id"]]


# ---------------------------------------------------------------------------
# Winter break — 3 rounds of offers (feature 6)
# ---------------------------------------------------------------------------

def wb_constructors_order(career):
    """Last season's final constructors' order (champion first) —
    the order winter-break offers are processed in, and the order a
    driver picks from. Falls back to the current constructor
    standings when there is no snapshot (migrated v5 save)."""
    snap = career.get("last_season") or {}
    order = [t["id"] for t in (snap.get("teams") or [])]
    if order:
        known = {t["id"] for t in career["teams"]}
        return [tid for tid in order if tid in known] + \
               [t["id"] for t in career["teams"] if t["id"] not in order]
    return [t["id"] for t in race_mod.constructor_standings(career)]


def winter_break_review(career, team_id=None):
    """v0.4.1: the last-season review for the winter-break card —
    final driver standings (top 5), final constructors (top 5), and
    the head-to-head of the given team's two main drivers LAST
    season. The head-to-head is read from the last_season snapshot,
    not the current lineup, so it still works when a driver has
    since left (e.g. Piastri after his McLaren contract ended):
    the team's current main drivers are preferred, and any gap is
    filled from the snapshot's other drivers for that team. No
    reputation is ever included — it is bot-only data."""
    snap = career.get("last_season") or {}
    drivers = list(snap.get("drivers") or [])
    teams = list(snap.get("teams") or [])
    h2h = []
    if team_id:
        team = get_team(career, team_id)
        by_id = {d["id"]: d for d in drivers}
        current = [team["driver1_id"], team["driver2_id"]] if team else []
        h2h = [by_id[did] for did in current if did in by_id]
        for d in drivers:
            if len(h2h) >= 2:
                break
            if d.get("team_id") == team_id and d not in h2h:
                h2h.append(d)
    return {
        "year": snap.get("year"),
        "drivers": drivers[:5],
        "teams": teams[:5],
        "head_to_head": h2h,
    }


def season_snapshot(season, career=None):
    """v0.7.0: strip a last_season-shaped dict down to the season
    snapshot stored in career['season_snapshots'] (and used by the
    clickable champion years) — the standings only, no per-driver
    head-to-head / average-finish stats.
    v0.7.2: when the live career is passed (the rollover, and the
    last_season fallback in get_season_detail), each team entry also
    captures that season's relationship state as it stood at season
    end — its supplier (id, name, customer/junior stage) and the
    teams it supplies (id, name, kind) — so the detail view shows
    the relationships AS THEY WERE that year, never the live ones.
    Snapshots built without a career (the migration rebuild of
    older saves) carry no relationship data; only the 2026 snapshot
    is backfilled, from the 2026 grid (see f1/saves.py)."""
    teams = [{
        "id": t["id"], "name": t["name"], "points": t["points"],
        "wins": t.get("wins", 0), "best_pos": t.get("best_pos"),
    } for t in (season.get("teams") or [])]
    if career is not None:
        live = {t["id"]: t for t in career.get("teams") or []}
        rel = {}
        for entry in teams:
            lt = live.get(entry["id"]) or {}
            sup = live.get(lt.get("supplier_id")) if lt.get("supplier_id") else None
            rel[entry["id"]] = {
                "supplier_id": sup["id"] if sup else None,
                "supplier_name": sup["name"] if sup else None,
                "sup_stage": int(lt.get("sup_stage") or 0),
            }
        for entry in teams:
            entry.update(rel[entry["id"]])
            entry["supplies"] = [{
                "id": t["id"], "name": t["name"],
                "kind": "junior" if rel[t["id"]]["sup_stage"] == 2 else "customer",
            } for t in teams if rel[t["id"]]["supplier_id"] == entry["id"]]
    return {
        "year": season.get("year"),
        "drivers": [{
            "name": d["name"], "team": d.get("team", ""),
            "team_id": d.get("team_id"), "points": d["points"],
            "wins": d.get("wins", 0), "podiums": d.get("podiums", 0),
            "best_pos": d.get("best_pos"),
        } for d in (season.get("drivers") or [])],
        "teams": teams,
    }


def winter_break_recap(career):
    """v0.7.0: the 'Season {year} in review' panel for the top of
    the winter-break screen — Top 8 drivers and Top 5 constructors
    from the last_season snapshot, plus the 12 round winners from
    race_history for the season just finished (winner = race pos 1;
    a 'DNF' pos is a string and can never equal 1, so it is simply
    never the winner). Returns empty lists when there is no
    snapshot yet (a season has never completed)."""
    snap = career.get("last_season") or {}
    year = snap.get("year")
    winners = []
    if year is not None:
        entries = sorted(
            (e for e in career.get("race_history") or []
             if e.get("year") == year and e.get("race")),
            key=lambda e: e.get("round") or 0)
        for e in entries:
            win = next((r for r in (e.get("race") or [])
                        if r.get("pos") == 1), None)
            if win:
                winners.append({
                    "round": e.get("round"), "circuit": e.get("circuit"),
                    "driver": win.get("driver"), "team": win.get("team"),
                    "team_id": win.get("team_id"),
                })
    return {
        "year": year,
        "drivers": list(snap.get("drivers") or [])[:8],
        "teams": list(snap.get("teams") or [])[:5],
        "winners": winners,
    }


def get_season_detail(career, year):
    """v0.7.0: the full standings for one season, for the clickable
    champion years. In-game years (2026+) come from
    season_snapshots (falling back to last_season for the season
    just finished, in case the snapshot was never written); real
    years 2010-2025 come from the baked-in REAL_STANDINGS. Returns
    {year, source, has_relations, drivers, constructors} — drivers
    carry pos, name, team, team_id (in-game only), points, wins;
    constructors carry pos, name, points, wins, plus the SNAPSHOT's
    relationship data (supplier_id, supplier_name, sup_stage,
    supplies) when the snapshot has it — or None when the year has
    no recorded standings. Team reputation is never part of a season
    detail."""
    try:
        year = int(year)
    except (TypeError, ValueError):
        return None
    if year >= 2026:
        snap = next((s for s in career.get("season_snapshots") or []
                     if s.get("year") == year), None)
        if snap is None and (career.get("last_season") or {}).get("year") == year:
            snap = season_snapshot(career["last_season"], career)
        if snap is None:
            return None
        # v0.7.2: constructors carry the SNAPSHOT's relationship data
        # (supplier + supplied teams as they were that year). A
        # snapshot built before v0.7.2 has none of these fields — the
        # detail then shows no relationship info at all, never the
        # live teams' current relations.
        has_relations = any("supplier_id" in t for t in (snap.get("teams") or []))
        return {
            "year": year, "source": "game", "has_relations": has_relations,
            "drivers": [{
                "pos": i + 1, "name": d["name"],
                "team": d.get("team", ""), "team_id": d.get("team_id"),
                "points": d["points"], "wins": d.get("wins", 0),
            } for i, d in enumerate(snap.get("drivers") or [])],
            "constructors": [{
                "pos": i + 1, "name": t["name"],
                "points": t["points"], "wins": t.get("wins", 0),
                "supplier_id": t.get("supplier_id"),
                "supplier_name": t.get("supplier_name"),
                "sup_stage": t.get("sup_stage"),
                "supplies": list(t.get("supplies") or []),
            } for i, t in enumerate(snap.get("teams") or [])],
        }
    real = REAL_STANDINGS.get(str(year))
    if not real:
        return None
    return {
        "year": year, "source": "real",
        "drivers": [{
            "pos": d.get("pos"), "name": d["name"],
            "team": d.get("team", ""), "team_id": None,
            "points": d["points"], "wins": d.get("wins", 0),
        } for d in (real.get("drivers") or [])],
        "constructors": [{
            "pos": c.get("pos"), "name": c["name"],
            "points": c["points"], "wins": c.get("wins", 0),
        } for c in (real.get("constructors") or [])],
    }


def start_winter_break_round(career, rng=None):
    """Open a winter-break round: bot teams make their offers for
    this round (one round at a time, like humans — no more
    insta-signing the best drivers), and bot teams do last-minute
    regulation R&D (adapt, then research) while the change is
    pending. Human offers are submitted by the players themselves."""
    rng = rng or random
    for team in career["teams"]:
        if team["is_ai"]:
            bot_winter_offers(career, team, rng=rng)
            bot_winter_rd(career, team)
            # v0.6: bot relation proposals / renewals / caps / WANTED.
            bot_winter_relations(career, team, rng=rng)


def _bot_offer_bribe(career, team, pick):
    """v0.8.0: a bot bribes a happy driver up to the want, never an
    unhappy one (the want is $0 then — bribing an unhappy driver is
    wasted). Returns the bribe $M (0 when none), only when the team
    keeps at least $1M after paying it."""
    want = bribe_want_for(career, team["id"], pick)
    if want <= 0:
        return 0.0
    if team["money"] - want < 1.0:
        return 0.0
    return want


def bot_winter_offers(career, team, rng=None):
    """v0.4 bot parity: a bot team's winter-break offers for one
    round — up to 3 offers (max 2 main, max 1 T&R), from the best
    available drivers by reputation. A team with an empty main seat
    MUST offer (main-seat offers are mandatory then); a full team
    may still offer to poach or to improve. T&R offers prefer young,
    cheap talent. Offers are stored in career["wb_offers"] by team
    id, in the order the team made them (main seats first)."""
    rng = rng or random
    offers = []
    # v0.8.0: bots cannot hold secret meetings, so they cannot poach
    # another team's academy junior (a meeting gates every poach) —
    # academy juniors of other teams are out of the bot pool.
    pool = [d for d in career["drivers"]
            if d["status"] in ("free", "f2")
            and not (d.get("academy_team_id")
                     and d["academy_team_id"] != team["id"])]
    # Main seats: offer for each empty seat, best available first.
    # A bot with a full lineup may still try to poach one better
    # driver for its weaker seat — only if it can afford the break
    # fee of the driver it would replace.
    seats = empty_seats(career, team)
    if not seats:
        # Full lineup: consider one poach/improvement offer for the
        # weaker main driver, only when a clearly better driver is
        # available and the team can afford both break fees.
        drivers = team_drivers(career, team)
        if drivers and pool:
            weakest = min(drivers, key=lambda d: d["rep"])
            best = max(pool, key=lambda d: d["rep"])
            if best["rep"] > weakest["rep"] + 0.5:
                seat = 1 if team["driver1_id"] == weakest["id"] else 2
                quote = sign_cost(career, team["id"], best["id"], ("seat", seat))
                if quote and quote["affordable"]:
                    seats = [seat]
    for seat in seats[:2]:
        if not pool:
            break
        pool.sort(key=lambda d: (-d["rep"], d["salary"], d["name"]))
        pick = pool[0]
        # v0.6: a junior team prefers a WANTED driver when offering
        # (the reward is a cash bounty — see _wanted_pick).
        wanted = _wanted_pick(career, team, pool)
        if wanted is not None:
            pick = wanted
        quote = sign_cost(career, team["id"], pick["id"], ("seat", seat))
        if quote is None or not quote["affordable"]:
            # The best candidate is unaffordable (break fee): try the
            # next-cheapest available driver instead.
            affordable = [d for d in pool
                          if (sign_cost(career, team["id"], d["id"], ("seat", seat)) or {})
                          .get("affordable")]
            if not affordable:
                continue
            pick = affordable[0]
        offers.append({"driver_id": pick["id"], "target": "seat", "seat": seat,
                       "years": ai_contract_years(pick, rng=rng),
                       "bribe": _bot_offer_bribe(career, team, pick)})
        pool.remove(pick)
    # T&R: one offer, youngest cheapest available. v0.4.2: no hard
    # cap any more — a bot may offer a 3rd (or further) T&R driver
    # only when it has plenty of cash: its bank, after paying a full
    # season of the fine the extra driver would add, must still be
    # over $50M. v0.7.1: the fine counts FIT T&R drivers — an ill
    # driver sitting in T&R while they recover does not count.
    trs = active_tr_drivers(career, team)
    # v0.8.1: only ~40% of bot teams plan a T&R signing in a given
    # season (see _ai_season_plan) — same plan as the mid-season
    # signing, so a team that skipped T&R all season does not
    # suddenly sign one in the winter break.
    if pool and _ai_season_plan(career, team, "tr"):
        young = [d for d in pool if d["age"] <= 25]
        if young:
            # v0.6: a junior team prefers a WANTED driver as a T&R
            # signing too (same cash-bounty preference as above).
            # v0.7.3: otherwise the pick is probability-weighted by
            # reputation (see _rep_weighted_pick) — bots prefer the
            # high-rep / top-of-F2 drivers, not the cheapest
            # youngster.
            pick = _wanted_pick(career, team, young) \
                or _rep_weighted_pick(young, rng=rng)
            fine_after = extra_driver_fine(len(trs) + 1, TR_FINE_FREE_COUNT)
            affordable = (len(trs) < TR_FINE_FREE_COUNT
                          or team["money"] - 12 * fine_after > 50)
            if affordable:
                quote = sign_cost(career, team["id"], pick["id"], ("tr", None))
                if quote and quote["affordable"]:
                    offers.append({"driver_id": pick["id"], "target": "tr", "seat": None,
                                   "years": ai_contract_years(pick, rng=rng),
                                   "bribe": _bot_offer_bribe(career, team, pick)})
    career.setdefault("wb_offers", {})[team["id"]] = offers


def bot_winter_rd(career, team):
    """v0.4 bot parity: last-minute regulation R&D during the winter
    break. Adapt first (up to what the team can afford, keeping a
    $1M buffer), then put a tenth of what is left into research once
    fully adapted."""
    if not team.get("reg_pending"):
        return
    if team["money"] is None:
        return
    unadapted = float(team.get("reg_unadapted") or 0.0)
    if unadapted > 0:
        budget = max(0.0, team["money"] - 1.0)
        points = min(unadapted, budget / REG_ADAPT_COST_PER_POINT)
        if points > 0:
            do_reg_adapt(career, team["id"], points=points)
    if float(team.get("reg_unadapted") or 0.0) <= 0:
        leftover = max(0.0, team["money"] - 1.0)
        if leftover > 5:
            do_reg_research(career, team["id"], round(leftover * 0.10, 2))


def submit_winter_offers(career, team_id, offers):
    """A human team submits its offers for the current winter-break
    round. offers is a list of {driver_id, target: 'seat'|'tr',
    seat: 1|2|null, years: 1-4, bribe?: $M}. At most 3 offers, at
    most 2 for main seats, at most 1 for a T&R slot. A team with an
    empty main seat MUST include at least one main-seat offer.
    v0.8.0: each offer may carry an optional bribe — an upfront
    signing cost paid to the driver if they sign (see bribe_want).
    A bribe must be >= 0 and the round's bribes together must fit
    the team's money. Returns (ok, message)."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    if career.get("phase") != "winter_break":
        return False, "Not in winter break."
    if not isinstance(offers, list):
        return False, "Offers must be a list."
    if len(offers) > 3:
        return False, "At most 3 offers per round."
    seats_seen = []
    tr_seen = 0
    seen_drivers = set()
    cleaned = []
    total_bribe = 0.0
    total_poach = 0.0
    for off in offers:
        if not isinstance(off, dict):
            return False, "Each offer needs a driver and a target."
        d = get_driver(career, off.get("driver_id"))
        if not d or d["status"] not in ("free", "f2"):
            return False, "That driver is not available for offers."
        if d["id"] in seen_drivers:
            return False, f"{d['name']} already has an offer from you this round."
        seen_drivers.add(d["id"])
        try:
            years = int(off.get("years", 2))
        except (TypeError, ValueError):
            return False, "Contract length must be 1–4 years."
        if not 1 <= years <= 4:
            return False, "Contract length must be 1–4 years."
        # v0.8.0: the optional bribe on an offer — an upfront signing
        # cost, paid only if the driver signs with this team.
        try:
            bribe = float(off.get("bribe") or 0.0)
        except (TypeError, ValueError):
            return False, "The bribe must be a number."
        if bribe < 0:
            return False, "The bribe cannot be negative."
        total_bribe += bribe
        if total_bribe > team["money"]:
            return False, (f"Not enough money for ${total_bribe:.2f}M of "
                           f"bribes — you have ${team['money']:.2f}M.")
        # v0.8.0: poaching an academy junior of another team needs
        # an agreed secret meeting for that target — the winter
        # break is the only window where academy juniors can be
        # poached, and the meeting gates it like a mid-season
        # poach. The poach fee + the meeting's bribe must fit the
        # team's money alongside this round's offer bribes.
        if is_poach(career, team_id, d):
            tgt = None
            if off.get("target") == "seat" and off.get("seat") in (1, 2):
                tgt = ("seat", off.get("seat"))
            elif off.get("target") == "tr":
                tgt = ("tr", None)
            if tgt is not None:
                meeting = agreed_meeting(career, team_id, d["id"], tgt)
                if meeting is None:
                    return False, (f"You need a secret meeting with {d['name']} "
                                   f"before you can offer them — request one "
                                   f"from the Driver Market.")
                q = sign_cost(career, team_id, d["id"], tgt)
                meeting_bribe = float(meeting.get("bribe") or 0.0)
                poach_fee = (q or {}).get("poach_fee", 0.0)
                if (total_bribe + total_poach + poach_fee + meeting_bribe
                        > team["money"]):
                    return False, (f"Not enough money to poach {d['name']} — "
                                   f"the poach fee (${poach_fee:.2f}M) + the "
                                   f"meeting bribe (${meeting_bribe:.2f}M) + "
                                   f"this round's offer bribes "
                                   f"(${total_bribe:.2f}M) exceed your "
                                   f"${team['money']:.2f}M.")
                total_poach += poach_fee + meeting_bribe
        target = off.get("target")
        if target == "seat":
            seat = off.get("seat")
            if seat not in (1, 2):
                return False, "A main-seat offer needs seat 1 or 2."
            if seat in seats_seen:
                return False, f"Seat {seat} already has an offer this round."
            seats_seen.append(seat)
            cleaned.append({"driver_id": d["id"], "target": "seat",
                            "seat": seat, "years": years, "bribe": bribe})
        elif target == "tr":
            if tr_seen >= 1:
                return False, "At most one T&R offer per round."
            tr_seen += 1
            cleaned.append({"driver_id": d["id"], "target": "tr",
                            "seat": None, "years": years, "bribe": bribe})
        else:
            return False, "An offer must target a main seat or a T&R slot."
    if empty_seats(career, team) and not seats_seen:
        return False, "Your lineup is not full — offer at least one driver a main seat."
    career.setdefault("wb_offers", {})[team["id"]] = cleaned
    return True, f"Offers submitted ({len(cleaned)}) ✓"


def winter_next(career, team_id, rng=None):
    """A team hits NEXT for the current winter-break round. When
    every team (human and bot) has hit NEXT, the round's offers
    resolve and the next round opens (or the break ends after round
    3). Returns (ok, advanced, message)."""
    team = get_team(career, team_id)
    if not team:
        return False, False, "No such team."
    if career.get("phase") != "winter_break":
        return False, False, "Not in winter break."
    if empty_seats(career, team):
        mine = (career.get("wb_offers") or {}).get(team["id"]) or []
        if not any(o.get("target") == "seat" for o in mine):
            return False, False, "Your lineup is not full — offer at least one driver a main seat."
    if team["id"] not in (career.get("wb_ready") or []):
        career.setdefault("wb_ready", []).append(team["id"])
    # Only human teams need to hit NEXT — bot teams' offers are
    # already submitted when the round opens (start_winter_break_round).
    human_ids = [t["id"] for t in human_teams(career)]
    if not all(tid in (career.get("wb_ready") or []) for tid in human_ids):
        n = len([tid for tid in (career.get("wb_ready") or []) if tid in human_ids])
        return True, False, f"Winter break: waiting for the other teams ({n}/{len(human_ids)}) ✓"
    # Everyone is ready: resolve this round.
    resolve_winter_break_round(career, rng=rng)
    # v0.6.6: a resolved winter-break round counts as a round for
    # pending contract-break notices — the tick happens here, once
    # per winter-break round (even the last one, before the season
    # starts).
    _tick_relation_notices(career)
    # v0.8.0: a winter-break round counts as a round for the
    # secret-meeting cooldowns too.
    tick_meeting_cooldowns(career)
    if career.get("wb_round", 1) >= WINTER_BREAK_ROUNDS:
        autofill_winter_break(career, rng=rng)
        career["wb_done"] = True
        return True, True, "Winter break complete — the new season is ready to start."
    career["wb_round"] = career.get("wb_round", 1) + 1
    career["wb_ready"] = []
    start_winter_break_round(career, rng=rng)
    return True, True, f"Winter-break round {career['wb_round']} — make your offers."


def _wb_offer_winners(career, offers_by_team, exclude=None):
    """v0.4.2: for each driver with winter-break offers, the team
    they sign with — the lowest EFFECTIVE constructors' position,
    where an academy driver's own academy team counts as
    ACADEMY_LOYALTY_BOOST (3) places higher. Ties go to the academy
    team first, then to the better real constructors' position.
    So a 5th-place academy team (effective 2nd, academy) beats a
    2nd-place rival (effective 2nd, not academy) for its own junior,
    and a 1st-place academy team always wins its junior. A driver
    with no academy affiliation just goes to the best-placed team
    that offered them. Returns {driver_id: team_id}.
    v0.8.0: a bribe that meets the driver's want counts as one
    constructors' place better (a sweetener that can flip a close
    call). exclude is a set of (driver_id, team_id) pairs to skip —
    used when a driver declines their old team's re-signing offer
    and the next-best offer wins instead."""
    order = wb_constructors_order(career)
    pos = {tid: i + 1 for i, tid in enumerate(order)}
    fallback = len(order) + 1
    winners = {}
    exclude = exclude or set()
    for team_id, offers in (offers_by_team or {}).items():
        for off in offers:
            did = off.get("driver_id")
            if (did, team_id) in exclude:
                continue
            d = get_driver(career, did)
            if not d:
                continue
            real = pos.get(team_id, fallback)
            # v0.7.0: the QUIET DROP — a team with a low hidden
            # reputation counts as a few constructors' places lower
            # when a driver picks between offers (rep 50+ drops 0,
            # 35 drops 1, 20 drops 2, 5 drops 3). The drop bends only
            # this ranking: the stored constructors' order is never
            # touched, and the academy loyalty boost still applies
            # on top of the dropped position.
            eff = real + wb_rep_drop(team_rep_of(get_team(career, team_id)))
            is_academy_team = d.get("academy_team_id") == team_id
            if is_academy_team:
                eff = max(1, eff - ACADEMY_LOYALTY_BOOST)
            # v0.8.0: a bribe that meets the driver's want counts as
            # one place better (the driver weighs the sweetener).
            want = bribe_want_for(career, team_id, d)
            if want > 0 and float(off.get("bribe") or 0.0) >= want:
                eff = max(1, eff - 1)
            key = (eff, 0 if is_academy_team else 1, real)
            if did not in winners or key < winners[did][0]:
                winners[did] = (key, team_id)
    return {did: tid for did, (_key, tid) in winners.items()}


def resolve_winter_break_round(career, rng=None):
    """Resolve one winter-break round: process teams in last
    season's final constructors' order (champion first); a driver
    accepts the first offer they receive (from the best-placed team
    offering them). A team's main-seat offers are processed before
    its T&R offer. Signing is free (salary only); the break fee of
    the driver being replaced is charged, as in sign_driver().
    v0.4.2: academy loyalty — a driver with offers from their
    academy team and from rivals signs with the academy team when
    the boost puts it ahead (see _wb_offer_winners)."""
    rng = rng or random
    offers_by_team = career.get("wb_offers") or {}
    winners = _wb_offer_winners(career, offers_by_team)
    # v0.8.0: re-signing declines — an unhappy driver mostly turns
    # down their old team's offer (70% unhappy, 90% very unhappy).
    # A declined driver falls to the next-best offer (or stays free
    # this round when nobody else offered). The roll happens once
    # per driver per round, before the signings run; a decline is
    # logged in Transfers and in Media.
    for did, team_id in list(winners.items()):
        d = get_driver(career, did)
        if not d or d.get("last_team_id") != team_id:
            continue  # not a re-sign — nothing to decline
        lvl = rules_mod.happiness_level(happiness_with(d, team_id))
        if lvl >= 3:
            continue
        p = RESIGN_DECLINE_VERY_UNHAPPY if lvl == 1 \
            else RESIGN_DECLINE_UNHAPPY
        if rng.random() < p:
            team = get_team(career, team_id)
            team_name = team["name"] if team else "their old team"
            log_transfer(career, 
                f"{d['name']} turned down {team_name}'s offer")
            add_media_entry(
                career,
                f"{d['name']} has rejected a new deal at {team_name} — "
                f"the driver is understood to be unhappy there.",
                "transfer")
            winners = _wb_offer_winners(
                career, offers_by_team, exclude={(did, team_id)})
    taken = set()
    for team_id in wb_constructors_order(career):
        team = get_team(career, team_id)
        if not team:
            continue
        mine = list(offers_by_team.get(team_id) or [])
        # Main-seat offers first, then the T&R offer.
        mine.sort(key=lambda o: 0 if o.get("target") == "seat" else 1)
        for off in mine:
            if off["driver_id"] in taken:
                continue
            if winners.get(off["driver_id"]) != team_id:
                continue  # another team wins this driver (loyalty / order)
            d = get_driver(career, off["driver_id"])
            if not d or d["status"] not in ("free", "f2"):
                continue
            # v0.8.0: a winter-break academy poach pays the academy
            # poach fee + the meeting's bribe BEFORE signing (the
            # seat break fee is charged inside sign_driver_wb, as
            # usual). If the team can no longer afford them, the
            # signing is skipped this round — like a signing refused
            # for an unaffordable seat fee.
            academy_team = None
            meeting = None
            if d.get("academy_team_id") and d["academy_team_id"] != team["id"]:
                academy_team = get_team(career, d["academy_team_id"])
                tgt = ("seat", off["seat"]) if off["target"] == "seat" else ("tr", None)
                meeting = agreed_meeting(career, team["id"], d["id"], tgt)
                q = sign_cost(career, team["id"], d["id"], tgt)
                poach_fee = (q or {}).get("poach_fee", 0.0)
                meeting_bribe = float((meeting or {}).get("bribe") or 0.0)
                if team["money"] < poach_fee + meeting_bribe:
                    continue
                team["money"] = round(team["money"] - poach_fee - meeting_bribe, 2)
            if off["target"] == "seat":
                slot = "driver1_id" if off["seat"] == 1 else "driver2_id"
                if team[slot]:
                    continue  # seat already filled by an earlier offer
                ok, _why = sign_driver_wb(career, team, d, off["seat"], off["years"])
            else:
                ok, _why = sign_tr_driver_wb(career, team, d, off["years"])
            if ok:
                taken.add(d["id"])
                # v0.8.0: the agreed meeting is consumed and the
                # losing academy team is notified in three places —
                # the Transfers feed, a Media entry, and a notice in
                # their Drivers tab — like a mid-season poach.
                if academy_team is not None:
                    if meeting is not None:
                        career["meetings"] = [
                            m for m in career.get("meetings") or []
                            if m is not meeting]
                    log_transfer(career, 
                        f"{d['name']} left {academy_team['name']}'s academy "
                        f"(poached by {team['name']})")
                    add_media_entry(
                        career,
                        f"{d['name']} has left {academy_team['name']} for "
                        f"{team['name']} — a secret meeting sealed the move.",
                        "transfer")
                    add_team_notice(
                        career, academy_team["id"],
                        f"{d['name']} left your team for {team['name']}.")
                # v0.8.0: the bribe on the winning offer is paid on
                # signing — if the team can still afford it after the
                # break fee (the submit check already capped the
                # round's bribes at the team's money).
                bribe = float(off.get("bribe") or 0.0)
                if bribe > 0 and team["money"] >= bribe:
                    team["money"] = round(team["money"] - bribe, 2)
    career["wb_offers"] = {}


def sign_driver_wb(career, team, d, seat, contract_years):
    """Sign a driver into a main seat during the winter break — the
    same charges as sign_driver(), but allowed in winter break (the
    direct sign_driver() is blocked then so signings go through
    offers). The break fee for the seat's current occupant is
    charged from the winter-break formula (contract x 12 rounds).
    v0.8.0: an academy poach fee + the meeting's bribe are charged
    by resolve_winter_break_round before this is called (see there)."""
    slot = "driver1_id" if seat == 1 else "driver2_id"
    current = get_driver(career, team[slot]) if team[slot] else None
    fee = 0.0
    if current:
        fee = break_fee_for_driver(career, current)
    if fee > team["money"]:
        return False, f"Need ${fee:.2f}M to break {current['name']}'s contract."
    team["money"] -= fee
    if current:
        _release_driver(career, current, team)
        current["status"] = "free"
        log_transfer(career, 
            f"{current['name']} left {team['name']} (contract broken)")
    _sign_driver_common(career, team, d, contract_years)
    d["status"] = "f1"
    d["team_id"] = team["id"]
    team[slot] = d["id"]
    _recompute_team_points(career, team)
    log_transfer(career, f"{d['name']} signed for {team['name']}")
    # v0.6: a WANTED signing pays the parent's reward to the junior team.
    _pay_wanted_reward(career, team, d)
    return True, "ok"


def sign_tr_driver_wb(career, team, d, contract_years):
    """Sign a driver as a T&R driver during the winter break — the
    same as sign_tr_driver(), but allowed in winter break."""
    _sign_driver_common(career, team, d, contract_years, as_tr=True)
    d["status"] = "tr"
    d["team_id"] = team["id"]
    log_transfer(career, 
        f"{d['name']} signed for {team['name']} as Test & Reserve")
    # v0.6: a WANTED signing pays the parent's reward to the junior team.
    _pay_wanted_reward(career, team, d)
    return True, "ok"


def autofill_winter_break(career, rng=None):
    """After the 3 winter-break rounds, any team still with an empty
    main seat gets the bot manager temporarily kicking in:
      - if the team has a T&R driver, PROMOTE the highest-reputation
        one to the main seat;
      - otherwise sign one of the five worst-reputation free agents
        (picked at random among the five lowest-reputation free
        agents), on a 1-year contract.
    This is the user-approved fallback (2026-11-11) — no team starts
    a season with an empty seat."""
    rng = rng or random
    for team in career["teams"]:
        for seat in empty_seats(career, team):
            # v0.7.1: only FIT T&R drivers — an ill driver recovering
            # in T&R is never promoted (the guard in
            # promote_tr_to_main would refuse them anyway, but that
            # would leave the seat empty here).
            trs = active_tr_drivers(career, team)
            if trs:
                trs.sort(key=lambda x: (-x["rep"], x["name"]))
                promote_tr_to_main(career, team["id"], trs[0]["id"], seat)
                continue
            free = [d for d in career["drivers"] if d["status"] == "free"]
            if not free:
                continue
            free.sort(key=lambda d: (d["rep"], d["salary"], d["name"]))
            worst_five = free[:5]
            pick = rng.choice(worst_five)
            _sign_driver_common(career, team, pick, 1)
            pick["status"] = "f1"
            pick["team_id"] = team["id"]
            slot = "driver1_id" if seat == 1 else "driver2_id"
            team[slot] = pick["id"]
            _recompute_team_points(career, team)
            log_transfer(career, 
                f"{pick['name']} signed for {team['name']} (winter-break auto-fill)")
            # v0.6: even an auto-fill signing of a WANTED driver pays
            # the parent's reward to the junior team.
            _pay_wanted_reward(career, team, pick)


def winter_break_ready(career):
    """Every team has both main seats filled (used by the UI to show
    whether the season can start)."""
    for team in career["teams"]:
        if empty_seats(career, team):
            return False
    return True


def start_next_season(career, rng=None):
    """Leave winter break once the 3 rounds are done and every team
    has both main seats filled. Applies the pending regulation change
    (v0.4: adapted perf + research gains, from the state built up
    since round 7 and during the winter break). A migrated v5 save
    has no pending reg change — its change was already applied under
    v0.3 — so nothing is applied for it."""
    rng = rng or random
    if career.get("phase") != "winter_break":
        return False, "Not in winter break."
    if not career.get("wb_done"):
        return False, "Finish the winter break first (3 rounds of offers)."
    if not winter_break_ready(career):
        return False, "Fill every empty seat on your team first."
    # v0.6: relations left at 0 years after the winter break lapse —
    # they were due for renewal and nobody renewed them. Pending
    # relation proposals expire with the break. v0.6.6: a relation
    # with a pending break notice does NOT lapse here — the notice
    # is already counting the break down, and it keeps ticking into
    # the new season (the break takes effect when the counter hits
    # 0, in complete_race_weekend). The research cap is gone (v0.6.6)
    # so there is no per-season research-spend reset any more; the
    # junior share's season totals reset below.
    # v0.6.6: pending donation requests expire with the break too.
    for team in career["teams"]:
        if (team.get("sup_stage", 0) > 0
                and (team.get("relation_years") or 0) <= 0
                and not team.get("break_notice")):
            supplier = supplier_of(career, team)
            _end_relation(career, team)
            log_transfer(career, 
                f"{team['name']}'s deal with "
                f"{supplier['name'] if supplier else 'its supplier'} "
                f"lapsed (contract expired, not renewed)")
        team["junior_share_season"] = 0.0
        team["junior_share_received_season"] = 0.0
        team["junior_share_last_race"] = 0.0
        team["junior_share_received_last_race"] = 0.0
    career["relation_proposals"] = []
    # v0.4: apply the pending reg change now — the winter break's
    # last-minute R&D is included because it ran before this point.
    pending = any(t.get("reg_pending") for t in career["teams"])
    if pending:
        kind = career.get("pending_reg_kind") or career.get("next_reg_kind") or "Minor"
        target = REG_MAJOR_TARGET if kind == "Major" else REG_MINOR_TARGET
        changes = []
        for team in career["teams"]:
            old = team["perf"]
            if team.get("reg_pending"):
                team["perf"] = reg_change_v2(
                    team.get("reg_adapted", 0.0),
                    team.get("reg_research", 0.0), target, rng=rng)
            team["reg_research"] = 0.0
            team["reg_total"] = 0.0
            team["reg_adapted"] = 0.0
            team["reg_unadapted"] = 0.0
            team["reg_pending"] = False
            changes.append({"team": team["name"], "old": round(old, 2),
                            "new": team["perf"]})
        career["last_reg_change"] = {
            "year": career.get("pending_reg_year") or career["year"],
            "kind": kind, "changes": changes}
    career["pending_reg_year"] = None
    career["pending_reg_kind"] = None
    career["round"] = 1
    career["phase"] = "round"
    career["advanced"] = []
    career["results_vote"] = []
    career["results_stage"] = None
    career["transfers"] = []
    career["retired_this_season"] = []
    career["wb_round"] = 0
    career["wb_offers"] = {}
    career["wb_ready"] = []
    career["wb_done"] = False
    set_circuit_for_round(career)
    # v0.7.1: the new season's round 1 opens — roll illness for it
    # (no driver carries an illness across the rollover: end_of_season
    # restored every ill driver first).
    open_new_round(career, rng=rng)
    return True, "ok"


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def retirement_rumours(career):
    """isRetiring(isRumour=True) for every driver who could retire,
    as in Main.py's rumours. v0.7.2: the same reworked formula as
    the rollover — chance from 30, gentler, and lower when the
    driver is performing well, in a good car, highly reputed, or on
    a team going up. Trajectory is last season's final order
    (prev_team_order) vs the current standings; there is no maximum
    age. v0.7.3: the list covers main drivers, T&R drivers, academy
    juniors and F2 drivers — grouped by category — and a driver at
    0% chance is HIDDEN (with the v0.7.2 formula, drivers under 30
    are 0%, so the list stays short). Each row carries its category
    and the driver's gender, so the UI can group and render it."""
    prev_order = career.get("prev_team_order") or [
        t["id"] for t in (career.get("last_season") or {}).get("teams") or []]
    old_pos = {tid: i + 1 for i, tid in enumerate(prev_order)}
    top_perf = max((t.get("perf", 0.0) or 0.0 for t in career["teams"]),
                   default=0.0)
    n_teams = len(career["teams"])

    def chance_for(d, team, points, wins):
        pos = constructor_pos(career, team["id"]) if team else n_teams
        traj = (old_pos.get(team["id"], pos) - pos) if team else 0
        return retirement_chance(
            d["age"], d["cha"], pos, points,
            team["points"] if team else 0,
            rep=d.get("rep", 2.5), wins=wins or 0, trajectory=traj,
            perf_gap=(top_perf - team["perf"]) if team else 0.0)

    def row(d, team, category, points, wins):
        chance = chance_for(d, team, points, wins)
        if chance <= 0:
            return None
        return {"driver": d["name"],
                "team": team["name"] if team else "",
                "age": d["age"], "chance": round(chance, 1),
                "category": category,
                "gender": d.get("gender", "M")}

    out = []
    # Main drivers — the full F1 standings, main seats only (as
    # before). A replaced driver who scored stays in the standings
    # but is not a main driver any more, so they are not rumoured
    # here — the free-agent roll at the rollover still covers them.
    for d in [x for x in race_mod.driver_standings(career)
              if x["status"] == "f1"]:
        team = get_team(career, d["team_id"]) if d.get("team_id") else None
        r = row(d, team, "Main", d["points"], d.get("season_wins", 0))
        if r:
            out.append(r)
    # Test & Reserve drivers — their F1 team's context; an f2_tr
    # driver reads their F1 tally, never their F2 points.
    for team in career["teams"]:
        for d in team_tr_drivers(career, team):
            tally = race_mod.f1_tally(d)
            r = row(d, team, "Test & Reserve", tally["points"], tally["wins"])
            if r:
                out.append(r)
    # Academy juniors — their academy team's context; they have no
    # F1 points unless they raced in F1 (f1_races > 0).
    for team in career["teams"]:
        for d in team_academy_drivers(career, team):
            tally = race_mod.f1_tally(d) if d.get("f1_races", 0) > 0 else None
            r = row(d, team, "Academy",
                    tally["points"] if tally else 0,
                    tally["wins"] if tally else 0)
            if r:
                out.append(r)
    # F2 drivers — no F1 team (their academy team, if any, is shown
    # for context); they have no F1 points unless they raced in F1.
    # A driver already listed above is skipped: an f2_tr driver is
    # under Test & Reserve, and an academy junior is under Academy.
    for d in f2_drivers(career):
        if d.get("f2_tr") or d.get("academy_team_id"):
            continue
        team = None
        tally = race_mod.f1_tally(d) if d.get("f1_races", 0) > 0 else None
        r = row(d, team, "F2",
                tally["points"] if tally else 0,
                tally["wins"] if tally else 0)
        if r:
            out.append(r)
    # Grouped by category (Main, Test & Reserve, Academy, F2), then
    # highest chance first within each group.
    order = {"Main": 0, "Test & Reserve": 1, "Academy": 2, "F2": 3}
    return sorted(out, key=lambda r: (order.get(r["category"], 9),
                                      -r["chance"]))


def finances_view(career, team):
    pos = constructor_pos(career, team["id"])
    est = finance_estimates(pos)
    drivers = team_drivers(career, team)
    # v0.4: T&R drivers are paid their full salary too — list them
    # separately so the manager can see the cost of the affiliation.
    tr = team_tr_drivers(career, team)
    # v0.4.2: the academy (1/3 salary each) and the exponential
    # fines, all per race — so the manager sees them before signing.
    academy = team_academy_drivers(career, team)
    charges = extra_charges_per_race(career, team)
    # v0.6.4: the per-race NET — what the team actually gains or
    # loses each race, as one signed number: the per-race gains
    # (EPRG, paid after each race) minus the per-race salary cost
    # minus the per-race extra charges (T&R fine, academy stipends,
    # academy fine). Positive = profit, negative = loss. This is the
    # figure Matthew asked for ("am I in the positives or negatives")
    # after signing too many T&R drivers and watching the fines eat
    # the prize money. It uses only figures this tab already
    # computes — no new accounting. It is computed from the same
    # values the tab displays (the rounded per-race salary and the
    # extra charges as shown), so the numbers the manager sees
    # always add up — a net computed from the unrounded salary
    # could differ by a cent from the displayed components. The
    # extra charges themselves stay unrounded in the payload, so
    # they still equal the sum of their components.
    salary = round(salary_per_round(career, team), 2)
    net_per_race = round(est["eprg"] - salary - charges["total"], 2)
    # v0.6.6: the junior income share — a junior team pays 15% of
    # its race prize to its parent after each race; a parent receives
    # it from its junior. The last-race and season figures are the
    # share's record (the Finances tab shows them as their own
    # lines). The share is NOT part of net_per_race: the net is an
    # EPRG-based estimate and the race prize it is a share of is not
    # in the net either.
    junior = junior_team_of(career, team)
    return {
        "money": round(team["money"], 2),
        "constructor_pos": pos,
        "epsci": est["epsci"],
        "eprg": est["eprg"],
        "net_per_race": net_per_race,
        "junior_share_last_race": round(
            float(team.get("junior_share_last_race") or 0.0), 2),
        "junior_share_season": round(
            float(team.get("junior_share_season") or 0.0), 2),
        "junior_share_received_last_race": round(
            float(team.get("junior_share_received_last_race") or 0.0), 2),
        "junior_share_received_season": round(
            float(team.get("junior_share_received_season") or 0.0), 2),
        "junior_team_name": junior["name"] if junior else None,
        "salary_per_race": salary,
        "driver_costs": [{"driver": d["name"],
                          "per_race": round(d["salary"] / SEASON_LENGTH, 2)}
                         for d in drivers],
        "tr_costs": [{"driver": d["name"],
                      "per_race": round(d["salary"] / SEASON_LENGTH, 2)}
                     for d in tr],
        "tr_fine_per_race": charges["tr_fine"],
        "academy_costs": [{"driver": d["name"],
                           "per_race": round(d["salary"] / 3.0 / SEASON_LENGTH, 2)}
                          for d in academy],
        "academy_cost_per_race": charges["academy_cost"],
        "academy_fine_per_race": charges["academy_fine"],
        "extra_charges_per_race": charges["total"],
    }


# ---------------------------------------------------------------------------
# v0.7.4: Notes — per-team post-it notes on the dashboard — and the
# cheats menu (car perf / money / driver rating), server-authoritative.
# ---------------------------------------------------------------------------

NOTE_MAX_COUNT = 15   # notes per team
NOTE_MAX_CHARS = 200  # characters per note (Matthew: "character limit
                      # per note", no number — 200 is Max's call)


def save_note(career, team_id, text, note_id=None):
    """Add a note (note_id None) or edit one (note_id set). Notes are
    per-team and private — the server only ever sends a team's notes
    to that team (public_state strips them; my_team_public carries
    them). Limits are enforced here as well as in the client."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    text = (text or "").strip()
    if not text:
        return False, "Write something first."
    if len(text) > NOTE_MAX_CHARS:
        return False, (f"Notes are at most {NOTE_MAX_CHARS} characters "
                       f"({len(text)} typed).")
    notes = team.setdefault("notes", [])
    if note_id is None:
        if len(notes) >= NOTE_MAX_COUNT:
            return False, (f"Only {NOTE_MAX_COUNT} notes per team — "
                           "delete one first.")
        nid = max((n.get("id", 0) for n in notes), default=0) + 1
        notes.append({"id": nid, "text": text})
        return True, "Note added ✓"
    for n in notes:
        if n.get("id") == note_id:
            n["text"] = text
            return True, "Note saved ✓"
    return False, "No such note."


def delete_note(career, team_id, note_id):
    """Delete one of the team's notes (immediately, no confirm)."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    notes = team.setdefault("notes", [])
    before = len(notes)
    notes[:] = [n for n in notes if n.get("id") != note_id]
    if len(notes) == before:
        return False, "No such note."
    return True, "Note deleted ✓"


def cheat_state(career):
    """The authoritative data for the cheats menu: every team's real
    perf and money, plus the mini drivers' and constructors'
    standings. Perf is NEVER in the broadcast state when the chart
    is off, so the menu reads it here instead of from the client.
    Driver rows also carry the rating (d["perf"]) and the min/max
    for the driver's current age, for the driver pop-up."""
    teams = sorted(career["teams"], key=lambda t: -t["perf"])
    drivers = race_mod.driver_standings(career)
    constructors = race_mod.constructor_standings(career)
    drv_rows = []
    for d in drivers:
        tm = get_team(career, d.get("team_id")) \
            or get_team(career, d.get("last_team_id"))
        lo, hi = rules_mod.rating_bounds(d.get("age") or 25)
        drv_rows.append({
            "id": d["id"], "name": d["name"],
            "team": tm["name"] if tm else "",
            "team_id": tm["id"] if tm else None,
            "colour": tm["colour"] if tm else None,
            "gender": d.get("gender", "M"),
            "points": race_mod.f1_tally(d)["points"],
            "age": d.get("age"),
            "rating": d.get("perf"),
            "min_rating": lo, "max_rating": hi,
            # v0.7.5: the retirement toggle for the driver pop-up.
            "retire_at_season_end": bool(d.get("retire_at_season_end", False)),
        })
    return {
        "custom_retirements": bool(career.get("custom_retirements", False)),
        "teams": [{"id": t["id"], "name": t["name"], "colour": t["colour"],
                   "perf": t["perf"], "money": t["money"]} for t in teams],
        "drivers": drv_rows,
        "constructors": [{"id": t["id"], "name": t["name"],
                          "colour": t["colour"], "points": t["points"]}
                         for t in constructors],
        # v0.8.0: the extra driver classes — T&R, free agents,
        # academy juniors and F2 drivers — each tappable to open the
        # same driver pop-up (rating editing + retirement toggle).
        # Rows share the mini-standings shape. Drivers already in
        # the mini standings are not repeated here.
        "extra": _cheat_extra_rows(career, {d["id"] for d in drivers}),
    }


def _cheat_extra_rows(career, standing_ids):
    """v0.8.0: the cheats-menu extra sections — T&R, free agents,
    academy juniors and F2 drivers, each as mini-standings-shaped
    rows (name, team/affiliation, age, rating, rating bounds,
    retirement toggle). Sorted by name within each section."""
    def _row(d, kind):
        tm = get_team(career, d.get("team_id")) \
            or get_team(career, d.get("academy_team_id")) \
            or get_team(career, d.get("last_team_id"))
        lo, hi = rules_mod.rating_bounds(d.get("age") or 25)
        return {
            "id": d["id"], "name": d["name"], "kind": kind,
            "team": tm["name"] if tm else "",
            "team_id": tm["id"] if tm else None,
            "colour": tm["colour"] if tm else None,
            "gender": d.get("gender", "M"),
            "points": race_mod.f1_tally(d)["points"],
            "age": d.get("age"),
            "rating": d.get("perf"),
            "min_rating": lo, "max_rating": hi,
            "retire_at_season_end": bool(d.get("retire_at_season_end", False)),
        }

    extra = {"tr": [], "free": [], "academy": [], "f2": []}
    for d in career["drivers"]:
        if d["id"] in standing_ids:
            continue
        if d.get("status") == "tr":
            extra["tr"].append(_row(d, "tr"))
        elif d.get("academy_team_id"):
            extra["academy"].append(_row(d, "academy"))
        elif d.get("status") == "free":
            extra["free"].append(_row(d, "free"))
        elif d.get("status") == "f2":
            extra["f2"].append(_row(d, "f2"))
        # Queued / pool / retired drivers are not in the game and
        # cannot be edited.
    for rows in extra.values():
        rows.sort(key=lambda r: r["name"])
    return extra


def _cheat_editable(career, driver_id):
    """v0.8.0: a driver is editable from the cheats menu when they
    are in the mini drivers' standings (the registered F1 drivers)
    OR in one of the extra classes — T&R, free agent, academy
    junior or F2 driver. Queued / pool / retired drivers are not in
    the game and cannot be edited."""
    d = get_driver(career, driver_id)
    if not d:
        return False
    if any(x["id"] == driver_id
           for x in race_mod.driver_standings(career)):
        return True
    return d.get("status") in ("tr", "free", "f2")


def cheat_adjust(career, team_id, perf_delta=None, money_delta=None):
    """Change one team's car perf and money. Perf is clamped to
    [0, 400]; money is floored at $0 — if the team is already
    negative, a negative money change is refused outright (no change
    at all), a positive one is allowed (the result may still be
    negative). No log entry: the Transfers and History tabs stay
    clean."""
    import math
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    try:
        pd = float(perf_delta if perf_delta is not None else 0)
        md = float(money_delta if money_delta is not None else 0)
    except (TypeError, ValueError):
        return False, "Perf and money changes must be numbers."
    if not (math.isfinite(pd) and math.isfinite(md)):
        return False, "Perf and money changes must be numbers."
    if pd == 0 and md == 0:
        return False, "Nothing to change."
    if md < 0 and team["money"] < 0:
        return False, (f"{team['name']} is already ${team['money']:.2f}M — "
                       "money cannot go further down, only up.")
    new_perf = min(400.0, max(0.0, team["perf"] + pd))
    new_money = team["money"] + md
    if team["money"] >= 0 and new_money < 0:
        new_money = 0.0
    team["perf"] = new_perf
    team["money"] = new_money
    return True, (f"{team['name']}: perf {new_perf:.1f}, "
                  f"money ${new_money:.2f}M ✓")


def cheat_set_rating(career, driver_id, rating):
    """Set a driver's skill rating (d["perf"]) — the cheats-menu
    driver pop-up. v0.8.0: drivers in the mini drivers' standings
    (the registered F1 drivers) AND the extra classes (T&R, free
    agents, academy juniors, F2 drivers) are editable. The new
    rating is clamped to [min(age), max(age)] for the driver's
    current age (rules.rating_bounds); no log entry."""
    d = get_driver(career, driver_id)
    if not d:
        return False, "No such driver."
    if not _cheat_editable(career, driver_id):
        return False, (f"{d['name']} cannot be edited from the cheats "
                       f"menu — only listed drivers can be edited.")
    try:
        val = int(float(rating))
    except (TypeError, ValueError):
        return False, "Rating must be a whole number."
    lo, hi = rules_mod.rating_bounds(d.get("age") or 25)
    new = max(lo, min(hi, val))
    d["perf"] = new
    note = ("" if new == val else
            f" (clamped to the age-{d.get('age')} range {lo}–{hi})")
    return True, f"{d['name']}'s rating is now {new}{note} ✓"


def _coerce_bool(value):
    """A checkbox value from the client: a real JSON bool, or a
    "true"/"false" string (defensive — bool("false") is True)."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def set_custom_retirements(career, on):
    """v0.7.5: the cheats-menu master toggle 'Custom retirements'.
    Turning it OFF clears every per-driver retirement toggle
    immediately. No log entry."""
    on = _coerce_bool(on)
    career["custom_retirements"] = on
    if not on:
        for d in career["drivers"]:
            d["retire_at_season_end"] = False
    return True, ("Custom retirements ON — only toggled drivers will "
                  "retire at season end ✓" if on else
                  "Custom retirements OFF — normal retirements resume, "
                  "all driver toggles cleared ✓")


def set_retire_toggle(career, driver_id, on):
    """v0.7.5: the per-driver 'Retire at season end' toggle in the
    driver pop-up. Only settable while the master is ON, and v0.8.0:
    drivers in the mini drivers' standings AND the extra classes
    (T&R, free agents, academy juniors, F2 drivers) have the toggle.
    No log entry."""
    d = get_driver(career, driver_id)
    if not d:
        return False, "No such driver."
    if not career.get("custom_retirements"):
        return False, ("Custom retirements are OFF — turn the master "
                       "toggle on at the top of the cheats menu first.")
    if not _cheat_editable(career, driver_id):
        return False, (f"{d['name']} cannot be toggled from the cheats "
                       f"menu — only listed drivers can be toggled.")
    on = _coerce_bool(on)
    d["retire_at_season_end"] = on
    return True, (f"{d['name']} will retire at season end ✓" if on else
                  f"{d['name']} will not retire at season end ✓")


def public_state(career, chart_on, viewer_team_id=None):
    """The state sent to a client. When the perf chart is OFF, perf
    numbers are hidden from humans — team perf and driver perf — and
    other teams' reliability and research pools are hidden too. Your
    own team's reliability STAYS visible (v0.6.3: it drives your DNF
    chance, and the dashboard shows it), and your own research pools
    stay visible (you need them to play), but never your perf.
    When the chart is ON, everything is shown."""
    state = copy.deepcopy(career)
    # v0.1: absolute lap/race times NEVER leave the server — results carry
    # gaps only, even when the chart is on. Strip them from every driver.
    for d in state["drivers"]:
        d.pop("qual_time", None)
        d.pop("race_time", None)
    # v0.3: reputation NEVER leaves the server either. Rep is only for
    # the bot teams to judge who's good — players must not see it, in
    # any payload, chart on or off. The live career keeps it (the AI
    # reads it from there); only this broadcast copy is stripped.
    for d in state["drivers"]:
        d.pop("rep", None)
        # v0.8.0: happiness and loyalty NEVER leave the server in the
        # broadcast either — a driver's own team sees their current
        # values in the private my_team message only (server.py);
        # other teams see nothing, and the Driver Market never shows
        # them (the bribe want below is computed server-side).
        d.pop("happiness", None)
        d.pop("loyalty", None)
        d.pop("happiness_by_team", None)
        d.pop("loyalty_by_team", None)
        d.pop("tuning_against_streak", None)
        d.pop("tr_bench_races", None)
        d.pop("activity_round", None)
    # v0.4: winter-break offers NEVER leave the server in full — they
    # would reveal what the bot teams (and other humans) are bidding
    # on. Each client only gets its own team's offers, attached as
    # wb_my_offers below. The wb_offers map is stripped here.
    state.pop("wb_offers", None)
    # v0.4: regulation raw numbers NEVER leave the server — a player
    # sees only the percentages and the money cost (reg_view), never
    # the true car performance value. Strip the raw fields from every
    # team; the client rebuilds the progress bar from reg_view.
    for team in state["teams"]:
        team.pop("reg_total", None)
        team.pop("reg_adapted", None)
        team.pop("reg_unadapted", None)
        # v0.7.0: hidden team reputation NEVER leaves the server —
        # no number, no hint. Players only ever see the outcomes
        # (donations, offers, proposals), never the value.
        team.pop("team_rep", None)
        # v0.7.4: per-team Notes are private — they never leave the
        # server in the broadcast. Each client only gets its OWN
        # team's notes, attached in my_team_public (server.py).
        team.pop("notes", None)
    # v0.4: replacement / signing costs for the viewer's team,
    # computed server-side from the exact formula the signing
    # functions charge, so the Driver Market always shows the number
    # that will be charged. {driver_id: {"1": {...}, "2": {...},
    # "tr": {...}}} — attached to the broadcast copy only, never
    # saved. The cost is the contract-break fee for the driver in the
    # seat (0 / Free for an empty seat, and for a T&R slot) PLUS the
    # poaching break fee when the driver is under contract with
    # another team (main driver or T&R driver). Signing itself is free.
    if viewer_team_id:
        viewer = get_team(career, viewer_team_id)
        if viewer is not None:
            # v0.4.2: the T&R entry also carries the fine that would
            # apply per race AFTER signing this driver (0 while the
            # team has fewer than 2 T&R drivers) — shown in the
            # market before signing, so the fine is never a surprise.
            def _costs_for(d):
                out = {str(seat): sign_cost(career, viewer["id"], d["id"], ("seat", seat))
                       for seat in (1, 2)}
                tr_quote = sign_cost(career, viewer["id"], d["id"], ("tr", None))
                if tr_quote is not None:
                    # v0.7.1: the fine counts FIT T&R drivers — an
                    # ill driver sitting in T&R while they recover
                    # does not count toward it.
                    tr_quote["fine_after"] = extra_driver_fine(
                        len(active_tr_drivers(career, viewer)) + 1, TR_FINE_FREE_COUNT)
                out["tr"] = tr_quote
                # v0.8.0: the bribe this driver wants from the viewer
                # (computed server-side from their hidden happiness /
                # loyalty — the market shows the want, never the
                # happiness behind it).
                out["bribe_want"] = bribe_want_for(career, viewer["id"], d)
                return out
            state["market_costs"] = {
                d["id"]: _costs_for(d)
                for d in career["drivers"]
                if d["status"] in ("free", "f2", "f1", "tr")
                and not (d.get("team_id") == viewer["id"])}
            # v0.8.0: the viewer's own secret meetings (agreed) and
            # cooldowns, keyed by driver id — other teams' meetings
            # are secret and never leave the server.
            state["my_meetings"] = {
                m["driver_id"]: {"target": m.get("target"),
                                "seat": m.get("seat"),
                                "bribe": m.get("bribe", 0.0)}
                for m in career.get("meetings") or []
                if m.get("requester_team_id") == viewer["id"]}
            state["my_cooldowns"] = {
                key.split(":", 1)[1]: rounds
                for key, rounds in (career.get("meeting_cooldowns") or {}).items()
                if key.split(":", 1)[0] == viewer["id"]}
            # v0.4: the viewer's own reg progress (percentages + cost
            # only) and their own winter-break offers for this round.
            state["reg_view"] = reg_view(career, viewer)
            state["wb_my_offers"] = list(
                (career.get("wb_offers") or {}).get(viewer["id"]) or [])
            state["wb_status"] = {
                "round": career.get("wb_round", 0),
                "total_rounds": WINTER_BREAK_ROUNDS,
                "ready": list(career.get("wb_ready") or []),
                "done": bool(career.get("wb_done")),
            }
    # v0.4.1: the winter-break review — last season's final driver
    # standings (top 5), constructors, and the head-to-head of the
    # viewer's team's two main drivers last season. Built from the
    # last_season snapshot, so it is there for a migrated save too.
    if career.get("phase") == "winter_break":
        state["wb_review"] = winter_break_review(career, viewer_team_id)
        # v0.7.0: the season recap panel at the top of the winter
        # break — top 8 drivers, top 5 constructors, 12 round
        # winners of the season just finished.
        state["wb_recap"] = winter_break_recap(career)
    if chart_on:
        return state
    for team in state["teams"]:
        team.pop("perf", None)
        # v0.6.3: your own car's reliability IS visible (it drives your
        # DNF chance, and the dashboard shows it) — other teams'
        # reliability stays hidden, and perf stays hidden for everyone.
        if team["id"] != viewer_team_id:
            team.pop("rel", None)
            team.pop("perf_res", None)
            team.pop("reli_res", None)
            team.pop("reg_research", None)
    for d in state["drivers"]:
        d.pop("perf", None)
    if state.get("last_reg_change"):
        for ch in state["last_reg_change"].get("changes", []):
            ch.pop("old", None)
            ch.pop("new", None)
    return state
