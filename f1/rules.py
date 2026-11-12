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


# ---------------------------------------------------------------------------
# v0.4 — regulation research revamp (feature 7). The old reg_change()
# above is kept for the v0.1–v0.3 tests and as the formula family this
# is adapted from. The new model works on ADAPTED car perf, not on the
# current car perf:
#
#   new_perf = adapted + research_gain + chaos
#   research_gain = 12 * log1p(research), capped at 70% of target
#   chaos = uniform(-4, 4)
#
# with one hard rule: if a team adapted nothing and researched
# nothing, the new car is 0 perf — exactly (no chaos, no catch-up,
# no floor). The catch-up bonus and the 72%-of-average floor from
# the old formula are deliberately NOT applied here: both would
# resurrect a car that adapted nothing, contradicting the "0 perf"
# rule, and the unadapted formula itself already compresses the
# field (a top team must adapt far more than a backmarker, and a
# backmarker starts fully adapted). The research gain keeps the
# 12*log1p shape and the 70%-of-target cap from regChange().
# ---------------------------------------------------------------------------
REG_ADAPT_COST_PER_POINT = 0.25   # $M per point of car perf adapted
REG_OPENS_ROUND = 7               # reg research opens at round 7
WINTER_BREAK_ROUNDS = 3           # v0.4: winter break is 3 offer rounds
TR_RESEARCH_BONUS = 0.25          # v0.4: +25% research per T&R driver

# v0.4.2: no hard cap on T&R or academy size — an exponential fine
# instead, charged per race. The first TR_FINE_FREE_COUNT T&R
# drivers (2) and the first ACADEMY_FINE_FREE_COUNT academy drivers
# (3) are free; the j-th extra driver beyond that costs
# $0.5M x 2^(j-1) per race: 1 extra = $0.5M, 2 extra = $1.5M,
# 3 extra = $3.5M. (Matthew's decision, 2026-11-11.)
TR_FINE_FREE_COUNT = 2
ACADEMY_FINE_FREE_COUNT = 3
EXTRA_DRIVER_FINE_BASE = 0.5     # $M per race for the first extra driver

# v0.4.2: academy loyalty — in winter-break offer resolution, an
# academy driver's own academy team's offer counts as if that team
# finished ACADEMY_LOYALTY_BOOST places higher in the constructors'
# championship (so a 5th-place academy team beats a 2nd-place rival
# for its own junior, and a 1st-place academy team always wins).
ACADEMY_LOYALTY_BOOST = 3
# v0.4.2: only unsigned drivers aged 25 or under may JOIN an academy
# (F2 or free agent). There is no age-out: members already in an
# academy stay until they sign a main/T&R contract or are released
# (e.g. the CSV's 26-year-old academy drivers Miyata and Herta are
# grandfathered in at the 2026 start).
ACADEMY_MAX_JOIN_AGE = 25


def extra_driver_fine(count, free_count):
    """v0.4.2 exponential fine, per race, for roster extras beyond
    the free allowance. count = how many T&R (or academy) drivers
    the team has; free_count = how many are free (2 for T&R, 3 for
    the academy). The j-th extra costs $0.5M x 2^(j-1) per race, so
    1 extra = $0.5M, 2 extra = $1.5M, 3 extra = $3.5M."""
    extras = max(0, int(count) - int(free_count))
    if extras <= 0:
        return 0.0
    return round(EXTRA_DRIVER_FINE_BASE * (2 ** extras - 1), 2)


# ---------------------------------------------------------------------------
# v0.4.2 — table-driven driver rating development (replaces DSUS()).
# The table is Attachment #2 ("Driver Rating Per Age"): age -> (Min,
# Average, Max skill), ages 12-49. The RATINGS in the attachments are
# NOT changed — this only changes how ratings DEVELOP at season end.
#
# At season end every driver ages +1 and their rating changes by a
# random integer drawn from the range implied by the table's
# year-over-year deltas for that age step:
#   low  = min(age+1) - min(age)
#   high = max(age+1) - max(age)
# (swapped when low > high). v0.4.3: the low end is the MIN-column
# delta (Matthew's choice — 2026-11-11); 20->21 still gives [2,3],
# but 26->27 is [0,1] (it was [0,0] under the v0.4.2 average delta).
# Examples from the spec: age 20->21 gives [2,3] (min 63->65, max
# 88->91) — Matthew's Câmara example; 25->26 gives [0,1]; 26->27
# gives [0,1]; 30->31 gives [-2,-1] (older drivers decline, never
# stay flat). The result is clamped to [min(age+1), max(age+1)] — so
# a driver who starts above the curve (Kimi Antonelli: 87 at 19,
# where the max is 85) is NEVER nerfed down, but can only grow up
# to the next age's max (88 at 20). Academy drivers get a 70%
# chance of the high end of the range (the smaller decline in
# decline years); everyone else draws uniformly.
# ---------------------------------------------------------------------------
RATING_TABLE = {
    12: (47, 51, 55), 13: (49, 54, 59), 14: (51, 57, 63),
    15: (53, 60, 67), 16: (55, 63, 71), 17: (57, 66, 75),
    18: (59, 70, 80), 19: (61, 73, 85), 20: (63, 76, 88),
    21: (65, 78, 91), 22: (67, 81, 94), 23: (68, 82, 96),
    24: (69, 84, 98), 25: (70, 85, 100), 26: (71, 86, 100),
    27: (72, 86, 100), 28: (72, 86, 99), 29: (72, 85, 98),
    30: (72, 84, 96), 31: (71, 83, 94), 32: (71, 82, 93),
    33: (71, 82, 92), 34: (70, 81, 91), 35: (70, 80, 90),
    36: (70, 80, 89), 37: (69, 79, 88), 38: (69, 79, 88),
    39: (68, 78, 88), 40: (67, 77, 87), 41: (66, 77, 87),
    42: (65, 76, 87), 43: (64, 75, 86), 44: (63, 75, 86),
    45: (62, 74, 86), 46: (61, 74, 86), 47: (60, 73, 86),
    48: (59, 73, 86), 49: (58, 72, 86),
}


def rating_growth_range(age):
    """(low, high) integer rating change for the age step age -> age+1,
    from the rating table's min- and max-skill deltas (v0.4.3: the
    low end is the MIN-column delta, Matthew's choice)."""
    cur = RATING_TABLE.get(age)
    nxt = RATING_TABLE.get(age + 1)
    if cur is None or nxt is None:
        return (0, 0)
    low = nxt[0] - cur[0]    # min-skill delta
    high = nxt[2] - cur[2]   # max-skill delta
    if low > high:
        low, high = high, low
    return (low, high)


def grow_rating(age, perf, is_academy=False, rng=None):
    """v0.4.2 rating development: the driver's NEW rating after the
    season-end step from age to age+1. age is the age BEFORE the
    birthday. Returns an int, clamped to [min(age+1), max(age+1)]."""
    rng = rng or random
    low, high = rating_growth_range(age)
    if is_academy and rng.random() < 0.70:
        change = high
    else:
        change = rng.randint(low, high)
    row = RATING_TABLE.get(age + 1)
    if row is None:
        return int(perf)
    return int(max(row[0], min(row[2], perf + change)))


def reg_change_v2(adapted, research, target, rng=None):
    """v0.4 reg change: next season's starting car perf from the
    adapted part of the car plus research gains.

    adapted: perf points already fitted to the new regulations.
    research: $M invested in regulation research after full adaptation.
    target: 12 (minor year) or 35 (major year) — the research gain is
    capped at 70% of it, as in regChange().
    Returns 0.0 exactly when nothing was adapted or researched."""
    rng = rng or random
    adapted = float(adapted or 0.0)
    research = float(research or 0.0)
    if adapted <= 0 and research <= 0:
        return 0.0
    research_gain = min(12 * math.log1p(research), target * 0.70)
    chaos = rng.uniform(-4, 4)
    return round(max(0.0, adapted + research_gain + chaos), 2)


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


# v0.5.2: Develop gain — no hard cap (Matthew never likes hard caps).
# Two diminishing returns instead:
#   (1) headroom: gain scales with (400 - perf)/400, so research gets
#       less effective the closer a car is to perf 400 and fades to
#       zero at 400;
#   (2) sqrt(pool): doubling the banked research only multiplies the
#       gain by ~1.41x, so saving up money at a low perf and dumping
#       it all at once is inefficient — developing regularly wins.
# The old formula ((400/perf)**2.5 * pool/3) exploded at low perf
# with a big pool (verified live: $7M at perf 125 gave +88.7,
# Aston 125 -> 213.7, top of the chart). The driver development
# factor is applied by the caller (do_develop), not here.
DEV_GAIN_K = 12.0


def add_perf_gain(perf, investment):
    """v0.5.2: performance += DEV_GAIN_K * ((400 - perf)/400) * sqrt(pool).

    Calibrated so the normal round is unchanged: $3M at perf 165
    (pool 6.0) gains ~20.9 before the driver factor (old: 22.1),
    while the $7M dump at perf 125 gains ~34.2 (old: 88.7).
    """
    if perf >= 400.0:
        return 0.0
    headroom = min(1.0, max(0.0, (400.0 - perf) / 400.0))
    pool = max(0.0, investment)
    return DEV_GAIN_K * headroom * math.sqrt(pool)


def add_rel_gain(investment):
    """From Team.py addRel(): reliability += investment / 100."""
    return investment / 100.0


# v0.4.2: DSUS() (the old age-banded skill updater from Rules.py)
# is GONE, replaced by grow_rating() above — the table-driven
# development from Attachment #2. The old bands disagreed with the
# table (e.g. they capped a 19-year-old at 85 while Antonelli sits
# at 87); the new function clamps to the table instead, so no
# rating in the attachments ever changes, and growth follows the
# table's year-over-year deltas. Retirement logic below is unchanged.


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
