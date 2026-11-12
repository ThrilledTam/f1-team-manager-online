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
import random

from . import race as race_mod
from .data import (
    GRID_2026, CHAMPION_HISTORY_2010_2025, PREV_SEASON_DRIVER_ORDER,
    PREV_SEASON_TEAM_ORDER, TR_2026, ACADEMY_2026, CAREER_STATS,
    circuit_for_round,
)
from .models import make_driver, make_team, new_career_dict
from .rules import SEASON_LENGTH, MAX_SEASONS
from .rules import (
    ACADEMY_FINE_FREE_COUNT, ACADEMY_LOYALTY_BOOST, ACADEMY_MAX_JOIN_AGE,
    REG_ADAPT_COST_PER_POINT, REG_OPENS_ROUND,
    REG_MAJOR_TARGET, REG_MINOR_TARGET, TR_FINE_FREE_COUNT,
    TR_RESEARCH_BONUS, WINTER_BREAK_ROUNDS, add_perf_gain, add_rel_gain,
    extra_driver_fine, finance_estimates, grow_rating, is_retiring,
    reg_change, reg_change_v2, retirement_chance, update_points_dist,
)

CUSTOM_TEAM_MONEY = 6.0
CUSTOM_TEAM_REL = 0.90  # v0 fix: Main.py passed rel=6 (a bug) for custom teams


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
    race instead (see tr_fine_per_race)."""
    return [d for d in career["drivers"]
            if d.get("status") == "tr" and d.get("team_id") == team["id"]]


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
    return extra_driver_fine(len(team_tr_drivers(career, team)),
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
    return 1.0 + TR_RESEARCH_BONUS * len(team_tr_drivers(career, team))


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
    return [d for d in career["drivers"] if d["status"] == "f2"]


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

def choose_team(career, team_id, player_id):
    """A human takes over an AI team (new career pick, or mid-career takeover).

    v0.4.1: taking over also clears any auto-generated winter-break
    offers for the team — those were made by the bot manager while
    the team was AI (e.g. when a career was loaded and every team
    was still flagged AI), and the human must choose their own
    offers. The team is also taken out of wb_ready, so a bot's
    readiness never counts as the human's."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    if team["owner_player_id"] is not None:
        return False, "That team is already taken."
    team["is_ai"] = False
    team["owner_player_id"] = player_id
    if career.get("phase") == "winter_break":
        (career.get("wb_offers") or {}).pop(team["id"], None)
        if team["id"] in (career.get("wb_ready") or []):
            career["wb_ready"].remove(team["id"])
    return True, f"You took over {team['name']} ✓"


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


def do_develop(career, team_id):
    """From Main.py: Develop Parts costs $1M, converts banked research into
    car performance / reliability (Team.py addPerf / addRel).

    v0.5: the PERF gain is multiplied by the team's driver
    development factor (1 + the main 2 + T&R drivers' contributions
    — experience, wins, rating). The RELIABILITY gain is unchanged:
    drivers contribute to performance development, not reliability.
    The T&R research-pool bonus is a separate mechanic and is
    untouched (it boosts the pool an investment buys, not the
    develop step)."""
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
    team["rel"] = max(0.5, min(0.99, team["rel"]))
    team["perf_res"] = 0.0
    team["reli_res"] = 0.0
    team["money"] -= 1.0
    return True, (f"Parts developed ✓ — Perf {team['perf']:.1f} (+{perf_gain:.1f}), "
                  f"Reliability {team['rel']:.2f} (+{rel_gain:.2f}), $1M spent"
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
    next season's regulations — $0.25M per point of car perf.
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


def break_fee_for_driver(career, driver):
    """v0.4: the contract-break fee for one driver, from the v0.3
    formula — rounds_remaining x salary/12 + $2.0M, where
    rounds_remaining = (12 - current_round + 1) + max(0,
    contract_years - 1) * 12 (in winter break: contract_years * 12).
    Used for the occupant of a seat being replaced AND for poaching
    a driver out of another team's contract."""
    if career.get("phase") == "winter_break":
        rounds_remaining = driver["contract_left"] * SEASON_LENGTH
    else:
        rounds_remaining = ((SEASON_LENGTH - career["round"] + 1)
                            + max(0, driver["contract_left"] - 1) * SEASON_LENGTH)
    return round(rounds_remaining * driver["salary"] / SEASON_LENGTH + 2.0, 2)


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
        under contract with another team (poaching, main or T&R).
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


def _release_driver(career, driver, team):
    """A driver leaves a team (replaced mid-season or contract ended):
    the points they scored FOR this team stay with the team (banked
    into points_kept — real F1), their last team is recorded so the
    standings can still show it, and their per-team tally is cleared
    for wherever they go next. Championship points/wins/best finish
    are NOT touched — they follow the driver (v0.2, revised)."""
    if team is not None:
        driver["last_team_id"] = team["id"]
        team["points_kept"] = team.get("points_kept", 0) + driver.get("team_points", 0)
    driver["team_points"] = 0
    driver["team_id"] = None
    driver["contract_left"] = 0


def _sign_driver_common(career, team, d, contract_years):
    """Shared tail of a signing: the driver joins the team with a
    fresh per-team tally (championship points/wins/best finish follow
    the driver; an F2 driver starts at 0 — their F2 points must not
    carry into F1)."""
    if d["status"] == "f2":
        d["points"] = 0
        d["season_wins"] = 0
        d["season_podiums"] = 0
        d["season_qual_h2h"] = 0
        d["best_pos"] = None
        d["best_finish_round"] = None
        d["finish_sum"] = 0
        d["finish_count"] = 0
        # v0.4.4: their F2 starts do not count as F1 races — a promoted
        # driver is not registered in the F1 standings until they
        # actually race in F1.
    # A free agent keeps championship points/wins/best finish from an
    # earlier stint; either way the per-team tally restarts at 0.
    d["team_points"] = 0
    d["last_team_id"] = None
    d["contract_left"] = contract_years
    # v0.4.2: signing a main or T&R contract ends the academy
    # affiliation, wherever it was — a driver cannot be an academy
    # junior and a contracted driver at the same time, and the
    # academy slot frees up.
    d["academy_team_id"] = None


def sign_driver(career, team_id, driver_id, seat, contract_years=2):
    """Sign a driver into a main seat (1 or 2).

    v0.4: the driver may come from free agency, F2, or another team
    (poaching — main driver or T&R driver, with the break fee from
    sign_cost()). The signed driver takes the seat offered. During
    the winter break, signings go through the offer system instead —
    use winter offers, not this direct signing.
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
            career["transfers"].append(
                f"{d['name']} left {source_team['name']} (poached by {team['name']})")
    team["money"] -= cost
    if current:
        _release_driver(career, current, team)
        current["status"] = "free"
        career["transfers"].append(
            f"{current['name']} left {team['name']} (contract broken)")
    _sign_driver_common(career, team, d, contract_years)
    d["status"] = "f1"
    d["team_id"] = team["id"]
    team[slot] = d["id"]
    # Team points are the current drivers' per-team points plus any
    # points banked from drivers who scored for this team and have
    # since left (apply_race_results does the same after a race).
    _recompute_team_points(career, team)
    career["transfers"].append(f"{d['name']} signed for {team['name']}")
    if cost > 0:
        return True, (f"{d['name']} signed for {team['name']} ✓ "
                      f"(Seat {seat}, {contract_years}-year contract) "
                      f"— ${cost:.2f}M break fee")
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
    if d.get("status") in ("f1", "tr") and d.get("team_id") == team["id"]:
        return False, "That driver already races for your team."
    quote = sign_cost(career, team_id, driver_id, ("tr", None))
    if quote is None:
        return False, "That signing is not possible."
    cost = quote["total"]
    if cost > team["money"]:
        return False, f"Need ${cost:.2f}M to sign {d['name']}."
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
            career["transfers"].append(
                f"{d['name']} left {source_team['name']} (poached by {team['name']})")
    team["money"] -= cost
    _sign_driver_common(career, team, d, contract_years)
    d["status"] = "tr"
    d["team_id"] = team["id"]
    career["transfers"].append(
        f"{d['name']} signed for {team['name']} as Test & Reserve")
    if cost > 0:
        return True, (f"{d['name']} signed for {team['name']} as Test & Reserve ✓ "
                      f"({contract_years}-year contract) — ${cost:.2f}M break fee")
    return True, (f"{d['name']} signed for {team['name']} as Test & Reserve ✓ "
                  f"({contract_years}-year contract) — Free")


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
    career["transfers"].append(
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
    career["transfers"].append(
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
    driver just leaves the roster. Logged in Transfers. Round phase
    only — in the winter break, lineups change through offers."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    if career.get("phase") != "round":
        return False, "You can only release drivers between races."
    d = get_driver(career, driver_id)
    if not d or d.get("team_id") != team["id"] \
            or d.get("status") not in ("f1", "tr"):
        return False, "That driver is not one of your main or T&R drivers."
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
    career["transfers"].append(
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
    # Outgoing main driver: bank their per-team points (real F1),
    # keep their contract and championship tally, become T&R.
    team["points_kept"] = team.get("points_kept", 0) + main.get("team_points", 0)
    main["team_points"] = 0
    main["last_team_id"] = None
    main["status"] = "tr"
    # Incoming T&R driver: take the seat, fresh per-team tally.
    tr["team_points"] = 0
    tr["last_team_id"] = None
    tr["status"] = "f1"
    team[slot] = tr["id"]
    _recompute_team_points(career, team)
    career["transfers"].append(
        f"{tr['name']} swapped with {main['name']} at {team['name']}")
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
    tr["team_points"] = 0
    tr["last_team_id"] = None
    tr["status"] = "f1"
    team[slot] = tr["id"]
    _recompute_team_points(career, team)
    career["transfers"].append(
        f"{tr['name']} promoted to a race seat at {team['name']}")
    return True, f"{tr['name']} promoted to Seat {seat} at {team['name']} ✓"


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


def ai_act(career, team, rng=None):
    """One AI turn for a round, from Main.py's AI block.

    Spends the remainder after salaries on research, develops when the
    banked research is large enough, sets tuning from results, and puts a
    share into reg research. Uses money / points / position only.
    """
    rng = rng or random
    if team["money"] is None:
        return
    salaries = salary_per_round(career, team)
    # v0.4.2: the funds a bot plans with are what's left after
    # salaries AND the per-race extra charges (T&R fine, academy
    # stipends, academy fine) — otherwise a team with 4 T&R drivers
    # would spend money the fine then takes away.
    charges = extra_charges_per_race(career, team)["total"]
    temp_funds = team["money"] - salaries - charges
    # v0.4 bot parity: fill an empty main seat before the race, from
    # the best available free agent / F2 driver (highest reputation).
    # An empty seat scores nothing, so this is the first priority.
    for seat in (1, 2):
        slot = "driver1_id" if seat == 1 else "driver2_id"
        if not team[slot]:
            best = _best_available_driver(career)
            if best is not None:
                sign_driver(career, team["id"], best["id"], seat,
                            contract_years=ai_contract_years(best))
    # v0.4 bot parity: optional T&R signing, preferring young, cheap
    # talent — one T&R driver, signed early in the season, for the
    # +25% research bonus. Only when the team can comfortably afford
    # it (salaries + a buffer), and only once per season.
    if (not team_tr_drivers(career, team)
            and career.get("round", 1) <= 6
            and team["money"] > salaries + charges + 10):
        young = [d for d in career["drivers"]
                 if d["status"] in ("free", "f2") and d["age"] <= 25]
        if young:
            young.sort(key=lambda d: (d["salary"], d["age"], d["name"]))
            sign_tr_driver(career, team["id"], young[0]["id"],
                           contract_years=ai_contract_years(young[0]))
    # v0.4.2 bot parity: an AI team may add one young unsigned
    # driver to its Driver Academy per season (youngest cheapest,
    # rounds 1-6), when it can comfortably afford it. Bots never
    # exceed 3 academy drivers — they avoid the exponential academy
    # fine entirely.
    if (not team.get("ai_academy_signed")
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
        perf_money = round_funds * rng.random() * 0.50
        reli_money = round_funds - perf_money
        team["perf_res"] += perf_money * mult
        team["reli_res"] += reli_money * mult
        team["money"] -= round_funds
        if (team["perf_res"] + team["reli_res"]) > (rng.random() + 2.0) \
                and rng.random() > 0.5:
            # v0.5: bots develop with the same driver factor as humans
            # (their main + T&R drivers contribute) — bot parity.
            team["perf"] += (add_perf_gain(team["perf"], team["perf_res"])
                             * team_dev_factor(career, team))
            team["perf_res"] = 0.0
            team["rel"] = max(0.5, min(0.99, team["rel"] + add_rel_gain(team["reli_res"])))
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


def _best_available_driver(career):
    """Best free-agent / F2 driver by reputation (bots may use rep;
    players never see it). Ties: cheaper salary, then name. None when
    nobody is available."""
    cands = [d for d in career["drivers"] if d["status"] in ("free", "f2")]
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
            for tr in team_tr_drivers(career, team):
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
            career["transfers"].append(
                f"{bad_tr['name']} released by {team['name']} "
                f"(poor results, ${fee:.2f}M break fee)")
            continue
        seat, d = worst
        trs = team_tr_drivers(career, team)
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
        career["transfers"].append(
            f"{d['name']} dropped by {team['name']} (poor results, ${fee:.2f}M break fee)")


# ---------------------------------------------------------------------------
# Simultaneous advance + race weekend
# ---------------------------------------------------------------------------

def human_teams(career):
    return [t for t in career["teams"] if t["owner_player_id"] is not None]


def all_humans_advanced(career, connected_player_ids):
    """True when every human team whose owner is still connected has
    advanced. Disconnected owners' teams are AI-driven for that round."""
    for team in human_teams(career):
        if team["owner_player_id"] in connected_player_ids \
                and team["id"] not in career["advanced"]:
            return False
    return True


def advance_team(career, team_id, connected_player_ids, rng=None):
    """A human hits Advance. Returns (advanced_ok, race_ran, message)."""
    team = get_team(career, team_id)
    if not team:
        return False, False, "No such team."
    if career["phase"] != "round":
        return False, False, "Not in a racing round right now."
    if team["id"] not in career["advanced"]:
        career["advanced"].append(team["id"])
    if all_humans_advanced(career, connected_player_ids):
        run_race_weekend(career, connected_player_ids, rng=rng)
        return True, True, "Qualifying complete — results are in."
    return True, False, "Waiting for the other teams to advance."


def results_voters(career, connected_player_ids):
    """Player ids that must vote NEXT at the current results stage: the
    owners of human teams who are still connected. Disconnected humans are
    excluded (their teams are AI-driven); bots never vote."""
    return [t["owner_player_id"] for t in human_teams(career)
            if t["owner_player_id"] in connected_player_ids]


def all_results_voted(career, connected_player_ids):
    """True when every required voter has voted — or when nobody is left
    connected to vote (then the stage must not get stuck)."""
    voters = results_voters(career, connected_player_ids)
    voted = set(career.get("results_vote") or [])
    return all(v in voted for v in voters)


def advance_results_stage(career, connected_player_ids, rng=None):
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


def check_results_vote(career, connected_player_ids, rng=None):
    """Advance the results stage if the vote is complete. Returns True if
    it advanced. Called after every vote, host skip, and disconnect."""
    if career.get("phase") not in ("qual_results", "race_results"):
        return False
    if all_results_voted(career, connected_player_ids):
        return advance_results_stage(career, connected_player_ids, rng=rng)
    return False


def vote_next(career, player_id, connected_player_ids, rng=None):
    """A human votes NEXT on the current results page. Returns
    (ok, advanced, message). Votes are stored by player_id, so a
    reconnecting player keeps their vote."""
    if career.get("phase") not in ("qual_results", "race_results"):
        return False, False, "There is no results vote right now."
    if player_id not in results_voters(career, connected_player_ids):
        return False, False, "You have no team in this career, so you cannot vote."
    if player_id in (career.get("results_vote") or []):
        return True, False, "Your NEXT vote is already recorded."
    career.setdefault("results_vote", []).append(player_id)
    advanced = check_results_vote(career, connected_player_ids, rng=rng)
    total = len(results_voters(career, connected_player_ids))
    voted = len(career.get("results_vote") or [])
    if advanced:
        return True, True, "Everyone voted NEXT — moving on."
    return True, False, f"NEXT vote recorded ({voted}/{total}) ✓"


def host_skip_results(career, player_id, connected_player_ids, is_host, rng=None):
    """The host's override: advances the results stage immediately. Only
    the host may call it (the button is only ever sent to the host)."""
    if not is_host:
        return False, False, "Only the host can skip the vote."
    if career.get("phase") not in ("qual_results", "race_results"):
        return False, False, "There is no results vote to skip right now."
    advanced = advance_results_stage(career, connected_player_ids, rng=rng)
    return True, advanced, "Host skipped the vote — moving on."


def complete_race_weekend(career, rng=None):
    """After the race-results vote: next round, or season end."""
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


def run_race_weekend(career, connected_player_ids, rng=None):
    """AI acts, then qualifying, then the race, then money. v0.1: the round
    does NOT advance here — the room goes to the qualifying results page
    first (phase 'qual_results'), and only the NEXT votes move it on."""
    rng = rng or random
    # AI teams act; disconnected humans are AI-driven this round.
    for team in career["teams"]:
        if team["is_ai"] or team["owner_player_id"] not in connected_player_ids:
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
    leader = qual[0][0]["qual_time"] if qual else 0
    career["last_qual"] = [
        result_row(d, t, i + 1,
                   "LEADER" if i == 0 else f"+{d['qual_time'] - leader:.3f}s")
        for i, (d, t) in enumerate(qual)]
    race_entries = race_mod.simulate_race(career, laps=gp["laps"], rng=rng)
    race_mod.apply_race_results(career, race_entries, points_dist)
    leader_time = race_entries[0][0]["race_time"] if race_entries else 0
    career["last_race"] = [
        result_row(d, t, i + 1,
                   "LEADER" if i == 0 else f"+{d['race_time'] - leader_time:.3f}s",
                   points=points_dist[i] if i < len(points_dist) else 0,
                   gl=(d.get("qual_pos") or 0) - (i + 1))
        for i, (d, t) in enumerate(race_entries)]
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
    career["prev_team_order"] = [t["id"] for t in standings]
    # v0.4: snapshot the season just finished for the winter-break
    # review (stats, final standings, head-to-head) — taken BEFORE
    # the points reset below, so it shows the real final numbers.
    career["last_season"] = {
        "year": career["year"],
        "drivers": [{
            "id": d["id"], "name": d["name"],
            "team": (get_team(career, d["team_id"]) or {}).get("name", ""),
            "team_id": d.get("team_id"),
            "points": d["points"], "wins": d.get("season_wins", 0),
            "podiums": d.get("season_podiums", 0),
            "best_pos": d.get("best_pos"),
            "qual_h2h": d.get("season_qual_h2h", 0),
            "avg_finish": round(d.get("finish_sum", 0) / d["finish_count"], 1)
                          if d.get("finish_count") else None,
        } for d in drivers],
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
    # F2 champion is promoted to the free-agent pool.
    f2_sorted = race_mod.f2_standings(career)
    if f2_sorted:
        f2_champ = f2_sorted[0]
        f2_champ["status"] = "free"
        f2_champ["team_id"] = None
        f2_champ["points"] = 0
        f2_champ["team_points"] = 0  # v0.2.1: F2 points never carry to F1
        f2_champ["season_wins"] = 0
        f2_champ["season_podiums"] = 0
        f2_champ["season_qual_h2h"] = 0
        f2_champ["best_pos"] = None
        f2_champ["best_finish_round"] = None
        f2_champ["finish_sum"] = 0
        f2_champ["finish_count"] = 0
        f2_champ["last_team_id"] = None
        career["transfers"].append(
            f"{f2_champ['name']} won F2 and joins the free-agent pool")
    # Prize money (Main.py): 1st $12M, others 11 - 0.65*pos. Plus EPSCI.
    for pos, team in enumerate(standings, start=1):
        team["money"] += 12.0 if pos == 1 else (11.0 - (0.65 * pos))
        team["money"] += finance_estimates(pos)["epsci"]
    # Retirements (F1 drivers, then free agents).
    retired = []
    for d in list(drivers):
        team = get_team(career, d["team_id"]) if d.get("team_id") else None
        pos = constructor_pos(career, d["team_id"]) if team else len(career["teams"])
        team_pts = team["points"] if team else 0
        if is_retiring(d["age"], d["cha"], pos, d["points"], team_pts, rng=rng):
            retired.append(d)
    for d in list(free_agents(career)):
        if d["age"] >= 40:
            chance = 20.0 + ((d["age"] - 40) * 5)
            if rng.random() * 100 <= chance or d["age"] >= 57:
                retired.append(d)
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
            if d["contract_left"] <= 0:
                d["status"] = "free"
                d["team_id"] = None
                d["last_team_id"] = team["id"]
                career["transfers"].append(
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
                career["transfers"].append(
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
    # Refill the F2 grid from queued / pool rookies.
    refill_f2(career, rng=rng)
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
    pool = [d for d in career["drivers"] if d["status"] in ("free", "f2")]
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
                       "years": ai_contract_years(pick, rng=rng)})
        pool.remove(pick)
    # T&R: one offer, youngest cheapest available. v0.4.2: no hard
    # cap any more — a bot may offer a 3rd (or further) T&R driver
    # only when it has plenty of cash: its bank, after paying a full
    # season of the fine the extra driver would add, must still be
    # over $50M.
    trs = team_tr_drivers(career, team)
    if pool:
        young = [d for d in pool if d["age"] <= 25]
        young.sort(key=lambda d: (d["salary"], d["age"], d["name"]))
        if young:
            pick = young[0]
            fine_after = extra_driver_fine(len(trs) + 1, TR_FINE_FREE_COUNT)
            affordable = (len(trs) < TR_FINE_FREE_COUNT
                          or team["money"] - 12 * fine_after > 50)
            if affordable:
                quote = sign_cost(career, team["id"], pick["id"], ("tr", None))
                if quote and quote["affordable"]:
                    offers.append({"driver_id": pick["id"], "target": "tr", "seat": None,
                                   "years": ai_contract_years(pick, rng=rng)})
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
    seat: 1|2|null, years: 1-4}. At most 3 offers, at most 2 for
    main seats, at most 1 for a T&R slot. A team with an empty main
    seat MUST include at least one main-seat offer. Returns
    (ok, message)."""
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
        target = off.get("target")
        if target == "seat":
            seat = off.get("seat")
            if seat not in (1, 2):
                return False, "A main-seat offer needs seat 1 or 2."
            if seat in seats_seen:
                return False, f"Seat {seat} already has an offer this round."
            seats_seen.append(seat)
            cleaned.append({"driver_id": d["id"], "target": "seat",
                            "seat": seat, "years": years})
        elif target == "tr":
            if tr_seen >= 1:
                return False, "At most one T&R offer per round."
            tr_seen += 1
            cleaned.append({"driver_id": d["id"], "target": "tr",
                            "seat": None, "years": years})
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
    if career.get("wb_round", 1) >= WINTER_BREAK_ROUNDS:
        autofill_winter_break(career, rng=rng)
        career["wb_done"] = True
        return True, True, "Winter break complete — the new season is ready to start."
    career["wb_round"] = career.get("wb_round", 1) + 1
    career["wb_ready"] = []
    start_winter_break_round(career, rng=rng)
    return True, True, f"Winter-break round {career['wb_round']} — make your offers."


def _wb_offer_winners(career, offers_by_team):
    """v0.4.2: for each driver with winter-break offers, the team
    they sign with — the lowest EFFECTIVE constructors' position,
    where an academy driver's own academy team counts as
    ACADEMY_LOYALTY_BOOST (3) places higher. Ties go to the academy
    team first, then to the better real constructors' position.
    So a 5th-place academy team (effective 2nd, academy) beats a
    2nd-place rival (effective 2nd, not academy) for its own junior,
    and a 1st-place academy team always wins its junior. A driver
    with no academy affiliation just goes to the best-placed team
    that offered them. Returns {driver_id: team_id}."""
    order = wb_constructors_order(career)
    pos = {tid: i + 1 for i, tid in enumerate(order)}
    fallback = len(order) + 1
    winners = {}
    for team_id, offers in (offers_by_team or {}).items():
        for off in offers:
            did = off.get("driver_id")
            d = get_driver(career, did)
            if not d:
                continue
            real = pos.get(team_id, fallback)
            eff = real
            is_academy_team = d.get("academy_team_id") == team_id
            if is_academy_team:
                eff = max(1, real - ACADEMY_LOYALTY_BOOST)
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
            if off["target"] == "seat":
                slot = "driver1_id" if off["seat"] == 1 else "driver2_id"
                if team[slot]:
                    continue  # seat already filled by an earlier offer
                ok, _why = sign_driver_wb(career, team, d, off["seat"], off["years"])
            else:
                ok, _why = sign_tr_driver_wb(career, team, d, off["years"])
            if ok:
                taken.add(d["id"])
    career["wb_offers"] = {}


def sign_driver_wb(career, team, d, seat, contract_years):
    """Sign a driver into a main seat during the winter break — the
    same charges as sign_driver(), but allowed in winter break (the
    direct sign_driver() is blocked then so signings go through
    offers). The break fee for the seat's current occupant is
    charged from the winter-break formula (contract x 12 rounds)."""
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
        career["transfers"].append(
            f"{current['name']} left {team['name']} (contract broken)")
    _sign_driver_common(career, team, d, contract_years)
    d["status"] = "f1"
    d["team_id"] = team["id"]
    team[slot] = d["id"]
    _recompute_team_points(career, team)
    career["transfers"].append(f"{d['name']} signed for {team['name']}")
    return True, "ok"


def sign_tr_driver_wb(career, team, d, contract_years):
    """Sign a driver as a T&R driver during the winter break — the
    same as sign_tr_driver(), but allowed in winter break."""
    _sign_driver_common(career, team, d, contract_years)
    d["status"] = "tr"
    d["team_id"] = team["id"]
    career["transfers"].append(
        f"{d['name']} signed for {team['name']} as Test & Reserve")
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
            trs = team_tr_drivers(career, team)
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
            career["transfers"].append(
                f"{pick['name']} signed for {team['name']} (winter-break auto-fill)")


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
    return True, "ok"


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def retirement_rumours(career):
    """isRetiring(isRumour=True) for every F1 driver, as in Main.py's rumours."""
    out = []
    # v0.2: driver_standings() also lists replaced drivers (free agents
    # with points), and v0.4.4: any driver who raced in F1 this season
    # — rumours are for current F1 drivers only.
    drivers = [d for d in race_mod.driver_standings(career)
               if d["status"] == "f1"]
    for d in drivers:
        team = get_team(career, d["team_id"]) if d.get("team_id") else None
        pos = constructor_pos(career, d["team_id"]) if team else len(career["teams"])
        chance = retirement_chance(d["age"], d["cha"], pos, d["points"],
                                   team["points"] if team else 0)
        out.append({"driver": d["name"], "team": team["name"] if team else "",
                    "age": d["age"], "chance": round(chance, 1)})
    return sorted(out, key=lambda r: -r["chance"])


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
    return {
        "money": round(team["money"], 2),
        "constructor_pos": pos,
        "epsci": est["epsci"],
        "eprg": est["eprg"],
        "salary_per_race": round(salary_per_round(career, team), 2),
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


def public_state(career, chart_on, viewer_team_id=None):
    """The state sent to a client. When the perf chart is OFF, ALL perf
    numbers are hidden from humans — team perf, reliability, driver perf —
    and other teams' research pools are hidden too. Your own team's research
    pools stay visible (you need them to play), but never its perf.
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
                    tr_quote["fine_after"] = extra_driver_fine(
                        len(team_tr_drivers(career, viewer)) + 1, TR_FINE_FREE_COUNT)
                out["tr"] = tr_quote
                return out
            state["market_costs"] = {
                d["id"]: _costs_for(d)
                for d in career["drivers"]
                if d["status"] in ("free", "f2", "f1", "tr")
                and not (d.get("team_id") == viewer["id"])}
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
    if chart_on:
        return state
    for team in state["teams"]:
        team.pop("perf", None)
        team.pop("rel", None)
        if team["id"] != viewer_team_id:
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
