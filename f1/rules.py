"""Clean rules module — formulas taken from the original Rules.py / Team.py.

No globals, no input(). Pure functions so tests can call them directly.
"""
import math
import random

SEASON_LENGTH = 12
REG_CHANGE_YEAR = 2026
REG_INTERVALS = 4
MAX_SEASONS = 30
POINTS_DIST = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]

# v0 decision: reg-change targets. The original Main.py had the reg-change call
# commented out with a TODO and a target formula (50 + perf - last perf) that
# would have wiped the grid; the task spec says minor every year, major every
# 4 years. Minor 12 / major 35 keeps research (capped at 70% of target) able
# to soften but never cancel a change, per regChange() itself.
REG_MINOR_TARGET = 12.0
REG_MAJOR_TARGET = 35.0


def update_points_dist(grid_size):
    """From Rules.py: top 10 for a 22-driver grid (our grid)."""
    if grid_size <= 22:
        return [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]
    if grid_size <= 26:
        return [25, 18, 15, 12, 10, 8, 6, 5, 4, 3, 2, 1]
    return [25, 18, 15, 12, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]


def reg_change(grid_avg, target, perf, research, rng=None):
    """From Rules.py regChange(), verbatim logic.

    base_loss = -target; research_gain = 12*log1p(research), capped at 70% of
    target; catch-up bonus max(0, (grid_avg - perf) * 0.35); chaos
    uniform(-4, 4); floor new_perf >= grid_avg * 0.72.
    """
    rng = rng or random
    base_loss = -target
    research_gain = 12 * math.log1p(research)
    research_gain = min(research_gain, target * 0.70)
    perf_gap = grid_avg - perf
    catchup_bonus = max(0, perf_gap * 0.35)
    chaos = rng.uniform(-4, 4)
    final_change = base_loss + research_gain + catchup_bonus + chaos
    new_perf = perf + final_change
    new_perf = max(new_perf, grid_avg * 0.72)
    return round(new_perf, 2)


def reg_squeeze(perfs):
    """From Rules.py regSqueeze(): compresses a too-spread field toward the top.

    Returns a dict {index: new_perf}. Only applied when spread > 150.
    """
    ordered = sorted(perfs, reverse=True)
    max_perf, min_perf = ordered[0], ordered[-1]
    perf_diff = max_perf - min_perf
    a_temp = (max_perf - 150) - min_perf
    out = {}
    for i, p in enumerate(perfs):
        if perf_diff > 150:
            out[i] = p + (a_temp * ((max_perf - p) / perf_diff)) if perf_diff else p
        else:
            out[i] = p
    return out


def add_perf_gain(perf, investment):
    """From Team.py addPerf(): diminishing returns as perf grows.

    performance += ((400 / performance) ** 2.5) * (investment / 3)
    """
    if perf <= 0:
        return 0.0
    return ((400.0 / perf) ** 2.5) * (investment / 3.0)


def add_rel_gain(investment):
    """From Team.py addRel(): reliability += investment / 100."""
    return investment / 100.0


def dsus(age, perf, rng=None):
    """Driver Skill Updating System, from Rules.py DSUS().

    Returns the skill change for a driver of the given age/perf. Young
    drivers improve, older drivers decline, clamped to age-specific bands.
    """
    rng = rng or random

    def clamp(min_rating, max_rating, change):
        new = perf + change
        if new < min_rating:
            return min_rating - perf
        if new > max_rating:
            return max_rating - perf
        return change

    if age in (12, 13, 14, 15, 16):
        return rng.randint(2, 4)
    if age == 17:
        return rng.randint(2, 5)
    if age == 18:
        return rng.randint(2, 5)
    if age in (19, 20, 21):
        return rng.randint(2, 3)
    if age in (22, 23, 24):
        return rng.randint(1, 2)
    if age == 25:
        return clamp(71, 100, rng.randint(0, 1))
    if age == 26:
        return clamp(72, 100, rng.randint(0, 1))
    if age == 27:
        return clamp(72, 99, -rng.randint(0, 1))
    if age == 28:
        return clamp(72, 98, -rng.randint(0, 1))
    if age == 29:
        return clamp(72, 96, -rng.randint(0, 2))
    if age == 30:
        return clamp(71, 94, -rng.randint(1, 2))
    if age == 31:
        return clamp(71, 93, -rng.randint(0, 1))
    if age == 32:
        return clamp(71, 92, -rng.randint(0, 1))
    if age == 33:
        return clamp(70, 91, -rng.randint(0, 1))
    if age == 34:
        return clamp(70, 90, -rng.randint(0, 1))
    if age == 35:
        return clamp(70, 89, -rng.randint(0, 1))
    if age == 36:
        return clamp(69, 88, -rng.randint(0, 1))
    if age == 37:
        return clamp(69, 88, -rng.randint(0, 1))
    if age == 38:
        return clamp(68, 88, -rng.randint(0, 1))
    if age == 39:
        return clamp(67, 87, -rng.randint(0, 1))
    if age == 40:
        return clamp(66, 87, -rng.randint(0, 1))
    if age == 41:
        return clamp(65, 87, -rng.randint(0, 1))
    if age == 42:
        return clamp(64, 86, -rng.randint(0, 1))
    if age == 43:
        return clamp(63, 86, -rng.randint(0, 1))
    if age == 44:
        return clamp(62, 86, -rng.randint(0, 1))
    if age == 45:
        return clamp(61, 86, -rng.randint(0, 1))
    if age == 46:
        return clamp(60, 86, -rng.randint(0, 1))
    if age == 47:
        return clamp(59, 86, -rng.randint(0, 1))
    if age == 48:
        return clamp(58, 86, -rng.randint(0, 1))
    if age == 49:
        return clamp(57, 86, -rng.randint(0, 1))
    return 0


def retirement_chance(age, championships, team_pos, driver_points, team_points,
                      ret_min_age=42):
    """From Main.py isRetiring(d, isRumour=True).

    Returns the retirement chance in percent (0-100). Hard cap: age >= 49
    always retires. Note: Main.py's retMinAge decrements were `==`
    comparisons (no-ops), so it stays 42 — preserved here as the default.
    """
    if age < ret_min_age + (championships / 1.5):
        return 0.0
    base = 10.0 + (age - ret_min_age) * 5
    if team_points > 0:
        performance_factor = driver_points / team_points
        team_factor = (0.5 - performance_factor) * (100 + (team_pos - 0.5) * 5)
        chance = base + team_factor
    else:
        chance = base + 35.0
    if age >= 49:
        return 100.0
    return chance


def is_retiring(age, championships, team_pos, driver_points, team_points,
                ret_min_age=42, rng=None):
    rng = rng or random
    if age >= 49:
        return True
    chance = retirement_chance(age, championships, team_pos, driver_points,
                               team_points, ret_min_age)
    if chance <= 0:
        return False
    return rng.random() * 100 <= chance


def finance_estimates(constructor_pos):
    """From Main.py Finances screen (displayed as estimates there).

    EPSCI: post-season cash injection. EPRG: estimated per-race gains.
    In v0 these are applied for real (see game.py): EPRG is paid after each
    race, EPSCI at the end of the season.
    """
    epsci = 9.0 - (0.50 * constructor_pos)
    if constructor_pos == 1:
        epsci += 1.50
    eprg = (1.75 - (0.025 * (constructor_pos * 2))) + \
        (1.75 - (0.025 * ((constructor_pos * 2) - 1)))
    return {"epsci": round(epsci, 2), "eprg": round(eprg, 2)}
