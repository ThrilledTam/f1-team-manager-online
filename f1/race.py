"""Race simulation — formulas from the original Race.py, cleaned up.

lapSim, simulateQual and simulateRace keep the original maths. One preserved
quirk: in the original simulateRace the 80-lap loop overwrote race_time on
every lap, so the final race time is effectively a single lapSim draw, then
(race_time + qual_pos) * 3.0. We keep that exact behaviour rather than
inventing a different race model — the ordering is still driven by driver
perf, team perf (with supplier blending) and tuning.
"""
import random

BASELINE_LAP = 75.000


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


def _team_perf(team, teams_by_id):
    """Supplier blending from Race.py: if the team has a supplier stage > 0,
    effective perf = (2*own + supplier) / 3."""
    if team.get("sup_stage", 0) > 0 and team.get("supplier_id"):
        sup = teams_by_id.get(team["supplier_id"])
        if sup:
            return (team["perf"] * 2 + sup["perf"]) / 3
    return team["perf"]


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
            d["qual_time"] = lap_sim(w, t_perf, max_perf, min_perf, 1, 2.5, rng)
            entries.append((d, team))
    entries.sort(key=lambda e: e[0]["qual_time"])
    for pos, (d, _t) in enumerate(entries, start=1):
        d["qual_pos"] = pos
    return entries


def simulate_race(career, rng=None):
    """Sets race_time / race_pos. Returns ordered list of (driver, team).

    Preserves Race.py: race_time = (lapSim(...) + qual_pos) * 3.0.
    (The original looped 80 times overwriting the value; net effect is one
    draw, so we do one draw.)
    """
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
            w = d["perf"] + (team["tuning"] * 4.0) if did == team["driver1_id"] \
                else d["perf"] - (team["tuning"] * 4.0)
            lap = lap_sim(w, t_perf, max_perf, min_perf, 2, 25.0, rng)
            d["race_time"] = (lap + (1.0 * (d.get("qual_pos") or 0))) * 3.0
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
        if d.get("best_pos") is None or pos < d["best_pos"]:
            d["best_pos"] = pos
        if pos <= len(points_dist):
            d["points"] += points_dist[pos - 1]
        if pos == 1:
            d["season_wins"] += 1
        team["money"] += 1.95 - (0.025 * pos)
        race_rep = 5 - (pos / (n_teams * 2)) * 3.2
        d["rep"] = (race_rep + d["rep"] * 11) / 12
    for team in career["teams"]:
        pts = 0
        for did in (team["driver1_id"], team["driver2_id"]):
            if did:
                dd = next((x for x in career["drivers"] if x["id"] == did), None)
                if dd:
                    pts += dd["points"]
        team["points"] = pts
        # team best pos = best of its drivers
        bps = [next((x for x in career["drivers"] if x["id"] == did), {}).get("best_pos")
               for did in (team["driver1_id"], team["driver2_id"]) if did]
        bps = [b for b in bps if b is not None]
        team["best_pos"] = min(bps) if bps else None


def driver_standings(career):
    """From Race.py updateDriverChampionship(): by points desc, best pos."""
    f1 = [d for d in career["drivers"] if d["status"] == "f1"]
    return sorted(f1, key=lambda d: (-d["points"], d["best_pos"] if d["best_pos"] is not None else 99))


def constructor_standings(career):
    """From Race.py updateConstructorChampionship()."""
    return sorted(career["teams"],
                  key=lambda t: (-t["points"], t["best_pos"] if t["best_pos"] is not None else 99))
