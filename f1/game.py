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
    PREV_SEASON_TEAM_ORDER, circuit_for_round,
)
from .models import make_driver, make_team, new_career_dict
from .rules import (
    SEASON_LENGTH, MAX_SEASONS, REG_MINOR_TARGET, REG_MAJOR_TARGET,
    add_perf_gain, add_rel_gain, dsus, finance_estimates, is_retiring,
    reg_change, retirement_chance, update_points_dist,
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
    total = sum(d["salary"] for d in team_drivers(career, team))
    return total / SEASON_LENGTH


# ---------------------------------------------------------------------------
# Team selection / custom teams
# ---------------------------------------------------------------------------

def choose_team(career, team_id, player_id):
    """A human takes over an AI team (new career pick, or mid-career takeover)."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    if team["owner_player_id"] is not None:
        return False, "That team is already taken."
    team["is_ai"] = False
    team["owner_player_id"] = player_id
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
    if priority == "perf":
        team["perf_res"] += amount
        team["reli_res"] -= amount / 3.0
    else:
        team["perf_res"] -= amount / 5.0
        team["reli_res"] += amount
    team["money"] -= amount
    label = "Performance" if priority == "perf" else "Reliability"
    pool = team["perf_res"] if priority == "perf" else team["reli_res"]
    return True, f"Invested ${amount:.2f}M in {label} research ✓ (pool now {pool:.2f})"


def do_develop(career, team_id):
    """From Main.py: Develop Parts costs $1M, converts banked research into
    car performance / reliability (Team.py addPerf / addRel)."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
    if team["money"] < 1.0:
        return False, "Need at least $1M to develop parts."
    perf_gain = add_perf_gain(team["perf"], team["perf_res"])
    rel_gain = add_rel_gain(team["reli_res"])
    team["perf"] += perf_gain
    team["rel"] += rel_gain
    team["rel"] = max(0.5, min(0.99, team["rel"]))
    team["perf_res"] = 0.0
    team["reli_res"] = 0.0
    team["money"] -= 1.0
    return True, (f"Parts developed ✓ — Perf {team['perf']:.1f} (+{perf_gain:.1f}), "
                  f"Reliability {team['rel']:.2f} (+{rel_gain:.2f}), $1M spent")


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


def do_reg_research(career, team_id, amount):
    """Reg Research (v0): a separate pool from normal research. Money in
    now is carried to the next reg change at season rollover."""
    team = get_team(career, team_id)
    if not team:
        return False, "No such team."
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


def break_cost(career, team_id, driver_id, seat):
    """Cost of signing a driver into a seat, computed the exact way
    sign_driver() charges it — the single source of truth, so the
    number shown in the Driver Market is always the number charged.

    v0.3 (replaces the v0.2.1 signing fee + old break fee): signing
    is FREE — a driver's MV is their salary per season, paid
    salary/12 after each race, never upfront. The only cost is
    breaking the current driver's contract, when the seat is
    occupied (0 for an empty seat):

        rounds_remaining = (12 - current_round + 1)
                           + max(0, contract_years - 1) * 12
        break_cost = rounds_remaining * salary / 12 + $2.0M

    current_round is the 1-indexed round about to be played and
    contract_years is the outgoing driver's remaining contract years
    including the current season. In winter break the current season
    is over, so rounds_remaining = contract_years * 12.

    Matthew's example: Gasly, contract 3, start of round 9 (4 rounds
    left this season), salary $4.0M -> 28 x $4.0M/12 + $2.0M = $11.33M.

    Returns {total, break_fee, affordable} or None when the
    team/driver/seat is invalid."""
    team = get_team(career, team_id)
    if not team:
        return None
    if seat not in (1, 2):
        return None
    d = get_driver(career, driver_id)
    if not d or d["status"] not in ("free", "f2"):
        return None
    slot = "driver1_id" if seat == 1 else "driver2_id"
    current = get_driver(career, team[slot]) if team[slot] else None
    if not current:
        return {"total": 0.0, "break_fee": 0.0, "affordable": True}
    if career.get("phase") == "winter_break":
        rounds_remaining = current["contract_left"] * SEASON_LENGTH
    else:
        rounds_remaining = ((SEASON_LENGTH - career["round"] + 1)
                            + max(0, current["contract_left"] - 1) * SEASON_LENGTH)
    fee = rounds_remaining * current["salary"] / SEASON_LENGTH + 2.0
    return {"total": round(fee, 2), "break_fee": round(fee, 2),
            "affordable": fee <= team["money"]}


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


def sign_driver(career, team_id, driver_id, seat, contract_years=2):
    """Sign a free-agent / F2 driver into a seat (1 or 2).

    v0.3: signing is FREE — no upfront fee. A driver's MV is their
    salary per season, paid salary/12 after each race (see
    run_race_weekend). The only charge is breaking the current
    driver's contract when the seat is occupied: break_cost()
    (rounds left x salary/12 + $2.0M). The new driver gets the
    contract length the signing manager picked: an integer 1–4
    years (the player picks in the Driver Market confirm step;
    bots pick via ai_contract_years()).

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
    if seat not in (1, 2):
        return False, "Seat must be 1 or 2."
    try:
        contract_years = int(contract_years)
    except (TypeError, ValueError):
        return False, "Contract length must be a whole number of years (1–4)."
    if not 1 <= contract_years <= 4:
        return False, "Contract length must be 1–4 years."
    d = get_driver(career, driver_id)
    if not d or d["status"] not in ("free", "f2"):
        return False, "That driver is not available to sign."
    slot = "driver1_id" if seat == 1 else "driver2_id"
    current = get_driver(career, team[slot]) if team[slot] else None
    quote = break_cost(career, team_id, driver_id, seat)
    cost = quote["total"]
    if cost > team["money"]:
        if current:
            return False, (f"Need ${cost:.2f}M to break "
                           f"{current['name']}'s contract.")
        return False, f"Need ${cost:.2f}M."
    team["money"] -= cost
    if current:
        _release_driver(career, current, team)
        current["status"] = "free"
        career["transfers"].append(
            f"{current['name']} left {team['name']} (contract broken)")
    if d["status"] == "f2":
        # F2 points are a different championship — a promoted driver
        # starts their F1 tally at 0.
        d["points"] = 0
        d["season_wins"] = 0
        d["best_pos"] = None
    # A free agent keeps championship points/wins/best finish from an
    # earlier stint; either way the per-team tally restarts at 0.
    d["team_points"] = 0
    d["last_team_id"] = None
    d["status"] = "f1"
    d["team_id"] = team["id"]
    d["contract_left"] = contract_years
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


def _recompute_team_points(career, team):
    """Team points / best finish: the points its current drivers scored
    FOR this team (team_points), plus points banked from departed
    drivers who scored for this team. A driver's championship total
    never counts here — it may include points scored for another
    team earlier in the season."""
    pts = team.get("points_kept", 0)
    bps = []
    for did in (team["driver1_id"], team["driver2_id"]):
        dd = get_driver(career, did) if did else None
        if dd:
            pts += dd.get("team_points", 0)
            if dd["best_pos"] is not None:
                bps.append(dd["best_pos"])
    team["points"] = pts
    team["best_pos"] = min(bps) if bps else None


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
    temp_funds = team["money"] - salaries
    if temp_funds >= 10:
        round_funds = (temp_funds - 1) * (rng.random() * 0.35 + 0.2)
    else:
        round_funds = (temp_funds - 1) * (rng.random() * 0.35)
    if round_funds > 0.5:
        temp_inv = round_funds * rng.random() * 0.50
        team["perf_res"] += temp_inv
        team["money"] -= temp_inv
        rest = round_funds - temp_inv
        team["reli_res"] += rest
        team["money"] -= rest
        if (team["perf_res"] + team["reli_res"]) > (rng.random() + 2.0) \
                and rng.random() > 0.5:
            team["perf"] += add_perf_gain(team["perf"], team["perf_res"])
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
    # Reg research: a modest share of what is left over.
    if temp_funds > 5 and team["money"] > salaries + 1:
        reg_amt = round(min(0.10 * temp_funds, team["money"] - salaries - 1), 2)
        if reg_amt > 0:
            team["reg_research"] += reg_amt
            team["money"] -= reg_amt


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
        if pos <= len(points_dist):
            d["points"] += points_dist[pos - 1]
        new_rep = ((2 - (pos / (len(f2_order) * 2)) * 1) * 1 + d["rep"] * 11) / 12
        d["rep"] = new_rep
        d["race_pos"] = pos
    # Money: salaries out, per-race gains (EPRG) in.
    for team in career["teams"]:
        team["money"] -= salary_per_round(career, team)
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
    promotion, reg change. Leaves the career in winter_break so humans can
    fill empty seats; AI teams fill theirs immediately."""
    rng = rng or random
    standings = race_mod.constructor_standings(career)
    # v0.2: driver_standings() also lists replaced drivers (free agents
    # with points), but the champion / retirements / ageing below are
    # for F1 drivers only.
    drivers = [d for d in race_mod.driver_standings(career)
               if d["status"] == "f1"]
    # v0.2: record this season's final driver order BEFORE the points
    # reset below, so the next season opens in this order (all on 0).
    career["prev_driver_order"] = [d["id"] for d in drivers]
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
    f2_sorted = sorted([d for d in career["drivers"] if d["status"] == "f2"],
                       key=lambda d: (-d["points"], d["best_pos"] or 99))
    if f2_sorted:
        f2_champ = f2_sorted[0]
        f2_champ["status"] = "free"
        f2_champ["team_id"] = None
        f2_champ["points"] = 0
        f2_champ["team_points"] = 0  # v0.2.1: F2 points never carry to F1
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
    career["retired_this_season"] = [d["name"] for d in retired]
    # Ageing (DSUS), salary recalc, contracts tick down, points reset.
    for i, d in enumerate(drivers):
        d["age"] += 1
        d["contract_left"] = max(0, d["contract_left"] - 1)
        team = get_team(career, d["team_id"]) if d.get("team_id") else None
        team_pos = constructor_pos(career, d["team_id"]) if team else len(career["teams"])
        age_val = (d["age"] / 20) * 3.5
        pos_val = (len(drivers) - i) / len(drivers) * 8 if drivers else 0
        val = (d["salary"] * 0.6) + (age_val * 0.2) + (pos_val * 0.2)
        d["salary"] = max(0.5, min(10.0, val))
        d["mv"] = d["salary"]
        d["perf"] += dsus(d["age"], d["perf"], rng=rng)
        d["points"] = 0
        d["team_points"] = 0  # v0.2.1: per-team tally resets with the season
        if d["status"] == "f1":
            # Still racing: last team is their current team (recorded
            # on departure instead). Retired drivers keep theirs.
            d["last_team_id"] = None
        d["season_wins"] = 0
        d["best_pos"] = None
    for d in free_agents(career):
        if d not in retired:
            d["age"] += 1
            d["perf"] += dsus(d["age"], d["perf"], rng=rng)
            # v0.2: free agents' points reset with the season, like F1/F2
            # drivers — otherwise a replaced driver's stale points would
            # show in next season's standings.
            d["points"] = 0
            d["team_points"] = 0  # v0.2.1
            d["season_wins"] = 0
            d["best_pos"] = None
    for d in f2_drivers(career):
        d["age"] += 1
        d["perf"] += dsus(d["age"], d["perf"], rng=rng)
        d["points"] = 0
        d["team_points"] = 0  # v0.2.1
        d["best_pos"] = None
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
        team["tuning"] = 0
    # Refill the F2 grid from queued / pool rookies.
    refill_f2(career, rng=rng)
    # Regulation change into the new year.
    new_year = career["year"] + 1
    kind = "Major" if new_year % 4 == 2 else "Minor"
    target = REG_MAJOR_TARGET if kind == "Major" else REG_MINOR_TARGET
    grid_avg = sum(t["perf"] for t in career["teams"]) / len(career["teams"])
    changes = []
    for team in career["teams"]:
        old = team["perf"]
        team["perf"] = reg_change(grid_avg, target, team["perf"],
                                  team["reg_research"], rng=rng)
        team["reg_research"] = 0.0
        changes.append({"team": team["name"], "old": round(old, 2),
                        "new": team["perf"]})
    career["last_reg_change"] = {"year": new_year, "kind": kind, "changes": changes}
    career["year"] = new_year
    career["season_index"] += 1
    career["next_reg_year"] = new_year + 1
    career["next_reg_kind"] = "Major" if (new_year + 1) % 4 == 2 else "Minor"
    career["round"] = SEASON_LENGTH
    # AI teams fill empty seats straight away.
    for team in career["teams"]:
        if team["is_ai"]:
            fill_empty_seats_ai(career, team, rng=rng)
    if career["season_index"] >= MAX_SEASONS:
        career["phase"] = "finished"
    else:
        career["phase"] = "winter_break"


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


def fill_empty_seats_ai(career, team, rng=None):
    """AI winter-break signing, results-based (ContractOffer logic)."""
    rng = rng or random
    for slot in ("driver1_id", "driver2_id"):
        if team[slot]:
            continue
        pool = free_agents(career) + f2_drivers(career)
        if not pool:
            continue
        pick = ai_best_candidate(career, team, pool)
        if pick is None:
            continue
        pick["status"] = "f1"
        pick["team_id"] = team["id"]
        pick["team_points"] = 0  # v0.2.1: fresh per-team tally
        pick["last_team_id"] = None
        # v0.3: bots also pick a 1–4 year contract (ai_contract_years).
        pick["contract_left"] = ai_contract_years(pick, rng=rng)
        team[slot] = pick["id"]
        career["transfers"].append(f"{pick['name']} signed for {team['name']}")


def empty_seats(career, team):
    return [s for s in (1, 2)
            if not team["driver1_id" if s == 1 else "driver2_id"]]


def winter_break_ready(career):
    """Every human team has both seats filled."""
    for team in human_teams(career):
        if empty_seats(career, team):
            return False
    return True


def start_next_season(career):
    """Leave winter break once every human team is satisfied."""
    if career["phase"] != "winter_break":
        return False, "Not in winter break."
    if not winter_break_ready(career):
        return False, "Fill every empty seat on your team first."
    career["round"] = 1
    career["phase"] = "round"
    career["advanced"] = []
    career["results_vote"] = []
    career["results_stage"] = None
    career["transfers"] = []
    career["retired_this_season"] = []
    set_circuit_for_round(career)
    return True, "ok"


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def retirement_rumours(career):
    """isRetiring(isRumour=True) for every F1 driver, as in Main.py's rumours."""
    out = []
    # v0.2: driver_standings() also lists replaced drivers (free agents
    # with points) — rumours are for current F1 drivers only.
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
    return {
        "money": round(team["money"], 2),
        "constructor_pos": pos,
        "epsci": est["epsci"],
        "eprg": est["eprg"],
        "salary_per_race": round(salary_per_round(career, team), 2),
        "driver_costs": [{"driver": d["name"],
                          "per_race": round(d["salary"] / SEASON_LENGTH, 2)}
                         for d in drivers],
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
    # v0.3: replacement costs for the viewer's team, computed
    # server-side from the exact formula sign_driver() charges, so the
    # Driver Market always shows the number that will be charged.
    # {driver_id: {"1": {...}, "2": {...}}} — attached to the broadcast
    # copy only, never saved. The cost is the contract-break fee for
    # the driver in the seat (0 / Free for an empty seat); signing
    # itself is free.
    if viewer_team_id:
        viewer = get_team(career, viewer_team_id)
        if viewer is not None:
            state["market_costs"] = {
                d["id"]: {str(seat): break_cost(career, viewer["id"], d["id"], seat)
                         for seat in (1, 2)}
                for d in career["drivers"]
                if d["status"] in ("free", "f2")}
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
