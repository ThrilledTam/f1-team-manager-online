"""Race simulation — formulas from the original Race.py, cleaned up.

lapSim and simulateQual keep the original maths (qualifying is a single
lapSim draw per driver, gaps ~0-3s). simulateRace changed in v0.1 with
Matthew's approval: the original looped 80 times overwriting race_time, so
a race was effectively one lap draw (~137s total). v0.1 sums a real
full-distance race — every driver runs the circuit's lap count — with a
compressed per-lap perf spread so gaps stay plausible (~20-100s) and the
winner's total is a Grand Prix length (~5,000s). See race_lap_time().
"""
import random

BASELINE_LAP = 75.000

RACE_LAP_BASE = 95.0
# v0.1: a full-distance race. Each driver runs the circuit's lap count and
# every lap is summed. The per-lap time is
#   95 - (driver_perf/20 + team_perf/80)  +/- tuning effect  + N(0, 0.12)
# so a lap is ~88s, a 57-lap race is ~5,000s (80+ minutes), and the
# driver+team perf spread is ~1.8s/lap, giving total gaps of roughly
# 20-100s — like Matthew's local race screenshot — instead of the hundreds
# of seconds the raw lapSim spread (driver/5 + team/20, ~5.6s/lap) would
# produce over a full race. Tuning helps Driver 1 and hurts Driver 2 by
# 0.1s/lap per tuning point. A one-off grid penalty of 0.5s per qualifying
# position reflects the start; the rest is pure pace.
# v0.2 adds per-driver, per-session form (see SESSION_FORM_* below): one
# random pace offset per driver per session, so the best combo wins ~45%
# of races instead of ~100%.
RACE_TUNING_PER_LAP = 0.1
RACE_GRID_PENALTY_PER_POS = 0.5

# v0.2: per-driver, per-session form ("race-day variance"). The v0.1 race
# was too predictable: the per-lap noise (sigma 0.12) averages out over
# 57 laps (total sigma ~0.9s) while the driver+car perf spread is ~1.8s/lap,
# i.e. tens of seconds over a race — so the best driver+car combo won
# every time. Qualifying was the same: one lap_sim draw with sigma 0.12*2.5
# = 0.3s against a ~2.9s perf spread. The fix is one random pace offset
# drawn ONCE per driver per session (not per lap, or it averages out
# again) and different between qualifying and the race:
#   qual:  + N(0, 0.35s) on the single lap time
#   race:  + N(0, 0.22s) per lap, the same offset every lap (total sigma
#          ~12.5s over 57 laps) on top of the per-lap noise.
# Measured with the 2026 grid over 120 seeded races (see README): the
# single best combo (Antonelli/Mercedes) wins 45% of races instead of
# ~100%, with 5 different winners and 9 different poles (Antonelli on
# pole 38%), while gaps stay plausible (qual P22 gap median 3.0s, race
# P22 gap median ~96s, winner ~5,010s). No crashes/DNFs or weather —
# only lap-time randomness, as asked.


def race_lap_time(driver_perf, team_perf, tuning, is_driver1, rng=None,
                  session_offset=0.0):
    """One lap of a full-distance race (v0.1). session_offset (v0.2) is
    the driver's per-session form: the same offset every lap, drawn once
    per driver per race by simulate_race()."""
    rng = rng or random
    lap = RACE_LAP_BASE - (driver_perf / 20.0 + team_perf / 80.0)
    if tuning:
        lap += (-tuning * RACE_TUNING_PER_LAP) if is_driver1 \
            else (tuning * RACE_TUNING_PER_LAP)
    lap += rng.gauss(0, 0.12)
    lap += session_offset
    return lap


def lap_sim(driver_perf, team_perf, max_perf, min_perf, session, error_multiplier, rng=None):
    """From Race.py lapSim(). session 1 = qual, 2 = race."""
    rng = rng or random
    if session == 1:
        field_diff = 180
    else:
        field_diff = 150
    perf_diff = max_perf - min_perf
    a_temp = (max_perf - field_diff) - min_perf
    if perf_diff > field_diff:
        w_perf = team_perf + (a_temp * ((max_perf - team_perf) / perf_diff))
    else:
        w_perf = team_perf
    if session == 1:
        lap_time = BASELINE_LAP - ((driver_perf / 25) + (w_perf / 40)) \
            + rng.gauss(0, 0.12) * error_multiplier
    else:
        lap_time = BASELINE_LAP - ((driver_perf / 5) + (w_perf / 20)) \
            + rng.gauss(0, 0.12) * error_multiplier
    return lap_time


def _driver_sort_key(d, prev):
    """v0.4 standings sort (feature 1): points desc, wins desc, best
    finish (lower is better), then whoever achieved that best finish
    FIRST (earliest round). Non-point scorers (0 pts, 0 wins) sort by
    best finish, then earliest. prev is the {driver_id: rank} map of
    last season's finishing order — the final tie-break, so a new
    season opens in last season's order (all on 0). Drivers with no
    previous order go after the carried-over drivers, by perf, name."""
    n_prev = len(prev)
    return (
        -d["points"],
        -d.get("season_wins", 0),
        d["best_pos"] if d["best_pos"] is not None else 99,
        d.get("best_finish_round") if d.get("best_finish_round") is not None else 99,
        prev.get(d["id"], n_prev),
        -d["perf"],
        d["name"],
    )


def _team_sort_key(t, prev):
    """v0.4 constructors' sort (feature 1): points desc, wins desc,
    best finish (best race finish by either driver), then whoever got
    that best finish FIRST. prev is the {team_id: rank} map of last
    season's constructors' order — the final tie-break."""
    n_prev = len(prev)
    return (
        -t["points"],
        -t.get("season_wins", 0),
        t["best_pos"] if t["best_pos"] is not None else 99,
        t.get("best_finish_round") if t.get("best_finish_round") is not None else 99,
        prev.get(t["id"], n_prev),
        t["name"],
    )


def _team_perf(team, teams_by_id):
    """Supplier blending from Race.py: if the team has a supplier stage > 0,
    effective perf = (2*own + supplier) / 3."""
    if team.get("sup_stage", 0) > 0 and team.get("supplier_id"):
        sup = teams_by_id.get(team["supplier_id"])
        if sup:
            return (team["perf"] * 2 + sup["perf"]) / 3
    return team["perf"]


SESSION_FORM_QUAL_SIGMA = 0.35   # seconds, one draw per driver per qual
SESSION_FORM_RACE_SIGMA = 0.22   # seconds per lap, one draw per driver per race


def simulate_qual(career, rng=None):
    """Sets qual_time / qual_pos on every F1 driver. Returns ordered list of
    (driver, team) from pole to last."""
    rng = rng or random
    teams = career["teams"]
    drivers = {d["id"]: d for d in career["drivers"]}
    by_id = {t["id"]: t for t in teams}
    perfs = sorted([t["perf"] for t in teams], reverse=True)
    max_perf, min_perf = perfs[0], perfs[-1]
    entries = []
    for team in teams:
        t_perf = _team_perf(team, by_id)
        for did in (team["driver1_id"], team["driver2_id"]):
            if not did:
                continue
            d = drivers[did]
            # Tuning: +tuning for driver 1, -tuning for driver 2 (Race.py).
            w = d["perf"] + (team["tuning"] * 1.0) if did == team["driver1_id"] \
                else d["perf"] - (team["tuning"] * 1.0)
            # v0.2: per-session form — one draw per driver per session.
            d["qual_time"] = lap_sim(w, t_perf, max_perf, min_perf, 1, 2.5, rng) \
                + rng.gauss(0, SESSION_FORM_QUAL_SIGMA)
            entries.append((d, team))
    entries.sort(key=lambda e: e[0]["qual_time"])
    for pos, (d, _t) in enumerate(entries, start=1):
        d["qual_pos"] = pos
    return entries


def simulate_race(career, laps=57, rng=None):
    """Full-distance race (v0.1): every driver runs `laps` laps, summing
    per-lap race_lap_time draws, plus a one-off grid penalty. Returns
    ordered list of (driver, team). race_time is the summed total (~5,000s
    for a 57-lap race) and is kept server-side only — results payloads
    carry gaps, never times.
    """
    rng = rng or random
    teams = career["teams"]
    drivers = {d["id"]: d for d in career["drivers"]}
    by_id = {t["id"]: t for t in teams}
    entries = []
    for team in teams:
        t_perf = _team_perf(team, by_id)
        for did in (team["driver1_id"], team["driver2_id"]):
            if not did:
                continue
            d = drivers[did]
            is_d1 = did == team["driver1_id"]
            # v0.2: per-session form — one draw per driver per race, the
            # same offset every lap (a fresh draw, different from qual).
            form = rng.gauss(0, SESSION_FORM_RACE_SIGMA)
            total = sum(race_lap_time(d["perf"], t_perf, team["tuning"],
                                      is_d1, rng, session_offset=form)
                        for _ in range(laps))
            total += RACE_GRID_PENALTY_PER_POS * (d.get("qual_pos") or 0)
            d["race_time"] = total
            entries.append((d, team))
    entries.sort(key=lambda e: e[0]["race_time"])
    return entries


def simulate_f2(career, rng=None):
    """From Race.py simulateRace2(): F2 drivers race over 30 laps on driver
    perf alone. Returns ordered F2 drivers; points are awarded by the caller
    (F2 uses the same top-10 points in the original)."""
    rng = rng or random
    f2 = [d for d in career["drivers"] if d["status"] == "f2"]
    for d in f2:
        total = 0.0
        for _ in range(30):
            total += BASELINE_LAP - ((d["perf"] / 100) + (rng.random() * 1.2))
        d["race_time"] = total
    f2.sort(key=lambda d: d["race_time"])
    return f2


def apply_race_results(career, race_entries, points_dist):
    """From Race.py displayRaceResults(): points, best pos, rep, prize money.

    Per driver: points for top 10, best_pos update, rep update
    new_rep = (race_rep + rep*11) / 12 where race_rep = 5 - (pos/(n*2))*3.2,
    team money += 1.95 - 0.025*pos. Team points are recomputed afterwards.
    """
    n_teams = len(career["teams"])
    teams_by_id = {t["id"]: t for t in career["teams"]}
    for pos, (d, team) in enumerate(race_entries, start=1):
        d["race_pos"] = pos
        # v0.4: best finish + the round it was achieved (standings
        # tie-break "whoever got there first"), podiums, and the
        # finishing-position sum/count (bot drop rule uses the average).
        if d.get("best_pos") is None or pos < d["best_pos"]:
            d["best_pos"] = pos
            d["best_finish_round"] = career["round"]
        if pos <= 3:
            d["season_podiums"] = d.get("season_podiums", 0) + 1
        d["finish_sum"] = d.get("finish_sum", 0) + pos
        d["finish_count"] = d.get("finish_count", 0) + 1
        # v0.4.4: F1 starts this season — the standings rule (a driver
        # who raced in F1 stays registered even with 0 points). The F2
        # loop has its own finish_count and must NOT touch this.
        d["f1_races"] = d.get("f1_races", 0) + 1
        # v0.5: CAREER F1 totals — every starter's career starts +1;
        # the winner's career wins +1 (below, with season_wins).
        # These never reset. F2 races never touch them.
        d["gp_starts"] = d.get("gp_starts", 0) + 1
        if pos <= len(points_dist):
            # Championship points follow the driver; team_points are
            # the points scored FOR this team (v0.2.1) — only these
            # count toward the constructor total.
            d["points"] += points_dist[pos - 1]
            d["team_points"] = d.get("team_points", 0) + points_dist[pos - 1]
        if pos == 1:
            d["season_wins"] += 1
            d["gp_wins"] = d.get("gp_wins", 0) + 1  # v0.5: career win
            # v0.4: a constructor win is a race win by either driver.
            team["season_wins"] = team.get("season_wins", 0) + 1
        # v0.4: the team's best finish is the best race finish by a
        # driver racing FOR this team in this race, and the round it
        # happened — tracked at race time, not recomputed from the
        # drivers afterwards (a driver's best finish may have been
        # scored for another team earlier in the season).
        if team.get("best_pos") is None or pos < team["best_pos"]:
            team["best_pos"] = pos
            team["best_finish_round"] = career["round"]
        team["money"] += 1.95 - (0.025 * pos)
        race_rep = 5 - (pos / (n_teams * 2)) * 3.2
        d["rep"] = (race_rep + d["rep"] * 11) / 12
    for team in career["teams"]:
        # v0.2.1: a constructor total is points banked from departed
        # drivers (points they scored FOR this team) plus the points
        # its current drivers scored for THIS team — never a driver's
        # championship total, which may include points scored for
        # another team earlier in the season.
        pts = team.get("points_kept", 0)
        for did in (team["driver1_id"], team["driver2_id"]):
            if did:
                dd = next((x for x in career["drivers"] if x["id"] == did), None)
                if dd:
                    pts += dd.get("team_points", 0)
        team["points"] = pts
        # (v0.4: team best_pos / best_finish_round are tracked at race
        # time above, not recomputed here.)


def driver_standings(career):
    """From Race.py updateDriverChampionship().

    v0.4 (feature 1): ties sort by points, then wins, then best
    finish, then whoever achieved that best finish FIRST (earliest
    round); the final tie-break is last season's finishing order
    (career["prev_driver_order"]) — so at the start of a new season,
    with everyone on 0 points, the standings show last season's
    order, like real F1. Drivers with no previous order (promoted F2
    drivers, new signings) go after the carried-over drivers, ordered
    by best finish, then driver perf, then name. Points always sort
    first.
    v0.2 (revised): a replaced driver keeps their points and stays in
    the standings, so free agents with points this season are listed
    too (they scored those points as an F1 driver). F2 points do not
    count — only 'free' drivers with points > 0 are included — and
    free-agent points are reset at season end (game.end_of_season), so
    a listed free agent always scored this season. Callers that need
    F1 drivers only (season rollover, retirement rumours) filter on
    status == 'f1' themselves.
    v0.4.4 (Matthew's rule): a driver who COMPETED in an F1 race this
    season is registered in the standings even with 0 points — so a
    driver swapped to T&R or dropped to free agency after racing
    (e.g. Stroll, P19 in Bahrain, then swapped for Vandoorne) stays
    listed, like real F1, where a replaced driver stays in the table.
    f1_races counts F1 starts only (F2 races do not count, and an F2
    driver signed into F1 starts at 0), so F2 drivers never leak in."""
    f1 = [d for d in career["drivers"]
          if d["status"] == "f1" or (d["status"] == "free" and d["points"] > 0)
          or d.get("f1_races", 0) > 0]
    prev = {did: i for i, did in enumerate(career.get("prev_driver_order") or [])}
    return sorted(f1, key=lambda d: _driver_sort_key(d, prev))


def f2_standings(career):
    """v0.4 (feature 2): the background F2 championship, drivers only
    (no F2 teams), sorted with the same rule as the F1 drivers'
    championship — points, wins, best finish, earliest."""
    f2 = [d for d in career["drivers"] if d["status"] == "f2"]
    return sorted(f2, key=lambda d: _driver_sort_key(d, {}))


def constructor_standings(career):
    """From Race.py updateConstructorChampionship().

    v0.4 (feature 1): points, then team wins (race wins by either
    driver), then best finish, then earliest; the final tie-break is
    last season's constructors' order (career["prev_team_order"]),
    so a new season opens in last season's order."""
    prev = {tid: i for i, tid in enumerate(career.get("prev_team_order") or [])}
    return sorted(career["teams"],
                  key=lambda t: _team_sort_key(t, prev))
