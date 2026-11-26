"""Tests for F1 Team Manager Online v0.1.1.

Deterministic: every test that touches randomness passes a seeded
random.Random. Run with:  python -m pytest tests/ -q
"""
import json
import random
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from f1 import game
from f1.data import CALENDAR, circuit_for_round
from f1.rules import (
    POINTS_DIST, add_perf_gain, finance_estimates, reg_change,
    is_retiring, retirement_chance,
)
from f1.saves import delete_save, list_saves, load_save, migrate_save, save_career


def seeded(seed=1234):
    return random.Random(seed)


class _FixedRng:
    """Deterministic stand-in for random: random() always returns
    `value`. Used for the v0.7.0 probability layers (bot donation
    acceptance, proposal acceptance) so a test can force the accept
    branch (0.0) or the decline branch (0.99) exactly."""

    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


def _ttexts(career):
    """Transfer entry texts — v0.8.1 entries are dicts ({text, year,
    round, label}); older saves carry plain strings."""
    return [(t["text"] if isinstance(t, dict) else t)
            for t in (career.get("transfers") or [])]


def two_human_career():
    career = game.build_new_career("Test Career", "Matthew", chart_on=True)
    ok, _ = game.choose_team(career, career["teams"][0]["id"], "p-matthew")
    assert ok
    ok, _ = game.choose_team(career, career["teams"][1]["id"], "p-cousin")
    assert ok
    return career


def play_full_round(career, rng):
    """Drive one round to completion the v0.1 way: all humans Advance,
    then everyone votes NEXT on the qual page, then on the race page.
    v0.6.4: the vote/advance count ALL human players (no connected
    set is passed any more)."""
    for t in list(game.human_teams(career)):
        game.advance_team(career, t["id"], rng=rng)
    assert career["phase"] == "qual_results"
    for t in list(game.human_teams(career)):
        game.vote_next(career, t["owner_player_id"], rng=rng)
    assert career["phase"] == "race_results"
    for t in list(game.human_teams(career)):
        game.vote_next(career, t["owner_player_id"], rng=rng)
    assert career["phase"] in ("round", "winter_break")


def play_winter_break(career, connected, rng):
    """Drive the 3-round winter-break offer flow (v0.4): each round,
    every human team with an empty main seat offers a free agent / F2
    driver for it, then every human hits NEXT. Bot offers are made
    when the round opens (bots are always ready), and the round
    resolves when every human is ready."""
    assert career["phase"] == "winter_break"
    for _ in range(3):
        assert career["wb_round"] <= 3
        for team in game.human_teams(career):
            offers = []
            for seat in game.empty_seats(career, team):
                pool = game.free_agents(career) + game.f2_drivers(career)
                assert pool, "no drivers left to fill a seat"
                offers.append({"driver_id": pool[0]["id"], "target": "seat",
                               "seat": seat, "years": 2})
            if offers:
                ok, why = game.submit_winter_offers(career, team["id"], offers)
                assert ok, why
        for team in game.human_teams(career):
            ok, _adv, why = game.winter_next(career, team["id"], rng=rng)
            assert ok, why
    assert career.get("wb_done")


# ---------------------------------------------------------------------------
# 1. New 2026 career
# ---------------------------------------------------------------------------

def test_new_career_2026_has_11_teams_and_correct_drivers():
    career = game.build_new_career("Test", "Matthew")
    assert career["year"] == 2026
    assert career["round"] == 1
    assert len(career["teams"]) == 11
    names = [t["name"] for t in career["teams"]]
    assert names == ["McLaren", "Mercedes", "Red Bull", "Ferrari", "Williams",
                     "Racing Bulls", "Aston Martin", "Haas", "Audi", "Alpine",
                     "Cadillac"]
    by_name = {t["name"]: t for t in career["teams"]}
    assert by_name["Mercedes"]["perf"] == 210.0
    assert by_name["Cadillac"]["perf"] == 110.0
    assert by_name["McLaren"]["rel"] == 0.935
    assert by_name["Williams"]["supplier_name"] == "Mercedes"
    assert by_name["McLaren"]["supplier_name"] == "Mercedes"
    d = {x["name"]: x for x in career["drivers"]}
    assert d["Lando Norris"]["team_id"] == by_name["McLaren"]["id"]
    assert d["Max Verstappen"]["perf"] == 91
    assert d["Lewis Hamilton"]["age"] == 41
    f1 = [x for x in career["drivers"] if x["status"] == "f1"]
    assert len(f1) == 22
    # v0.4.2: 22 of the 33 free agents are re-homed as Test &
    # Reserve drivers for their CSV teams (11 free agents left),
    # and 10 F2 drivers join a Driver Academy.
    assert len([x for x in career["drivers"] if x["status"] == "free"]) == 11
    assert len([x for x in career["drivers"] if x["status"] == "f2"]) == 22
    assert len([x for x in career["drivers"] if x["status"] == "tr"]) == 22
    assert len([x for x in career["drivers"]
                if x.get("academy_team_id")]) == 10
    # No 2025 data anywhere.
    assert career["year"] == 2026
    assert "2025" not in json.dumps(career["teams"])


def test_custom_team_uses_main_py_formula():
    career = game.build_new_career("Test", "Matthew")
    pool = game.free_agents(career)[:2]
    perfs = sorted((t["perf"] for t in career["teams"]), reverse=True)
    expected = (perfs[0] + 5 * perfs[-1]) / 6.0
    ok, _why, team = game.create_custom_team(
        career, "p1", "Taylor Racing", "#ff0000", pool[0]["id"], pool[1]["id"])
    assert ok
    assert len(career["teams"]) == 12
    assert abs(team["perf"] - expected) < 1e-9
    assert team["money"] == 6.0
    assert team["rel"] == 0.90
    assert pool[0]["status"] == "f1" and pool[0]["contract_left"] == 1


# ---------------------------------------------------------------------------
# 2. Simultaneous advance
# ---------------------------------------------------------------------------

def test_simultaneous_advance_blocks_until_all_humans():
    career = two_human_career()
    t1, t2 = career["teams"][0], career["teams"][1]
    ok, raced, _ = game.advance_team(career, t1["id"], rng=seeded())
    assert ok and not raced
    assert career["round"] == 1
    assert career["last_race"] is None
    ok, raced, _ = game.advance_team(career, t2["id"], rng=seeded(7))
    assert ok and raced
    # v0.1: the race ran, but the room is on the qualifying results page —
    # the round does NOT advance until the NEXT votes are done.
    assert career["phase"] == "qual_results"
    assert career["results_stage"] == "qual"
    assert career["round"] == 1
    assert career["last_race"] is not None
    assert len(career["last_race"]) == 22
    assert len(career["last_qual"]) == 22
    assert career["last_race"][0]["points"] == 25
    assert sum(d["points"] for d in career["drivers"]) > 0
    # One vote is not enough: the vote is unanimous.
    ok, advanced, msg = game.vote_next(career, "p-matthew", rng=seeded(7))
    assert ok and not advanced
    assert career["phase"] == "qual_results"
    ok, advanced, msg = game.vote_next(career, "p-cousin", rng=seeded(7))
    assert ok and advanced
    assert career["phase"] == "race_results"
    assert career["results_stage"] == "race"
    # Second vote returns everyone to their dashboards for round 2.
    ok, advanced, _ = game.vote_next(career, "p-matthew", rng=seeded(7))
    assert ok and not advanced
    ok, advanced, _ = game.vote_next(career, "p-cousin", rng=seeded(7))
    assert ok and advanced
    assert career["phase"] == "round"
    assert career["round"] == 2


def test_race_points_and_money_change():
    career = two_human_career()
    money_before = {t["id"]: t["money"] for t in career["teams"]}
    game.advance_team(career, career["teams"][0]["id"], rng=seeded())
    game.advance_team(career, career["teams"][1]["id"], rng=seeded(7))
    assert any(t["money"] != money_before[t["id"]] for t in career["teams"])
    assert career["teams"][0]["points"] == max(t["points"] for t in career["teams"])


# ---------------------------------------------------------------------------
# 3. Finances / research / develop formulas (Main.py)
# ---------------------------------------------------------------------------

def test_finance_formulas():
    assert abs(finance_estimates(1)["epsci"] - 10.0) < 1e-9
    assert abs(finance_estimates(1)["eprg"] - 3.42) < 0.011
    assert abs(finance_estimates(11)["epsci"] - 3.5) < 1e-9
    assert abs(finance_estimates(11)["eprg"] - 2.42) < 0.011


def test_research_and_develop_formulas():
    career = game.build_new_career("Test", "Matthew")
    # Racing Bulls starts with no T&R drivers, so the research
    # multiplier is x1.00 (McLaren/Mercedes start with 4 T&R
    # drivers and a x2.00 bonus — see the v0.4.2 T&R test).
    team = next(t for t in career["teams"] if t["name"] == "Racing Bulls")
    money0, perf0 = team["money"], team["perf"]
    ok, _ = game.do_research(career, team["id"], "perf", 1.0)
    assert ok
    assert team["perf_res"] == 1.0
    assert abs(team["reli_res"] - (-1.0 / 3.0)) < 1e-9
    assert team["money"] == money0 - 1.0
    ok, _ = game.do_research(career, team["id"], "reliability", 1.0)
    assert ok
    assert abs(team["reli_res"] - (-1.0 / 3.0 + 1.0)) < 1e-9
    assert abs(team["perf_res"] - (1.0 - 1.0 / 5.0)) < 1e-9
    ok, _ = game.do_develop(career, team["id"])
    assert ok
    assert team["perf_res"] == 0.0 and team["reli_res"] == 0.0
    assert team["perf"] > perf0  # banked perf research raised perf
    assert team["money"] == money0 - 3.0  # 1 + 1 research, 1 develop
    assert add_perf_gain(100.0, 1.0) > add_perf_gain(200.0, 1.0)  # diminishing


# ---------------------------------------------------------------------------
# v0.5.3 — Develop gain: headroom x sqrt(pool), no hard cap.
# The old formula ((400/perf)**2.5 * pool/3) exploded at low perf with
# a big pool (verified live: $7M at perf 125 gave +88.7, Aston
# 125 -> 213.7, top of the chart). The new formula fades to zero at
# perf 400 and gives the pool diminishing returns via sqrt, so the
# normal round is unchanged and dumping a hoarded pile is inefficient.
# ---------------------------------------------------------------------------

def test_dev_gain_worked_examples():
    """v0.5.3: the approved formula's worked examples, before the
    driver factor is applied in do_develop."""
    # Normal round: $3M at McLaren, perf 165, pool 6.0 (x2 T&R
    # bonus) -> ~17.27 raw, ~20.9 with the x1.21 driver factor
    # (old formula: 22.1 with the factor — basically unchanged).
    assert add_perf_gain(165.0, 6.0) == pytest.approx(17.27, abs=0.05)
    # The $7M dump at perf 125 (pool 12.25) -> ~28.88 raw,
    # ~34.2 with Aston's x1.186 driver factor (old: 88.7).
    assert add_perf_gain(125.0, 12.25) == pytest.approx(28.88, abs=0.05)
    # Same car, $3M instead (pool 5.25) -> ~18.90 raw, ~22.4 factored.
    assert add_perf_gain(125.0, 5.25) == pytest.approx(18.90, abs=0.05)
    # Top team: $3M at perf 210 (pool 6.0) -> ~13.96 raw, ~16.8 factored.
    assert add_perf_gain(210.0, 6.0) == pytest.approx(13.96, abs=0.05)
    # Near the ceiling: perf 350 (pool 6.0) -> ~3.67 raw, ~4.4 factored.
    assert add_perf_gain(350.0, 6.0) == pytest.approx(3.67, abs=0.05)
    # Banked 3 rounds then dumped (pool 18.0 at perf 165) -> ~29.91 raw.
    assert add_perf_gain(165.0, 18.0) == pytest.approx(29.91, abs=0.05)


def test_dev_gain_exploit_no_longer_tops_chart():
    """v0.5.3: the exact live exploit — Aston Martin, perf 125,
    $7M of Performance research (pool 12.25 after the x1.75 T&R
    bonus) — now gains ~34 and lands at ~159, below Mercedes 210,
    instead of +88.7 to 213.7."""
    career = game.build_new_career("Test", "Matthew")
    team = next(t for t in career["teams"] if t["name"] == "Aston Martin")
    team["money"] = 100.0
    team["perf"] = 125.0
    team["perf_res"] = 12.25
    team["reli_res"] = 0.0
    factor = game.team_dev_factor(career, team)
    ok, _ = game.do_develop(career, team["id"])
    assert ok
    assert team["perf"] == pytest.approx(125.0 + 28.875 * factor, abs=0.5)
    assert team["perf"] < 210.0  # below Mercedes, not top of the chart


def test_dev_gain_banked_dump_is_inefficient():
    """v0.5.3: saving up and dumping is inefficient — developing
    the same 18.0 of research in three 6.0 chunks (perf rising
    after each one) gains more in total than one 18.0 dump, and
    doubling a pool only multiplies the gain by sqrt(2) ~ 1.41x."""
    single = add_perf_gain(165.0, 18.0)
    p, total = 165.0, 0.0
    for _ in range(3):
        g = add_perf_gain(p, 6.0)
        total += g
        p += g
    assert single < total
    assert add_perf_gain(165.0, 12.0) < 2 * add_perf_gain(165.0, 6.0)
    assert add_perf_gain(165.0, 12.0) == pytest.approx(
        add_perf_gain(165.0, 6.0) * 2 ** 0.5)


def test_dev_gain_edge_cases():
    """v0.5.3: perf >= 400 gains nothing (headroom is zero); a
    zero or negative pool gains nothing and never crashes (pools
    go negative via the research cross-penalty). A 0-perf car —
    possible after a reg change with no adaptation — CAN recover
    via Develop (headroom 1.0); the old formula returned 0 for
    perf <= 0 and left such a car stuck forever."""
    assert add_perf_gain(400.0, 6.0) == 0.0
    assert add_perf_gain(450.0, 6.0) == 0.0
    assert add_perf_gain(165.0, 0.0) == 0.0
    assert add_perf_gain(165.0, -2.0) == 0.0
    assert add_perf_gain(0.0, 6.0) == pytest.approx(12.0 * 6.0 ** 0.5)


def test_tuning_bounds():
    career = game.build_new_career("Test", "Matthew")
    team = career["teams"][0]
    assert game.do_tuning(career, team["id"], 3)[0]
    assert game.do_tuning(career, team["id"], -3)[0]
    assert not game.do_tuning(career, team["id"], 4)[0]
    assert not game.do_tuning(career, team["id"], "x")[0]


def test_retirement_chance():
    assert retirement_chance(25, 0, 1, 0, 0) == 0.0
    # v0.7.2: no maximum retirement age — a 49-year-old frontrunner
    # is not forced out (the old formula returned 100.0 here).
    assert retirement_chance(49, 0, 1, 0, 0) == 28.5
    # Older + fewer titles + backmarker team share => higher chance.
    assert retirement_chance(45, 0, 11, 0, 100) > retirement_chance(45, 7, 1, 80, 100)


def test_v072_retirement_gentle_ramp_and_no_max_age():
    """v0.7.2 retirement rework: the chance starts at 30 (0 below
    it), rises GENTLY with age for a midfield driver, is much lower
    for a frontrunner with high reputation than a backmarker at the
    same age, lower in a rising team than a falling one — and there
    is no forced retirement at any age (a strong 50+ performer has
    a low chance; a 50+ backmarker is high but never certain)."""
    # P = 0 below 30.
    for age in (20, 25, 29):
        assert retirement_chance(age, 0, 6, 50, 100) == 0.0
    # Midfield driver (P6, half the team's points, average rep,
    # no wins, flat trajectory, midfield car): a gentle rise,
    # +7.5 points per 5 years — not very likely to retire in the
    # first few years after 30.
    midfield = {a: retirement_chance(a, 0, 6, 50, 100)
                for a in (30, 35, 40, 45, 50, 55)}
    assert midfield == {30: 10.0, 35: 17.5, 40: 25.0,
                        45: 32.5, 50: 40.0, 55: 47.5}
    # Same age, better position => lower chance (frontrunner with
    # a high reputation, wins and a rising team; backmarker with a
    # low reputation, no wins, a falling team and a slow car).
    for age in (45, 50, 55):
        front = retirement_chance(age, 0, 1, 60, 100, rep=4.4,
                                  wins=3, trajectory=2)
        back = retirement_chance(age, 0, 11, 0, 100, rep=1.5,
                                 wins=0, trajectory=-2, perf_gap=50)
        assert front < back, (age, front, back)
    assert retirement_chance(50, 0, 1, 60, 100, rep=4.4, wins=3,
                             trajectory=2) < 10.0  # strong 50yo: ~5%
    assert retirement_chance(55, 0, 11, 0, 100, rep=1.5, wins=0,
                             trajectory=-2, perf_gap=50) < 95.0
    # Rising team < falling team at the same age.
    assert retirement_chance(45, 0, 6, 50, 100, trajectory=2) == 24.5
    assert retirement_chance(45, 0, 6, 50, 100, trajectory=-2) == 40.5
    # No forced retirement at 49 / 50 / 55 — the chance stays below
    # 1.0 even for the worst case, and a roll can keep anyone.
    for age in (49, 50, 55):
        assert retirement_chance(age, 0, 11, 0, 100, rep=1.5, wins=0,
                                 trajectory=-2, perf_gap=50) < 100.0
    # is_retiring is one roll against the chance, never forced: a
    # 49-year-old frontrunner survives a high roll, and a 55-year-old
    # backmarker can survive a low one.
    assert not is_retiring(49, 0, 1, 0, 0, rng=seeded(0))
    assert not is_retiring(55, 0, 11, 0, 100, rng=seeded(2),
                           rep=1.5, wins=0, trajectory=-2, perf_gap=50)


# ---------------------------------------------------------------------------
# 4. Reg change
# ---------------------------------------------------------------------------

def test_reg_change_research_helps_but_cannot_cancel():
    grid_avg, perf = 160.0, 160.0
    low = reg_change(grid_avg, 35.0, perf, 0.0, rng=random.Random(1))
    high = reg_change(grid_avg, 35.0, perf, 50.0, rng=random.Random(1))
    assert high > low  # research recovers some of the loss
    # Even infinite research cannot fully cancel: cap is 70% of target.
    huge = reg_change(grid_avg, 35.0, perf, 10**9, rng=random.Random(1))
    assert huge < perf  # still a net loss (target 35, max gain 24.5, chaos <= 4)
    assert huge - low <= 35.0 * 0.70 + 1e-9


def test_reg_change_floor_and_major_gt_minor():
    grid_avg = 160.0
    # A backmarker with no research cannot collapse below the floor.
    assert reg_change(grid_avg, 35.0, 50.0, 0.0, rng=random.Random(2)) >= grid_avg * 0.72 - 1e-9
    minor = reg_change(grid_avg, 12.0, 160.0, 0.0, rng=random.Random(3))
    major = reg_change(grid_avg, 35.0, 160.0, 0.0, rng=random.Random(3))
    assert major < minor  # same chaos draw, bigger target = bigger loss


def test_full_season_rollover_applies_minor_reg_change():
    career = two_human_career()
    connected = {"p-matthew", "p-cousin"}
    rng = seeded(99)
    for _ in range(12):
        play_full_round(career, rng)
    assert career["phase"] == "winter_break"
    assert career["year"] == 2027
    assert career["next_reg_year"] == 2028
    # v0.4: the reg change is NOT applied at season end any more — it
    # waits until the winter break is done (start_next_season), so the
    # winter break's last-minute R&D counts. The pending change is the
    # 2027 Minor one, initialised at round 7.
    assert career.get("pending_reg_kind") == "Minor"
    assert career.get("pending_reg_year") == 2027
    assert career.get("last_reg_change") is None
    # Points reset for the new season.
    assert all(t["points"] == 0 for t in career["teams"])
    assert all(d["points"] == 0 for d in career["drivers"] if d["status"] == "f1")
    # Winter break can be completed: 3 rounds of offers, then start.
    play_winter_break(career, connected, rng)
    ok, why = game.start_next_season(career, rng=rng)
    assert ok, why
    assert career["phase"] == "round" and career["round"] == 1
    # The reg change applied at season start, into 2027 (Minor).
    assert career["last_reg_change"]["year"] == 2027
    assert career["last_reg_change"]["kind"] == "Minor"


# ---------------------------------------------------------------------------
# 5. Save / load round-trip + JSON shape
# ---------------------------------------------------------------------------

def test_save_load_round_trip_preserves_state():
    career = two_human_career()
    play_full_round(career, seeded(7))
    assert career["round"] == 2
    snapshot = {t["id"]: (t["points"], t["money"], t["perf"]) for t in career["teams"]}
    with tempfile.TemporaryDirectory() as tmp:
        meta = save_career(career, "Mid-season test", tmp)
        assert meta["year"] == 2026 and meta["round"] == 2
        assert [m["save_id"] for m in list_saves(tmp)] == [meta["save_id"]]
        loaded = load_save(tmp, meta["save_id"])
        assert loaded is not None
        assert loaded["year"] == career["year"] and loaded["round"] == career["round"]
        for t in loaded["teams"]:
            pts, money, perf = snapshot[t["id"]]
            assert t["points"] == pts
            assert abs(t["money"] - money) < 1e-9
            assert abs(t["perf"] - perf) < 1e-9
        # The save document is plain JSON (Firestore/Supabase compatible shape).
        json.dumps(loaded)
        assert loaded["version"] == 18  # v0.8.1 save format
        assert loaded["circuit"] == "Saudi Arabian Grand Prix"
        assert len(loaded["race_history"]) == 1
        assert loaded["race_history"][0]["circuit"] == "Bahrain Grand Prix"
        assert delete_save(tmp, meta["save_id"])
        assert list_saves(tmp) == []


# ---------------------------------------------------------------------------
# 6. Chart OFF hides perf + AI is results-based
# ---------------------------------------------------------------------------

def test_chart_off_hides_all_perf_numbers():
    career = two_human_career()
    state = game.public_state(career, chart_on=False, viewer_team_id=career["teams"][0]["id"])
    blob = json.dumps(state)
    assert '"perf"' not in blob
    # v0.6.3: your own car's reliability IS visible (it drives your DNF
    # chance, and the dashboard shows it) — other teams' is hidden.
    assert state["teams"][0]["rel"] == career["teams"][0]["rel"]
    assert "rel" not in state["teams"][1]
    # Your own research pools stay visible; other teams' do not.
    assert state["teams"][0]["perf_res"] == 0.0
    assert "perf_res" not in state["teams"][1]
    assert "reg_research" not in state["teams"][1]
    # The reg-change detail (old/new perf) is stripped too.
    state["last_reg_change"] = {"year": 2027, "kind": "Minor",
                                "changes": [{"team": "X", "old": 100, "new": 90}]}
    state = game.public_state(career, chart_on=False)
    assert "old" not in json.dumps(state["last_reg_change"])
    # Chart ON shows everything.
    on = game.public_state(career, chart_on=True)
    assert '"perf"' in json.dumps(on)


def test_chart_off_shows_own_reliability_v063():
    """v0.6.3: with the chart OFF, the dashboard payload shows your own
    car's reliability — but never perf, never driver perf, and never
    another team's reliability."""
    career = two_human_career()
    viewer = career["teams"][0]["id"]
    state = game.public_state(career, chart_on=False, viewer_team_id=viewer)
    # Your own reliability is there, with the exact value.
    assert state["teams"][0]["rel"] == career["teams"][0]["rel"]
    # No other team's reliability, no perf anywhere, no driver perf.
    assert "rel" not in state["teams"][1]
    assert all("perf" not in t for t in state["teams"])
    assert all("perf" not in d for d in state["drivers"])
    # With no viewer, nobody's reliability is shown.
    assert '"rel"' not in json.dumps(game.public_state(career, chart_on=False))
    # The private my_team payload (what the dashboard actually renders)
    # carries rel and never perf, chart on or off.
    import server as srv
    room = srv.new_room("Matthew", "#e74c3c")
    host = srv.add_player(room, "Matthew", "#e74c3c")
    room["career"] = game.build_new_career("T", "Matthew")
    game.choose_team(room["career"], room["career"]["teams"][0]["id"], host["player_id"])
    room["chart_on"] = False
    mt = srv.my_team_public(room, host["player_id"])
    assert mt["rel"] == room["career"]["teams"][0]["rel"]
    assert "perf" not in mt
    assert all("perf" not in d for d in mt["drivers"])


def test_ai_decisions_identical_when_perf_swapped_chart_off():
    def run(seed):
        c = game.build_new_career("T", "H")
        team = c["teams"][3]  # Ferrari
        rng = random.Random(seed)
        game.ai_act(c, team, rng=rng)
        return team
    a = run(42)
    # Swap perf values across the grid: decisions must not change.
    c2 = game.build_new_career("T", "H")
    perfs = [t["perf"] for t in c2["teams"]]
    for t, p in zip(c2["teams"], reversed(perfs)):
        t["perf"] = p
    team2 = c2["teams"][3]
    game.ai_act(c2, team2, rng=random.Random(42))
    assert team2["money"] == a["money"]
    assert team2["perf_res"] == a["perf_res"]
    assert team2["reli_res"] == a["reli_res"]
    assert team2["tuning"] == a["tuning"]
    assert team2["reg_research"] == a["reg_research"]


# ---------------------------------------------------------------------------
# 7. Lobby / rooms over WebSocket + mid-career takeover
# ---------------------------------------------------------------------------

def test_ws_lobby_kick_host_promotion_and_takeover():
    from fastapi.testclient import TestClient
    from server import app

    client = TestClient(app)

    def recv_until(ws, want, limit=40, check=None):
        for _ in range(limit):
            m = ws.receive_json()
            if m.get("type") == want and (check is None or check(m)):
                return m
        raise AssertionError(f"never got {want}")

    with client.websocket_connect("/ws") as host_ws:
        host_ws.send_json({"type": "create_room", "nickname": "Matthew", "colour": "#e74c3c"})
        joined = recv_until(host_ws, "joined")
        code, host_id = joined["code"], joined["player_id"]
        assert len(code) == 6

        with client.websocket_connect("/ws") as cousin_ws:
            cousin_ws.send_json({"type": "join_room", "code": code,
                                 "nickname": "Cousin", "colour": "#3498db"})
            c_joined = recv_until(cousin_ws, "joined")
            cousin_id = c_joined["player_id"]
            # Host kicks the cousin (lobby only).
            host_ws.send_json({"type": "kick_player", "player_id": cousin_id})
            kicked = recv_until(cousin_ws, "kicked")
            assert "removed" in kicked["message"]

        # Host starts a solo career (only the host is in the room now).
        host_ws.send_json({"type": "new_career", "name": "WS Career"})
        state = recv_until(host_ws, "room_state",
                           check=lambda m: m["room"]["career"] is not None)
        assert state["room"]["career"]["year"] == 2026
        team_id = state["room"]["career"]["teams"][0]["id"]
        host_ws.send_json({"type": "choose_team", "team_id": team_id})
        host_ws.send_json({"type": "start_career"})
        state = recv_until(host_ws, "room_state",
                           check=lambda m: m["room"]["started"] is True)
        assert state["room"]["started"] is True

        # Cousin joins mid-career and takes over an AI team.
        with client.websocket_connect("/ws") as cousin_ws:
            cousin_ws.send_json({"type": "join_room", "code": code,
                                 "nickname": "Cousin", "colour": "#3498db"})
            c2_joined = recv_until(cousin_ws, "joined")
            cousin_id = c2_joined["player_id"]
            ai_team = next(t for t in c2_joined["room"]["career"]["teams"]
                           if not t.get("owner_player_id"))
            cousin_ws.send_json({"type": "choose_team", "team_id": ai_team["id"]})
            state = recv_until(
                cousin_ws, "room_state",
                check=lambda m: any(t["id"] == ai_team["id"] and t.get("owner_player_id")
                                    for t in m["room"]["career"]["teams"]))
            taken = next(t for t in state["room"]["career"]["teams"]
                         if t["id"] == ai_team["id"])
            assert taken["owner_player_id"] == cousin_id

            # v0.6.4 RULE CHANGE (was: host disconnects -> the cousin
            # is promoted to host immediately). A brief disconnect
            # must NOT move the host crown — the cousin rejoins well
            # inside the 60s grace period, so the crown stays with
            # the original host (Matthew), who regains it on rejoin.
            # The 60s transfer itself is tested at the unit level in
            # test_host_crown_transfers_after_long_absence_v064.
    # (host_ws context exits here)
    with client.websocket_connect("/ws") as cousin_ws:
        cousin_ws.send_json({"type": "rejoin", "code": code,
                             "player_id": cousin_id})
        rejoined = recv_until(cousin_ws, "joined")
        assert rejoined["room"]["host_id"] == host_id
        assert rejoined["room"]["started"] is True


def test_team_takeable_grace_period():
    """v0.6.2: the takeover rule — AI teams are always takeable, a
    connected human's team never is, and a disconnected human's
    team becomes takeable once the 60s grace period has passed."""
    import time as _time
    import server as srv

    room = srv.new_room("Matthew", "#e74c3c")
    host = srv.add_player(room, "Matthew", "#e74c3c")
    career = game.build_new_career("Test", "Matthew")
    mclaren = career["teams"][0]
    ferrari = career["teams"][1]
    ok, _ = game.choose_team(career, mclaren["id"], host["player_id"])
    assert ok
    now = _time.time()
    # Connected owner's team: never takeable.
    ok, why = srv.team_takeable(room, mclaren, now=now)
    assert not ok and "still connected" in why
    # AI team: takeable.
    ok, _ = srv.team_takeable(room, ferrari, now=now)
    assert ok
    # Owner disconnects: still not takeable inside the grace period.
    host["connected"] = False
    host["disconnected_at"] = now
    ok, why = srv.team_takeable(room, mclaren, now=now + 10)
    assert not ok and "available in" in why
    ok, why = srv.team_takeable(room, mclaren, now=now + 59)
    assert not ok
    # Past the grace period: takeable.
    ok, _ = srv.team_takeable(room, mclaren, now=now + 60)
    assert ok
    ok, _ = srv.team_takeable(room, mclaren, now=now + 61)
    assert ok
    # Grace period length is the named constant.
    assert srv.TAKEOVER_GRACE_SECONDS == 60


def _ws_started_room(client, host_nick="Matthew"):
    """Helper: a room with a started career; the host owns the first
    team. Returns (code, host_id, host_ws, recv_until)."""
    from fastapi.testclient import TestClient

    def recv_until(ws, want, limit=40, check=None):
        for _ in range(limit):
            m = ws.receive_json()
            if m.get("type") == want and (check is None or check(m)):
                return m
        raise AssertionError(f"never got {want}")

    host_ws = client.websocket_connect("/ws")
    host_ws.__enter__()
    host_ws.send_json({"type": "create_room", "nickname": host_nick, "colour": "#e74c3c"})
    joined = recv_until(host_ws, "joined")
    code, host_id = joined["code"], joined["player_id"]
    host_ws.send_json({"type": "new_career", "name": "WS Career"})
    state = recv_until(host_ws, "room_state",
                       check=lambda m: m["room"]["career"] is not None)
    team_id = state["room"]["career"]["teams"][0]["id"]
    host_ws.send_json({"type": "choose_team", "team_id": team_id})
    recv_until(host_ws, "action", check=lambda m: m.get("action") == "choose_team")
    host_ws.send_json({"type": "start_career"})
    recv_until(host_ws, "room_state",
               check=lambda m: m["room"]["started"] is True)
    return code, host_id, team_id, host_ws, recv_until


def test_ws_takeover_after_grace_period():
    """v0.6.2: a disconnected owner's team becomes takeable by a new
    player once the 60s grace period has passed."""
    import time as _time
    from fastapi.testclient import TestClient
    import server as srv

    client = TestClient(srv.app)
    code, host_id, team_id, host_ws, recv_until = _ws_started_room(client)
    host_ws.__exit__(None, None, None)
    # Simulate 61s of absence (the disconnect handler stamps
    # disconnected_at with the real clock when the socket closes).
    for _ in range(50):
        hp = next((p for p in srv.ROOMS[code]["players"]
                   if p["player_id"] == host_id), None)
        if hp is not None and not hp["connected"]:
            break
        _time.sleep(0.05)
    assert hp is not None and hp["disconnected_at"] is not None
    hp["disconnected_at"] = _time.time() - 61
    with client.websocket_connect("/ws") as guest_ws:
        guest_ws.send_json({"type": "join_room", "code": code,
                            "nickname": "Cousin", "colour": "#3498db"})
        g_joined = recv_until(guest_ws, "joined")
        guest_id = g_joined["player_id"]
        guest_ws.send_json({"type": "choose_team", "team_id": team_id})
        state = recv_until(
            guest_ws, "room_state",
            check=lambda m: any(t["id"] == team_id and t.get("owner_player_id") == guest_id
                                for t in m["room"]["career"]["teams"]))
        taken = next(t for t in state["room"]["career"]["teams"]
                     if t["id"] == team_id)
        assert taken["owner_player_id"] == guest_id


def test_ws_takeover_blocked_within_grace_period():
    """v0.6.2: a disconnected owner's team is NOT takeable inside
    the 60s grace period."""
    import time as _time
    from fastapi.testclient import TestClient
    import server as srv

    client = TestClient(srv.app)
    code, host_id, team_id, host_ws, recv_until = _ws_started_room(client)
    host_ws.__exit__(None, None, None)
    _time.sleep(0.2)
    with client.websocket_connect("/ws") as guest_ws:
        guest_ws.send_json({"type": "join_room", "code": code,
                            "nickname": "Cousin", "colour": "#3498db"})
        recv_until(guest_ws, "joined")
        guest_ws.send_json({"type": "choose_team", "team_id": team_id})
        err = recv_until(guest_ws, "error")
        assert "available in" in err["message"]
    # Ownership did not change.
    taken = next(t for t in srv.ROOMS[code]["career"]["teams"]
                 if t["id"] == team_id)
    assert taken["owner_player_id"] == host_id


def test_ws_takeover_blocked_when_owner_connected():
    """v0.6.2: a connected human's team is NEVER takeable."""
    from fastapi.testclient import TestClient
    import server as srv

    client = TestClient(srv.app)
    code, host_id, team_id, host_ws, recv_until = _ws_started_room(client)
    with client.websocket_connect("/ws") as guest_ws:
        guest_ws.send_json({"type": "join_room", "code": code,
                            "nickname": "Cousin", "colour": "#3498db"})
        recv_until(guest_ws, "joined")
        guest_ws.send_json({"type": "choose_team", "team_id": team_id})
        err = recv_until(guest_ws, "error")
        assert "still connected" in err["message"]
    taken = next(t for t in srv.ROOMS[code]["career"]["teams"]
                 if t["id"] == team_id)
    assert taken["owner_player_id"] == host_id
    host_ws.__exit__(None, None, None)


def test_ws_same_player_rejoin_keeps_team():
    """v0.6.2: auto-rejoin with the SAME player_id keeps the team
    (the grace period is for other players, not the owner)."""
    import time as _time
    from fastapi.testclient import TestClient
    import server as srv

    client = TestClient(srv.app)
    code, host_id, team_id, host_ws, recv_until = _ws_started_room(client)
    host_ws.__exit__(None, None, None)
    _time.sleep(0.2)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "rejoin", "code": code, "player_id": host_id})
        rejoined = recv_until(ws, "joined")
        assert rejoined["room"]["started"] is True
        taken = next(t for t in rejoined["room"]["career"]["teams"]
                     if t["id"] == team_id)
        assert taken["owner_player_id"] == host_id
        # The disconnect timestamp is cleared on rejoin.
        me = next(p for p in rejoined["room"]["players"]
                  if p["player_id"] == host_id)
        assert me["connected"] is True
        assert me["disconnected_at"] is None


def test_playwright_midcareer_picker():
    """v0.6.2: in a real browser, a player joining a started career
    with no team sees the mid-career team picker (not a team-less
    game screen), and can take over an AI team from it."""
    import socket
    import threading
    import time as _time
    import urllib.request
    import uvicorn
    from playwright.sync_api import sync_playwright
    from server import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = _time.time() + 20
    while True:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    break
        except Exception:
            if _time.time() > deadline:
                raise
            _time.sleep(0.2)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            # Host: create a room, start a career, take a team, start it.
            host_ctx = browser.new_context()
            host = host_ctx.new_page()
            host.goto(f"http://127.0.0.1:{port}/")
            host.fill("#home-nick", "Max")
            host.click("#btn-create")
            host.locator("#screen-lobby").wait_for(state="visible", timeout=10000)
            host.click("#btn-new-career")
            host.locator("#team-list button[data-team]").first.wait_for(
                state="visible", timeout=10000)
            host.locator("#team-list button[data-team]").first.click()
            host.click("#btn-start-career")
            host.locator("#screen-game").wait_for(state="visible", timeout=10000)
            code = host.evaluate("() => room.code")
            assert len(code) == 6
            # Guest: join mid-career with no team.
            guest_ctx = browser.new_context()
            guest = guest_ctx.new_page()
            guest.goto(f"http://127.0.0.1:{port}/")
            guest.fill("#home-nick", "Matthew")
            guest.fill("#home-code", code)
            guest.click("#btn-join")
            guest.locator("#screen-picker").wait_for(state="visible", timeout=10000)
            assert not guest.locator("#screen-game").is_visible()
            assert not guest.locator("#screen-lobby").is_visible()
            picker = guest.locator("#picker-team-list")
            # The host's team shows as taken; AI teams have a take button.
            assert "taken by Max" in picker.inner_text()
            assert picker.locator("button[data-team]").count() >= 1
            # Take the first available team and land on the dashboard.
            picker.locator("button[data-team]").first.click()
            guest.locator("#screen-game").wait_for(state="visible", timeout=10000)
            assert not guest.locator("#screen-picker").is_visible()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_playwright_dashboard_shows_own_reliability_chart_off():
    """v0.6.3: with the perf chart OFF, the dashboard still shows your
    own car's reliability (with the DNF chance) — but never a perf
    number and never driver ratings."""
    import socket
    import threading
    import time as _time
    import urllib.request
    import uvicorn
    from playwright.sync_api import sync_playwright
    from server import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = _time.time() + 20
    while True:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    break
        except Exception:
            if _time.time() > deadline:
                raise
            _time.sleep(0.2)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/")
            page.fill("#home-nick", "Max")
            page.click("#btn-create")
            page.locator("#screen-lobby").wait_for(state="visible", timeout=10000)
            # Turn the perf chart OFF in the lobby (host-only setting).
            page.uncheck("#lobby-chart")
            page.click("#btn-new-career")
            page.locator("#team-list button[data-team]").first.wait_for(
                state="visible", timeout=10000)
            page.locator("#team-list button[data-team]").first.click()
            page.click("#btn-start-career")
            page.locator("#screen-game").wait_for(state="visible", timeout=10000)
            # The dashboard header shows your own reliability + DNF
            # chance even with the chart off — and no perf number.
            page.wait_for_function(
                "() => document.querySelector('#my-team').innerText.includes('Reliability')",
                timeout=10000)
            header = page.locator("#my-team").inner_text()
            assert "Reliability" in header
            assert "DNF per car" in header
            assert "Perf" not in header
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_http_health_and_export_import(tmp_path):
    from fastapi.testclient import TestClient
    from server import app
    client = TestClient(app)
    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/").status_code == 200


def test_mobile_friendly_markup():
    """Mobile + mouse: viewport meta, >=44px tap targets, responsive CSS,
    scrollable tables, no hover-only / mouse-only / touch-only handlers, and
    every main action is a real button. (Layout at 360px is also checked
    with a headless browser — see README 'Mobile checks'.)"""
    from fastapi.testclient import TestClient
    from server import app
    html = TestClient(app).get("/").text
    assert 'width=device-width, initial-scale=1' in html
    assert "min-height:44px" in html
    assert "@media (max-width: 600px)" in html
    assert "table-scroll" in html
    assert "touch-action:manipulation" in html
    assert "onmouseover" not in html
    assert "onmousedown" not in html
    assert "ontouchstart" not in html
    for label in ["Create Room", "Join Room", "Advance to Race Weekend",
                  "Copy join link", "Invest in research", "Develop Parts",
                  "Save career"]:
        assert label in html
    # No action is a bare text link: the only <a> tags are none (links are buttons).
    assert "<a " not in html


# ---------------------------------------------------------------------------
# 6. v0.1 — no absolute times, full-distance races, NEXT votes, circuits
# ---------------------------------------------------------------------------

def test_results_payloads_never_contain_absolute_times():
    career = two_human_career()
    play_full_round(career, seeded(5))
    for key in ("last_qual", "last_race"):
        rows = career[key]
        assert len(rows) == 22
        for r in rows:
            assert "time" not in r, f"absolute time leaked in {key}: {r}"
            # v0.6.2: a DNF row's gap is "DNF" — not an absolute time.
            assert r["gap"] in ("LEADER", "DNF") or r["gap"].startswith("+")
        assert rows[0]["gap"] == "LEADER"
    # public_state must not leak qual_time / race_time either, chart on or off.
    for chart_on in (True, False):
        dumped = json.dumps(game.public_state(career, chart_on))
        assert "qual_time" not in dumped
        assert "race_time" not in dumped
    # History rows are gap-only too.
    entry = career["race_history"][0]
    for key in ("qual", "race"):
        assert all("time" not in r for r in entry[key])


def test_race_is_full_distance_with_plausible_gaps():
    career = two_human_career()
    for seed in (1, 7, 42):
        c = game.build_new_career("T", "H")
        rng = random.Random(seed)
        from f1 import race as race_mod
        qual = race_mod.simulate_qual(c, rng=rng)
        qtimes = [d["qual_time"] for d, _ in qual]
        # v0.2 form widens qual gaps a little (v0.6.6 Proposal B:
        # sim median 3.3s, max 4.9s over 120 races) — still a few
        # seconds, not tens.
        assert qtimes[-1] - qtimes[0] <= 4.5
        race_entries, _dnfs = race_mod.simulate_race(c, laps=57, rng=rng)
        rtimes = [d["race_time"] for d, _ in race_entries]
        winner = rtimes[0]
        assert 4000 <= winner <= 7000, f"winner total {winner:.0f}s is not a GP distance"
        # v0.6.2: the last row of race_entries is the last FINISHER
        # (DNFs are returned separately) — its gap is still plausible.
        # v0.6.6 (Proposal B): the field spreads out — the P22 gap
        # median is ~150s (max ~196s over 120 races), so the bound
        # is 250s; the P10 gap median is ~74s (max ~118s).
        assert 10 <= rtimes[-1] - winner <= 250  # last finisher's gap
        assert 5 <= rtimes[9] - winner <= 120    # P10 gap plausible
    # The played round agrees (server-side times are what the gaps came from).
    play_full_round(career, seeded(5))
    assert career["last_race"][0]["gap"] == "LEADER"
    # v0.6.2: DNF rows (gap "DNF") sit after the finishers — the gap
    # check is on the last FINISHER, not the last row.
    finishers = [r for r in career["last_race"] if r["pos"] != "DNF"]
    assert finishers[-1]["gap"].startswith("+")
    last_gap = float(finishers[-1]["gap"][1:-1])
    assert 10 <= last_gap <= 250


def test_next_vote_flow_host_override_and_disconnect():
    career = two_human_career()
    for t in list(game.human_teams(career)):
        game.advance_team(career, t["id"], rng=seeded())
    assert career["phase"] == "qual_results"
    # A player with no team cannot vote.
    ok, _adv, _msg = game.vote_next(career, "p-stranger", rng=seeded())
    assert not ok
    # The host override is host-only.
    ok, _adv, _msg = game.host_skip_results(career, "p-cousin",
                                            is_host=False, rng=seeded())
    assert not ok
    assert career["phase"] == "qual_results"
    ok, adv, _msg = game.host_skip_results(career, "p-matthew",
                                           is_host=True, rng=seeded())
    assert ok and adv
    assert career["phase"] == "race_results"
    # Second vote: one vote is not enough, both votes return to dashboards.
    ok, adv, _ = game.vote_next(career, "p-matthew", rng=seeded())
    assert ok and not adv
    assert career["phase"] == "race_results"
    ok, adv, _ = game.vote_next(career, "p-cousin", rng=seeded())
    assert ok and adv
    assert career["phase"] == "round" and career["round"] == 2
    # v0.6.4 RULE CHANGE (was: disconnected humans are excluded, so
    # one remaining voter could pass alone). Now the vote counts ALL
    # human players: a player who has not voted is still pending,
    # even if their connection dropped — the vote must not complete
    # on one player's vote alone. A recorded vote still survives a
    # disconnect/reconnect, and the host skip is the escape hatch.
    career2 = two_human_career()
    for t in list(game.human_teams(career2)):
        game.advance_team(career2, t["id"], rng=seeded())
    ok, adv, _ = game.vote_next(career2, "p-matthew", rng=seeded())
    assert ok and not adv  # cousin has not voted yet
    assert "p-matthew" in career2["results_vote"]  # vote is recorded
    # Cousin "disconnects" (stops voting): the vote must NOT complete
    # on Matthew's vote alone — cousin is still a pending voter.
    assert not game.check_results_vote(career2, rng=seeded())
    assert career2["phase"] == "qual_results"
    # Cousin reconnects and votes: now it completes.
    ok, adv, _ = game.vote_next(career2, "p-cousin", rng=seeded())
    assert ok and adv
    assert career2["phase"] == "race_results"


def test_history_gp_revisit_returns_full_tables():
    career = two_human_career()
    play_full_round(career, seeded(3))
    play_full_round(career, seeded(4))
    assert len(career["race_history"]) == 2
    circuits = [e["circuit"] for e in career["race_history"]]
    assert circuits == ["Bahrain Grand Prix", "Saudi Arabian Grand Prix"]
    for entry in career["race_history"]:
        assert len(entry["qual"]) == 22 and len(entry["race"]) == 22
        assert entry["qual"][0]["gap"] == "LEADER"
        assert entry["race"][0]["gap"] == "LEADER"
        assert all("time" not in r for r in entry["qual"] + entry["race"])
        assert len(entry["top3"]) == 3
        assert "gl" in entry["race"][0]  # gained/lost vs qualifying


def test_button_actions_confirm_and_reflect_state():
    career = game.build_new_career("Test", "Matthew")
    # Racing Bulls starts with no T&R drivers, so research is x1.00
    # (McLaren starts with 4 T&R drivers and a x2.00 bonus).
    team = next(t for t in career["teams"] if t["name"] == "Racing Bulls")
    ok, msg = game.do_tuning(career, team["id"], 2)
    assert ok and team["tuning"] == 2
    assert msg == "Tuning set to +2 ✓"
    ok, msg = game.do_research(career, team["id"], "perf", 1.0)
    assert ok and "Invested $1.00M in Performance research" in msg
    assert team["perf_res"] == 1.0
    # A failed action says so clearly instead of failing silently.
    ok, msg = game.do_research(career, team["id"], "perf", 10**9)
    assert not ok and "Not enough money" in msg
    # v0.4: regulation research opens at round 7 — before that the
    # control is greyed out, and research needs a fully adapted car.
    ok, msg = game.do_reg_research(career, team["id"], 1.0)
    assert not ok and "round 7" in msg
    team["money"] = 50.0
    career["round"] = 7
    game.init_reg_state(career)
    ok, msg = game.do_reg_adapt(career, team["id"])
    assert ok and "Adapted" in msg
    ok, msg = game.do_reg_research(career, team["id"], 1.0)
    assert ok and "regulation research" in msg
    assert team["reg_research"] == 1.0
    ok, msg = game.do_develop(career, team["id"])
    assert ok and "Parts developed" in msg
    # Signing confirms with the driver's name.
    pool = game.free_agents(career)
    team["money"] = 50.0
    ok, msg = game.sign_driver(career, team["id"], pool[0]["id"], 1)
    assert ok and pool[0]["name"] in msg and "signed" in msg


def test_calendar_covers_all_12_rounds():
    assert len(CALENDAR) == 12
    names = [gp["name"] for gp in CALENDAR]
    assert len(set(names)) == 12
    assert all(50 <= gp["laps"] <= 78 for gp in CALENDAR)
    assert circuit_for_round(1)["name"] == "Bahrain Grand Prix"
    assert circuit_for_round(1)["laps"] == 57
    assert circuit_for_round(12)["name"] == "British Grand Prix"
    assert circuit_for_round(12)["laps"] == 52
    career = game.build_new_career("Test", "Matthew")
    assert career["circuit"] == "Bahrain Grand Prix"
    assert career["circuit_laps"] == 57
    # (two_human_career already picks teams; use a fresh one here)
    career = two_human_career()
    play_full_round(career, seeded(11))
    assert career["circuit"] == "Saudi Arabian Grand Prix"
    assert career["circuit_laps"] == 50


def test_old_v0_save_still_loads():
    # A v0 save: version 1, no circuit / results fields, history with only
    # top3, and results rows that still carry a "time" field.
    v0_doc = {
        "version": 1, "name": "Old v0 career", "host": "Matthew",
        "year": 2026, "round": 3, "season_index": 0, "phase": "round",
        "teams": [], "drivers": [],
        "race_history": [{"year": 2026, "round": 1,
                          "top3": [{"driver": "Lewis Hamilton", "team": "Ferrari"}]}],
        "last_qual": [{"pos": 1, "driver": "Lewis Hamilton", "team": "Ferrari",
                       "time": 66.1, "gap": "LEADER"}],
        "last_race": [{"pos": 1, "driver": "Lewis Hamilton", "team": "Ferrari",
                       "time": 137.3, "gap": "LEADER", "points": 25}],
    }
    migrated = migrate_save(v0_doc)
    assert migrated["circuit"] == "Australian Grand Prix"  # round 3
    assert migrated["circuit_laps"] == 58
    assert migrated["results_vote"] == [] and migrated["results_stage"] is None
    assert migrated["prev_driver_order"] == []  # v0.2 field defaults
    assert migrated["race_history"][0]["circuit"] == "Round 1"
    assert migrated["race_history"][0]["qual"] is None  # no full tables in v0
    assert "time" not in migrated["last_qual"][0]
    assert "time" not in migrated["last_race"][0]
    # The same doc also loads through the real load_save path.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "oldv0.json"
        path.write_text(json.dumps(v0_doc), encoding="utf-8")
        loaded = load_save(tmp, "oldv0")
        assert loaded is not None and loaded["circuit"] == "Australian Grand Prix"


def test_host_override_only_sent_to_host_client():
    import server as srv
    room = srv.new_room("Matthew", "#e74c3c")
    host = srv.add_player(room, "Matthew", "#e74c3c")
    cousin = srv.add_player(room, "Cousin", "#3498db")
    assert room["host_id"] == host["player_id"]
    assert srv.room_public(room, host["player_id"])["is_host"] is True
    assert srv.room_public(room, cousin["player_id"])["is_host"] is False
    # my_team never carries absolute times either.
    room["career"] = game.build_new_career("T", "Matthew")
    game.choose_team(room["career"], room["career"]["teams"][0]["id"], host["player_id"])
    mt = srv.my_team_public(room, host["player_id"])
    assert mt is not None
    assert all("qual_time" not in d and "race_time" not in d for d in mt["drivers"])


def test_driver_surname_markup_bold_uppercase_team_coloured():
    """Render the real page in headless Chromium and check the markup
    helper: team-coloured bold UPPERCASE surname + '|' bar for a driver,
    neutral bold uppercase for a free agent, and a gap-only table."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright
    import subprocess, time, urllib.request
    port = 8791
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 360, "height": 800})
            page.goto(f"http://127.0.0.1:{port}/")
            html = page.evaluate(
                "window.F1UI.driverNameHtml('Lando Norris', '#EF9B33', 1, true)")
            assert "NORRIS" in html and "Lando" in html
            assert "#EF9B33" in html and 'class="surname"' in html
            assert 'class="team-bar"' in html and "|" in html
            assert "🏆1" in html
            free = page.evaluate(
                "window.F1UI.driverNameHtml('Yuki Tsunoda', null, 0, true)")
            assert "TSUNODA" in free and "Yuki" in free
            assert "team-bar" not in free and "#" not in free
            table = page.evaluate(
                "window.F1UI.classificationTable("
                "[{pos:1,driver:'Lewis Hamilton',driver_id:'x',team:'Ferrari',"
                "team_id:null,cha:7,gap:'LEADER',points:25,gl:0},"
                "{pos:2,driver:'Max Verstappen',driver_id:'y',team:'Red Bull',"
                "team_id:null,cha:4,gap:'+12.345s',points:18,gl:-1}], {showGL:true})")
            assert "Time" not in table and "GAP" in table and "G/L" in table
            assert "🥇" in table and "▲" not in table  # P1 gl=0 shows 0
            assert "▼1" in table
            # v0.6.2: a DNF row shows "DNF" for position and gap, 0
            # points, and a dash (not 0) for gain/loss.
            dnf_table = page.evaluate(
                "window.F1UI.classificationTable("
                "[{pos:1,driver:'Lewis Hamilton',driver_id:'x',team:'Ferrari',"
                "team_id:null,cha:7,gap:'LEADER',points:25,gl:0},"
                "{pos:'DNF',driver:'Lance Stroll',driver_id:'z',team:'Aston Martin',"
                "team_id:null,cha:0,gap:'DNF',points:0}], {showGL:true})")
            assert "DNF" in dnf_table
            assert "—" in dnf_table  # DNF gain/loss is a dash, not 0
            assert ">0<" in dnf_table  # the DNF row scores 0 points
            browser.close()
    finally:
        proc.terminate()


def test_ws_next_vote_and_host_skip_over_real_websocket():
    """The v0.1 vote messages end to end: vote_next confirmations, the
    unanimous rule, and the host-only skip, over real WebSockets."""
    from fastapi.testclient import TestClient
    from server import app

    client = TestClient(app)

    def recv_until(ws, want, limit=60, check=None):
        for _ in range(limit):
            m = ws.receive_json()
            if m.get("type") == want and (check is None or check(m)):
                return m
        raise AssertionError(f"never got {want}")

    with client.websocket_connect("/ws") as host_ws:
        host_ws.send_json({"type": "create_room", "nickname": "Matthew", "colour": "#e74c3c"})
        joined = recv_until(host_ws, "joined")
        code, host_id = joined["code"], joined["player_id"]
        with client.websocket_connect("/ws") as guest_ws:
            guest_ws.send_json({"type": "join_room", "code": code,
                                "nickname": "Cousin", "colour": "#3498db"})
            g_joined = recv_until(guest_ws, "joined")
            guest_id = g_joined["player_id"]
            host_ws.send_json({"type": "new_career", "name": "Vote Career"})
            state = recv_until(host_ws, "room_state",
                               check=lambda m: m["room"]["career"] is not None)
            teams = state["room"]["career"]["teams"]
            host_ws.send_json({"type": "choose_team", "team_id": teams[0]["id"]})
            guest_ws.send_json({"type": "choose_team", "team_id": teams[1]["id"]})
            host_ws.send_json({"type": "start_career"})
            recv_until(host_ws, "room_state",
                       check=lambda m: m["room"]["started"] is True)
            # Both advance -> qualifying results page.
            host_ws.send_json({"type": "advance"})
            guest_ws.send_json({"type": "advance"})
            state = recv_until(host_ws, "room_state",
                               check=lambda m: m["room"]["career"]["phase"] == "qual_results")
            career = state["room"]["career"]
            assert career["circuit"] == "Bahrain Grand Prix"
            assert state["room"]["is_host"] is True
            assert all("time" not in r for r in career["last_qual"] + career["last_race"])
            # Non-host skip is rejected.
            guest_ws.send_json({"type": "host_skip"})
            err = recv_until(guest_ws, "error")
            assert "Only the host" in err["message"]
            # Host votes: confirmation arrives, stage does not move yet.
            host_ws.send_json({"type": "vote_next"})
            act = recv_until(host_ws, "action")
            assert "NEXT vote recorded (1/2)" in act["message"]
            # Guest votes -> race results page.
            guest_ws.send_json({"type": "vote_next"})
            state = recv_until(host_ws, "room_state",
                               check=lambda m: m["room"]["career"]["phase"] == "race_results")
            assert state["room"]["career"]["results_stage"] == "race"
            # Host skip on the race page returns everyone to dashboards.
            host_ws.send_json({"type": "host_skip"})
            state = recv_until(host_ws, "room_state",
                               check=lambda m: m["room"]["career"]["phase"] == "round")
            assert state["room"]["career"]["round"] == 2
            assert state["room"]["career"]["circuit"] == "Saudi Arabian Grand Prix"


# ---------------------------------------------------------------------------
# v0.1.1: WebSocket connect race
# ---------------------------------------------------------------------------

def test_advance_toast_says_qualifying_not_race_weekend_complete():
    """The Advance confirmation must not claim the weekend is over: the
    player is about to see the qualifying page first, the race page
    only after the NEXT vote."""
    career = two_human_career()
    rng = seeded()
    ok, ran, _msg = game.advance_team(
        career, career["teams"][0]["id"], rng=rng)
    assert ok and not ran
    ok, ran, msg = game.advance_team(
        career, career["teams"][1]["id"], rng=rng)
    assert ok and ran
    assert career["phase"] == "qual_results"
    assert msg == "Qualifying complete — results are in."
    assert "Race weekend complete" not in msg


def test_connection_code_queues_while_connecting():
    """Static check on the fix itself: messages queue and flush on open,
    the home screen shows connection status, and the old fixed 300ms
    send timeouts and the 'Not connected yet' error are gone."""
    html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
    assert "wsQueue" in html and "onopen" in html and "conn-status" in html
    assert "Connected ✓" in html and "Connection lost — retrying…" in html
    assert "setTimeout(() => send(" not in html
    assert "Not connected yet" not in html


def test_create_room_succeeds_with_slow_websocket_handshake():
    """The v0.1 bug, reproduced and fixed: with the WebSocket handshake
    delayed 1.5s (5x the old 300ms deadline), clicking Create Room
    immediately must still open the lobby — the create_room message
    queues while connecting and flushes on open — and exactly one room
    is created per player, even when Create is double-clicked."""
    pytest.importorskip("playwright")
    import asyncio
    import threading
    import time
    import urllib.request

    import uvicorn
    from server import app as real_app

    class SlowHandshake:
        """Delay only the WebSocket upgrade, like a slow Render proxy."""
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "websocket":
                await asyncio.sleep(1.5)
            await self.app(scope, receive, send)

    port = 8793
    config = uvicorn.Config(SlowHandshake(real_app), host="127.0.0.1",
                            port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)

        def room_count():
            return json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=5).read())["rooms"]

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            before = room_count()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(f"http://127.0.0.1:{port}/")
            page.fill("#home-nick", "Max")
            page.click("#btn-create")  # handshake still in flight (1.5s)
            page.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
            assert "Not connected yet" not in page.inner_text("body")
            # text_content (not inner_text): the home screen is hidden
            # once the lobby opens, but the status text must still say so.
            assert "Connected" in (page.text_content("#conn-status") or "")
            assert room_count() == before + 1
            # A second player double-clicking Create gets exactly one room.
            page2 = browser.new_page(viewport={"width": 1280, "height": 800})
            page2.goto(f"http://127.0.0.1:{port}/")
            page2.fill("#home-nick", "Max2")
            page2.click("#btn-create")
            page2.click("#btn-create")
            page2.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
            assert room_count() == before + 2
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ---------------------------------------------------------------------------
# v0.2 — session form, replacement rules (revised), standings carry-over,
# nickname placeholder
# ---------------------------------------------------------------------------

def _simulate_many_races(n=120, base_seed=10000):
    """Run n seeded races (fresh career each) and return winner/pole
    counters plus gap lists. Same setup as the /tmp/sim_v02.py tuning
    script, so the measured numbers here match the README."""
    from collections import Counter
    from f1 import race as race_mod
    wins, poles = Counter(), Counter()
    q_gaps, r_gaps, totals = [], [], []
    for i in range(n):
        c = game.build_new_career("Sim", "H")
        rng = random.Random(base_seed + i)
        qual = race_mod.simulate_qual(c, rng=rng)
        poles[qual[0][0]["name"]] += 1
        qtimes = [d["qual_time"] for d, _ in qual]
        q_gaps.append(qtimes[-1] - qtimes[0])
        entries, _dnfs = race_mod.simulate_race(c, laps=57, rng=rng)
        wins[entries[0][0]["name"]] += 1
        rtimes = [d["race_time"] for d, _ in entries]
        totals.append(rtimes[0])
        r_gaps.append(rtimes[-1] - rtimes[0])
    return wins, poles, q_gaps, r_gaps, totals


def test_session_form_winner_diversity():
    """The best combo must not win every race: over 120 seeded races
    Antonelli/Mercedes wins 40 (33%) — in the 30-50% band — with 8
    different winners (Russell 38, Hamilton 13, Verstappen 12,
    Leclerc 7, Norris 5, Hadjar 3, Piastri 2), while races stay a
    Grand Prix distance with plausible gaps. v0.6.6 (Proposal B):
    the P22 gap median is ~150s (max ~196s), so the gap bound is
    250s."""
    wins, _poles, _q, r_gaps, totals = _simulate_many_races()
    assert 36 <= wins["Kimi Antonelli"] <= 60  # 30-50% of 120
    assert len(wins) >= 4
    assert all(4000 <= t <= 7000 for t in totals)
    assert all(10 <= g <= 250 for g in r_gaps)


def test_session_form_pole_diversity():
    """Qualifying is less deterministic too: over the same 120 seeded
    sessions Antonelli takes pole 38 times (32%), with 12 different
    poles, and the P22 qual gap stays a few seconds (median 3.3s,
    max 4.9s). v0.6.6 (Proposal B): the qual form sigma is 0.50s,
    so the qual spread is a little wider than the v0.2 numbers."""
    _wins, poles, q_gaps, _r, _t = _simulate_many_races()
    assert poles["Kimi Antonelli"] <= 60  # not every pole
    assert len(poles) >= 5
    q_sorted = sorted(q_gaps)
    assert q_sorted[len(q_sorted) // 2] <= 3.5
    assert q_sorted[-1] <= 5.0


# ---------------------------------------------------------------------------
# v0.6.2 — mechanical failures (DNFs) driven by reliability
# ---------------------------------------------------------------------------

def test_dnf_chance_and_deterministic_dnfs():
    """v0.6.2: DNF chance is 1 - rel per car, each car rolling its own
    team's reliability. rel 0.0 is a 100% DNF chance (both cars DNF);
    rel 1.0 is 0% (never DNFs)."""
    from f1 import race as race_mod
    assert race_mod.dnf_chance(0.95) == pytest.approx(0.05)
    assert race_mod.dnf_chance(0.90) == pytest.approx(0.10)
    assert race_mod.dnf_chance(1.0) == 0.0
    assert race_mod.dnf_chance(1.2) == 0.0
    assert race_mod.dnf_chance(0.0) == 1.0
    career = game.build_new_career("Test", "Matthew")
    # One team is a guaranteed DNF (rel 0.0); the rest never DNF (rel 1.0).
    bad = next(t for t in career["teams"] if t["name"] == "Aston Martin")
    bad["rel"] = 0.0
    for t in career["teams"]:
        if t is not bad:
            t["rel"] = 1.0
    entries, dnfs = race_mod.simulate_race(career, laps=57, rng=random.Random(3))
    assert len(entries) == 20
    assert len(dnfs) == 2
    assert {d["name"] for d, _t in dnfs} == {
        game.get_driver(career, bad["driver1_id"])["name"],
        game.get_driver(career, bad["driver2_id"])["name"],
    }
    # All reliable: nobody DNFs.
    for t in career["teams"]:
        t["rel"] = 1.0
    entries, dnfs = race_mod.simulate_race(career, laps=57, rng=random.Random(3))
    assert len(entries) == 22
    assert dnfs == []


def test_dnf_rate_near_five_percent_at_rel_095():
    """v0.6.2: with rel 0.95 the per-car DNF rate is near 5% — over
    200 one-lap races (4,400 car-races) it lands in 2-8%, a wide band
    around the 5% expectation, so the test is not flaky."""
    from f1 import race as race_mod
    career = game.build_new_career("Test", "Matthew")
    for t in career["teams"]:
        t["rel"] = 0.95
    rng = random.Random(11)
    dnfs = 0
    total = 0
    for _ in range(200):
        _entries, d = race_mod.simulate_race(career, laps=1, rng=rng)
        dnfs += len(d)
        total += 22
    rate = dnfs / total
    assert 0.02 <= rate <= 0.08, f"DNF rate {rate:.3f} is not near 5%"


def test_dnf_driver_consequences_and_move_up():
    """v0.6.2: a DNF driver scores 0 points, keeps their best finish,
    still counts as having raced (f1_races +1, so they stay in the
    standings), rep unchanged, and the team pays a $1.0M repair bill
    per DNF with no prize money for that car. The other cars move up —
    the 10th finisher (11th overall) scores a point."""
    from f1 import race as race_mod
    career = game.build_new_career("Test", "Matthew")
    f1_drivers = [d for d in career["drivers"] if d["status"] == "f1"]
    assert len(f1_drivers) == 22
    # The first two F1 drivers (both from the same team) DNF.
    dnf_drivers = f1_drivers[:2]
    team_of = {}
    for t in career["teams"]:
        for did in (t["driver1_id"], t["driver2_id"]):
            if did:
                team_of[did] = t
    dnf_team = team_of[dnf_drivers[0]["id"]]
    assert team_of[dnf_drivers[1]["id"]] is dnf_team
    finishers = f1_drivers[2:]
    # Give the DNF drivers a known best finish and rep to check they
    # are untouched, and a known f1_races count.
    for d in dnf_drivers:
        d["best_pos"] = 3
        d["best_finish_round"] = 1
        d["f1_races"] = 0
    rep_before = {d["id"]: d["rep"] for d in dnf_drivers}
    money_before = dnf_team["money"]
    # The 10th finisher is the 12th driver overall — it scores a point
    # only because the two DNFs ahead of it moved it up.
    tenth_finisher = finishers[9]
    entries = [(d, team_of[d["id"]]) for d in finishers]
    dnf_entries = [(d, dnf_team) for d in dnf_drivers]
    n_dnf = race_mod.apply_race_results(career, entries, POINTS_DIST, dnf_entries)
    assert n_dnf == 2
    for d in dnf_drivers:
        assert d["points"] == 0
        assert d["team_points"] == 0
        assert d["best_pos"] == 3  # a DNF is not a finish
        assert d["f1_races"] == 1  # still counts as having raced
        assert d["rep"] == rep_before[d["id"]]  # rep unchanged
        assert d["race_pos"] is None
    assert dnf_drivers[0] in race_mod.driver_standings(career)
    assert tenth_finisher["points"] == 1  # P10 after the move-up
    assert tenth_finisher["team_points"] == 1
    # The DNF team paid two $1.0M repair bills and earned no prize
    # money (neither of its cars finished).
    assert dnf_team["money"] == money_before - 2 * race_mod.DNF_REPAIR_COST
    # The winner still gets 25.
    assert finishers[0]["points"] == 25


def test_reliability_cap_is_one_not_099():
    """v0.6.2: a team can develop its reliability up to 1.0 (not stuck
    at 0.99) — so "all good" (0% DNF) is reachable."""
    career = game.build_new_career("Test", "Matthew")
    team = next(t for t in career["teams"] if t["name"] == "Aston Martin")
    team["rel"] = 0.99
    team["reli_res"] = 100.0  # add_rel_gain = 1.0
    team["money"] = 100.0
    ok, _msg = game.do_develop(career, team["id"])
    assert ok
    assert team["rel"] == 1.0
    # The bot path has the same cap.
    team["rel"] = 0.99
    team["reli_res"] = 50.0
    team["perf_res"] = 0.0
    team["money"] = 100.0
    game.ai_act(career, team, rng=random.Random(4))
    assert team["rel"] == 1.0


def test_f2_race_has_no_dnfs():
    """v0.6.2: F2 races are unaffected — even with every F1 team at
    rel 0.0 (all F1 cars would DNF), the F2 race still returns every
    F2 driver with a race time."""
    from f1 import race as race_mod
    career = game.build_new_career("Test", "Matthew")
    for t in career["teams"]:
        t["rel"] = 0.0
    f2_before = [d for d in career["drivers"] if d["status"] == "f2"]
    order = race_mod.simulate_f2(career, rng=random.Random(6))
    assert len(order) == len(f2_before)
    assert all("race_time" in d for d in order)


def test_midseason_replacement_keeps_outgoing_points():
    """Revised v0.2 rule (real F1): the replaced driver KEEPS their
    points and stays in the driver standings; a free agent signed
    mid-season keeps points from an earlier stint (v0.2.1 — only F2
    drivers start at 0); the team's constructor total still includes
    the points the outgoing driver scored for that team — even after
    the next race — and is never inflated by the incoming driver's
    old points."""
    from f1 import race as race_mod
    career = two_human_career()
    play_full_round(career, seeded(5))
    # Find a team with a points-scoring driver in a seat.
    team = outgoing = None
    for t in career["teams"]:
        for d in game.team_drivers(career, t):
            if d["points"] > 0:
                team, outgoing = t, d
                break
        if team:
            break
    assert team is not None and outgoing is not None
    out_points, out_wins, out_best = (outgoing["points"],
                                      outgoing["season_wins"],
                                      outgoing["best_pos"])
    team_points_before = team["points"]
    seat = 1 if team["driver1_id"] == outgoing["id"] else 2
    # Give the incoming free agent points from an earlier stint, to
    # prove they KEEP them when signed (v0.2.1: only F2 drivers
    # start at 0).
    incoming = game.free_agents(career)[0]
    incoming["points"] = 13
    incoming["season_wins"] = 1
    incoming["best_pos"] = 4
    team["money"] = 100.0
    ok, msg = game.sign_driver(career, team["id"], incoming["id"], seat)
    assert ok, msg
    # Outgoing keeps everything, stays in the standings, and their old
    # team is recorded for the standings display.
    assert outgoing["status"] == "free" and outgoing["team_id"] is None
    assert outgoing["points"] == out_points
    assert outgoing["season_wins"] == out_wins
    assert outgoing["best_pos"] == out_best
    assert outgoing["last_team_id"] == team["id"]
    assert any(d["id"] == outgoing["id"]
               for d in race_mod.driver_standings(career))
    # Incoming keeps their earlier points, but starts a fresh per-team
    # tally — the team's total must not include the 13.
    assert incoming["status"] == "f1" and incoming["team_id"] == team["id"]
    assert incoming["points"] == 13
    assert incoming["season_wins"] == 1
    assert incoming["best_pos"] == 4
    assert incoming["team_points"] == 0
    # Constructor total is unchanged by the swap (banked points).
    assert team["points"] == team_points_before
    assert team["points"] >= out_points
    # ... and the banked points survive the next race too, while the
    # incoming driver's new points count only from now on.
    play_full_round(career, seeded(6))
    assert team["points"] >= out_points
    assert incoming["points"] == 13 + incoming["team_points"]


def test_standings_order_carries_into_next_season():
    """Like real F1: the new season opens in last season's driver order
    (everyone on 0 points), and once points are scored, points sort
    takes over."""
    from f1 import race as race_mod
    career = two_human_career()
    connected = {"p-matthew", "p-cousin"}
    rng = seeded(99)
    for _ in range(12):
        play_full_round(career, rng)
    assert career["phase"] == "winter_break"
    order = career["prev_driver_order"]
    assert len(order) == 22
    # Fill empty human seats via the winter-break offer flow, then start.
    play_winter_break(career, connected, rng)
    ok, why = game.start_next_season(career, rng=rng)
    assert ok, why
    assert career["round"] == 1 and career["year"] == 2027
    assert all(d["points"] == 0 for d in career["drivers"]
               if d["status"] == "f1")
    standings = race_mod.driver_standings(career)
    # Every carried-over driver appears in exactly last season's order;
    # newcomers (promoted F2 / fresh signings) come after them.
    carried = [d["id"] for d in standings if d["id"] in order]
    survivors = [did for did in order
                 if did in {d["id"] for d in standings}]
    assert carried == survivors
    assert standings[0]["id"] == carried[0]  # top = best surviving finisher
    # Newcomers all sit after every carried-over driver.
    assert all(d["id"] not in order for d in standings[len(carried):])
    # After round 1, points sort takes over: the race winner leads.
    play_full_round(career, seeded(21))
    standings2 = race_mod.driver_standings(career)
    assert standings2[0]["points"] == 25
    assert standings2[0]["name"] == career["last_race"][0]["driver"]


def test_nickname_placeholder_is_generic():
    """The home nickname input must say exactly "e.g. [name]" and
    Matthew's name must not appear as an example anywhere in the
    frontend (comments mentioning his screenshots are not examples)."""
    import re
    html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
    assert 'placeholder="e.g. [name]"' in html
    assert "e.g. Matthew" not in html
    placeholders = re.findall(r'placeholder="([^"]*)"', html)
    assert all("Matthew" not in p for p in placeholders)

# ---------------------------------------------------------------------------
# v0.2.1 — points follow the driver, last team shown, cost upfront
# ---------------------------------------------------------------------------

def test_midseason_move_keeps_points_across_teams():
    """Flag 1, refined (real F1): a driver with 44 points at Alpine who
    moves to Audi mid-season KEEPS the 44 in the driver standings; the
    44 stay at Alpine's constructor total; only points scored from now
    on for Audi count toward Audi."""
    from f1 import race as race_mod
    career = two_human_career()
    play_full_round(career, seeded(5))
    alpine = next(t for t in career["teams"] if t["name"] == "Alpine")
    audi = next(t for t in career["teams"] if t["name"] == "Audi")
    # The Alpine driver scored 44 points for Alpine this season.
    driver = game.team_drivers(career, alpine)[0]
    driver["points"] = 44
    driver["team_points"] = 44
    driver["season_wins"] = 2
    driver["best_pos"] = 1
    game._recompute_team_points(career, alpine)
    alpine_before = alpine["points"]
    audi_before = audi["points"]
    assert alpine_before >= 44
    # Drop him from Alpine (replace with a free agent).
    alpine["money"] = 100.0
    seat = 1 if alpine["driver1_id"] == driver["id"] else 2
    filler = game.free_agents(career)[0]
    ok, msg = game.sign_driver(career, alpine["id"], filler["id"], seat)
    assert ok, msg
    assert driver["status"] == "free" and driver["points"] == 44
    assert driver["last_team_id"] == alpine["id"]
    assert any(d["id"] == driver["id"]
               for d in race_mod.driver_standings(career))
    assert alpine["points"] == alpine_before  # the 44 stay at Alpine
    # Sign him for Audi.
    audi["money"] = 100.0
    ok, msg = game.sign_driver(career, audi["id"], driver["id"], 1)
    assert ok, msg
    assert driver["status"] == "f1" and driver["team_id"] == audi["id"]
    assert driver["points"] == 44 and driver["team_points"] == 0
    assert audi["points"] == audi_before  # the 44 did NOT inflate Audi
    # He scores for Audi in the next round: only the new points add
    # to Audi's total.
    before = {d["id"]: d["points"] for d in game.team_drivers(career, audi)}
    play_full_round(career, seeded(6))
    gains = sum(game.get_driver(career, did)["points"] - pts
                for did, pts in before.items())
    assert audi["points"] == audi_before + gains
    assert driver["points"] == 44 + driver["team_points"]


def test_f2_signing_starts_at_zero():
    """v0.7.3: an F2 driver signed mid-season can ONLY join as Test &
    Reserve — a main seat is rejected until the F2 season ends. As a
    T&R driver they keep racing F2 (their F2 points stay in the plain
    fields) while their F1 tally starts at 0. In the winter break a
    main-seat signing is allowed, and then the F2 tally is cleared so
    F2 championship points never carry into F1."""
    career = two_human_career()
    # v0.8.0: the first F2 driver is an academy junior — signing one
    # as T&R is a poach and needs a secret meeting, so use a plain
    # (unaffiliated) F2 driver here.
    f2 = next(d for d in game.f2_drivers(career)
              if not d.get("academy_team_id"))
    f2["points"] = 30  # F2 championship points
    team = career["teams"][0]
    team["money"] = 100.0
    ok, msg = game.sign_driver(career, team["id"], f2["id"], 1)
    assert not ok and "Test & Reserve" in msg, msg
    assert f2["status"] == "f2" and f2["points"] == 30
    ok, msg = game.sign_tr_driver(career, team["id"], f2["id"], 2)
    assert ok, msg
    assert f2["status"] == "tr" and f2.get("f2_tr") is True
    assert f2["points"] == 30  # still racing F2 — the tally stays
    assert f2["f1_points"] == 0 and f2["f1_wins"] == 0
    assert f2["f1_best_pos"] is None
    # Winter break: the same driver may now take a main seat, and the
    # F2 tally is cleared on the move.
    career["phase"] = "winter_break"
    other = next(d for d in game.f2_drivers(career)
                 if not d.get("academy_team_id"))
    other["points"] = 40
    team["money"] = 100.0
    # In the winter break the direct sign is blocked (offers only),
    # so the winter-break signing API is used here.
    ok, msg = game.sign_driver_wb(career, team, other, 2, 2)
    assert ok, msg
    assert other["status"] == "f1" and other["team_id"] == team["id"]
    assert other["points"] == 0 and other["team_points"] == 0
    assert other["season_wins"] == 0 and other["best_pos"] is None
    assert other.get("f2_tr") is False


def test_replacement_cost_matches_charge_and_is_shown():
    """v0.3: signing is FREE — the Driver Market shows only the
    contract-break fee for an occupied seat (0 / Free for an empty
    seat), computed by break_cost(), and sign_driver() charges
    exactly that. The sign confirmation states the break fee (or
    Free), and an unaffordable seat is refused with the amount
    needed."""
    career = two_human_career()
    team = career["teams"][0]  # McLaren: Norris (salary 5.2, 2 yr),
    incoming = game.free_agents(career)[0]  # Tsunoda
    occupant = game.get_driver(career, team["driver1_id"])
    # Round 1: 12 rounds left this season + 1 more year (12 rounds).
    expected = (12 + 12) * occupant["salary"] / 12 + 3.0
    quote = game.break_cost(career, team["id"], incoming["id"], 1)
    assert quote is not None
    assert quote["total"] == pytest.approx(expected)
    assert quote["break_fee"] == pytest.approx(expected)
    assert "signing" not in quote  # no signing fee exists any more
    assert quote["affordable"] == (expected <= team["money"])
    # The same quote is in the market data sent to the client.
    state = game.public_state(career, True, team["id"])
    assert state["market_costs"][incoming["id"]]["1"]["total"] == quote["total"]
    assert state["market_costs"][incoming["id"]]["1"]["affordable"] == quote["affordable"]
    # An empty seat is Free.
    team["driver2_id"] = None
    quote2 = game.break_cost(career, team["id"], incoming["id"], 2)
    assert quote2["break_fee"] == 0
    assert quote2["total"] == 0.0
    assert quote2["affordable"] is True
    # The charge equals the displayed total, and the confirmation
    # states the break fee.
    team["money"] = 100.0
    money_before = team["money"]
    ok, msg = game.sign_driver(career, team["id"], incoming["id"], 1)
    assert ok, msg
    assert team["money"] == pytest.approx(money_before - quote["total"])
    assert f"${quote['total']:.2f}M" in msg and "break fee" in msg
    # Unaffordable: the quote says so and signing is refused with the
    # amount needed.
    team2 = career["teams"][1]
    team2["money"] = 0.5
    poor = game.free_agents(career)[0]
    quote3 = game.break_cost(career, team2["id"], poor["id"], 1)
    assert quote3["affordable"] is False
    ok, msg = game.sign_driver(career, team2["id"], poor["id"], 1)
    assert not ok and "Need $" in msg


def test_replaced_driver_old_team_in_standings_ui():
    """Flag 2, end to end in a real browser: replace a McLaren driver
    through the Driver Market UI, then the standings tab must show the
    outgoing driver with McLaren in the team column — and the market
    must have shown the per-seat replacement cost before the click.
    v0.4.2: the race sorts D1/D2 by team points, and the $1.5M/race
    T&R fine can leave Seat 1 unaffordable after round 1 — so the
    test replaces the first affordable seat, whichever it is."""
    pytest.importorskip("playwright")
    import re
    import subprocess
    import time
    import urllib.request
    port = 8797
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            p1 = browser.new_page(viewport={"width": 1280, "height": 800})
            p1.goto(f"http://127.0.0.1:{port}/")
            p1.fill("#home-nick", "Max")
            p1.click("#btn-create")
            p1.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
            p1.click("#btn-new-career")
            p1.wait_for_selector("[data-team]", timeout=15000)
            p1.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.textContent.includes('McLaren'))
                    .querySelector('[data-team]').click();
            }""")
            p1.wait_for_selector("#btn-start-career:not(.hidden)", timeout=15000)
            code = re.search(r"\b[A-Z0-9]{6}\b",
                             p1.inner_text("#screen-lobby")).group(0)
            p2 = browser.new_page(viewport={"width": 1280, "height": 800})
            p2.goto(f"http://127.0.0.1:{port}/?code={code}")
            p2.fill("#home-nick", "Cousin")
            p2.click("#btn-join")
            p2.wait_for_selector("[data-team]", timeout=15000)
            p2.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.textContent.includes('Ferrari'))
                    .querySelector('[data-team]').click();
            }""")
            p1.click("#btn-start-career")
            p1.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            # Play rounds until a McLaren seat is affordable. v0.7.3:
            # the $3.0M flat raised break fees (Norris's seat is now
            # $12.97M at round 2), so a bad round 1 can leave both
            # seats unaffordable against McLaren's worst-case $12.26M
            # after round 1 — each extra round adds prize money, so
            # loop (cap 4 rounds) and re-read the seats each time
            # (the race sorts D1/D2 by team points).
            seats = []
            for _round in range(4):
                p1.click("#btn-advance")
                p2.click("#btn-advance")
                p1.wait_for_selector("#screen-results:not(.hidden)", timeout=15000)
                p1.click("#btn-vote-next")
                p2.click("#btn-vote-next")
                p1.wait_for_selector("text=Race Results", timeout=15000)
                p1.click("#btn-vote-next")
                p2.click("#btn-vote-next")
                p1.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
                # Who is in each McLaren seat? (The outgoing driver's
                # surname must stay visible with McLaren in the standings
                # after replacement.) The race sorts D1/D2 by team points,
                # so either driver can be in seat 1 after a round.
                my_team = p1.inner_text("#my-team")
                seats = re.findall(r"\b([A-Z][a-z]+)\s+([A-Z]+)\b", my_team)
                assert len(seats) >= 2, my_team
                p1.click("button[data-tab='market']")
                affordable = p1.evaluate("""() => {
                    const btns = [...document.querySelectorAll('#market-box button[data-sign][data-seat]')]
                      .filter(b => (b.dataset.seat === '1' || b.dataset.seat === '2') && !b.disabled);
                    return btns.length > 0;
                }""")
                if affordable:
                    break
                # Not affordable yet: back to the dashboard and play
                # another round for more prize money.
                p1.click("button[data-tab='research']")
            else:
                raise AssertionError("no McLaren seat affordable after 4 rounds")
            # Market tab: per-seat costs are shown before committing.
            p1.click("button[data-tab='market']")
            market = p1.inner_text("#market-box")
            assert "Cost to sign" in market
            # v0.4.2: an unaffordable seat still shows its label
            # ("Seat 1 — Need $15.42M"), so the cost is always visible.
            assert re.search(r"Seat 1 — (Need )?\$\d+\.\d\dM", market), market
            assert "Break $" in market
            assert "Signing $" not in market  # v0.3: no signing fee
            assert "Rep" not in market  # v0.3: reputation is hidden
            # Replace the first AFFORDABLE seat with the first listed
            # free agent: tapping the seat opens the contract-length
            # picker (v0.3), then picking 2 years signs the driver.
            # (v0.4.2: the $1.5M/race T&R fine leaves McLaren with
            # $12.26M at worst after round 1 — below Piastri's $15.42M
            # break fee — so seat 1 is not always the affordable one;
            # Norris's $11.97M seat always is.)
            seat = p1.evaluate("""() => {
                const btns = [...document.querySelectorAll('#market-box button[data-sign][data-seat]')]
                  .filter(b => (b.dataset.seat === '1' || b.dataset.seat === '2') && !b.disabled);
                return btns.length ? btns[0].dataset.seat : null;
            }""")
            assert seat in ("1", "2"), market
            surname = seats[int(seat) - 1][1]
            p1.click(f"#market-box button[data-sign][data-seat='{seat}']")
            p1.wait_for_selector("[data-contract-years]", timeout=15000)
            # v0.3.1: the picker is an overlay pop-up now, not inline
            # in the market box.
            assert "Pick the contract length" in p1.inner_text("#ov-root")
            p1.click("[data-contract-years='2']")
            p1.wait_for_selector("text=signed for McLaren", timeout=15000)
            toast = p1.inner_text("body")
            assert re.search(r"signed for McLaren.*\$\d+\.\d\dM", toast), toast
            # Standings tab: outgoing driver still listed, McLaren shown.
            p1.click("button[data-tab='standings']")
            standings = p1.inner_text("#standings-box")
            assert surname in standings, standings
            row = next(r for r in standings.splitlines() if surname in r)
            assert "McLaren" in row, row
            # v0.3.1: the free agent's row carries the '|' bar in the
            # LAST team's colour (McLaren orange) — while the surname
            # itself stays uncoloured (v0.1 free-agent rule).
            bar = p1.evaluate("""(surname) => {
                const rows = [...document.querySelectorAll('#standings-box table tr')];
                const row = rows.find(r => r.textContent.includes(surname));
                if (!row) return null;
                const barEl = row.querySelector('.team-bar');
                const surnameEl = row.querySelector('.surname');
                return { bar: barEl ? barEl.style.color : null,
                         surnameStyle: surnameEl ? surnameEl.getAttribute('style') : null };
            }""", surname)
            assert bar is not None, "outgoing driver row missing from standings"
            assert bar["bar"] == "rgb(239, 155, 51)", bar  # McLaren #EF9B33
            assert bar["surnameStyle"] is None, bar  # name stays uncoloured
            browser.close()
    finally:
        proc.terminate()

def test_swapped_driver_stays_in_standings_ui():
    """v0.4.5: the standings TABLE is built by the page itself, and
    v0.4.4 fixed only the server's copy of the rule — so a driver
    swapped to T&R after racing (Stroll, P19 Bahrain) still vanished
    from the table the player sees. End to end in a real browser:
    play Round 1, swap a McLaren main driver to T&R through the
    Drivers tab, and the standings tab must still list him, with
    McLaren in the team column."""
    pytest.importorskip("playwright")
    import re
    import subprocess
    import time
    import urllib.request
    port = 8798
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            p1 = browser.new_page(viewport={"width": 1280, "height": 800})
            p1.goto(f"http://127.0.0.1:{port}/")
            p1.fill("#home-nick", "Max")
            p1.click("#btn-create")
            p1.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
            p1.click("#btn-new-career")
            p1.wait_for_selector("[data-team]", timeout=15000)
            p1.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.textContent.includes('McLaren'))
                    .querySelector('[data-team]').click();
            }""")
            p1.wait_for_selector("#btn-start-career:not(.hidden)", timeout=15000)
            code = re.search(r"\b[A-Z0-9]{6}\b",
                             p1.inner_text("#screen-lobby")).group(0)
            p2 = browser.new_page(viewport={"width": 1280, "height": 800})
            p2.goto(f"http://127.0.0.1:{port}/?code={code}")
            p2.fill("#home-nick", "Cousin")
            p2.click("#btn-join")
            p2.wait_for_selector("[data-team]", timeout=15000)
            p2.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.textContent.includes('Ferrari'))
                    .querySelector('[data-team]').click();
            }""")
            p1.click("#btn-start-career")
            p1.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            # Play one round so the McLaren drivers have an F1 start.
            p1.click("#btn-advance")
            p2.click("#btn-advance")
            p1.wait_for_selector("#screen-results:not(.hidden)", timeout=15000)
            p1.click("#btn-vote-next")
            p2.click("#btn-vote-next")
            p1.wait_for_selector("text=Race Results", timeout=15000)
            p1.click("#btn-vote-next")
            p2.click("#btn-vote-next")
            p1.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            # Seat 1's surname, before the swap (the race sorts D1/D2
            # by team points, so either driver can be in seat 1).
            my_team = p1.inner_text("#my-team")
            seats = re.findall(r"\b([A-Z][a-z]+)\s+([A-Z]+)\b", my_team)
            assert len(seats) >= 2, my_team
            surname = seats[0][1]
            # Drivers tab: swap a T&R driver into Seat 1 (the confirm
            # dialog is an overlay pop-up).
            p1.click("button[data-tab='drivers']")
            p1.wait_for_selector("#drivers-box [data-swap-tr][data-seat='1']",
                                 timeout=15000)
            p1.click("#drivers-box [data-swap-tr][data-seat='1']")
            p1.wait_for_selector("[data-confirm-yes]", timeout=15000)
            p1.click("[data-confirm-yes]")
            p1.wait_for_selector("text=swapped with", timeout=15000)
            # Standings tab: the outgoing driver is still listed, with
            # McLaren in the team column — he raced in F1 this season.
            p1.click("button[data-tab='standings']")
            standings = p1.inner_text("#standings-box")
            assert surname in standings, standings
            row = next(r for r in standings.splitlines() if surname in r)
            assert "McLaren" in row, row
            browser.close()
    finally:
        proc.terminate()


# ---------------------------------------------------------------------------
# v0.3 — hidden reputation, compact market, contract choice, free
# signing, new break-cost formula, 2025 initial order, history grid,
# champion history, save format v5
# ---------------------------------------------------------------------------

def test_break_cost_gasly_example():
    """Matthew's own example, verbatim: Gasly, contract 3, start of
    round 9 (4 rounds left this season), salary $4.0M ->
    28 x $4.0M/12 + $3.0M = $12.33M."""
    career = game.build_new_career("Test", "Matthew")
    alpine = next(t for t in career["teams"] if t["name"] == "Alpine")
    gasly = next(d for d in game.team_drivers(career, alpine)
                 if d["name"] == "Pierre Gasly")
    gasly["salary"] = 4.0
    gasly["contract_left"] = 3
    career["round"] = 9
    fa = game.free_agents(career)[0]
    quote = game.break_cost(career, alpine["id"], fa["id"], 1)
    assert quote is not None
    assert quote["total"] == pytest.approx(12.33, abs=0.01)
    assert quote["break_fee"] == pytest.approx(12.33, abs=0.01)
    # Seat 2 (Colapinto, salary 1.5, 1 year left): only the 4 rounds
    # left this season count -> 4 x 1.5/12 + 3 = $3.50M.
    quote2 = game.break_cost(career, alpine["id"], fa["id"], 2)
    assert quote2["total"] == pytest.approx(3.50, abs=0.01)


def test_break_cost_winter_break_and_final_round():
    """In winter break the current season is over, so the rounds left
    are the whole remaining contract; in round 12 only one round is
    left this season."""
    career = game.build_new_career("Test", "Matthew")
    alpine = next(t for t in career["teams"] if t["name"] == "Alpine")
    gasly = next(d for d in game.team_drivers(career, alpine)
                 if d["name"] == "Pierre Gasly")
    gasly["salary"] = 4.0
    gasly["contract_left"] = 2
    fa = game.free_agents(career)[0]
    career["phase"] = "winter_break"
    career["round"] = 12
    quote = game.break_cost(career, alpine["id"], fa["id"], 1)
    # 2 full years left: 24 x 4/12 + 3 = $11.00M.
    assert quote["total"] == pytest.approx(11.00, abs=0.01)
    career["phase"] = "round"
    career["round"] = 12
    gasly["contract_left"] = 1
    quote = game.break_cost(career, alpine["id"], fa["id"], 1)
    # 1 round left: 1 x 4/12 + 3 = $3.33M.
    assert quote["total"] == pytest.approx(3.33, abs=0.01)


def test_signing_into_empty_seat_is_free():
    """v0.3: no upfront signing cost — signing into an empty seat
    costs $0 and the confirmation says Free."""
    career = game.build_new_career("Test", "Matthew")
    team = career["teams"][0]  # McLaren
    # Empty seat 2 (Piastri "retires" for the test).
    piastri = game.get_driver(career, team["driver2_id"])
    piastri["status"] = "free"
    piastri["team_id"] = None
    team["driver2_id"] = None
    team["money"] = 10.0
    incoming = game.free_agents(career)[0]
    quote = game.break_cost(career, team["id"], incoming["id"], 2)
    assert quote["total"] == 0.0 and quote["affordable"] is True
    money_before = team["money"]
    ok, msg = game.sign_driver(career, team["id"], incoming["id"], 2,
                               contract_years=3)
    assert ok, msg
    assert team["money"] == pytest.approx(money_before)  # nothing charged
    assert "Free" in msg
    assert incoming["status"] == "f1" and incoming["team_id"] == team["id"]
    assert incoming["contract_left"] == 3


def test_contract_length_choice_validated_and_stored():
    """Every signing stores the picked contract length (1–4 years);
    anything outside 1–4 is rejected."""
    career = game.build_new_career("Test", "Matthew")
    team = career["teams"][0]
    team["money"] = 100.0
    for years in (1, 4):
        pool = game.free_agents(career)
        seat = 1 if years == 1 else 2
        ok, msg = game.sign_driver(career, team["id"], pool[0]["id"],
                                   seat, contract_years=years)
        assert ok, msg
        assert pool[0]["contract_left"] == years
    # Invalid lengths are refused and the driver stays unsigned.
    career2 = game.build_new_career("Test", "Matthew")
    team2 = career2["teams"][0]
    team2["money"] = 100.0
    for bad in (0, 5, -1, "x"):
        pool = game.free_agents(career2)
        ok, msg = game.sign_driver(career2, team2["id"], pool[0]["id"],
                                   1, contract_years=bad)
        assert not ok and "1–4" in msg
        assert pool[0]["status"] == "free"
    # A string that parses ("3") is accepted; no choice defaults to 2.
    pool = game.free_agents(career2)
    ok, _ = game.sign_driver(career2, team2["id"], pool[0]["id"], 1,
                             contract_years="3")
    assert ok and pool[0]["contract_left"] == 3
    pool = game.free_agents(career2)
    ok, _ = game.sign_driver(career2, team2["id"], pool[0]["id"], 2)
    assert ok and pool[0]["contract_left"] == 2


def test_bot_contract_length_rule():
    """Bots pick 1–4 years too (ai_contract_years): stars get 4 years,
    weaker drivers get shorter deals — and the winter-break AI
    signing stores a valid length."""
    assert game.ai_contract_years({"rep": 4.4}) == 4
    assert game.ai_contract_years({"rep": 4.0}) == 4
    rng = random.Random(1)
    for _ in range(20):
        assert 2 <= game.ai_contract_years({"rep": 3.5}, rng=rng) <= 4
        assert 1 <= game.ai_contract_years({"rep": 2.5}, rng=rng) <= 3
        assert 1 <= game.ai_contract_years({"rep": 1.5}, rng=rng) <= 2
    career = game.build_new_career("Test", "Matthew")
    team = next(t for t in career["teams"] if t["name"] == "Alpine")
    team["driver2_id"] = None
    # v0.4: bots fill empty seats through the winter-break offer flow
    # (one round at a time, like humans) — no more instant AI signing
    # at season end.
    career["phase"] = "winter_break"
    career["wb_round"] = 1
    career["wb_offers"] = {}
    game.bot_winter_offers(career, team, rng=random.Random(3))
    game.resolve_winter_break_round(career, rng=random.Random(3))
    assert team["driver2_id"] is not None
    picked = game.get_driver(career, team["driver2_id"])
    assert 1 <= picked["contract_left"] <= 4
    assert picked["status"] == "f1"


def test_salary_paid_per_race_after_each_race():
    """MV is the salary per season, paid salary/12 after each race.
    The exact money change for a team over one round is prize money
    (1.95 - 0.025*pos per driver) + per-race gains - salaries/12 -
    the per-race extra charges (v0.4.2: McLaren starts with 4 T&R
    drivers, so it also pays the $1.5M/race T&R fine)."""
    from f1.rules import finance_estimates
    career = two_human_career()
    team = career["teams"][0]  # McLaren
    # McLaren's salaries: Norris 5.2 + Piastri 4.6 + 4 T&R drivers
    # (de Vries 1.5, O'Ward 2.5, Fornaroli 2.0, Verschoor 2.2).
    assert game.salary_per_round(career, team) == pytest.approx(18.0 / 12)
    assert game.tr_fine_per_race(career, team) == pytest.approx(1.5)
    pos_before = game.constructor_pos(career, team["id"])
    money_before = team["money"]
    salaries = sum(d["salary"] for d in game.team_drivers(career, team)) \
        + sum(d["salary"] for d in game.team_tr_drivers(career, team))
    game.advance_team(career, team["id"], rng=seeded())
    game.advance_team(career, career["teams"][1]["id"], rng=seeded(7))
    prize = sum(1.95 - 0.025 * d["race_pos"]
                for d in game.team_drivers(career, team))
    expected = money_before + prize + finance_estimates(pos_before)["eprg"] \
        - salaries / 12 - game.tr_fine_per_race(career, team)
    assert team["money"] == pytest.approx(expected, abs=0.01)


def test_rep_never_reaches_clients_but_bots_still_use_it():
    """Reputation must not appear in any payload sent to the browser
    (chart on or off, broadcast or my_team), and must not be rendered
    by the frontend — while the live career keeps it and the AI still
    judges drivers by it."""
    import server as srv
    career = two_human_career()
    # The live career keeps rep (bots read it from here).
    assert career["drivers"][0]["rep"] > 0
    for chart_on in (True, False):
        dumped = json.dumps(game.public_state(
            career, chart_on, viewer_team_id=career["teams"][0]["id"]))
        assert '"rep"' not in dumped, f"rep leaked with chart_on={chart_on}"
    # my_team (private view) is clean too.
    room = srv.new_room("Matthew", "#e74c3c")
    host = srv.add_player(room, "Matthew", "#e74c3c")
    room["career"] = career
    game.choose_team(career, career["teams"][0]["id"], host["player_id"])
    mt = srv.my_team_public(room, host["player_id"])
    assert '"rep"' not in json.dumps(mt)
    # Bots still judge by rep: a high-rep driver scores higher, and
    # the AI picks the high-rep candidate for a front-running team.
    d1 = dict(career["drivers"][0])
    d2 = dict(d1, rep=1.0)
    assert game.ai_visible_score(d1, career) > game.ai_visible_score(d2, career)
    mclaren = career["teams"][0]
    hi = dict(career["drivers"][0], status="free", team_id=None)
    lo = dict(career["drivers"][1], status="free", team_id=None, rep=1.0, cha=0)
    assert game.ai_best_candidate(career, mclaren, [lo, hi])["id"] == hi["id"]
    # The frontend never renders rep anywhere.
    html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
    assert "d.rep" not in html
    assert "rep ${" not in html
    assert "<th>Rep</th>" not in html
    assert "rep " not in html  # e.g. "rep 3.83" in a driver line


def test_season_one_standings_follow_real_2025_order():
    """Season 1 (2026) opens in the real 2025 final standings order,
    all on 0 points; the three drivers who did not race in F1 in
    2025 (Pérez, Bottas, Lindblad) follow, ordered by perf. The
    constructors open in 2025 constructors order (Audi in Sauber's
    slot, Cadillac last)."""
    from f1 import race as race_mod
    career = game.build_new_career("Test", "Matthew")
    assert [d["name"] for d in race_mod.driver_standings(career)] == [
        "Lando Norris", "Max Verstappen", "Oscar Piastri", "George Russell",
        "Charles Leclerc", "Lewis Hamilton", "Kimi Antonelli", "Alex Albon",
        "Carlos Sainz", "Fernando Alonso", "Nico Hülkenberg", "Isack Hadjar",
        "Oliver Bearman", "Liam Lawson", "Esteban Ocon", "Lance Stroll",
        "Pierre Gasly", "Gabriel Bortoleto", "Franco Colapinto",
        "Sergio Pérez", "Valtteri Bottas", "Arvid Lindblad",
    ]
    assert [t["name"] for t in race_mod.constructor_standings(career)] == [
        "McLaren", "Mercedes", "Red Bull", "Ferrari", "Williams",
        "Racing Bulls", "Aston Martin", "Haas", "Audi", "Alpine", "Cadillac",
    ]
    # Only the 19 drivers with a 2025 order are in prev_driver_order.
    assert len(career["prev_driver_order"]) == 19
    by_id = {d["id"]: d for d in career["drivers"]}
    assert by_id[career["prev_driver_order"][0]]["name"] == "Lando Norris"
    assert by_id[career["prev_driver_order"][-1]]["name"] == "Franco Colapinto"


def test_driver_initials():
    """Race-history grid initials: first initial + '.' + first 3
    surname letters, uppercased (L.HAM)."""
    assert game.driver_initials("Lewis Hamilton") == "L.HAM"
    assert game.driver_initials("Kimi Antonelli") == "K.ANT"
    assert game.driver_initials("Max Verstappen") == "M.VER"
    assert game.driver_initials("Isack Hadjar") == "I.HAD"
    assert game.driver_initials("Oscar Piastri") == "O.PIA"
    assert game.driver_initials("Pierre Gasly") == "P.GAS"


def test_race_history_top3_has_team_id():
    """Each race-history entry records the top 3 with their team id,
    so the history grid can colour each finisher by the team they
    raced for at the time."""
    from f1 import race as race_mod
    career = two_human_career()
    play_full_round(career, seeded(3))
    entry = career["race_history"][0]
    assert len(entry["top3"]) == 3
    for item in entry["top3"]:
        assert item.get("team_id"), item
    assert [x["driver"] for x in entry["top3"]] == [
        r["driver"] for r in career["last_race"][:3]]
    assert game.driver_initials(entry["top3"][0]["driver"]) == \
        game.driver_initials(career["last_race"][0]["driver"])


def test_champion_history_prefill_and_append():
    """Champion history starts with the real 2010–2025 table (16
    rows, no 2026 pre-fill), and a completed in-game season appends
    a row with the sim's champion: age at season start, team,
    points, cumulative trophies, and the constructors' winner."""
    from f1 import race as race_mod
    career = game.build_new_career("Test", "Matthew")
    hist = career["champion_history"]
    assert len(hist) == 16
    assert [r["year"] for r in hist] == list(range(2010, 2026))
    assert hist[0] == {"year": 2010, "driver": "Sebastian Vettel", "age": 22,
                       "team": "Red Bull", "points": 256, "trophies": 1,
                       "winning_team": "Red Bull"}
    # 2021: Verstappen won the drivers' title, Mercedes the
    # constructors' — the table keeps them separate on purpose.
    assert hist[11]["driver"] == "Max Verstappen"
    assert hist[11]["points"] == 395.5 and hist[11]["trophies"] == 1
    assert hist[11]["winning_team"] == "Mercedes"
    # 2024: same split the other way (McLaren won the constructors').
    assert hist[14]["winning_team"] == "McLaren"
    assert hist[-1]["driver"] == "Lando Norris" and hist[-1]["points"] == 423
    # Hamilton's counts include his 2008 title (2014 is x2).
    assert hist[4]["driver"] == "Lewis Hamilton" and hist[4]["trophies"] == 2
    assert hist[10]["trophies"] == 7  # 2020, his seventh title
    # Play a full season and capture the expected champion BEFORE the
    # rollover resets points and ticks ages.
    career = two_human_career()
    connected = {"p-matthew", "p-cousin"}
    rng = seeded(99)
    for _ in range(11):
        play_full_round(career, rng)
    for t in list(game.human_teams(career)):
        game.advance_team(career, t["id"], rng=rng)
    assert career["phase"] == "qual_results"
    for pid in sorted(connected):
        game.vote_next(career, pid, rng=rng)
    assert career["phase"] == "race_results"
    champ = [d for d in race_mod.driver_standings(career)
             if d["status"] == "f1"][0]
    champ_team = game.get_team(career, champ["team_id"])
    winners = race_mod.constructor_standings(career)
    expected = {"year": 2026, "driver": champ["name"], "age": champ["age"],
                "team": champ_team["name"], "points": champ["points"],
                "trophies": champ["cha"] + 1,
                "winning_team": winners[0]["name"]}
    for pid in sorted(connected):
        game.vote_next(career, pid, rng=rng)
    assert career["phase"] == "winter_break"
    assert career["year"] == 2027
    assert len(career["champion_history"]) == 17
    assert career["champion_history"][-1] == expected
    # The champion's age ticked up after the row was recorded.
    assert game.get_driver(career, champ["id"])["age"] == expected["age"] + 1


def test_save_v5_migration_prefills_champion_history():
    """Old saves gain the 2010–2025 champion table on load; an
    in-game champion row (e.g. a simulated 2026) is kept, and the
    save format is v5."""
    from f1.data import CHAMPION_HISTORY_2010_2025
    v4_doc = {
        "version": 4, "name": "Old", "host": "Matthew", "year": 2026,
        "round": 5, "season_index": 0, "phase": "round",
        "teams": [{"name": "McLaren", "id": "t-1", "points": 10}],
        "drivers": [{"name": "Lando Norris", "id": "d-1", "status": "f1",
                     "points": 25, "team_id": "t-1"}],
        "champion_history": [],
        "race_history": [{"year": 2026, "round": 1,
                          "top3": [{"driver": "Lewis Hamilton",
                                    "team": "Ferrari"}]}],
    }
    migrated = migrate_save(v4_doc)
    assert [r["year"] for r in migrated["champion_history"]] == list(range(2010, 2026))
    assert migrated["champion_history"][0]["driver"] == "Sebastian Vettel"
    # top3 entries gain team_id (resolved from the team name).
    assert migrated["race_history"][0]["top3"][0]["team_id"] is None  # no Ferrari team in this doc
    # A v4 save that already played a 2026 season keeps its row.
    v4_doc["champion_history"] = [
        {"year": 2026, "driver": "Kimi Antonelli", "team": "Mercedes", "points": 238}]
    migrated = migrate_save(v4_doc)
    assert len(migrated["champion_history"]) == 17
    assert migrated["champion_history"][-1]["year"] == 2026
    assert migrated["champion_history"][-1]["driver"] == "Kimi Antonelli"
    assert migrated["champion_history"][-1]["age"] is None  # pre-v0.3 row
    # And a save round-trip writes version 6 with the pre-filled table.
    career = two_human_career()
    with tempfile.TemporaryDirectory() as tmp:
        meta = save_career(career, "v5 test", tmp)
        loaded = load_save(tmp, meta["save_id"])
        assert loaded["version"] == 18
        assert len(loaded["champion_history"]) == 16
        assert loaded["champion_history"][0]["year"] == 2010


def test_v03_market_compact_no_rep_history_grid():
    """Headless smoke check: the market renders compactly with
    break-cost / Free display and no reputation anywhere, the
    standings rows carry the coloured '|' bar, and the History tab
    shows the race-history grid (clickable to the full GP) plus the
    champion-history table."""
    pytest.importorskip("playwright")
    import re
    import subprocess
    import time
    import urllib.request
    port = 8799
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(f"http://127.0.0.1:{port}/")
            assert page.evaluate("window.F1UI.driverInitials('Lewis Hamilton')") == "L.HAM"
            assert page.evaluate("window.F1UI.driverInitials('Kimi Antonelli')") == "K.ANT"
            page.fill("#home-nick", "Max")
            page.click("#btn-create")
            page.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
            page.click("#btn-new-career")
            page.wait_for_selector("[data-team]", timeout=15000)
            page.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.textContent.includes('McLaren'))
                    .querySelector('[data-team]').click();
            }""")
            page.wait_for_selector("#btn-start-career:not(.hidden)", timeout=15000)
            page.click("#btn-start-career")
            page.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            # Market: compact rows, break costs, no rep anywhere.
            page.click("button[data-tab='market']")
            page.wait_for_selector("#market-box .market-table", timeout=15000)
            market = page.inner_text("#market-box")
            assert "Cost to sign" in market
            assert "Rep" not in market
            assert "Break $" in market
            assert "Signing $" not in market
            # (No empty seats at round 1, so "Free" is covered by the
            # Python-level empty-seat test instead.)
            # v0.8.1: the first market table is now the F1 grid
            # (poaching) — its rows carry 3-4 action buttons with
            # cost breakdowns, so the compact bound is looser than
            # the old v0.3 rule (which measured the free-agents
            # table when it came first).
            row_h = page.evaluate(
                "document.querySelector('#market-box .market-table tr:nth-child(2)')"
                ".getBoundingClientRect().height")
            assert row_h < 150, f"market row {row_h}px is not compact"
            # Standings: coloured '|' bar on the rows.
            page.click("button[data-tab='standings']")
            page.wait_for_selector("#standings-box .team-bar", timeout=15000)
            standings_html = page.inner_html("#standings-box")
            assert 'class="team-bar"' in standings_html
            assert "Lando Norris" in page.inner_text("#standings-box") or \
                "NORRIS" in page.inner_text("#standings-box")
            # The 2025 order is visible before any points are scored:
            # Norris first, Verstappen second, Piastri third.
            first_rows = page.inner_text("#standings-box").splitlines()
            norris_i = next(i for i, r in enumerate(first_rows) if "NORRIS" in r)
            verstappen_i = next(i for i, r in enumerate(first_rows) if "VERSTAPPEN" in r)
            piastri_i = next(i for i, r in enumerate(first_rows) if "PIASTRI" in r)
            assert norris_i < verstappen_i < piastri_i
            # Play one round, then the History tab.
            page.click("#btn-advance")
            page.wait_for_selector("#screen-results:not(.hidden)", timeout=15000)
            page.click("#btn-vote-next")
            page.wait_for_selector("text=Race Results", timeout=15000)
            page.click("#btn-vote-next")
            page.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            page.click("button[data-tab='history']")
            page.wait_for_selector("#history-box .hist-grid", timeout=15000)
            history = page.inner_text("#history-box")
            assert "R1" in history and "R12" in history
            assert "2026" in history
            # The grid cell for R1 shows the winner's initials.
            winner = page.text_content(
                "#history-box .hist-cell:not(.empty) div").strip()
            assert re.match(r"^[A-Z]\.[A-Z]{3}$", winner), winner
            # Champion history: the pre-filled table, 2010 first.
            assert "Champion history" in history
            assert "2010" in history and "VETTEL" in history
            assert "🏆 x1" in history and "Winning team" in history
            assert "2025" in history and "NORRIS" in history
            # Clicking the R1 cell opens the full Grand Prix — as an
            # overlay pop-up (v0.3.1), not inline in the history box.
            page.click("#history-box .hist-cell:not(.empty)")
            page.wait_for_selector("#btn-gp-back", timeout=15000)
            gp = page.inner_text("#ov-root")
            assert "Qualifying —" in gp and "Race —" in gp
            browser.close()
    finally:
        proc.terminate()


def test_health_reports_v080():
    """The live version string is v0.8.1 everywhere it is shown."""
    from fastapi.testclient import TestClient
    from server import app
    client = TestClient(app)
    assert client.get("/api/health").json()["version"] == "v0.8.1"
    html = client.get("/").text
    assert "<title>F1 Team Manager Online — v0.8.1</title>" in html
    assert "Online · v0.8.1 beta" in html


# ---------------------------------------------------------------------------
# v0.3.1 — minimizable pop-ups: every pop-up (full-GP view, contract
# picker, confirm dialogs, home settings) gets a minimize ("—") and a
# close ("×") button; minimizing collapses it to a floating chip at
# the bottom-left, and clicking the chip restores it exactly as it
# was. Escape and a backdrop click dismiss the topmost open pop-up.
# ---------------------------------------------------------------------------

def _play_one_round(page):
    """Advance through one round (qual vote, race vote) to round 2."""
    page.click("#btn-advance")
    page.wait_for_selector("#screen-results:not(.hidden)", timeout=15000)
    page.click("#btn-vote-next")
    page.wait_for_selector("text=Race Results", timeout=15000)
    page.click("#btn-vote-next")
    page.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)


def test_popups_minimize_restore_close_escape():
    """v0.3.1: the contract picker, the full-GP view, the delete-save
    confirm and the home settings panel can all be minimized to a
    chip (dashboard stays usable), restored by clicking the chip,
    and dismissed with the × button, Cancel, Escape, or a backdrop
    click — without signing anything or losing state."""
    pytest.importorskip("playwright")
    import subprocess
    import time
    import urllib.request
    port = 8801
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(f"http://127.0.0.1:{port}/")
            # --- Home settings panel: minimize / restore / close. ---
            page.click("#btn-settings")
            page.wait_for_selector("#home-settings:not(.hidden)", timeout=15000)
            assert page.is_visible("#home-settings")
            page.click("#btn-settings-min")
            page.wait_for_selector(".ov-chip[data-chip='settings']", timeout=15000)
            assert not page.is_visible("#home-settings")
            assert "Settings" in page.inner_text(".ov-chip[data-chip='settings']")
            page.click(".ov-chip[data-chip='settings'] span")
            page.wait_for_selector("#home-settings:not(.hidden)", timeout=15000)
            assert page.is_visible("#home-settings")
            page.click("#btn-settings-close")
            page.wait_for_function(
                "() => document.getElementById('home-settings')"
                ".classList.contains('hidden')")
            assert not page.is_visible("#home-settings")
            assert page.query_selector(".ov-chip[data-chip='settings']") is None
            # --- Start a solo McLaren career, play one round. ---
            page.fill("#home-nick", "Max")
            page.click("#btn-create")
            page.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
            page.click("#btn-new-career")
            page.wait_for_selector("[data-team]", timeout=15000)
            page.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.textContent.includes('McLaren'))
                    .querySelector('[data-team]').click();
            }""")
            page.wait_for_selector("#btn-start-career:not(.hidden)", timeout=15000)
            page.click("#btn-start-career")
            page.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            _play_one_round(page)
            my_team_before = page.inner_text("#my-team")
            assert "NORRIS" in my_team_before and "PIASTRI" in my_team_before

            def open_picker():
                page.click("button[data-tab='market']")
                page.wait_for_selector("#market-box .market-table", timeout=15000)
                # v0.4.2: after round 1 the D1/D2 swap can leave Seat 1
                # unaffordable (Piastri's $15.42M fee vs ~$12-14M in
                # the bank after the $1.5M/race T&R fine) — open the
                # picker from the first affordable seat instead.
                # v0.7.0: a bad round 1 can leave the bank at ~$11.2M,
                # below even Norris's $11.97M fee, so NO seat is
                # affordable. The bank grows ~$2M+/round, so play
                # another round and retry (up to 2 extra rounds).
                seat = None
                for _ in range(3):
                    seat = page.evaluate("""() => {
                        const btns = [...document.querySelectorAll('#market-box button[data-sign][data-seat]')]
                          .filter(b => (b.dataset.seat === '1' || b.dataset.seat === '2') && !b.disabled);
                        return btns.length ? btns[0].dataset.seat : null;
                    }""")
                    if seat:
                        break
                    _play_one_round(page)
                    page.click("button[data-tab='market']")
                    page.wait_for_selector("#market-box .market-table", timeout=15000)
                assert seat in ("1", "2")
                page.click(f"#market-box button[data-sign][data-seat='{seat}']")
                page.wait_for_selector("[data-contract-years]", timeout=15000)
                assert page.is_visible(".ov-panel")
                assert page.is_visible("#ov-backdrop")
                assert "Sign" in page.inner_text(".ov-panel .ov-head h3")

            def assert_picker_gone():
                assert page.query_selector("[data-contract-years]") is None
                assert page.query_selector(".ov-panel") is None
                # Nothing was signed: the original line-up is intact.
                assert "NORRIS" in page.inner_text("#my-team")
                assert "PIASTRI" in page.inner_text("#my-team")

            # --- Contract picker: minimize, dashboard stays usable,
            #     restore, then dismiss with Escape. ---
            open_picker()
            page.click("[data-ov-min='sign']")
            page.wait_for_selector(".ov-chip[data-chip='sign']", timeout=15000)
            assert not page.is_visible(".ov-panel")
            assert page.query_selector("#ov-backdrop.hidden") is not None
            assert "Sign" in page.inner_text(".ov-chip[data-chip='sign']")
            # Dashboard usable while minimized: switch tabs freely.
            page.click("button[data-tab='standings']")
            page.wait_for_selector("#standings-box .team-bar", timeout=15000)
            assert page.is_visible("#standings-box")
            page.click(".ov-chip[data-chip='sign'] span")
            page.wait_for_selector("[data-contract-years]", timeout=15000)
            assert page.is_visible(".ov-panel")
            assert "Pick the contract length" in page.inner_text("#ov-root")
            page.keyboard.press("Escape")
            page.wait_for_selector(".ov-panel", state="detached", timeout=15000)
            assert_picker_gone()
            # --- Same picker: close with ×, then with a backdrop click,
            #     then with Cancel. ---
            open_picker()
            page.click("[data-ov-close='sign']")
            page.wait_for_selector(".ov-panel", state="detached", timeout=15000)
            assert_picker_gone()
            open_picker()
            page.mouse.click(20, 20)  # top-left corner: the backdrop, not the panel
            page.wait_for_selector(".ov-panel", state="detached", timeout=15000)
            assert_picker_gone()
            open_picker()
            page.click("#btn-sign-cancel")
            page.wait_for_selector(".ov-panel", state="detached", timeout=15000)
            assert_picker_gone()
            # --- Full-GP view: minimize / restore / Escape / backdrop. ---
            page.click("button[data-tab='history']")
            page.wait_for_selector("#history-box .hist-grid", timeout=15000)
            page.click("#history-box .hist-cell:not(.empty)")
            page.wait_for_selector("#btn-gp-back", timeout=15000)
            assert page.is_visible(".ov-panel")
            assert "Bahrain" in page.inner_text(".ov-panel .ov-head h3")
            gp_text = page.inner_text("#ov-root")
            assert "Qualifying —" in gp_text and "Race —" in gp_text
            page.click("[data-ov-min='gp']")
            page.wait_for_selector(".ov-chip[data-chip='gp']", timeout=15000)
            assert not page.is_visible(".ov-panel")
            assert "Bahrain" in page.inner_text(".ov-chip[data-chip='gp']")
            # Dashboard usable while minimized.
            page.click("button[data-tab='market']")
            page.wait_for_selector("#market-box .market-table", timeout=15000)
            assert page.is_visible("#market-box")
            page.click(".ov-chip[data-chip='gp'] span")
            page.wait_for_selector("#btn-gp-back", timeout=15000)
            assert page.is_visible(".ov-panel")
            assert "Qualifying —" in page.inner_text("#ov-root")
            page.keyboard.press("Escape")
            page.wait_for_selector(".ov-panel", state="detached", timeout=15000)
            # (The Market tab was active while the GP was minimized, so
            # switch back to History before checking the grid.)
            page.click("button[data-tab='history']")
            page.wait_for_selector("#history-box .hist-grid", timeout=15000)
            assert page.is_visible("#history-box .hist-grid")
            page.click("#history-box .hist-cell:not(.empty)")
            page.wait_for_selector("#btn-gp-back", timeout=15000)
            page.mouse.click(20, 20)
            page.wait_for_selector(".ov-panel", state="detached", timeout=15000)
            assert page.is_visible("#history-box .hist-grid")
            # --- Delete-save confirm: Escape cancels, Yes deletes. ---
            # (Unique name: earlier runs may have left saves behind.)
            import time as _time
            save_name = f"Popup test {_time.time():.0f}"
            page.click("button[data-tab='saves']")
            page.wait_for_selector("#tab-saves:not(.hidden)", timeout=15000)
            page.fill("#save-name", save_name)
            page.click("#btn-save")
            # (The same saves list also renders in the hidden lobby
            # screen, so scope every selector to the game tab's list,
            # and to this run's save name.)
            del_sel = f"#saves-list-game [data-del-name='{save_name}']"
            page.wait_for_selector(del_sel, timeout=15000)
            page.click(del_sel)
            page.wait_for_selector(".ov-panel", timeout=15000)
            assert "Delete save" in page.inner_text(".ov-panel .ov-head h3")
            assert page.is_visible("[data-confirm-yes]")
            page.keyboard.press("Escape")
            page.wait_for_selector(".ov-panel", state="detached", timeout=15000)
            assert page.query_selector(del_sel) is not None  # save kept
            page.click(del_sel)
            page.wait_for_selector("[data-confirm-yes]", timeout=15000)
            page.click("[data-confirm-yes]")
            page.wait_for_selector(del_sel, state="detached", timeout=15000)
            browser.close()
    finally:
        proc.terminate()


# ---------------------------------------------------------------------------
# v0.4.0 — standings sort, T&R drivers, winter break, reg revamp, bot parity
# ---------------------------------------------------------------------------

def test_standings_sort_tiebreaks_points_wins_best_finish_earliest():
    """Feature 1: ties sort by points, then wins, then best finish,
    then whoever achieved that best finish FIRST. Non-point scorers
    sort by best finish, then earliest."""
    from f1 import race as race_mod
    career = game.build_new_career("Test", "Matthew")
    f1 = [d for d in career["drivers"] if d["status"] == "f1"][:3]
    a, b, c = f1
    for x in (a, b, c):
        x["points"] = 10
        x["season_wins"] = 1
    a["best_pos"] = 2
    a["best_finish_round"] = 3
    b["best_pos"] = 2
    b["best_finish_round"] = 5
    c["best_pos"] = 3
    c["best_finish_round"] = 1
    top = [d["id"] for d in race_mod.driver_standings(career)[:3]]
    # a before b (same best finish, a got it first); c last (worse finish).
    assert top == [a["id"], b["id"], c["id"]]
    # Non-point scorers: same rule, wins 0.
    for x in (a, b, c):
        x["points"] = 0
        x["season_wins"] = 0
    top = [d["id"] for d in race_mod.driver_standings(career)[:3]]
    assert top == [a["id"], b["id"], c["id"]]
    # Wins break a points tie before best finish does.
    a["points"] = b["points"] = c["points"] = 10
    a["season_wins"] = 0
    b["season_wins"] = 2
    c["season_wins"] = 1
    top = [d["id"] for d in race_mod.driver_standings(career)[:3]]
    assert top == [b["id"], c["id"], a["id"]]


def test_zero_point_driver_who_raced_stays_in_standings_after_tr_swap():
    """v0.4.4 (Matthew's rule): a driver who competed in an F1 race is
    registered in the standings even with 0 points — so Stroll, P19 in
    Bahrain then swapped to T&R for Vandoorne, stays listed, like real
    F1. An F2 driver with F2 finishes must NOT leak into the F1 table."""
    from f1 import race as race_mod
    career = game.build_new_career("Test", "Matthew")
    f1 = [d for d in career["drivers"] if d["status"] == "f1"]
    racer = f1[0]
    racer["points"] = 0
    racer["f1_races"] = 1
    racer["finish_count"] = 1
    racer["best_pos"] = 19
    racer["best_finish_round"] = 1
    # Swapped to T&R after racing: still registered.
    racer["status"] = "tr"
    assert any(d["id"] == racer["id"]
               for d in race_mod.driver_standings(career))
    # Dropped to free agency after racing: still registered.
    racer["status"] = "free"
    racer["team_id"] = None
    assert any(d["id"] == racer["id"]
               for d in race_mod.driver_standings(career))
    # But an F2 driver with finishes (from F2 races) and no F1 starts
    # stays out of the F1 table.
    f2 = [d for d in career["drivers"] if d["status"] == "f2"][0]
    f2["finish_count"] = 3
    f2["f1_races"] = 0
    assert not any(d["id"] == f2["id"]
                   for d in race_mod.driver_standings(career))
    # And a T&R driver who never raced in F1 stays out.
    tr = [d for d in career["drivers"] if d["status"] == "tr"][0]
    tr["finish_count"] = 0
    tr["f1_races"] = 0
    assert not any(d["id"] == tr["id"]
                   for d in race_mod.driver_standings(career))


def test_swapped_zero_point_driver_stays_in_standings_after_a_real_round():
    """v0.4.4 end to end: play Round 1, swap a 0-point main driver to
    T&R, and he is still in the Drivers' Championship."""
    from f1 import race as race_mod
    career = two_human_career()
    play_full_round(career, seeded(6))
    assert career["round"] == 2
    target = None
    for t in career["teams"]:
        trs = game.team_tr_drivers(career, t)
        if not trs:
            continue
        for seat, slot in ((1, "driver1_id"), (2, "driver2_id")):
            d = game.get_driver(career, t[slot])
            if d and d["points"] == 0:
                target = (t, seat, d, trs[0])
                break
        if target:
            break
    assert target, "no 0-point main driver with a T&R teammate after Round 1"
    team, seat, main, tr = target
    # One F1 start from Round 1, recorded by the race loop.
    assert main.get("f1_races", 0) == 1
    ok, msg = game.swap_main_tr(career, team["id"], seat, tr["id"])
    assert ok, msg
    assert main["status"] == "tr"
    assert any(d["id"] == main["id"]
               for d in race_mod.driver_standings(career))


def test_migrate_backfills_f1_races_from_f1_finishes():
    """v0.4.4: a v7 save has no f1_races — migration backfills it from
    finish_count for non-F2 drivers (their finishes came from F1 races)
    and 0 for F2 drivers, so Stroll's Round 1 start survives the load."""
    from f1 import race as race_mod
    career = game.build_new_career("Test", "Matthew")
    doc = json.loads(json.dumps(career))  # a plain v7-style dict
    for d in doc["drivers"]:
        d.pop("f1_races", None)
    # The Stroll shape: T&R now, raced once in F1, 0 points.
    stroll = next(d for d in doc["drivers"] if d["status"] == "f1")
    stroll["status"] = "tr"
    stroll["points"] = 0
    stroll["finish_count"] = 1
    stroll["best_pos"] = 19
    f2 = next(d for d in doc["drivers"] if d["status"] == "f2")
    f2["finish_count"] = 3
    migrate_save(doc)
    assert stroll["f1_races"] == 1
    assert f2["f1_races"] == 0
    assert any(d["id"] == stroll["id"]
               for d in race_mod.driver_standings(doc))
    assert not any(d["id"] == f2["id"]
                   for d in race_mod.driver_standings(doc))


def test_constructors_sort_tiebreaks_and_wins():
    """Feature 1 (constructors): points, wins, best finish, earliest."""
    from f1 import race as race_mod
    career = game.build_new_career("Test", "Matthew")
    teams = career["teams"][:3]
    for t in teams:
        t["points"] = 20
        t["season_wins"] = 1
    teams[0]["best_pos"] = 2
    teams[0]["best_finish_round"] = 4
    teams[1]["best_pos"] = 2
    teams[1]["best_finish_round"] = 2
    teams[2]["best_pos"] = 1
    teams[2]["best_finish_round"] = 6
    top = [t["id"] for t in race_mod.constructor_standings(career)[:3]]
    # Best finish 1 wins outright; then the earlier round wins the tie.
    assert top == [teams[2]["id"], teams[1]["id"], teams[0]["id"]]


def test_f2_standings_use_same_sort():
    """Feature 2: the background F2 championship, drivers only, same sort."""
    from f1 import race as race_mod
    career = game.build_new_career("Test", "Matthew")
    f2 = [d for d in career["drivers"] if d["status"] == "f2"]
    assert len(f2) == 22
    f2[0]["points"] = 50
    f2[0]["season_wins"] = 2
    f2[0]["best_pos"] = 1
    f2[0]["best_finish_round"] = 5
    f2[1]["points"] = 50
    f2[1]["season_wins"] = 2
    f2[1]["best_pos"] = 1
    f2[1]["best_finish_round"] = 3
    standings = race_mod.f2_standings(career)
    assert all(d["status"] == "f2" for d in standings)
    # Same points/wins/best finish — the earlier round wins.
    assert standings[0]["id"] == f2[1]["id"]
    assert standings[1]["id"] == f2[0]["id"]


def test_tr_signing_research_bonus_swap_and_poaching():
    """Feature 4: T&R signing (1–4 yrs), +25% research per T&R driver,
    full salary, free swap (points banked), and poaching with the
    break fee. v0.4.2: there is NO hard cap any more — a 3rd T&R
    driver signs fine, and the exponential fine ($0.5M/race for the
    3rd) applies instead."""
    career = game.build_new_career("Test", "Matthew")
    # Racing Bulls starts with no T&R drivers, so the research bonus
    # is a clean x1.25 with one T&R driver (McLaren starts with 4).
    team = next(t for t in career["teams"] if t["name"] == "Racing Bulls")
    team["money"] = 100.0
    pool = [d for d in game.free_agents(career) if d["age"] <= 25]
    assert pool
    tr = pool[0]
    ok, msg = game.sign_tr_driver(career, team["id"], tr["id"], contract_years=2)
    assert ok, msg
    assert tr["status"] == "tr" and tr["team_id"] == team["id"]
    assert tr["contract_left"] == 2
    assert game.team_tr_drivers(career, team) == [tr]
    # Research bonus: the pool gained is +25% with one T&R driver.
    ok, msg = game.do_research(career, team["id"], "perf", 4.0)
    assert ok, msg
    assert team["perf_res"] == pytest.approx(4.0 * 1.25)
    assert team["money"] == pytest.approx(100.0 - 4.0)
    # T&R drivers are paid their full salary.
    main_salaries = sum(d["salary"] for d in game.team_drivers(career, team))
    assert game.salary_per_round(career, team) == pytest.approx(
        (main_salaries + tr["salary"]) / 12)
    # Swap: the Seat 2 driver's per-team points are banked, the T&R
    # driver takes the seat with a fresh per-team tally.
    seat2 = game.get_driver(career, team["driver2_id"])
    seat2["team_points"] = 15
    team["points_kept"] = 0
    ok, msg = game.swap_main_tr(career, team["id"], 2, tr["id"])
    assert ok, msg
    assert game.get_driver(career, team["driver2_id"])["id"] == tr["id"]
    assert tr["status"] == "f1" and tr["team_points"] == 0
    assert seat2["status"] == "tr" and seat2["team_id"] == team["id"]
    assert team["points_kept"] == 15
    # Poaching: another team signs Racing Bulls' T&R driver (the old
    # Seat 2 driver) by paying the break fee.
    other = next(t for t in career["teams"] if t["name"] == "Mercedes")
    other["money"] = 100.0
    quote = game.sign_cost(career, other["id"], seat2["id"], ("tr", None))
    assert quote is not None and quote["poach_fee"] > 0
    assert quote["total"] == quote["poach_fee"]
    # v0.8.0: seat2 is Racing Bulls' T&R driver, so this is a
    # poach — a secret meeting comes first (money_before is read
    # after the meeting, so the fee assertion still holds exactly).
    ok, msg = game.request_meeting(
        career, other["id"], seat2["id"], ("tr", None),
        rng=_SeqRng([0.0, 0.99]))
    assert ok and "agreed" in msg, msg
    money_before = other["money"]
    ok, msg = game.sign_tr_driver(career, other["id"], seat2["id"], contract_years=1)
    assert ok, msg
    assert other["money"] == pytest.approx(money_before - quote["total"])
    assert seat2["status"] == "tr" and seat2["team_id"] == other["id"]
    # v0.4.2: NO hard cap — a 3rd T&R driver signs fine, and the
    # exponential fine applies ($0.5M/race for the 3rd driver).
    team["money"] = 100.0
    for _ in range(2):
        p = next(d for d in game.free_agents(career) if d["age"] <= 25)
        ok, msg = game.sign_tr_driver(career, team["id"], p["id"], contract_years=1)
        assert ok, msg
    assert len(game.team_tr_drivers(career, team)) == 2
    assert game.tr_fine_per_race(career, team) == 0.0
    p = next(d for d in game.free_agents(career) if d["age"] <= 25)
    ok, msg = game.sign_tr_driver(career, team["id"], p["id"], contract_years=1)
    assert ok, msg
    assert len(game.team_tr_drivers(career, team)) == 3
    assert game.tr_fine_per_race(career, team) == pytest.approx(0.5)
    # The market quote shows the fine that would apply AFTER signing
    # one more (a 4th T&R driver would take the fine to $1.5M/race).
    state = game.public_state(career, True, viewer_team_id=team["id"])
    nxt = next(d for d in game.free_agents(career))
    assert state["market_costs"][nxt["id"]]["tr"]["fine_after"] == pytest.approx(1.5)


def test_winter_break_offer_resolution_champion_first():
    """Feature 6: offers resolve in last season's constructors' order
    (champion first); a driver accepts the first offer they get."""
    career = game.build_new_career("Test", "Matthew")
    career["phase"] = "winter_break"
    career["wb_round"] = 1
    career["wb_offers"] = {}
    career["wb_ready"] = []
    career["wb_done"] = False
    teams = career["teams"]
    # Mercedes is the "champion", McLaren last — offers resolve in
    # that order.
    career["last_season"] = {
        "year": 2026, "drivers": [],
        "teams": [{"id": t["id"], "name": t["name"], "points": 0, "wins": 0,
                   "best_pos": None} for t in reversed(teams)],
    }
    mclaren, mercedes = teams[0], teams[1]
    mclaren["driver1_id"] = None
    mercedes["driver1_id"] = None
    mclaren["money"] = mercedes["money"] = 100.0
    target = game.free_agents(career)[0]
    # Both teams offer the same driver for Seat 1; Mercedes (champion)
    # is processed first and signs them.
    career["wb_offers"] = {
        mclaren["id"]: [{"driver_id": target["id"], "target": "seat", "seat": 1, "years": 2}],
        mercedes["id"]: [{"driver_id": target["id"], "target": "seat", "seat": 1, "years": 2}],
    }
    game.resolve_winter_break_round(career, rng=random.Random(1))
    assert mercedes["driver1_id"] == target["id"]
    assert target["status"] == "f1" and target["team_id"] == mercedes["id"]
    assert mclaren["driver1_id"] is None
    # A team's main-seat offers are processed before its T&R offer.
    # (McLaren starts with 4 T&R drivers in v0.4.2, so the T&R offer
    # takes it to 5 — the point here is that both offers resolve.)
    tr_before = len(game.team_tr_drivers(career, mclaren))
    career["wb_offers"] = {
        mclaren["id"]: [
            {"driver_id": game.free_agents(career)[0]["id"], "target": "tr", "seat": None, "years": 1},
            {"driver_id": game.free_agents(career)[1]["id"], "target": "seat", "seat": 1, "years": 2},
        ],
    }
    game.resolve_winter_break_round(career, rng=random.Random(1))
    assert mclaren["driver1_id"] is not None
    assert len(game.team_tr_drivers(career, mclaren)) == tr_before + 1


def test_winter_break_autofill_promotes_tr_then_worst_free_agent():
    """Feature 6 auto-fill: a team with a T&R driver promotes the
    highest-rep one; a team with none signs one of the five
    worst-reputation free agents."""
    career = game.build_new_career("Test", "Matthew")
    # Racing Bulls starts with no T&R drivers (most teams start with
    # some in v0.4.2); Alpine's single T&R driver is cleared so it
    # takes the worst-free-agent path.
    rb = next(t for t in career["teams"] if t["name"] == "Racing Bulls")
    alpine = next(t for t in career["teams"] if t["name"] == "Alpine")
    rb["money"] = alpine["money"] = 100.0
    for d in game.team_tr_drivers(career, alpine):
        d["status"] = "free"
        d["team_id"] = None
    assert game.team_tr_drivers(career, alpine) == []
    # Racing Bulls signs two T&R drivers — the higher-rep one is promoted.
    # (T&R signings happen before the winter break: direct signing is
    # blocked once the break starts, offers only.)
    trs = [d for d in game.free_agents(career) if d["age"] <= 25][:2]
    for tr in trs:
        ok, msg = game.sign_tr_driver(career, rb["id"], tr["id"], contract_years=2)
        assert ok, msg
    career["phase"] = "winter_break"
    rb["driver1_id"] = None
    alpine["driver1_id"] = None
    # The five worst-reputation free agents, before the auto-fill.
    worst_five = sorted([d for d in career["drivers"] if d["status"] == "free"],
                        key=lambda d: (d["rep"], d["salary"], d["name"]))[:5]
    worst_ids = {d["id"] for d in worst_five}
    game.autofill_winter_break(career, rng=random.Random(2))
    assert rb["driver1_id"] is not None
    promoted = game.get_driver(career, rb["driver1_id"])
    assert promoted["status"] == "f1"
    assert promoted["id"] == max(trs, key=lambda d: d["rep"])["id"]
    # Alpine has no T&R driver: it signs one of the five
    # worst-reputation free agents.
    assert alpine["driver1_id"] is not None
    signed = game.get_driver(career, alpine["driver1_id"])
    assert signed["status"] == "f1"
    assert signed["id"] in worst_ids


def test_reg_adaptation_math_and_gating():
    """Feature 7: greyed out before round 7; minor unadapted = own
    perf − bottom team's perf; major = full perf; $0.30M/pt; 0 perf
    if nothing is adapted on a major year."""
    from f1.rules import reg_change_v2
    career = game.build_new_career("Test", "Matthew")
    team = career["teams"][0]
    assert not game.reg_open_for(career, team)
    ok, msg = game.do_reg_research(career, team["id"], 1.0)
    assert not ok and "round 7" in msg
    ok, msg = game.do_reg_adapt(career, team["id"])
    assert not ok and "round 7" in msg
    career["round"] = 7
    game.init_reg_state(career)
    bottom = min(t["perf"] for t in career["teams"])
    assert team["reg_unadapted"] == pytest.approx(team["perf"] - bottom)
    assert team["reg_adapted"] == pytest.approx(bottom)
    assert game.reg_open_for(career, team)
    # The bottom team is fully adapted and can research straight away.
    bottom_team = min(career["teams"], key=lambda t: t["perf"])
    assert bottom_team["reg_unadapted"] == 0
    ok, msg = game.do_reg_research(career, bottom_team["id"], 1.0)
    assert ok, msg
    # Adaptation costs $0.30M per point.
    team["money"] = 100.0
    unadapted = team["reg_unadapted"]
    ok, msg = game.do_reg_adapt(career, team["id"], points=10)
    assert ok, msg
    assert team["money"] == pytest.approx(100.0 - 10 * 0.30)
    assert team["reg_unadapted"] == pytest.approx(unadapted - 10)
    assert team["reg_adapted"] == pytest.approx(bottom + 10)
    # Research is refused until the car is fully adapted.
    ok, msg = game.do_reg_research(career, team["id"], 1.0)
    assert not ok and "Adapt" in msg
    # The reg view shows percentages and money only — never perf.
    view = game.reg_view(career, team)
    assert set(view) == {"open", "adapted_pct", "unadapted_pct",
                         "adapt_cost", "research", "kind", "year"}
    assert view["adapted_pct"] + view["unadapted_pct"] == pytest.approx(100.0)
    # reg_change_v2: nothing adapted and nothing researched = 0 perf.
    assert reg_change_v2(0.0, 0.0, 35.0, rng=random.Random(1)) == 0.0
    assert reg_change_v2(0.0, 0.0, 12.0, rng=random.Random(1)) == 0.0
    # Fully adapted + research gives a real car back.
    assert reg_change_v2(150.0, 10.0, 12.0, rng=random.Random(1)) > 100
    # Major year: every team starts fully unadapted.
    career2 = game.build_new_career("Test2", "Matthew")
    career2["next_reg_kind"] = "Major"
    career2["round"] = 7
    game.init_reg_state(career2)
    for t in career2["teams"]:
        assert t["reg_unadapted"] == pytest.approx(t["perf"])
        assert t["reg_adapted"] == 0


def test_save_v7_migration_and_season3_save_enters_winter_break():
    """Feature 5/6: save format v7; a v5/v6 doc gains the v0.4 fields
    plus the v0.4.2 academy fields (empty academies, T&R state
    unchanged); the real Season 3 save (v5, winter break) migrates
    into the new 3-round winter-break flow with its last season
    reconstructed."""
    from f1.saves import SAVE_VERSION
    assert SAVE_VERSION == 18
    doc = {"version": 6, "phase": "round", "round": 3, "year": 2026,
           "teams": [{"id": "t1", "name": "McLaren", "points": 10}],
           "drivers": [{"id": "d1", "name": "Lando Norris", "status": "f1",
                        "points": 5, "team_id": "t1"}],
           "race_history": [], "champion_history": []}
    migrated = migrate_save(doc)
    assert migrated["prev_team_order"] == []
    assert migrated["wb_round"] == 0
    assert migrated["wb_offers"] == {}
    assert migrated["wb_done"] is False
    assert migrated["last_season"] is None
    assert migrated["drivers"][0]["best_finish_round"] is None
    assert migrated["drivers"][0]["season_podiums"] == 0
    assert migrated["teams"][0]["reg_pending"] is False
    assert migrated["teams"][0]["season_wins"] == 0
    # v0.4.2: the academy fields are present and empty.
    assert migrated["drivers"][0]["academy_team_id"] is None
    assert migrated["teams"][0]["ai_academy_signed"] is False
    # A v6 winter-break save enters the new flow at round 1.
    doc["phase"] = "winter_break"
    migrated = migrate_save(doc)
    assert migrated["wb_round"] == 1
    # The real Season 3 save (if present in this checkout).
    path = Path(__file__).parent.parent / "saves" / "matthew-max-season3-complete.json"
    if not path.exists():
        pytest.skip("Season 3 save not present in this checkout")
    real = json.loads(path.read_text())
    assert real["version"] == 5
    migrated = migrate_save(real)
    assert migrated["phase"] == "winter_break"
    assert migrated["wb_round"] == 1
    assert migrated["wb_done"] is False
    ls = migrated["last_season"]
    assert ls is not None and ls["year"] == 2028
    assert ls["drivers"][0]["name"] == "Oliver Bearman"
    assert ls["drivers"][0]["points"] == 221
    assert ls["drivers"][0]["wins"] == 5
    assert ls["teams"][0]["name"] == "Ferrari"
    assert ls["teams"][0]["points"] == 331
    assert len(migrated["prev_team_order"]) == 11
    # And it loads through load_save (the path the server uses).
    with tempfile.TemporaryDirectory() as tmp:
        import shutil
        shutil.copy(path, Path(tmp) / "matthew-max-season3-complete.json")
        loaded = load_save(tmp, "matthew-max-season3-complete")
        assert loaded is not None
        assert loaded["phase"] == "winter_break"
        assert loaded["wb_round"] == 1
        assert loaded["last_season"]["drivers"][0]["name"] == "Oliver Bearman"


def test_continue_saved_career_from_home():
    """Feature 5: the home screen's Continue action creates a room
    with the saved career loaded; the saves list endpoint serves it."""
    from fastapi.testclient import TestClient
    import server as srv

    with tempfile.TemporaryDirectory() as tmp:
        # Point the server's saves dir at the temp dir for this test.
        old_dir = srv.SAVES_DIR
        srv.SAVES_DIR = Path(tmp)
        try:
            career = two_human_career()
            meta = save_career(career, "Continue test", tmp)
            client = TestClient(srv.app)
            assert any(s["save_id"] == meta["save_id"]
                       for s in client.get("/api/saves").json()["saves"])

            def recv_until(ws, want, limit=40, check=None):
                for _ in range(limit):
                    m = ws.receive_json()
                    if m.get("type") == want and (check is None or check(m)):
                        return m
                raise AssertionError(f"never got {want}")

            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "create_from_save",
                              "save_id": meta["save_id"],
                              "nickname": "Matthew", "colour": "#e74c3c"})
                joined = recv_until(ws, "joined")
                assert joined["room"]["career"] is not None
                assert joined["room"]["career"]["name"] == "Continue test"
                assert joined["room"]["career_mode"] == "loaded"
                # The host is in the room; the career is the saved one.
                assert joined["room"]["career"]["year"] == 2026
        finally:
            srv.SAVES_DIR = old_dir


def test_bot_drops_or_swaps_bad_driver():
    """Bot parity: from round 6, a bot drops (or swaps, when it has a
    T&R driver) a main driver with 0 points and an average finish of
    18th or worse — never in the last 2 rounds, and never when it
    cannot afford the break fee. Every action is logged."""
    career = game.build_new_career("Test", "Matthew")
    # Racing Bulls starts with no T&R drivers (Alpine starts with
    # Paul Aron in v0.4.2, so its bot would swap instead of drop).
    team = next(t for t in career["teams"] if t["name"] == "Racing Bulls")
    team["money"] = 100.0
    career["round"] = 8
    bad = game.get_driver(career, team["driver2_id"])
    bad["points"] = 0
    bad["finish_sum"] = 19 * 6
    bad["finish_count"] = 6
    # No T&R driver: the bot DROPS the driver and pays the break fee.
    money_before = team["money"]
    game.bot_post_race(career, rng=random.Random(1))
    assert team["driver2_id"] is None
    assert bad["status"] == "free"
    assert team["money"] < money_before
    assert any("dropped" in t and bad["name"] in t for t in _ttexts(career))
    # With a T&R driver, the bot SWAPS instead of dropping.
    ok, msg = game.sign_driver(career, team["id"], game.free_agents(career)[0]["id"], 2)
    assert ok, msg
    new_bad = game.get_driver(career, team["driver2_id"])
    new_bad["points"] = 0
    new_bad["finish_sum"] = 20 * 6
    new_bad["finish_count"] = 6
    tr = next(d for d in game.free_agents(career) if d["age"] <= 25)
    ok, msg = game.sign_tr_driver(career, team["id"], tr["id"], contract_years=1)
    assert ok, msg
    game.bot_post_race(career, rng=random.Random(1))
    assert game.get_driver(career, team["driver2_id"])["id"] == tr["id"]
    assert new_bad["status"] == "tr"
    assert any("swapped" in t for t in _ttexts(career))
    # Last 2 rounds: no drop or swap, however bad the driver is.
    career["round"] = 11
    seat1 = game.get_driver(career, team["driver1_id"])
    seat1["points"] = 0
    seat1["finish_sum"] = 22 * 11
    seat1["finish_count"] = 11
    game.bot_post_race(career, rng=random.Random(1))
    assert team["driver1_id"] == seat1["id"]
    # Unaffordable break fee: no drop either.
    career["round"] = 8
    team["money"] = 0.5
    # Remove the team's T&R driver (new_bad, swapped out above) so the
    # only option is a drop — which the team cannot afford.
    new_bad["status"] = "free"
    new_bad["team_id"] = None
    game.bot_post_race(career, rng=random.Random(1))
    assert team["driver1_id"] == seat1["id"]


def test_bot_reg_participation():
    """Bot parity: from round 7 a bot team adapts its car to the new
    regulations (and researches once fully adapted), like a player."""
    career = game.build_new_career("Test", "Matthew")
    team = next(t for t in career["teams"] if t["name"] == "Alpine")
    team["money"] = 100.0
    career["round"] = 7
    game.init_reg_state(career)
    unadapted_before = team["reg_unadapted"]
    assert unadapted_before > 0
    game.ai_act(career, team, rng=random.Random(5))
    assert team["reg_unadapted"] < unadapted_before or team["reg_research"] > 0
    # A fully adapted bot researches instead.
    team["reg_unadapted"] = 0.0
    team["reg_adapted"] = team["reg_total"]
    team["money"] = 100.0
    game.ai_act(career, team, rng=random.Random(5))
    assert team["reg_research"] > 0


def test_bot_develops_more_v053():
    """v0.5.3: bots develop whenever the banked research is worth
    converting (combined pool > 2.0) and put most of their research
    into Performance (50-75%). The old code gated develop behind a
    50% coin flip and split 0-50% to Performance, so bot cars
    barely developed."""
    career = game.build_new_career("Test", "Matthew")
    team = next(t for t in career["teams"] if t["name"] == "Alpine")
    team["money"] = 100.0
    # A banked pool worth converting must develop, every time.
    team["perf_res"] = 3.0
    team["reli_res"] = 1.0
    perf_before = team["perf"]
    game.ai_act(career, team, rng=random.Random(7))
    assert team["perf"] > perf_before
    assert team["perf_res"] == 0.0 and team["reli_res"] == 0.0
    # Over several rounds a bot car should gain real performance.
    team["money"] = 10.0
    perf_before = team["perf"]
    for i in range(6):
        game.ai_act(career, team, rng=random.Random(100 + i))
    assert team["perf"] - perf_before > 5.0


def test_ws_winter_break_offers_next_and_start():
    """Feature 6 over the real WebSocket: a human submits offers and
    hits NEXT for 3 rounds, then starts the next season. Bot offers
    are made when each round opens."""
    from fastapi.testclient import TestClient
    import server as srv

    client = TestClient(srv.app)

    def recv_until(ws, want, limit=60, check=None):
        for _ in range(limit):
            m = ws.receive_json()
            if m.get("type") == want and (check is None or check(m)):
                return m
        raise AssertionError(f"never got {want}")

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "create_room", "nickname": "Matthew", "colour": "#e74c3c"})
        joined = recv_until(ws, "joined")
        code = joined["code"]
        ws.send_json({"type": "new_career", "name": "WS Winter"})
        state = recv_until(ws, "room_state",
                           check=lambda m: m["room"]["career"] is not None)
        team_id = state["room"]["career"]["teams"][0]["id"]
        ws.send_json({"type": "choose_team", "team_id": team_id})
        ws.send_json({"type": "start_career"})
        recv_until(ws, "room_state", check=lambda m: m["room"]["started"] is True)
        # Drive 12 rounds at the game level (the room's career dict is
        # the same object the socket serves).
        room = srv.ROOMS[code]
        career = room["career"]
        connected = {joined["player_id"]}
        rng = seeded(11)
        for _ in range(12):
            play_full_round(career, rng)
        assert career["phase"] == "winter_break"
        # The human's team has an empty seat (its driver left at season
        # end) or not — either way, offers + NEXT work over the socket.
        my_team = next(t for t in career["teams"] if t["id"] == team_id)
        for rnd in (1, 2, 3):
            assert career["wb_round"] == rnd
            if game.empty_seats(career, my_team):
                pool = game.free_agents(career) + game.f2_drivers(career)
                offers = [{"driver_id": pool[0]["id"], "target": "seat",
                           "seat": game.empty_seats(career, my_team)[0], "years": 2}]
                ws.send_json({"type": "winter_offers", "offers": offers})
                act = recv_until(ws, "action", check=lambda m: m.get("action") == "winter_offers")
                assert act["ok"]
            ws.send_json({"type": "winter_next"})
            act = recv_until(ws, "action", check=lambda m: m.get("action") == "winter_next")
            assert act["ok"]
        assert career.get("wb_done")
        ws.send_json({"type": "start_next_season"})
        act = recv_until(ws, "action", check=lambda m: m.get("action") == "start_next_season")
        assert act["ok"]
        assert career["phase"] == "round" and career["round"] == 1


# ---------------------------------------------------------------------------
# v0.4.1 — winter-break load fix, winter-break review, stale-render fix
# ---------------------------------------------------------------------------

def test_choose_team_clears_auto_generated_winter_offers():
    """Defect 1 (game level): a team that was AI when its round opened
    has bot offers in wb_offers; a human taking it over must start
    with a clean slate and choose their own offers."""
    career = game.build_new_career("Test", "Matthew")
    career["phase"] = "winter_break"
    career["wb_round"] = 1
    mclaren = career["teams"][0]
    mclaren["driver2_id"] = None  # empty seat, so bots offer for it
    game.start_winter_break_round(career, rng=random.Random(4))
    assert (career.get("wb_offers") or {}).get(mclaren["id"]), \
        "bot offers should exist while the team is AI"
    ok, msg = game.choose_team(career, mclaren["id"], "p-max")
    assert ok, msg
    assert (career.get("wb_offers") or {}).get(mclaren["id"]) is None
    # ...and the human is not blocked from submitting their own.
    pool = game.free_agents(career) + game.f2_drivers(career)
    ok, msg = game.submit_winter_offers(
        career, mclaren["id"],
        [{"driver_id": pool[0]["id"], "target": "seat", "seat": 2, "years": 2}])
    assert ok, msg


def test_load_season3_save_human_team_gets_no_auto_offers():
    """Defect 1 (server level): Continue a winter-break save, take
    over McLaren, start the career — McLaren must have NO submitted
    offers (only AI teams get auto-generated ones), and the AI teams
    DO have offers for round 1."""
    from fastapi.testclient import TestClient
    import server as srv

    path = Path(__file__).parent.parent / "saves" / "matthew-max-season3-complete.json"
    if not path.exists():
        pytest.skip("Season 3 save not present in this checkout")
    with tempfile.TemporaryDirectory() as tmp:
        old_dir = srv.SAVES_DIR
        srv.SAVES_DIR = Path(tmp)
        try:
            import shutil
            shutil.copy(path, Path(tmp) / "matthew-max-season3-complete.json")
            client = TestClient(srv.app)

            def recv_until(ws, want, limit=60, check=None):
                for _ in range(limit):
                    m = ws.receive_json()
                    if m.get("type") == want and (check is None or check(m)):
                        return m
                raise AssertionError(f"never got {want}")

            with client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "create_from_save",
                              "save_id": "matthew-max-season3-complete",
                              "nickname": "Max", "colour": "#e74c3c"})
                joined = recv_until(ws, "joined")
                code = joined["code"]
                room = srv.ROOMS[code]
                # At load, nobody has picked a team yet: no offers
                # exist for anyone (the round opens at start_career).
                assert (room["career"].get("wb_offers") or {}) == {}
                mclaren = next(t for t in room["career"]["teams"]
                               if t["name"] == "McLaren")
                ws.send_json({"type": "choose_team", "team_id": mclaren["id"]})
                recv_until(ws, "action",
                           check=lambda m: m.get("action") == "choose_team")
                ws.send_json({"type": "start_career"})
                recv_until(ws, "room_state",
                           check=lambda m: m["room"]["started"] is True)
                offers = room["career"].get("wb_offers") or {}
                assert mclaren["id"] not in offers, \
                    "human McLaren must have no auto-generated offers"
                ai_ids = [t["id"] for t in room["career"]["teams"] if t["is_ai"]]
                assert ai_ids and all(tid in offers for tid in ai_ids), \
                    "every AI team must have offers for round 1"
                # ...and the human can still submit their own offers.
                pool = game.free_agents(room["career"]) + game.f2_drivers(room["career"])
                ws.send_json({"type": "winter_offers", "offers": [
                    {"driver_id": pool[0]["id"], "target": "seat",
                     "seat": 2, "years": 2}]})
                act = recv_until(ws, "action",
                                 check=lambda m: m.get("action") == "winter_offers")
                assert act["ok"]
        finally:
            srv.SAVES_DIR = old_dir


def test_winter_break_review_payload_has_drivers_and_head_to_head():
    """Defect 2: the winter-break state payload includes the final
    driver standings (top 5) and the head-to-head of the player's
    team's two main drivers last season — from the snapshot, so it
    works for the migrated Season 3 save where Piastri has left."""
    path = Path(__file__).parent.parent / "saves" / "matthew-max-season3-complete.json"
    if not path.exists():
        pytest.skip("Season 3 save not present in this checkout")
    real = json.loads(path.read_text())
    migrated = migrate_save(real)
    mclaren = next(t for t in migrated["teams"] if t["name"] == "McLaren")
    state = game.public_state(migrated, True, viewer_team_id=mclaren["id"])
    review = state.get("wb_review")
    assert review is not None
    assert review["year"] == 2028
    assert review["drivers"][0]["name"] == "Oliver Bearman"
    assert review["drivers"][0]["points"] == 221
    assert review["drivers"][0]["wins"] == 5
    assert [d["name"] for d in review["drivers"][:3]] == [
        "Oliver Bearman", "Mick Schumacher", "Arvid Lindblad"]
    assert review["teams"][0]["name"] == "Ferrari"
    assert review["teams"][0]["points"] == 331
    # Head-to-head: McLaren's two main drivers last season — Lindblad
    # (still there) and Piastri (left at contract end, still in the
    # snapshot), with their real 2028 numbers.
    h2h = {d["name"]: d for d in review["head_to_head"]}
    assert set(h2h) == {"Arvid Lindblad", "Oscar Piastri"}
    assert h2h["Arvid Lindblad"]["points"] == 175
    assert h2h["Arvid Lindblad"]["wins"] == 3
    assert h2h["Oscar Piastri"]["points"] == 92
    # No reputation leaks into the review payload.
    assert not any("rep" in d for d in review["drivers"])


def test_imported_funds_match_save_file_exactly():
    """Funds check: importing the Season 3 save and playing winter
    break round 1 moves McLaren's bank by exactly the WANTED reward
    ($1.0M) — v0.6.6: the bot parent (Mercedes) marks Oscar Piastri
    WANTED for McLaren, its best customer, at the round open, and
    McLaren signs him in round 1, so the parent pays the reward.
    Nothing else moves the bank through load, takeover and round-1
    resolution."""
    path = Path(__file__).parent.parent / "saves" / "matthew-max-season3-complete.json"
    if not path.exists():
        pytest.skip("Season 3 save not present in this checkout")
    raw = json.loads(path.read_text())
    saved_money = next(t["money"] for t in raw["teams"] if t["name"] == "McLaren")
    assert saved_money == pytest.approx(150.19, abs=0.01)
    with tempfile.TemporaryDirectory() as tmp:
        import shutil
        shutil.copy(path, Path(tmp) / "matthew-max-season3-complete.json")
        loaded = load_save(tmp, "matthew-max-season3-complete")
        mclaren = next(t for t in loaded["teams"] if t["name"] == "McLaren")
        assert mclaren["money"] == pytest.approx(saved_money)
        # Simulate the server load flow (all teams AI, then start).
        for t in loaded["teams"]:
            t["is_ai"] = True
            t["owner_player_id"] = None
        ok, _ = game.choose_team(loaded, mclaren["id"], "p-max")
        assert ok
        game.start_winter_break_round(loaded, rng=random.Random(4))
        assert mclaren["money"] == pytest.approx(saved_money)
        # v0.6.6: Mercedes marks Piastri WANTED for McLaren ($1.0M).
        piastri = next(d for d in loaded["drivers"] if d["name"] == "Oscar Piastri")
        assert any(w["driver_id"] == piastri["id"] and w["reward"] == 1.0
                   for w in mclaren.get("wanted") or [])
        # The human has no auto-offers (v0.4.1), so they submit their
        # own for the empty seat, then hit NEXT to resolve round 1.
        ok, why = game.submit_winter_offers(
            loaded, mclaren["id"],
            [{"driver_id": piastri["id"], "target": "seat", "seat": 2, "years": 2}])
        assert ok, why
        ok, _adv, why = game.winter_next(loaded, mclaren["id"], rng=random.Random(4))
        assert ok, why
        assert loaded["wb_round"] == 2
        assert mclaren["money"] == pytest.approx(saved_money + 1.0)
        assert mclaren["wanted"] == []  # the mark is consumed


def test_reg_progress_bar_data_in_state_payload_when_open():
    """Reg progress bar (verify): once reg research is open (round 7+),
    the state payload carries the bar data — % fitted, % to adapt and
    the cost to adapt — for the viewer's team."""
    career = game.build_new_career("Test", "Matthew")
    ok, _ = game.choose_team(career, career["teams"][0]["id"], "p-max")
    assert ok
    team = career["teams"][0]
    # Locked before round 7: the payload says so, with no bar data
    # to show (open False).
    state = game.public_state(career, True, viewer_team_id=team["id"])
    assert state["reg_view"]["open"] is False
    career["round"] = 7
    game.init_reg_state(career)
    state = game.public_state(career, True, viewer_team_id=team["id"])
    rv = state["reg_view"]
    assert rv["open"] is True
    assert set(rv) == {"open", "adapted_pct", "unadapted_pct",
                       "adapt_cost", "research", "kind", "year"}
    assert rv["adapted_pct"] + rv["unadapted_pct"] == pytest.approx(100.0)
    assert rv["adapt_cost"] >= 0
    # And the raw perf numbers never ride along with it.
    assert "reg_total" not in state["teams"][0]
    assert "reg_unadapted" not in state["teams"][0]


def test_ws_winter_break_kick_hands_offers_to_bot():
    """Defect 1 (kick side): kicking a player in the lobby of a loaded
    winter-break career reverts their team to AI and the bot manager
    takes over its offers for the round — the team is not left
    offer-less when the career starts."""
    from fastapi.testclient import TestClient
    import server as srv

    path = Path(__file__).parent.parent / "saves" / "matthew-max-season3-complete.json"
    if not path.exists():
        pytest.skip("Season 3 save not present in this checkout")
    with tempfile.TemporaryDirectory() as tmp:
        old_dir = srv.SAVES_DIR
        srv.SAVES_DIR = Path(tmp)
        try:
            import shutil
            shutil.copy(path, Path(tmp) / "matthew-max-season3-complete.json")
            client = TestClient(srv.app)

            def recv_until(ws, want, limit=60, check=None):
                for _ in range(limit):
                    m = ws.receive_json()
                    if m.get("type") == want and (check is None or check(m)):
                        return m
                raise AssertionError(f"never got {want}")

            with client.websocket_connect("/ws") as host_ws:
                host_ws.send_json({"type": "create_from_save",
                                   "save_id": "matthew-max-season3-complete",
                                   "nickname": "Max", "colour": "#e74c3c"})
                joined = recv_until(host_ws, "joined")
                code = joined["code"]
                room = srv.ROOMS[code]
                mclaren = next(t for t in room["career"]["teams"]
                               if t["name"] == "McLaren")
                ferrari = next(t for t in room["career"]["teams"]
                               if t["name"] == "Ferrari")
                host_ws.send_json({"type": "choose_team", "team_id": mclaren["id"]})
                recv_until(host_ws, "action",
                           check=lambda m: m.get("action") == "choose_team")
                with client.websocket_connect("/ws") as guest_ws:
                    guest_ws.send_json({"type": "join_room", "code": code,
                                        "nickname": "Matthew", "colour": "#d52f38"})
                    g_joined = recv_until(guest_ws, "joined")
                    guest_id = g_joined["player_id"]
                    guest_ws.send_json({"type": "choose_team", "team_id": ferrari["id"]})
                    recv_until(guest_ws, "action",
                               check=lambda m: m.get("action") == "choose_team")
                    # Kick in the lobby (kicks are lobby-only): Ferrari
                    # reverts to AI and the bot manager makes its
                    # offers for the round.
                    host_ws.send_json({"type": "kick_player", "player_id": guest_id})
                    recv_until(guest_ws, "kicked")
                    assert ferrari["is_ai"] is True
                    assert ferrari["owner_player_id"] is None
                    offers = room["career"].get("wb_offers") or {}
                    assert ferrari["id"] in offers, \
                        "kicked team's offers revert to the bot manager"
                    assert mclaren["id"] not in offers
                # Starting the career opens the round for the AI teams;
                # the human team still has no auto-generated offers.
                host_ws.send_json({"type": "start_career"})
                recv_until(host_ws, "room_state",
                           check=lambda m: m["room"]["started"] is True)
                offers = room["career"].get("wb_offers") or {}
                assert mclaren["id"] not in offers
                ai_ids = [t["id"] for t in room["career"]["teams"] if t["is_ai"]]
                assert ai_ids and all(tid in offers for tid in ai_ids)
        finally:
            srv.SAVES_DIR = old_dir


# ---------------------------------------------------------------------------
# v0.4.2 — import-flow fix, T&R at the 2026 start, Driver Academy,
# exponential fines, table-driven rating growth
# ---------------------------------------------------------------------------

def test_tr_fine_calculation():
    """The exponential T&R/academy fine: the j-th extra driver beyond
    the free count costs $0.5M x 2^(j-1) per race — so 3 T&R = $0.5M,
    4 T&R = $1.5M, 5 T&R = $3.5M per race."""
    from f1.rules import extra_driver_fine
    assert extra_driver_fine(0, 2) == 0.0
    assert extra_driver_fine(2, 2) == 0.0
    assert extra_driver_fine(3, 2) == pytest.approx(0.5)
    assert extra_driver_fine(4, 2) == pytest.approx(1.5)
    assert extra_driver_fine(5, 2) == pytest.approx(3.5)
    assert extra_driver_fine(6, 2) == pytest.approx(7.5)
    # The academy fine uses the same formula after 3 drivers.
    assert extra_driver_fine(3, 3) == 0.0
    assert extra_driver_fine(4, 3) == pytest.approx(0.5)
    assert extra_driver_fine(5, 3) == pytest.approx(1.5)


def test_new_career_tr_and_academy_assignments():
    """Smoke check: a fresh 2026 career starts with the CSV T&R and
    academy assignments — McLaren 4 T&R, Ferrari 2 academy drivers
    including Camara; CSV overrides (Westcott salary, Pourchaire age,
    Kirakawa perf) are applied at build time."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = next(t for t in career["teams"] if t["name"] == "McLaren")
    ferrari = next(t for t in career["teams"] if t["name"] == "Ferrari")
    tr_names = {d["name"] for d in game.team_tr_drivers(career, mclaren)}
    assert tr_names == {"Nyck de Vries", "Pato O'Ward",
                        "Leonardo Fornaroli", "Richard Verschoor"}
    assert len(game.team_tr_drivers(career, mclaren)) == 4
    assert game.tr_fine_per_race(career, mclaren) == pytest.approx(1.5)
    mercedes = next(t for t in career["teams"] if t["name"] == "Mercedes")
    assert len(game.team_tr_drivers(career, mercedes)) == 4
    aston = next(t for t in career["teams"] if t["name"] == "Aston Martin")
    assert len(game.team_tr_drivers(career, aston)) == 3
    assert game.tr_fine_per_race(career, aston) == pytest.approx(0.5)
    ac_names = {d["name"] for d in game.team_academy_drivers(career, ferrari)}
    assert ac_names == {"Dino Beganovic", "Rafael Câmara"}
    camara = next(d for d in career["drivers"] if d["name"] == "Rafael Câmara")
    assert camara["age"] == 20 and camara["perf"] == 83
    assert camara["academy_team_id"] == ferrari["id"]
    # CSV overrides applied at build time (data.py is untouched).
    westcott = next(d for d in career["drivers"] if d["name"] == "Payton Westcott")
    assert westcott["salary"] == pytest.approx(1.4)
    pourchaire = next(d for d in career["drivers"] if d["name"] == "Théo Pourchaire")
    assert pourchaire["age"] == 25
    kirakawa = next(d for d in career["drivers"] if d["name"] == "Ryō Kirakawa")
    assert kirakawa["perf"] == 84
    # Racing Bulls starts with neither.
    rb = next(t for t in career["teams"] if t["name"] == "Racing Bulls")
    assert game.team_tr_drivers(career, rb) == []
    assert game.team_academy_drivers(career, rb) == []


def test_academy_cost_third_of_salary():
    """An academy driver costs the team 1/3 of their salary per
    season (salary/36 after each race), shown in the finances view."""
    career = game.build_new_career("Test", "Matthew")
    ferrari = next(t for t in career["teams"] if t["name"] == "Ferrari")
    ac = game.team_academy_drivers(career, ferrari)
    assert len(ac) == 2
    # Camara $1.0M/yr and Beganovic $1.5M/yr: 1/3 each, per race /12.
    # (The per-race figure is rounded to 4dp, so compare loosely.)
    expected = sum(d["salary"] / 36 for d in ac)
    assert game.academy_cost_per_race(career, ferrari) == pytest.approx(expected, abs=1e-3)
    assert game.academy_cost_per_race(career, ferrari) == pytest.approx(2.5 / 36, abs=1e-3)
    fin = game.finances_view(career, ferrari)
    assert fin["academy_cost_per_race"] == pytest.approx(2.5 / 36, abs=1e-3)
    assert len(fin["academy_costs"]) == 2
    assert fin["extra_charges_per_race"] == pytest.approx(
        fin["tr_fine_per_race"] + fin["academy_cost_per_race"]
        + fin["academy_fine_per_race"])
    # Ferrari has 2 T&R drivers (no fine) and 2 academy drivers (no
    # fine either): the extra charges are just the academy stipends.
    assert fin["tr_fine_per_race"] == 0.0
    assert fin["academy_fine_per_race"] == 0.0


def test_academy_loyalty_boost_winter_break():
    """A 5th-place academy team beats a 2nd-place rival's offer for
    its own junior (effective 2nd, academy-first tie-break); a driver
    with no academy tie goes to the best-placed team that offered."""
    career = game.build_new_career("Test", "Matthew")
    ferrari = next(t for t in career["teams"] if t["name"] == "Ferrari")
    mercedes = next(t for t in career["teams"] if t["name"] == "Mercedes")
    aston = next(t for t in career["teams"] if t["name"] == "Aston Martin")
    rb = next(t for t in career["teams"] if t["name"] == "Racing Bulls")
    mclaren = next(t for t in career["teams"] if t["name"] == "McLaren")
    # Constructors' order for the winter break: Ferrari 1st,
    # Mercedes 2nd, Red Bull 3rd, McLaren 4th, Aston Martin 5th.
    career["last_season"] = {"teams": [
        {"id": ferrari["id"]}, {"id": mercedes["id"]},
        {"id": next(t for t in career["teams"] if t["name"] == "Red Bull")["id"]},
        {"id": mclaren["id"]}, {"id": aston["id"]},
        {"id": rb["id"]},
    ]}
    boya = next(d for d in career["drivers"] if d["name"] == "Mari Boya")
    assert boya["academy_team_id"] == aston["id"]
    # Both teams offer Boya a main seat (both seats are emptied).
    aston["driver1_id"] = None
    mercedes["driver1_id"] = None
    aston["money"] = mercedes["money"] = 100.0
    career["phase"] = "winter_break"
    career["wb_offers"] = {
        aston["id"]: [{"driver_id": boya["id"], "target": "seat", "seat": 1, "years": 2}],
        mercedes["id"]: [{"driver_id": boya["id"], "target": "seat", "seat": 1, "years": 2}],
    }
    game.resolve_winter_break_round(career, rng=random.Random(1))
    assert boya["status"] == "f1" and boya["team_id"] == aston["id"]
    assert aston["driver1_id"] == boya["id"]
    assert mercedes["driver1_id"] is None
    # The counter-case: a driver with no academy tie goes to the
    # best-placed team that offered (Mercedes, 2nd, beats Aston, 5th).
    free = next(d for d in career["drivers"]
                if d["status"] == "free" and not d.get("academy_team_id"))
    career["wb_offers"] = {
        aston["id"]: [{"driver_id": free["id"], "target": "seat", "seat": 1, "years": 2}],
        mercedes["id"]: [{"driver_id": free["id"], "target": "seat", "seat": 1, "years": 2}],
    }
    game.resolve_winter_break_round(career, rng=random.Random(1))
    assert free["team_id"] == mercedes["id"]


def test_academy_leaving_on_signing():
    """An academy driver leaves the academy when they sign a main or
    T&R contract with ANY team; a team can also release them (free)."""
    career = game.build_new_career("Test", "Matthew")
    ferrari = next(t for t in career["teams"] if t["name"] == "Ferrari")
    mercedes = next(t for t in career["teams"] if t["name"] == "Mercedes")
    camara = next(d for d in career["drivers"] if d["name"] == "Rafael Câmara")
    assert camara["academy_team_id"] == ferrari["id"]
    # Main signing with another team frees the academy slot.
    # (v0.7.3: an F2 driver can only take a main seat in the winter
    # break, so the winter-break signing API is used here.)
    career["phase"] = "winter_break"
    mercedes["driver1_id"] = None
    mercedes["money"] = 100.0
    ok, msg = game.sign_driver_wb(career, mercedes, camara, 1, 2)
    assert ok, msg
    assert camara["status"] == "f1" and camara["academy_team_id"] is None
    assert game.team_academy_drivers(career, ferrari) == [
        d for d in game.team_academy_drivers(career, ferrari)
        if d["name"] == "Dino Beganovic"]
    # T&R signing frees it too (T&R is allowed mid-season).
    career["phase"] = "round"
    dunne = next(d for d in career["drivers"] if d["name"] == "Alex Dunne")
    alpine = next(t for t in career["teams"] if t["name"] == "Alpine")
    assert dunne["academy_team_id"] == alpine["id"]
    # v0.8.0: Dunne is Alpine's academy junior, so signing him as
    # T&R is a poach — a secret meeting comes first.
    ferrari["money"] = 100.0
    ok, msg = game.request_meeting(
        career, ferrari["id"], dunne["id"], ("tr", None),
        rng=_SeqRng([0.0, 0.99]))
    assert ok and "agreed" in msg, msg
    ok, msg = game.sign_tr_driver(career, ferrari["id"], dunne["id"], contract_years=1)
    assert ok, msg
    assert dunne["status"] == "tr" and dunne["academy_team_id"] is None
    # Manual release is free and leaves the driver a free agent / F2.
    beganovic = next(d for d in career["drivers"] if d["name"] == "Dino Beganovic")
    ok, msg = game.release_academy_driver(career, ferrari["id"], beganovic["id"])
    assert ok, msg
    assert beganovic["academy_team_id"] is None
    assert beganovic["status"] == "f2"
    assert game.team_academy_drivers(career, ferrari) == []


def test_rating_growth_ranges():
    """The table-driven growth ranges: low = MIN delta, high =
    max delta for the age step (see f1/rules.py RATING_TABLE).
    v0.4.3: the low end is the min-column delta (Matthew's choice)
    — 26->27 is [0,1] now (it was [0,0] under the v0.4.2 average
    delta); 20->21 stays [2,3]."""
    from f1.rules import rating_growth_range
    assert rating_growth_range(20) == (2, 3)   # Camara at 20 grows 2-3
    assert rating_growth_range(25) == (0, 1)
    assert rating_growth_range(26) == (0, 1)
    assert rating_growth_range(30) == (-2, -1)  # decline, never flat
    assert rating_growth_range(18) == (2, 5)
    assert rating_growth_range(40) == (-1, 0)


def test_antonelli_clamp():
    """Antonelli is 19 with rating 87 — above the age-19 max of 85.
    The fix is in the logic only: he is NEVER nerfed to 85, and at
    20 (max 88) he can grow at most +1."""
    from f1.rules import grow_rating
    rng = random.Random(7)
    for _ in range(500):
        assert 87 <= grow_rating(19, 87, False, rng=rng) <= 88
    rng = random.Random(7)
    for _ in range(500):
        assert 87 <= grow_rating(19, 87, True, rng=rng) <= 88
    # A 19-year-old at the table max (85) grows to 87-88, not beyond.
    rng = random.Random(7)
    for _ in range(200):
        assert 87 <= grow_rating(19, 85, False, rng=rng) <= 88


def test_decline_clamps_to_min():
    """Decline years clamp to the new age's minimum — a driver never
    falls below the table floor for their new age."""
    from f1.rules import RATING_TABLE, grow_rating
    for age in (31, 35, 40, 45):
        lo, hi = RATING_TABLE[age + 1][0], RATING_TABLE[age + 1][2]
        rng = random.Random(age)
        for _ in range(200):
            r = grow_rating(age, RATING_TABLE[age][0], False, rng=rng)
            assert lo <= r <= hi, (age, r, lo, hi)


def test_academy_growth_weighting():
    """Academy drivers get the high end of their growth range 70% of
    the time (vs uniform for everyone else) — so over many draws
    their mean growth is clearly higher, including in decline years
    (where the high end is the smaller decline)."""
    from f1.rules import grow_rating
    rng = random.Random(3)
    n = 2000
    plain = [grow_rating(20, 80, False, rng=rng) - 80 for _ in range(n)]
    academy = [grow_rating(20, 80, True, rng=rng) - 80 for _ in range(n)]
    assert 2.4 < sum(plain) / n < 2.6          # uniform over [2, 3]
    assert sum(academy) / n > 2.6              # 70% chance of 3
    assert sum(academy) / n > sum(plain) / n
    # Decline years: academy drivers decline less on average.
    rng = random.Random(4)
    plain_d = [grow_rating(30, 80, False, rng=rng) - 80 for _ in range(n)]
    academy_d = [grow_rating(30, 80, True, rng=rng) - 80 for _ in range(n)]
    assert -1.6 < sum(plain_d) / n < -1.4      # uniform over [-2, -1]
    assert sum(academy_d) / n > -1.4           # 70% chance of -1
    assert sum(academy_d) / n > sum(plain_d) / n


def test_end_of_season_growth_applies_to_all_drivers():
    """End of season: every driver ages +1 and grows by the table
    range — F1, F2, free agents, T&R and academy alike."""
    career = game.build_new_career("Test", "Matthew")
    camara = next(d for d in career["drivers"] if d["name"] == "Rafael Câmara")
    antonelli = next(d for d in career["drivers"] if d["name"] == "Kimi Antonelli")
    assert camara["age"] == 20 and camara["perf"] == 83
    assert antonelli["age"] == 19 and antonelli["perf"] == 87
    game.end_of_season(career, rng=random.Random(11))
    assert camara["age"] == 21
    assert 85 <= camara["perf"] <= 86  # 83 + [2, 3]
    assert antonelli["age"] == 20
    assert 87 <= antonelli["perf"] <= 88  # never nerfed, at most +1
    # A free agent aged too.
    free = next(d for d in career["drivers"] if d["status"] == "free")
    assert free["age"] >= 18


def test_academy_fine_after_three():
    """No hard cap on academy size — the 4th academy driver costs
    $0.5M/race, the 5th $1.5M/race (same exponential formula)."""
    career = game.build_new_career("Test", "Matthew")
    rb = next(t for t in career["teams"] if t["name"] == "Racing Bulls")
    assert game.academy_fine_per_race(career, rb) == 0.0
    young = [d for d in career["drivers"]
             if d["status"] in ("free", "f2") and d["age"] <= 25
             and not d.get("academy_team_id")][:5]
    for d in young[:3]:
        ok, msg = game.add_academy_driver(career, rb["id"], d["id"])
        assert ok, msg
    assert game.academy_fine_per_race(career, rb) == 0.0
    ok, msg = game.add_academy_driver(career, rb["id"], young[3]["id"])
    assert ok, msg
    assert game.academy_fine_per_race(career, rb) == pytest.approx(0.5)
    ok, msg = game.add_academy_driver(career, rb["id"], young[4]["id"])
    assert ok, msg
    assert game.academy_fine_per_race(career, rb) == pytest.approx(1.5)


def test_bot_academy_signing_limits():
    """Bots may add at most one academy driver per season, and never
    go over 3 academy drivers (they avoid the fine entirely).
    v0.8.1: only ~40% of bot teams plan an academy signing in a
    given season (see _ai_season_plan), so the test uses a team
    whose plan says yes."""
    career = game.build_new_career("Test", "Matthew")
    team = next(t for t in career["teams"]
                if game._ai_season_plan(career, t, "academy")
                and len(game.team_academy_drivers(career, t)) < 3)
    team["money"] = 100.0
    career["round"] = 3
    ac0 = len(game.team_academy_drivers(career, team))
    game.ai_act(career, team, rng=random.Random(1))
    assert len(game.team_academy_drivers(career, team)) == ac0 + 1
    assert team["ai_academy_signed"] is True
    game.ai_act(career, team, rng=random.Random(2))
    assert len(game.team_academy_drivers(career, team)) == ac0 + 1
    # A team already at 3 academy drivers gets no 4th from the bot —
    # top the team up directly, clear the once-a-season flag so the
    # 3-driver cap itself is what blocks it.
    young = [d for d in career["drivers"]
             if d["status"] in ("free", "f2") and d["age"] <= 25
             and not d.get("academy_team_id")]
    for d in young:
        if len(game.team_academy_drivers(career, team)) >= 3:
            break
        ok, _ = game.add_academy_driver(career, team["id"], d["id"])
        assert ok
    assert len(game.team_academy_drivers(career, team)) == 3
    team["ai_academy_signed"] = False
    game.ai_act(career, team, rng=random.Random(1))
    assert len(game.team_academy_drivers(career, team)) == 3


def test_import_flow_save_appears_immediately():
    """v0.4.2 import fix: after POST /api/import, GET /api/saves
    lists the save straight away (the v0.4.1 bug left the lobby
    showing 'No saves yet'), and the home page offers a 'Create
    room with this save' next step."""
    from fastapi.testclient import TestClient
    import server as srv

    with tempfile.TemporaryDirectory() as tmp:
        old_dir = srv.SAVES_DIR
        srv.SAVES_DIR = Path(tmp)
        try:
            career = two_human_career()
            doc = dict(career)
            doc["version"] = 7
            payload = json.dumps(doc).encode("utf-8")
            client = TestClient(srv.app)
            assert client.get("/api/saves").json()["saves"] == []
            resp = client.post("/api/import",
                               files={"file": ("career.json", payload,
                                               "application/json")})
            assert resp.status_code == 200, resp.text
            saves = client.get("/api/saves").json()["saves"]
            assert len(saves) == 1
            assert saves[0]["save_id"] == resp.json()["save"]["save_id"]
            # The home page carries the import input and the
            # create-room-with-this-save next step.
            html = client.get("/").text
            assert 'id="import-file-home"' in html
            assert "Create room with this save" in html
        finally:
            srv.SAVES_DIR = old_dir


# ---------------------------------------------------------------------------
# v0.4.3
# ---------------------------------------------------------------------------

def test_release_tr_driver_costs_main_driver_break_fee():
    """v0.4.3: releasing a T&R driver costs EXACTLY what breaking a
    main driver's contract costs — rounds_remaining x salary/12 +
    $3.0M — shown upfront and charged on confirm. The driver becomes
    a free agent, keeps championship points, and the points they
    scored for the team stay with the team."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = next(t for t in career["teams"] if t["name"] == "McLaren")
    trs = game.team_tr_drivers(career, mclaren)
    assert len(trs) == 4
    d = next(x for x in trs if x["name"] == "Nyck de Vries")
    d["points"] = 10  # a T&R driver can hold points from an earlier stint
    fee = game.break_fee_for_driver(career, d)
    # Round 1, 2-year contract: (12 + 12) x 1.5/12 + 3.0 = $6.0M.
    assert fee == pytest.approx(6.0)
    money_before = mclaren["money"]
    ok, msg = game.release_driver(career, mclaren["id"], d["id"])
    assert ok, msg
    assert mclaren["money"] == pytest.approx(money_before - fee)
    assert d["status"] == "free" and d["team_id"] is None
    assert d["points"] == 10  # championship points follow the driver
    assert d not in game.team_tr_drivers(career, mclaren)
    assert any("released by McLaren" in t and "Nyck de Vries" in t
               for t in _ttexts(career))


def test_release_main_driver_empties_seat_and_keeps_points():
    """v0.4.3: releasing a main driver empties the seat, banks the
    points they scored FOR the team, and keeps their championship
    points with them — the same real-F1 rules as a replacement."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = next(t for t in career["teams"] if t["name"] == "McLaren")
    norris = game.get_driver(career, mclaren["driver1_id"])
    assert norris["name"] == "Lando Norris"
    norris["points"] = 25
    norris["team_points"] = 25
    mclaren["points"] = 25
    mclaren["money"] = 100.0  # Norris's fee ($13.4M) needs a full bank
    fee = game.break_fee_for_driver(career, norris)
    assert fee == pytest.approx(13.4)
    ok, msg = game.release_driver(career, mclaren["id"], norris["id"])
    assert ok, msg
    assert mclaren["driver1_id"] is None
    assert norris["status"] == "free" and norris["team_id"] is None
    assert norris["points"] == 25
    assert mclaren["points_kept"] == 25  # banked with the team
    assert mclaren["points"] == 25
    assert mclaren["money"] == pytest.approx(100.0 - fee)


def test_release_driver_blocked_when_unaffordable_but_allowed_in_winter_break():
    """v0.4.3: a release the team cannot afford does nothing. v0.6.5:
    releases are now ALLOWED in the winter break (Matthew: teams
    could not drop a driver there at all) — the fee uses the
    winter-break rounds_remaining (contract_left x 12 rounds, the
    full remaining contract), shown upfront and charged on confirm."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = next(t for t in career["teams"] if t["name"] == "McLaren")
    d = game.team_tr_drivers(career, mclaren)[0]
    mclaren["money"] = 0.5
    ok, msg = game.release_driver(career, mclaren["id"], d["id"])
    assert not ok and "Need $" in msg
    assert d["status"] == "tr" and mclaren["money"] == 0.5
    # Winter break: allowed when affordable. Nyck de Vries, 2-year
    # contract, $1.5M/yr: 24 x 1.5/12 + 3.0 = $6.0M.
    mclaren["money"] = 100.0
    career["phase"] = "winter_break"
    assert d["name"] == "Nyck de Vries"
    fee = game.break_fee_for_driver(career, d)
    assert fee == pytest.approx(6.0)
    ok, msg = game.release_driver(career, mclaren["id"], d["id"])
    assert ok, msg
    assert d["status"] == "free" and d["team_id"] is None
    assert mclaren["money"] == pytest.approx(100.0 - fee)
    assert d not in game.team_tr_drivers(career, mclaren)


def test_my_team_payload_has_break_fees():
    """v0.4.3: the private my_team payload carries each main and T&R
    driver's break fee (same formula as a replacement), so the
    Drivers tab can show the Drop cost upfront. Rep is still never
    sent to players."""
    import server as srv
    room = srv.new_room("Matthew", "#e74c3c")
    host = srv.add_player(room, "Matthew", "#e74c3c")
    room["career"] = game.build_new_career("T", "Matthew")
    mclaren = next(t for t in room["career"]["teams"] if t["name"] == "McLaren")
    game.choose_team(room["career"], mclaren["id"], host["player_id"])
    mt = srv.my_team_public(room, host["player_id"])
    assert mt is not None
    for d in mt["drivers"] + mt["tr_drivers"]:
        assert "break_fee" in d
        live = game.get_driver(room["career"], d["id"])
        assert d["break_fee"] == game.break_fee_for_driver(room["career"], live)
        assert "rep" not in d
    # McLaren's four T&R drivers are all in the payload.
    assert len(mt["tr_drivers"]) == 4


def test_bot_releases_bad_tr_driver():
    """v0.4.3 bot parity: a T&R driver who raced earlier in the
    season (swapped out of a main seat) and meets the same
    bad-performance rule — 0 points, average finish 18th or worse —
    is released outright, paying the same break fee. Only when no
    main driver needs acting on (one action per team per race)."""
    career = game.build_new_career("Test", "Matthew")
    team = next(t for t in career["teams"] if t["name"] == "Racing Bulls")
    team["money"] = 100.0
    career["round"] = 8
    # Give the team a T&R driver with bad race stats from an
    # earlier main-seat stint (points 0, avg finish 19th).
    tr = next(d for d in game.free_agents(career) if d["age"] <= 25)
    ok, msg = game.sign_tr_driver(career, team["id"], tr["id"], contract_years=1)
    assert ok, msg
    tr["points"] = 0
    tr["finish_sum"] = 19 * 6
    tr["finish_count"] = 6
    fee = game.break_fee_for_driver(career, tr)
    money_before = team["money"]
    game.bot_post_race(career, rng=random.Random(1))
    assert tr["status"] == "free" and tr["team_id"] is None
    assert team["money"] == pytest.approx(money_before - fee)
    assert any("released by Racing Bulls" in t and tr["name"] in t
               for t in _ttexts(career))
    # Main drivers untouched (they had no race stats).
    assert team["driver1_id"] and team["driver2_id"]


def test_drivers_tab_and_history_grid_playwright():
    """v0.4.3 in a real browser: the Drivers tab shows the whole
    roster (main 2, T&R, academy) with team-coloured T&R surnames,
    a T&R Drop goes through the confirm dialog at the exact break
    fee, and the race-history grid fits its container (no horizontal
    scroll) after a round is played."""
    pytest.importorskip("playwright")
    import subprocess
    import time
    import urllib.request
    port = 8805
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(f"http://127.0.0.1:{port}/")
            page.fill("#home-nick", "Max")
            page.click("#btn-create")
            page.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
            page.click("#btn-new-career")
            page.wait_for_selector("[data-team]", timeout=15000)
            page.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.textContent.includes('McLaren'))
                    .querySelector('[data-team]').click();
            }""")
            page.wait_for_selector("#btn-start-career:not(.hidden)", timeout=15000)
            page.click("#btn-start-career")
            page.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            # Drivers tab: all three roster groups, team-coloured
            # T&R surnames, and a Drop button with the break fee.
            page.click("button[data-tab='drivers']")
            page.wait_for_selector("#drivers-box .bar-row", timeout=15000)
            drivers_text = page.inner_text("#drivers-box")
            assert "Main drivers" in drivers_text
            assert "Test & Reserve" in drivers_text
            assert "Driver Academy" in drivers_text
            assert "NORRIS" in drivers_text and "PIASTRI" in drivers_text
            assert "VRIES" in drivers_text and "VERSCHOOR" in drivers_text
            # McLaren starts with no academy drivers: the empty state
            # shows (the section is not hidden, unlike the old My Team
            # card, which only rendered the academy line when non-empty).
            assert "No academy drivers yet" in drivers_text
            # A T&R surname carries the McLaren team colour (#EF9B33).
            tr_colour = page.evaluate("""() => {
                const rows = [...document.querySelectorAll('#drivers-box .bar-row')];
                const row = rows.find(r => r.textContent.includes('VRIES'));
                const span = row && row.querySelector('.surname');
                return span ? span.style.color : null;
            }""")
            assert tr_colour in ("rgb(239, 155, 51)", "#EF9B33", "#ef9b33"), tr_colour
            # Drop a T&R driver (de Vries, $5.00M break fee at round 1
            # on a 2-year contract) through the confirm dialog.
            page.evaluate("""() => {
                const rows = [...document.querySelectorAll('#drivers-box .bar-row')];
                const row = rows.find(r => r.textContent.includes('VRIES'));
                row.querySelector('[data-drop-driver]').click();
            }""")
            page.wait_for_selector("[data-confirm-yes]", timeout=15000)
            confirm_text = page.inner_text(".ov-panel[data-overlay='confirm']")
            assert "Breaking their contract costs $6.00M" in confirm_text
            page.click("[data-confirm-yes]")
            page.wait_for_function(
                "() => !document.querySelector('#drivers-box').innerText.includes('VRIES')",
                timeout=15000)
            drivers_text = page.inner_text("#drivers-box")
            assert "VRIES" not in drivers_text
            assert "VERSCHOOR" in drivers_text  # the other T&R drivers stay
            # History grid: play one round, then the grid (with a
            # filled R1 cell) must fit its container — no horizontal
            # scroll, and the last column (R12) is inside it.
            _play_one_round(page)
            page.click("button[data-tab='history']")
            page.wait_for_selector("#history-box .hist-grid", timeout=15000)
            fits = page.evaluate("""() => {
                const sc = document.querySelector('#tab-history .table-scroll');
                const grid = document.querySelector('.hist-grid');
                const r12 = document.querySelector('.hist-grid th:last-child');
                return {
                    scroll: sc.scrollWidth, client: sc.clientWidth,
                    gridRight: grid.getBoundingClientRect().right,
                    tabRight: document.querySelector('#tab-history').getBoundingClientRect().right,
                    r12Right: r12.getBoundingClientRect().right,
                    scRight: sc.getBoundingClientRect().right,
                };
            }""")
            assert fits["scroll"] <= fits["client"] + 1, fits
            assert fits["gridRight"] <= fits["tabRight"] + 1, fits
            assert fits["r12Right"] <= fits["scRight"] + 1, fits
            browser.close()
    finally:
        proc.terminate()

# ---------------------------------------------------------------------------
# v0.5 — dashboard shows main 2 only, driver development contribution,
# F2 standings coloured by academy team
# ---------------------------------------------------------------------------

def test_dev_contrib_orderings():
    """v0.5: the development contribution orders drivers the way
    Matthew stated — Hamilton > Leclerc, Norris > Piastri,
    Gasly > Aron — from the real start-of-2026 career totals."""
    career = game.build_new_career("Test", "Matthew")
    by_name = {d["name"]: d for d in career["drivers"]}
    ham = game.driver_dev_contrib(by_name["Lewis Hamilton"])
    lec = game.driver_dev_contrib(by_name["Charles Leclerc"])
    nor = game.driver_dev_contrib(by_name["Lando Norris"])
    pia = game.driver_dev_contrib(by_name["Oscar Piastri"])
    gas = game.driver_dev_contrib(by_name["Pierre Gasly"])
    aro = game.driver_dev_contrib(by_name["Paul Aron"])
    assert ham > lec, (ham, lec)
    assert nor > pia, (nor, pia)
    assert gas > aro, (gas, aro)
    # The range is [0.02, 0.08]: a driver with no starts, no wins
    # and the lowest rating contributes exactly 0.02; even the most
    # decorated driver stays under 0.08.
    assert game.driver_dev_contrib(
        {"gp_starts": 0, "gp_wins": 0, "perf": 70}) == pytest.approx(0.02)
    assert game.driver_dev_contrib(
        {"gp_starts": 500, "gp_wins": 105, "perf": 100}) < 0.08
    assert 0.02 <= aro < 0.08
    assert 0.02 <= ham < 0.08


def test_do_develop_applies_driver_factor():
    """v0.5: Develop multiplies the perf gain by the team's driver
    factor (1 + main 2 + T&R contributions); the reliability gain
    is unchanged; a weak lineup gains less than a strong one."""
    from f1.rules import add_perf_gain, add_rel_gain
    career = two_human_career()
    team = career["teams"][0]  # McLaren (2025 champions open first)
    team["money"] = 100.0
    team["perf"] = 180.0
    team["rel"] = 0.70
    team["perf_res"] = 30.0
    team["reli_res"] = 20.0
    factor = game.team_dev_factor(career, team)
    assert factor > 1.0
    ok, msg = game.do_develop(career, team["id"])
    assert ok, msg
    assert team["perf"] - 180.0 == pytest.approx(
        add_perf_gain(180.0, 30.0) * factor)
    assert team["rel"] - 0.70 == pytest.approx(add_rel_gain(20.0))
    assert f"×{factor:.2f}" in msg
    # A weak lineup (rookies: no starts, no wins, rating 70) gains less.
    for d in game.team_drivers(career, team) + game.team_tr_drivers(career, team):
        d["gp_starts"] = 0
        d["gp_wins"] = 0
        d["perf"] = 70
    weak_factor = game.team_dev_factor(career, team)
    assert weak_factor < factor
    team["money"] = 100.0
    team["perf"] = 180.0
    team["rel"] = 0.70
    team["perf_res"] = 30.0
    team["reli_res"] = 20.0
    ok, _ = game.do_develop(career, team["id"])
    assert ok
    assert team["perf"] - 180.0 == pytest.approx(
        add_perf_gain(180.0, 30.0) * weak_factor)


def test_gp_starts_wins_increment_and_survive():
    """v0.5: every F1 starter's career starts +1 per race, the
    winner's career wins +1; the totals survive a team change and a
    season rollover (they are career totals, never reset — while
    points and the season tally f1_races do reset)."""
    career = two_human_career()
    before = {d["id"]: (d["gp_starts"], d["gp_wins"])
              for d in career["drivers"]}
    play_full_round(career, seeded(6))
    # Every F1 driver started once; nobody else did.
    for d in career["drivers"]:
        s0, _ = before[d["id"]]
        if d["status"] == "f1":
            assert d["gp_starts"] == s0 + 1, d["name"]
        else:
            assert d["gp_starts"] == s0, d["name"]
    winner = next(d for d in career["drivers"]
                  if d["status"] == "f1" and d.get("race_pos") == 1)
    assert winner["gp_wins"] == before[winner["id"]][1] + 1
    # Team change: sign a free agent with career totals into a seat —
    # the totals follow the driver unchanged, and the outgoing
    # driver's totals are unchanged too.
    ric = next(d for d in career["drivers"]
               if d["name"] == "Daniel Ricciardo")
    assert (ric["gp_starts"], ric["gp_wins"]) == (257, 8)
    cadillac = next(t for t in career["teams"] if t["name"] == "Cadillac")
    cadillac["money"] = 100.0
    outgoing = game.team_drivers(career, cadillac)[0]
    out_before = (outgoing["gp_starts"], outgoing["gp_wins"])
    ok, msg = game.sign_driver(career, cadillac["id"], ric["id"], 1)
    assert ok, msg
    assert (ric["gp_starts"], ric["gp_wins"]) == (257, 8)
    assert (outgoing["gp_starts"], outgoing["gp_wins"]) == out_before
    # Season rollover: play the remaining 11 rounds. Verstappen
    # (Red Bull, an AI team) scores points every round, so no bot
    # swap touches him — he races all 12 rounds.
    for _ in range(11):
        play_full_round(career, seeded(6))
    assert career["phase"] == "winter_break"
    ver = next(d for d in career["drivers"]
               if d["name"] == "Max Verstappen")
    assert ver["gp_starts"] == 210 + 12, ver["gp_starts"]
    assert ver["gp_wins"] >= 65
    # The rollover DID run (season tallies reset) — and the career
    # totals survived it.
    for d in career["drivers"]:
        if d["status"] == "f1":
            assert d["points"] == 0
            assert d["f1_races"] == 0
        assert d["gp_starts"] >= before[d["id"]][0]
        assert d["gp_wins"] >= before[d["id"]][1]


def test_migrate_backfills_gp_totals():
    """v0.5: a v7 save has no gp_starts / gp_wins — migration
    backfills them from the start-of-2026 table by name; a driver
    not in the table (a junior, or a driver with no F1 starts)
    gets (0, 0)."""
    career = game.build_new_career("Test", "Matthew")
    doc = json.loads(json.dumps(career))
    for d in doc["drivers"]:
        d.pop("gp_starts", None)
        d.pop("gp_wins", None)
    migrate_save(doc)
    by_name = {d["name"]: d for d in doc["drivers"]}
    assert (by_name["Lewis Hamilton"]["gp_starts"],
            by_name["Lewis Hamilton"]["gp_wins"]) == (360, 105)
    assert (by_name["Lando Norris"]["gp_starts"],
            by_name["Lando Norris"]["gp_wins"]) == (130, 7)
    assert (by_name["Paul Aron"]["gp_starts"],
            by_name["Paul Aron"]["gp_wins"]) == (0, 0)
    junior = next(d for d in doc["drivers"] if d["status"] == "f2")
    assert (junior["gp_starts"], junior["gp_wins"]) == (0, 0)


def test_dashboard_shows_only_main_drivers():
    """v0.5: the dashboard My Team card shows ONLY the main 2
    drivers; T&R and academy drivers are viewed in the Drivers tab.
    End to end in a real browser: start a career and read the
    dashboard card."""
    pytest.importorskip("playwright")
    import re
    import subprocess
    import time
    import urllib.request
    port = 8799
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app",
         "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health")
                break
            except Exception:
                time.sleep(0.5)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            p1 = browser.new_page()
            p1.goto(f"http://127.0.0.1:{port}/")
            p1.fill("#home-nick", "Max")
            p1.click("#btn-create")
            p1.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
            p1.click("#btn-new-career")
            p1.wait_for_selector("[data-team]", timeout=15000)
            p1.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.textContent.includes('McLaren'))
                    .querySelector('[data-team]').click();
            }""")
            p1.wait_for_selector("#btn-start-career:not(.hidden)", timeout=15000)
            code = re.search(r"\b[A-Z0-9]{6}\b",
                             p1.inner_text("#screen-lobby")).group(0)
            p2 = browser.new_page()
            p2.goto(f"http://127.0.0.1:{port}/?code={code}")
            p2.fill("#home-nick", "Cousin")
            p2.click("#btn-join")
            p2.wait_for_selector("[data-team]", timeout=15000)
            p2.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.textContent.includes('Ferrari'))
                    .querySelector('[data-team]').click();
            }""")
            p1.click("#btn-start-career")
            p1.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            my_team = p1.inner_text("#my-team")
            assert "NORRIS" in my_team and "PIASTRI" in my_team, my_team
            # The one-line pointer to the Drivers tab is the only
            # mention of T&R/academy on the dashboard now.
            assert "Test & Reserve and academy juniors are in the Drivers tab." in my_team, my_team
            assert "Swap" not in my_team, my_team
            assert "Promote" not in my_team, my_team
            assert "Release" not in my_team, my_team
            # No T&R surname leaks in: McLaren's T&R drivers are
            # de Vries, O'Ward, Fornaroli, Verschoor.
            upper = my_team.upper()
            for surname in ("DE VRIES", "O'WARD", "FORNAROLI", "VERSCHOOR"):
                assert surname not in upper, my_team
            browser.close()
    finally:
        proc.terminate()


# ---------------------------------------------------------------------------
# v0.6.2 — team relationships: donations, customer/junior contracts,
# WANTED drivers, bot parity, v10 save migration (v0.6.6)
# ---------------------------------------------------------------------------

def team_by_name(career, name):
    return next(t for t in career["teams"] if t["name"] == name)


def driver_by_name(career, name):
    return next(d for d in career["drivers"] if d["name"] == name)


def test_relations_initial_2026():
    """v0.6: a fresh 2026 career starts with the grid's relations —
    Mercedes supplies McLaren / Williams / Alpine (customers), Red
    Bull supplies Racing Bulls (junior), Ferrari supplies Haas /
    Cadillac (customers); Aston Martin and Audi are independent.
    Starting deals last 2 years; the new fields default correctly."""
    career = game.build_new_career("Test", "Matthew")
    assert career.get("relation_proposals") == []
    mercedes = team_by_name(career, "Mercedes")
    redbull = team_by_name(career, "Red Bull")
    ferrari = team_by_name(career, "Ferrari")
    for name in ("McLaren", "Williams", "Alpine"):
        t = team_by_name(career, name)
        assert t["supplier_id"] == mercedes["id"], name
        assert t["sup_stage"] == 1, name
        assert t["relation_years"] == 2, name
    rbr = team_by_name(career, "Racing Bulls")
    assert rbr["supplier_id"] == redbull["id"]
    assert rbr["sup_stage"] == 2
    assert rbr["relation_years"] == 2
    for name in ("Haas", "Cadillac"):
        t = team_by_name(career, name)
        assert t["supplier_id"] == ferrari["id"], name
        assert t["sup_stage"] == 1, name
        assert t["relation_years"] == 2, name
    for name in ("Aston Martin", "Audi", "Mercedes", "Red Bull", "Ferrari"):
        t = team_by_name(career, name)
        assert t["supplier_id"] is None, name
        assert t["sup_stage"] == 0, name
        assert t["relation_years"] == 0, name
    for t in career["teams"]:
        assert t["break_notice"] is None
        assert t["wanted"] == []
        assert t["junior_share_last_race"] == 0.0
        assert t["junior_share_season"] == 0.0
        assert t["junior_share_received_last_race"] == 0.0
        assert t["junior_share_received_season"] == 0.0
    assert "donation_requests" not in career  # v0.8.0: field removed


def test_donate():
    """v0.6: a donation moves the exact amount from a supplier to a
    team it supplies, any time, no fee — and is rejected when the
    donor cannot afford it, the recipient is not supplied, or the
    amount is not positive. v0.6.5: a customer/junior team can also
    donate UP to its supplier (parent) — same mechanics."""
    career = game.build_new_career("Test", "Matthew")
    mercedes = team_by_name(career, "Mercedes")
    mclaren = team_by_name(career, "McLaren")
    ferrari = team_by_name(career, "Ferrari")
    ok, why = game.donate(career, mercedes["id"], mclaren["id"], 2.0)
    assert ok, why
    assert mercedes["money"] == 5.0
    assert mclaren["money"] == 12.0
    # v0.6.5: upstream — McLaren (Mercedes' customer) donates to
    # its parent, Mercedes. Same mechanics: any amount, no fee.
    ok, why = game.donate(career, mclaren["id"], mercedes["id"], 1.0)
    assert ok, why
    assert mclaren["money"] == 11.0
    assert mercedes["money"] == 6.0
    ok, why = game.donate(career, mercedes["id"], mclaren["id"], 100.0)
    assert not ok and "Not enough money" in why
    assert mercedes["money"] == 6.0 and mclaren["money"] == 11.0
    ok, why = game.donate(career, mercedes["id"], ferrari["id"], 1.0)
    assert not ok and "supply relation" in why
    ok, why = game.donate(career, mercedes["id"], mclaren["id"], 0)
    assert not ok
    ok, why = game.donate(career, mercedes["id"], mclaren["id"], -1.0)
    assert not ok


def test_relation_progression():
    """v0.6: nothing -> customer -> junior works (one step at a
    time); nothing -> junior directly is rejected; junior ->
    customer, junior -> nothing, customer -> nothing all work; a
    second junior for the same supplier and a second supplier for
    the same team are rejected; changes outside the winter break are
    rejected. Human targets keep proposals pending until they answer;
    only the target may answer. v0.6.5: the supplier here is
    Mercedes — McLaren, a Mercedes customer, cannot offer supply
    contracts at all."""
    career = game.build_new_career("Test", "Matthew")
    # McLaren + Aston Martin as the two humans (no relation between
    # them — two_human_career picks McLaren + Mercedes, who already
    # have one). v0.7.2: Ferrari can no longer play the customer
    # here — it supplies Haas and Cadillac, so a team that supplies
    # others cannot become a customer or junior team; Aston Martin
    # is independent and supplies nobody.
    # Mercedes plays the supplier role: it is independent, while
    # McLaren — a Mercedes customer — cannot offer supply contracts
    # at all under v0.6.5.
    ok, _ = game.choose_team(career, team_by_name(career, "McLaren")["id"], "p-matthew")
    assert ok
    ok, _ = game.choose_team(career, team_by_name(career, "Aston Martin")["id"], "p-cousin")
    assert ok
    mclaren = team_by_name(career, "McLaren")
    aston = team_by_name(career, "Aston Martin")
    mercedes = team_by_name(career, "Mercedes")
    # Outside the winter break: rejected.
    ok, why = game.propose_relation(career, mercedes["id"], aston["id"],
                                    "customer", mercedes["id"], 3)
    assert not ok and "winter break" in why
    career["phase"] = "winter_break"
    # Direct nothing -> junior: rejected.
    ok, why = game.propose_relation(career, mercedes["id"], aston["id"],
                                    "junior", mercedes["id"], 2)
    assert not ok and "must be a customer" in why
    # Customer formation: Aston Martin is human, so the proposal stays pending.
    ok, why = game.propose_relation(career, mercedes["id"], aston["id"],
                                    "customer", mercedes["id"], 3)
    assert ok, why
    assert len(career["relation_proposals"]) == 1
    assert aston["sup_stage"] == 0  # not applied until accepted
    proposal = career["relation_proposals"][0]
    # Only the target may answer.
    ok, why = game.respond_relation(career, mclaren["id"], proposal["id"], True)
    assert not ok and "Only the team" in why
    ok, why = game.respond_relation(career, aston["id"], proposal["id"], True)
    assert ok, why
    assert aston["sup_stage"] == 1
    assert aston["supplier_id"] == mercedes["id"]
    assert aston["relation_years"] == 3
    assert career["relation_proposals"] == []
    # A second supplier for the same team is rejected.
    redbull = team_by_name(career, "Red Bull")
    ok, why = game.propose_relation(career, redbull["id"], aston["id"],
                                    "customer", redbull["id"], 2)
    assert not ok and "already has a supplier" in why
    # Upgrade to junior, then a second junior for Mercedes is rejected.
    ok, why = game.propose_relation(career, mercedes["id"], aston["id"],
                                    "junior", mercedes["id"], 2)
    assert ok, why
    proposal = career["relation_proposals"][0]
    ok, why = game.respond_relation(career, aston["id"], proposal["id"], True)
    assert ok, why
    assert aston["sup_stage"] == 2
    assert aston["relation_years"] == 2
    # Audi becomes Mercedes' customer first (AI target: the bot
    # accepts with p = 0.75 x 1.25 poor = 0.9375 — the roll is pinned
    # so the test is deterministic) — then a second junior for
    # Mercedes is rejected.
    audi = team_by_name(career, "Audi")
    ok, why = game.propose_relation(career, mercedes["id"], audi["id"],
                                    "customer", mercedes["id"], 2,
                                    rng=_FixedRng(0.0))
    assert ok, why
    assert audi["sup_stage"] == 1 and audi["supplier_id"] == mercedes["id"]
    ok, why = game.propose_relation(career, mercedes["id"], audi["id"],
                                    "junior", mercedes["id"], 2)
    assert not ok and "already has a junior" in why
    # Downgrade: junior -> customer, years unchanged. The supplier
    # proposes it to human Aston Martin so it stays pending (an AI target
    # would decide on the spot).
    ok, why = game.propose_relation(career, mercedes["id"], aston["id"],
                                    "downgrade")
    assert ok, why
    proposal = career["relation_proposals"][0]
    ok, why = game.respond_relation(career, aston["id"], proposal["id"], True)
    assert ok, why
    assert aston["sup_stage"] == 1
    assert aston["relation_years"] == 2
    # End: customer -> nothing.
    ok, why = game.propose_relation(career, mercedes["id"], aston["id"], "end")
    assert ok, why
    proposal = career["relation_proposals"][0]
    ok, why = game.respond_relation(career, aston["id"], proposal["id"], True)
    assert ok, why
    assert aston["sup_stage"] == 0
    assert aston["supplier_id"] is None
    assert aston["relation_years"] == 0
    # Junior -> nothing directly (skip customer).
    for kind in ("customer", "junior"):
        ok, why = game.propose_relation(career, mercedes["id"], aston["id"],
                                        kind, mercedes["id"], 2)
        assert ok, why
        proposal = career["relation_proposals"][0]
        ok, why = game.respond_relation(career, aston["id"], proposal["id"], True)
        assert ok, why
    assert aston["sup_stage"] == 2
    ok, why = game.propose_relation(career, mercedes["id"], aston["id"], "end")
    assert ok, why
    proposal = career["relation_proposals"][0]
    ok, why = game.respond_relation(career, aston["id"], proposal["id"], True)
    assert ok, why
    assert aston["sup_stage"] == 0
    # Declining leaves everything as it was.
    ok, why = game.propose_relation(career, mercedes["id"], aston["id"],
                                    "customer", mercedes["id"], 2)
    assert ok, why
    proposal = career["relation_proposals"][0]
    ok, why = game.respond_relation(career, aston["id"], proposal["id"], False)
    assert ok, why
    assert aston["sup_stage"] == 0
    assert career["relation_proposals"] == []


def test_contracts_rollover_renew_break_lapse():
    """v0.6: relation years tick down one per season at season end;
    an expired deal renews by agreement (new 1–4 year term) or lapses
    at the next season start. v0.6.6: breaking early costs $2.0M per
    remaining year, paid when the break STARTS, blocked when
    unaffordable, free once expired — and the break takes effect
    after a 3-round notice, during which the relation stays fully
    active."""
    career = game.build_new_career("Test", "Matthew")
    # McLaren human, Mercedes AI (so the renew proposal gets a bot
    # decision on the spot).
    ok, _ = game.choose_team(career, team_by_name(career, "McLaren")["id"], "p-matthew")
    assert ok
    mclaren = team_by_name(career, "McLaren")
    mercedes = team_by_name(career, "Mercedes")
    assert mclaren["relation_years"] == 2
    game.end_of_season(career, rng=seeded(5))
    assert career["phase"] == "winter_break"
    assert mclaren["relation_years"] == 1
    assert mclaren["sup_stage"] == 1
    # The season-end round opening also left Mercedes' bot proposal
    # (upgrading McLaren to junior) pending — clear it so the renew
    # proposal below is the only one between these teams.
    career["relation_proposals"] = []
    # Renewal is only for expired deals.
    ok, why = game.propose_relation(career, mclaren["id"], mercedes["id"],
                                    "renew", mercedes["id"], 3)
    assert not ok and "still has 1 year(s) left" in why
    # Due deal: renew by agreement (Mercedes is a bot — it accepts:
    # McLaren is within 60 perf of it).
    mclaren["relation_years"] = 0
    ok, why = game.propose_relation(career, mclaren["id"], mercedes["id"],
                                    "renew", mercedes["id"], 3)
    assert ok, why
    assert mclaren["relation_years"] == 3
    # Early break: $2.0M per remaining year, paid when the break
    # starts; the deal stays fully active during the 3-round notice
    # and ends when the counter hits 0. A second break while the
    # notice runs is rejected.
    money_before = mclaren["money"]
    ok, why = game.break_relation(career, mclaren["id"], mercedes["id"])
    assert ok, why
    assert mclaren["money"] == round(money_before - 6.0, 2)
    assert mclaren["sup_stage"] == 1  # still active during the notice
    assert mclaren["break_notice"] == {"rounds_left": 3,
                                       "breaker_id": mclaren["id"]}
    ok, why = game.break_relation(career, mclaren["id"], mercedes["id"])
    assert not ok and "already being broken" in why
    assert mclaren["money"] == round(money_before - 6.0, 2)
    game._tick_relation_notices(career)
    assert mclaren["sup_stage"] == 1
    assert mclaren["break_notice"]["rounds_left"] == 2
    game._tick_relation_notices(career)
    game._tick_relation_notices(career)
    assert mclaren["sup_stage"] == 0
    assert mclaren["supplier_id"] is None
    assert mclaren["break_notice"] is None
    # Blocked when unaffordable; free once expired.
    career2 = game.build_new_career("Test", "Matthew")
    ok, _ = game.choose_team(career2, team_by_name(career2, "McLaren")["id"], "p-matthew")
    assert ok
    mclaren2 = team_by_name(career2, "McLaren")
    mercedes2 = team_by_name(career2, "Mercedes")
    career2["phase"] = "winter_break"
    mclaren2["money"] = 1.0
    ok, why = game.break_relation(career2, mclaren2["id"], mercedes2["id"])
    assert not ok and "Need $4.00M" in why
    assert mclaren2["money"] == 1.0
    assert mclaren2["sup_stage"] == 1
    mclaren2["money"] = 10.0
    mclaren2["relation_years"] = 0
    ok, why = game.break_relation(career2, mclaren2["id"], mercedes2["id"])
    assert ok, why
    assert mclaren2["money"] == 10.0
    assert mclaren2["sup_stage"] == 0
    # Lapse: a due deal nobody renewed ends at the next season start.
    career3 = game.build_new_career("Test", "Matthew")
    ok, _ = game.choose_team(career3, team_by_name(career3, "McLaren")["id"], "p-matthew")
    assert ok
    mclaren3 = team_by_name(career3, "McLaren")
    career3["phase"] = "winter_break"
    career3["wb_done"] = True
    mclaren3["relation_years"] = 0
    ok, why = game.start_next_season(career3, rng=seeded(7))
    assert ok, why
    assert career3["phase"] == "round"
    assert mclaren3["sup_stage"] == 0
    assert mclaren3["supplier_id"] is None
    assert all(t["junior_share_season"] == 0.0 for t in career3["teams"])
    assert all(t["junior_share_received_season"] == 0.0
               for t in career3["teams"])
    assert career3["relation_proposals"] == []
    assert "donation_requests" not in career3  # v0.8.0: field removed


def test_wanted_reward():
    """v0.6: a parent marks a market driver WANTED with a reward; the
    junior team signing them (main or T&R, in-season or in the winter
    break) gets the reward, the parent loses it, and the mark is
    consumed. Another team signing the same driver pays nothing and
    the mark stays. Marks are limited to 3, market drivers only, and
    the reward must fit the parent's bank."""
    career = game.build_new_career("Test", "Matthew")
    redbull = team_by_name(career, "Red Bull")
    rbr = team_by_name(career, "Racing Bulls")
    ricciardo = driver_by_name(career, "Daniel Ricciardo")
    assert ricciardo["status"] == "free"
    ok, why = game.set_wanted(career, redbull["id"], ricciardo["id"], 2.0)
    assert ok, why
    assert rbr["wanted"] == [{"driver_id": ricciardo["id"], "reward": 2.0}]
    # Marking the same driver again updates the reward, not the count.
    ok, why = game.set_wanted(career, redbull["id"], ricciardo["id"], 3.0)
    assert ok, why
    assert rbr["wanted"] == [{"driver_id": ricciardo["id"], "reward": 3.0}]
    # Junior signs them into a main seat: Lawson's break fee is $4.0M.
    ok, why = game.sign_driver(career, rbr["id"], ricciardo["id"], 1, 2)
    assert ok, why
    assert rbr["money"] == 3.0  # 4.0 - 4.0 fee + 3.0 reward
    assert redbull["money"] == 5.5  # 8.5 - 3.0 reward
    assert rbr["wanted"] == []
    assert ricciardo["status"] == "f1" and ricciardo["team_id"] == rbr["id"]
    # T&R signing pays too.
    career = game.build_new_career("Test", "Matthew")
    redbull = team_by_name(career, "Red Bull")
    rbr = team_by_name(career, "Racing Bulls")
    magnussen = driver_by_name(career, "Kevin Magnussen")
    ok, why = game.set_wanted(career, redbull["id"], magnussen["id"], 1.5)
    assert ok, why
    ok, why = game.sign_tr_driver(career, rbr["id"], magnussen["id"], 2)
    assert ok, why
    assert rbr["money"] == 5.5  # 4.0 + 1.5 reward, T&R signing is free
    assert redbull["money"] == 7.0
    assert rbr["wanted"] == []
    # Another team signing the driver pays nothing and the mark stays.
    career = game.build_new_career("Test", "Matthew")
    redbull = team_by_name(career, "Red Bull")
    rbr = team_by_name(career, "Racing Bulls")
    haas = team_by_name(career, "Haas")
    vettel = driver_by_name(career, "Sebastian Vettel")
    ok, why = game.set_wanted(career, redbull["id"], vettel["id"], 2.0)
    assert ok, why
    ok, why = game.sign_driver(career, haas["id"], vettel["id"], 1, 2)
    assert ok, why
    assert redbull["money"] == 8.5
    assert rbr["wanted"] == [{"driver_id": vettel["id"], "reward": 2.0}]
    # Winter-break signing pays too (via the offer flow — offers fill
    # an EMPTY seat, so Lawson leaves the seat first).
    career = game.build_new_career("Test", "Matthew")
    redbull = team_by_name(career, "Red Bull")
    rbr = team_by_name(career, "Racing Bulls")
    ricciardo = driver_by_name(career, "Daniel Ricciardo")
    lawson = driver_by_name(career, "Liam Lawson")
    rbr["driver1_id"] = None
    lawson["status"] = "free"
    lawson["team_id"] = None
    career["phase"] = "winter_break"
    ok, why = game.set_wanted(career, redbull["id"], ricciardo["id"], 2.0)
    assert ok, why
    ok, why = game.submit_winter_offers(
        career, rbr["id"],
        [{"driver_id": ricciardo["id"], "target": "seat", "seat": 1, "years": 2}])
    assert ok, why
    game.resolve_winter_break_round(career, rng=random.Random(1))
    assert ricciardo["status"] == "f1" and ricciardo["team_id"] == rbr["id"]
    assert rbr["money"] == 6.0  # 4.0 + 2.0 reward (empty seat: no fee)
    assert redbull["money"] == 6.5
    assert rbr["wanted"] == []
    # Limits and validation.
    career = game.build_new_career("Test", "Matthew")
    redbull = team_by_name(career, "Red Bull")
    rbr = team_by_name(career, "Racing Bulls")
    aston = team_by_name(career, "Aston Martin")
    norris = driver_by_name(career, "Lando Norris")
    ok, why = game.set_wanted(career, redbull["id"], norris["id"], 1.0)
    assert not ok and "Only free agents and F2" in why
    ok, why = game.set_wanted(career, aston["id"],
                              driver_by_name(career, "Daniel Ricciardo")["id"], 1.0)
    assert not ok and "don't supply a customer or junior team" in why
    ok, why = game.set_wanted(career, redbull["id"],
                              driver_by_name(career, "Daniel Ricciardo")["id"], 100.0)
    assert not ok and "Not enough money" in why
    for name in ("Daniel Ricciardo", "Kevin Magnussen", "Sebastian Vettel"):
        ok, why = game.set_wanted(career, redbull["id"],
                                  driver_by_name(career, name)["id"], 1.0)
        assert ok, why
    ok, why = game.set_wanted(career, redbull["id"],
                              driver_by_name(career, "Mick Schumacher")["id"], 1.0)
    assert not ok and "At most 3" in why
    assert len(rbr["wanted"]) == 3
    ok, why = game.remove_wanted(career, redbull["id"],
                                 driver_by_name(career, "Kevin Magnussen")["id"])
    assert ok, why
    assert len(rbr["wanted"]) == 2


def test_research_cap_removed_v066():
    """v0.6.6: the research cap feature is gone entirely — there is
    no set_research_cap, do_research is never capped (not even for a
    junior team), new teams carry no cap fields, and a v9 save that
    still carries research_cap / research_spent_season loads without
    them (migration drops the fields)."""
    career = game.build_new_career("Test", "Matthew")
    assert not hasattr(game, "set_research_cap")
    rbr = team_by_name(career, "Racing Bulls")
    assert "research_cap" not in rbr
    assert "research_spent_season" not in rbr
    # do_research is uncapped even for a junior team.
    rbr["money"] = 20.0
    ok, why = game.do_research(career, rbr["id"], "perf", 10.0)
    assert ok, why
    assert rbr["money"] == 10.0
    # A v9 save carrying the old cap fields loads cleanly.
    from f1.saves import migrate_save
    doc = json.loads(json.dumps(career))
    doc["version"] = 9
    for t in doc["teams"]:
        t["research_cap"] = 5.0
        t["research_spent_season"] = 3.0
    migrated = migrate_save(doc)
    for t in migrated["teams"]:
        assert "research_cap" not in t
        assert "research_spent_season" not in t


def test_migration_v8_to_v12():
    """v0.7.1 (save v12): an old v8 save backfills the 2026 relations
    (Mercedes -> McLaren/Williams/Alpine customer, Red Bull ->
    Racing Bulls junior, Ferrari -> Haas/Cadillac customer, 2 years)
    and the new fields default correctly (break_notice None, junior
    share 0, donation_requests []); a v9 save carrying the removed
    research-cap fields loads without them; a save round-trips at
    version 12."""
    from f1.saves import SAVE_VERSION, load_save, migrate_save, save_career
    career = game.build_new_career("Test", "Matthew")
    doc = json.loads(json.dumps(career))
    doc["version"] = 8
    doc.pop("relation_proposals", None)
    doc.pop("donation_requests", None)
    for t in doc["teams"]:
        t.pop("relation_years", None)
        t.pop("wanted", None)
        t.pop("break_notice", None)
        t.pop("junior_share_last_race", None)
        t.pop("junior_share_season", None)
        t.pop("junior_share_received_last_race", None)
        t.pop("junior_share_received_season", None)
        if t["name"] == "McLaren":
            # Simulate a save that never resolved the supplier link.
            t["supplier_id"] = None
            t.pop("sup_stage", None)
    migrated = migrate_save(doc)
    assert migrated.get("relation_proposals") == []
    mercedes = team_by_name(migrated, "Mercedes")
    redbull = team_by_name(migrated, "Red Bull")
    ferrari = team_by_name(migrated, "Ferrari")
    for name in ("McLaren", "Williams", "Alpine"):
        t = team_by_name(migrated, name)
        assert t["supplier_id"] == mercedes["id"], name
        assert t["sup_stage"] == 1, name
        assert t["relation_years"] == 2, name
    rbr = team_by_name(migrated, "Racing Bulls")
    assert rbr["supplier_id"] == redbull["id"]
    assert rbr["sup_stage"] == 2
    assert rbr["relation_years"] == 2
    for name in ("Haas", "Cadillac"):
        t = team_by_name(migrated, name)
        assert t["supplier_id"] == ferrari["id"], name
        assert t["sup_stage"] == 1, name
        assert t["relation_years"] == 2, name
    for name in ("Aston Martin", "Audi"):
        t = team_by_name(migrated, name)
        assert t["supplier_id"] is None, name
        assert t["sup_stage"] == 0, name
        assert t["relation_years"] == 0, name
    for t in migrated["teams"]:
        assert t["break_notice"] is None
        assert t["wanted"] == []
        assert t["junior_share_last_race"] == 0.0
        assert t["junior_share_season"] == 0.0
        assert t["junior_share_received_last_race"] == 0.0
        assert t["junior_share_received_season"] == 0.0
    assert migrated.get("donation_requests") is None  # v0.8.0: dropped on migration
    # A v9 save round-trips (migrating twice changes nothing), and a
    # v9 save carrying the removed research-cap fields loads without
    # them.
    assert SAVE_VERSION == 18
    migrated2 = migrate_save(json.loads(json.dumps(migrated)))
    assert team_by_name(migrated2, "McLaren")["relation_years"] == 2
    doc9 = json.loads(json.dumps(migrated))
    doc9["version"] = 9
    for t in doc9["teams"]:
        t["research_cap"] = 5.0
        t["research_spent_season"] = 3.0
    migrated9 = migrate_save(doc9)
    for t in migrated9["teams"]:
        assert "research_cap" not in t
        assert "research_spent_season" not in t
    with tempfile.TemporaryDirectory() as tmp:
        meta = save_career(career, "v10 test", tmp)
        loaded = load_save(tmp, meta["save_id"])
        assert loaded is not None
        assert team_by_name(loaded, "Racing Bulls")["sup_stage"] == 2
        assert team_by_name(loaded, "Racing Bulls")["relation_years"] == 2
        assert loaded.get("relation_proposals") == []
        assert loaded.get("donation_requests") is None  # v0.8.0: dropped on migration


def test_bot_relations():
    """v0.6 bot parity: a rich bot supplier donates to a poor supplied
    team; a bot parent marks a WANTED driver in the winter break; a
    bot junior prefers a WANTED driver when signing; bot targets
    accept/decline proposals by the simple rule. v0.6.5: a
    customer/junior team cannot offer supply contracts at all.
    v0.6.6: no bot parent sets a research cap any more (the feature
    is gone); a bot parent with no junior marks WANTED for its best
    customer instead."""
    # Donation: Mercedes (> $20M) -> McLaren (< $5M), once per round.
    career = game.build_new_career("Test", "Matthew")
    mercedes = team_by_name(career, "Mercedes")
    mclaren = team_by_name(career, "McLaren")
    mercedes["money"] = 25.0
    mclaren["money"] = 3.0
    game.ai_act(career, mercedes, rng=random.Random(3))
    assert any("donated" in line and "Mercedes" in line and "McLaren" in line
               for line in _ttexts(career))
    assert 4.0 < mclaren["money"] <= 5.0
    # Winter break round 1: bot parents mark a WANTED driver (and set
    # no research cap — the feature is gone).
    career = game.build_new_career("Test", "Matthew")
    career["phase"] = "winter_break"
    career["wb_round"] = 1
    game.start_winter_break_round(career, rng=random.Random(4))
    rbr = team_by_name(career, "Racing Bulls")
    assert "research_cap" not in rbr
    assert len(rbr["wanted"]) == 1
    marked = next(d for d in career["drivers"]
                  if d["id"] == rbr["wanted"][0]["driver_id"])
    best_rep = max(d["rep"] for d in career["drivers"]
                   if d["status"] in ("free", "f2"))
    assert marked["rep"] == best_rep
    assert rbr["wanted"][0]["reward"] == 1.0
    # A bot junior prefers a WANTED driver when filling an empty seat.
    career = game.build_new_career("Test", "Matthew")
    redbull = team_by_name(career, "Red Bull")
    rbr = team_by_name(career, "Racing Bulls")
    lawson = driver_by_name(career, "Liam Lawson")
    rbr["driver1_id"] = None
    lawson["status"] = "free"
    lawson["team_id"] = None
    drugovich = driver_by_name(career, "Felipe Drugovich")
    ok, why = game.set_wanted(career, redbull["id"], drugovich["id"], 1.0)
    assert ok, why
    game.ai_act(career, rbr, rng=random.Random(9))
    assert rbr["driver1_id"] == drugovich["id"]
    assert rbr["wanted"] == []
    assert redbull["money"] == 7.5
    # Bot proposal decisions (v0.7.3: probability formulas, not
    # hard rules — the rolls are pinned so the tests are
    # deterministic): a poor team accepts a supplier; a comfortable
    # team declines a non-frontrunner supplier; a customer accepts
    # becoming a junior.
    career = game.build_new_career("Test", "Matthew")
    career["phase"] = "winter_break"
    aston = team_by_name(career, "Aston Martin")
    audi = team_by_name(career, "Audi")
    # Aston is P7 (not a frontrunner, x0.57) and Audi is poor ($3.5M,
    # x1.25): p = 0.75 x 0.57 x 1.25 = 0.54 — a low roll accepts.
    ok, why = game.propose_relation(career, aston["id"], audi["id"],
                                    "customer", aston["id"], 2,
                                    rng=_FixedRng(0.0))
    assert ok and "accepted" in why, why  # Audi is poor ($3.5M)
    assert audi["sup_stage"] == 1 and audi["supplier_id"] == aston["id"]
    assert career["relation_proposals"] == []
    career = game.build_new_career("Test", "Matthew")
    career["phase"] = "winter_break"
    aston = team_by_name(career, "Aston Martin")
    cadillac = team_by_name(career, "Cadillac")
    # v0.6.5: a customer team cannot offer supply contracts at all —
    # Cadillac (a Ferrari customer) proposing to supply Aston is
    # rejected outright, not decided by the bot.
    ok, why = game.propose_relation(career, cadillac["id"], aston["id"],
                                    "customer", cadillac["id"], 2)
    assert not ok and "already has a supplier" in why
    assert aston["sup_stage"] == 0
    assert career["relation_proposals"] == []
    # A comfortable team declines a non-frontrunner supplier: Aston
    # (P7, not a frontrunner) offers to supply Audi, made comfortable
    # here with $25M (no poor bonus): p = 0.75 x 0.57 = 0.43 — a high
    # roll declines. v0.7.2: Mercedes can no longer play this role —
    # it supplies McLaren / Williams / Alpine, so it cannot become a
    # customer team at all.
    audi = team_by_name(career, "Audi")
    audi["money"] = 25.0
    ok, why = game.propose_relation(career, aston["id"], audi["id"],
                                    "customer", aston["id"], 2,
                                    rng=_FixedRng(0.9))
    assert ok and "declined" in why, why
    assert audi["sup_stage"] == 0
    assert career["relation_proposals"] == []
    career = game.build_new_career("Test", "Matthew")
    career["phase"] = "winter_break"
    mercedes = team_by_name(career, "Mercedes")
    alpine = team_by_name(career, "Alpine")
    # Mercedes is P2 (a frontrunner) and Alpine is not beating it:
    # p = 0.75 — a low roll accepts.
    ok, why = game.propose_relation(career, mercedes["id"], alpine["id"],
                                    "junior", mercedes["id"], 2,
                                    rng=_FixedRng(0.0))
    assert ok and "accepted" in why, why
    assert alpine["sup_stage"] == 2
    assert alpine["supplier_id"] == mercedes["id"]


# ---------------------------------------------------------------------------
# v0.6.6 — team relationships, second pass: lobby team switching,
# customer WANTED, junior income share, parent promotion/swap, two-way
# donation requests, research-cap removal, Proposal B race sim,
# contract-break notice. Save format v10.
# ---------------------------------------------------------------------------

def test_leave_team_v066():
    """v0.6.6: a player who picked a team on the setup screen (before
    the career starts) can leave it and pick a different one — nobody
    has to remake the room. The team goes back to AI; leaving with no
    team fails."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    ok, _ = game.choose_team(career, mclaren["id"], "p-matthew")
    assert ok
    assert mclaren["owner_player_id"] == "p-matthew"
    assert mclaren["is_ai"] is False
    ok, why = game.leave_team(career, "p-matthew")
    assert ok, why
    assert mclaren["owner_player_id"] is None
    assert mclaren["is_ai"] is True
    # And the player can pick a different team right after.
    ferrari = team_by_name(career, "Ferrari")
    ok, why = game.choose_team(career, ferrari["id"], "p-matthew")
    assert ok, why
    assert ferrari["owner_player_id"] == "p-matthew"
    # Leaving with no team fails.
    ok, why = game.leave_team(career, "p-nobody")
    assert not ok and "don't have a team" in why


def test_customer_wanted_v066():
    """v0.6.6: WANTED works for CUSTOMER teams too, same rules as
    juniors — the parent names which supplied team the mark is for,
    the customer signing the driver (main seat) gets the reward, the
    parent loses it, and the mark is consumed. A parent with several
    supplied teams must name one; a parent with one supplied team
    does not have to."""
    career = game.build_new_career("Test", "Matthew")
    mercedes = team_by_name(career, "Mercedes")
    mclaren = team_by_name(career, "McLaren")  # Mercedes customer
    ricciardo = driver_by_name(career, "Daniel Ricciardo")
    assert ricciardo["status"] == "free"
    # Mercedes supplies three customers — no target means "pick one".
    ok, why = game.set_wanted(career, mercedes["id"], ricciardo["id"], 2.0)
    assert not ok and "several teams" in why
    # Name the customer explicitly.
    ok, why = game.set_wanted(career, mercedes["id"], ricciardo["id"], 2.0,
                              mclaren["id"])
    assert ok, why
    assert mclaren["wanted"] == [{"driver_id": ricciardo["id"], "reward": 2.0}]
    # The customer signs them into an empty main seat (Norris leaves
    # the seat first, as in the winter-break WANTED test).
    norris = driver_by_name(career, "Lando Norris")
    mclaren["driver1_id"] = None
    norris["status"] = "free"
    norris["team_id"] = None
    ok, why = game.sign_driver(career, mclaren["id"], ricciardo["id"], 1, 2)
    assert ok, why
    assert mclaren["money"] == 12.0  # 10.0 + 2.0 reward (empty seat: no fee)
    assert mercedes["money"] == 5.0  # 7.0 - 2.0 reward
    assert mclaren["wanted"] == []
    assert ricciardo["status"] == "f1" and ricciardo["team_id"] == mclaren["id"]
    # A parent with exactly one supplied team need not name it.
    career = game.build_new_career("Test", "Matthew")
    redbull = team_by_name(career, "Red Bull")
    rbr = team_by_name(career, "Racing Bulls")
    ok, why = game.set_wanted(career, redbull["id"],
                              driver_by_name(career, "Kevin Magnussen")["id"], 1.0)
    assert ok, why
    assert len(rbr["wanted"]) == 1
    # Removing a mark without a target works the same way.
    ok, why = game.remove_wanted(career, redbull["id"],
                                 driver_by_name(career, "Kevin Magnussen")["id"])
    assert ok, why
    assert rbr["wanted"] == []


def test_junior_income_share_v066():
    """v0.6.6: after each race a junior team pays 15% of its RACE
    PRIZE money (per-car prize only — not EPRG, not the season-end
    payout) to its parent. The junior's and the parent's last-race
    and season figures track it; a customer team pays nothing."""
    career = game.build_new_career("Test", "Matthew")
    redbull = team_by_name(career, "Red Bull")
    rbr = team_by_name(career, "Racing Bulls")
    mclaren = team_by_name(career, "McLaren")  # customer — pays nothing
    game.run_race_weekend(career, rng=seeded(11))
    prize = 0.0
    for r in career["last_race"]:
        if r.get("team_id") == rbr["id"] and isinstance(r.get("pos"), int):
            prize += 1.95 - 0.025 * r["pos"]
    expected = round(round(prize, 2) * 0.15, 2)
    assert rbr["junior_share_last_race"] == expected
    assert redbull["junior_share_received_last_race"] == expected
    assert rbr["junior_share_season"] == expected
    assert redbull["junior_share_received_season"] == expected
    assert mclaren["junior_share_last_race"] == 0.0
    assert mclaren["junior_share_season"] == 0.0
    assert not any("15% junior share" in line for line in _ttexts(career))
    # A second race accumulates the season total.
    game.complete_race_weekend(career, rng=seeded(12))
    game.run_race_weekend(career, rng=seeded(13))
    prize2 = 0.0
    for r in career["last_race"]:
        if r.get("team_id") == rbr["id"] and isinstance(r.get("pos"), int):
            prize2 += 1.95 - 0.025 * r["pos"]
    expected2 = round(round(prize2, 2) * 0.15, 2)
    assert rbr["junior_share_last_race"] == expected2
    assert rbr["junior_share_season"] == round(expected + expected2, 2)
    assert redbull["junior_share_received_season"] == round(expected + expected2, 2)


def test_finances_view_junior_share_v066():
    """v0.6.6: finances_view carries the junior share figures for
    both sides (last race + season, paid and received) and names the
    junior team for the parent. The net is unchanged by the share —
    it is an EPRG-based estimate and the race prize the share comes
    from is not in it either."""
    career = game.build_new_career("Test", "Matthew")
    game.run_race_weekend(career, rng=seeded(11))
    rbr = team_by_name(career, "Racing Bulls")
    redbull = team_by_name(career, "Red Bull")
    f_rbr = game.finances_view(career, rbr)
    f_rb = game.finances_view(career, redbull)
    assert f_rbr["junior_share_last_race"] == rbr["junior_share_last_race"]
    assert f_rbr["junior_share_season"] == rbr["junior_share_season"]
    assert f_rb["junior_share_received_last_race"] == redbull["junior_share_received_last_race"]
    assert f_rb["junior_share_received_season"] == redbull["junior_share_received_season"]
    assert f_rb["junior_team_name"] == "Racing Bulls"
    assert f_rbr["junior_team_name"] is None
    assert f_rbr["net_per_race"] == round(
        f_rbr["eprg"] - f_rbr["salary_per_race"]
        - f_rbr["extra_charges_per_race"], 2)


def test_promote_junior_driver_v066():
    """v0.6.6: the parent takes a junior driver into a main seat or
    T&R, any time, with NO break fee. Straight into an empty main
    seat; swapping with an occupied seat (the occupant takes the
    junior driver's old slot); a named swap partner; an academy
    junior into T&R. Contracts move with the driver; a driver
    leaving a main seat banks their per-team points with the old
    team."""
    # Straight promotion into an empty main seat (Hadjar leaves
    # seat 2 first). Lawson banks the points he scored FOR RBR.
    career = game.build_new_career("Test", "Matthew")
    redbull = team_by_name(career, "Red Bull")
    rbr = team_by_name(career, "Racing Bulls")
    lawson = driver_by_name(career, "Liam Lawson")  # RBR seat 1
    hadjar = driver_by_name(career, "Isack Hadjar")
    money_before = redbull["money"]
    lawson["team_points"] = 10
    redbull["driver2_id"] = None
    hadjar["status"] = "free"
    hadjar["team_id"] = None
    ok, why = game.promote_junior_driver(career, redbull["id"], lawson["id"],
                                         ("seat", 2))
    assert ok, why
    assert redbull["driver2_id"] == lawson["id"]
    assert lawson["status"] == "f1" and lawson["team_id"] == redbull["id"]
    assert lawson["team_points"] == 0  # fresh tally for the new team
    assert rbr["driver1_id"] is None  # Lawson's old seat emptied
    assert rbr["points_kept"] == 10  # his RBR points stay with RBR
    assert redbull["money"] == money_before  # no break fee
    # Swap (default partner): Lindblad (RBR seat 2) into Red Bull
    # seat 1 — Verstappen takes Lindblad's old seat at Racing Bulls.
    career = game.build_new_career("Test", "Matthew")
    redbull = team_by_name(career, "Red Bull")
    rbr = team_by_name(career, "Racing Bulls")
    lindblad = driver_by_name(career, "Arvid Lindblad")  # RBR seat 2
    verstappen = driver_by_name(career, "Max Verstappen")
    ok, why = game.promote_junior_driver(career, redbull["id"], lindblad["id"],
                                         ("seat", 1))
    assert ok, why
    assert redbull["driver1_id"] == lindblad["id"]
    assert lindblad["status"] == "f1" and lindblad["team_id"] == redbull["id"]
    assert verstappen["status"] == "f1" and verstappen["team_id"] == rbr["id"]
    assert rbr["driver2_id"] == verstappen["id"]
    # Swap (named partner): Lawson (RBR seat 1) into Red Bull seat 2,
    # naming Hadjar — Hadjar takes Lawson's old seat at Racing Bulls.
    career = game.build_new_career("Test", "Matthew")
    redbull = team_by_name(career, "Red Bull")
    rbr = team_by_name(career, "Racing Bulls")
    lawson = driver_by_name(career, "Liam Lawson")
    hadjar = driver_by_name(career, "Isack Hadjar")
    ok, why = game.promote_junior_driver(career, redbull["id"], lawson["id"],
                                         ("seat", 2),
                                         swap_driver_id=hadjar["id"])
    assert ok, why
    assert redbull["driver2_id"] == lawson["id"]
    assert rbr["driver1_id"] == hadjar["id"]
    assert hadjar["status"] == "f1" and hadjar["team_id"] == rbr["id"]
    # Academy: a young F2 driver joins RBR's academy, then the parent
    # promotes them straight into its T&R.
    career = game.build_new_career("Test", "Matthew")
    redbull = team_by_name(career, "Red Bull")
    rbr = team_by_name(career, "Racing Bulls")
    junior = next(d for d in career["drivers"]
                  if d["status"] == "f2" and d["age"] <= 25
                  and not d.get("academy_team_id"))
    ok, why = game.add_academy_driver(career, rbr["id"], junior["id"])
    assert ok, why
    assert junior["academy_team_id"] == rbr["id"]
    ok, why = game.promote_junior_driver(career, redbull["id"], junior["id"],
                                         ("tr", None))
    assert ok, why
    assert junior["status"] == "tr" and junior["team_id"] == redbull["id"]
    assert junior.get("academy_team_id") is None
    # Rejections: a parent with no junior team, and a driver who is
    # not one of the junior team's.
    aston = team_by_name(career, "Aston Martin")
    ok, why = game.promote_junior_driver(career, aston["id"], junior["id"],
                                         ("tr", None))
    assert not ok and "don't supply a junior team" in why
    norris = driver_by_name(career, "Lando Norris")
    ok, why = game.promote_junior_driver(career, redbull["id"], norris["id"],
                                         ("tr", None))
    assert not ok and "not one of your junior team's drivers" in why


def test_v080_donation_requests_removed():
    """v0.8.0 (1): donation requests are GONE — the request/respond
    functions and the donation_accept_probability /
    donation_amount_factor helpers no longer exist, a new career
    carries no donation_requests field, and the old save field is
    dropped on migration. Voluntary donations (any amount, any time,
    along a supply tie) still work, and bots' spontaneous giving
    still works."""
    assert not hasattr(game, "request_donation")
    assert not hasattr(game, "respond_donation")
    assert not hasattr(game, "donation_accept_probability")
    assert not hasattr(game, "donation_amount_factor")
    career = game.build_new_career("Test", "Matthew")
    assert "donation_requests" not in career
    mercedes = team_by_name(career, "Mercedes")
    mclaren = team_by_name(career, "McLaren")  # Mercedes customer
    # Voluntary donation still works, either way along the tie.
    ok, why = game.donate(career, mercedes["id"], mclaren["id"], 2.0)
    assert ok, why
    assert mercedes["money"] == 5.0 and mclaren["money"] == 12.0
    # A save that still carries pending requests migrates cleanly —
    # the field is dropped, the money is untouched.
    career2 = game.build_new_career("Test", "Matthew")
    career2["donation_requests"] = [
        {"id": "dr-old", "requester_team_id": "t-x",
         "target_team_id": "t-y", "amount": 2.0}]
    career2["version"] = 16
    migrated = migrate_save(career2)
    assert migrated["version"] == 18
    assert "donation_requests" not in migrated


def test_break_notice_v066():
    """v0.6.6: a unilateral break costs $2.0M per remaining year,
    paid when the break STARTS, and takes effect after a 3-round
    notice — winter-break rounds count as rounds, the relation stays
    fully active meanwhile (donations still work), and the notice
    survives the season start. A mutual 'end it' (proposal) or a
    natural expiry has NO notice — it ends at once."""
    career = game.build_new_career("Test", "Matthew")
    ok, _ = game.choose_team(career, team_by_name(career, "McLaren")["id"], "p-matthew")
    assert ok
    mclaren = team_by_name(career, "McLaren")
    mercedes = team_by_name(career, "Mercedes")
    # v0.7.3: the break must START in the winter break; the notice
    # then ticks once per race weekend (complete_race_weekend) once
    # the season is running again.
    career["phase"] = "winter_break"
    ok, why = game.break_relation(career, mclaren["id"], mercedes["id"])
    career["phase"] = "round"
    assert ok, why
    assert mclaren["money"] == 6.0  # 10.0 - 2 yrs x $2.0M
    assert mclaren["sup_stage"] == 1  # still active
    assert mclaren["break_notice"]["rounds_left"] == 3
    # Fully active during the notice: a donation still works.
    ok, why = game.donate(career, mercedes["id"], mclaren["id"], 1.0)
    assert ok, why
    game.complete_race_weekend(career, rng=seeded(3))
    assert mclaren["break_notice"]["rounds_left"] == 2
    assert mclaren["sup_stage"] == 1
    game.complete_race_weekend(career, rng=seeded(3))
    game.complete_race_weekend(career, rng=seeded(3))
    assert mclaren["sup_stage"] == 0
    assert mclaren["supplier_id"] is None
    assert mclaren["break_notice"] is None
    # A notice started in the winter break survives the season start.
    career2 = game.build_new_career("Test", "Matthew")
    ok, _ = game.choose_team(career2, team_by_name(career2, "McLaren")["id"], "p-matthew")
    assert ok
    mclaren2 = team_by_name(career2, "McLaren")
    mercedes2 = team_by_name(career2, "Mercedes")
    career2["phase"] = "winter_break"
    career2["wb_done"] = True
    ok, why = game.break_relation(career2, mclaren2["id"], mercedes2["id"])
    assert ok, why
    game._tick_relation_notices(career2)  # winter-break round 3 resolves
    assert mclaren2["break_notice"]["rounds_left"] == 2
    ok, why = game.start_next_season(career2, rng=seeded(7))
    assert ok, why
    assert mclaren2["sup_stage"] == 1  # still active into the new season
    assert mclaren2["break_notice"]["rounds_left"] == 2
    # Even an expired deal does not lapse while its notice is running.
    mclaren2["relation_years"] = 0
    career2["phase"] = "winter_break"
    career2["wb_done"] = True
    ok, why = game.start_next_season(career2, rng=seeded(7))
    assert ok, why
    assert mclaren2["sup_stage"] == 1
    # A mutual 'end it' has NO notice — it ends at once (see the
    # v0.6 progression test: end by agreement clears the relation
    # immediately, with no break_notice ever set).


def test_race_sim_proposal_b_v066():
    """v0.6.6 (Proposal B, Matthew's call): the grid penalty is
    3.0s per position and the per-session form randomness is 0.35s/lap
    (race) / 0.50s (qualifying) — qualifying matters more, great
    drives from the back are rarer, and the front stays varied. No
    change to per-lap noise, tuning, DNFs or points."""
    from f1.race import (RACE_GRID_PENALTY_PER_POS,
                        SESSION_FORM_QUAL_SIGMA, SESSION_FORM_RACE_SIGMA)
    assert RACE_GRID_PENALTY_PER_POS == 3.0
    assert SESSION_FORM_RACE_SIGMA == 0.35
    assert SESSION_FORM_QUAL_SIGMA == 0.50


def test_ws_leave_team_and_promote_v066():
    """v0.6.6 over the real WebSocket: leave_team in the lobby (then
    pick another team), and promote_junior_driver after the career
    starts — all through the server's action wiring."""
    from fastapi.testclient import TestClient
    import server as srv

    client = TestClient(srv.app)

    def recv_until(ws, want, limit=60, check=None):
        for _ in range(limit):
            m = ws.receive_json()
            if m.get("type") == want and (check is None or check(m)):
                return m
        raise AssertionError(f"never got {want}")

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "create_room", "nickname": "Matthew", "colour": "#e74c3c"})
        joined = recv_until(ws, "joined")
        code = joined["code"]
        ws.send_json({"type": "new_career", "name": "WS v066"})
        state = recv_until(ws, "room_state",
                           check=lambda m: m["room"]["career"] is not None)
        teams = {t["name"]: t for t in state["room"]["career"]["teams"]}
        ws.send_json({"type": "choose_team", "team_id": teams["Red Bull"]["id"]})
        recv_until(ws, "action", check=lambda m: m.get("action") == "choose_team")
        # Lobby: leave Red Bull, then take McLaren instead.
        ws.send_json({"type": "leave_team"})
        act = recv_until(ws, "action", check=lambda m: m.get("action") == "leave_team")
        assert act["ok"], act
        room = srv.ROOMS[code]
        assert team_by_name(room["career"], "Red Bull")["owner_player_id"] is None
        ws.send_json({"type": "choose_team", "team_id": teams["McLaren"]["id"]})
        recv_until(ws, "action", check=lambda m: m.get("action") == "choose_team")
        # Leave McLaren too, retake Red Bull, then start the career.
        ws.send_json({"type": "leave_team"})
        recv_until(ws, "action", check=lambda m: m.get("action") == "leave_team")
        ws.send_json({"type": "choose_team", "team_id": teams["Red Bull"]["id"]})
        recv_until(ws, "action", check=lambda m: m.get("action") == "choose_team")
        ws.send_json({"type": "start_career"})
        recv_until(ws, "room_state", check=lambda m: m["room"]["started"] is True)
        # Leave is lobby-only once the career has started.
        ws.send_json({"type": "leave_team"})
        err = recv_until(ws, "error")
        assert "already started" in err["message"]
        # Promote Lawson (Racing Bulls seat 1) into an empty Red Bull
        # seat 2.
        career = room["career"]
        redbull = team_by_name(career, "Red Bull")
        lawson = driver_by_name(career, "Liam Lawson")
        hadjar = driver_by_name(career, "Isack Hadjar")
        redbull["driver2_id"] = None
        hadjar["status"] = "free"
        hadjar["team_id"] = None
        ws.send_json({"type": "promote_junior_driver",
                      "junior_driver_id": lawson["id"],
                      "target": {"kind": "seat", "seat": 2}})
        act = recv_until(ws, "action",
                         check=lambda m: m.get("action") == "promote_junior_driver")
        assert act["ok"], act
        assert redbull["driver2_id"] == lawson["id"]
        assert lawson["team_id"] == redbull["id"]


def test_ws_relationship_actions():
    """v0.6 over the real WebSocket: donate to a supplied team, mark a
    driver WANTED, and propose a relation (a bot target decides on
    the spot) — all through the server's action wiring."""
    from fastapi.testclient import TestClient
    import server as srv

    client = TestClient(srv.app)

    def recv_until(ws, want, limit=60, check=None):
        for _ in range(limit):
            m = ws.receive_json()
            if m.get("type") == want and (check is None or check(m)):
                return m
        raise AssertionError(f"never got {want}")

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "create_room", "nickname": "Matthew", "colour": "#e74c3c"})
        joined = recv_until(ws, "joined")
        code = joined["code"]
        ws.send_json({"type": "new_career", "name": "WS Relations"})
        state = recv_until(ws, "room_state",
                           check=lambda m: m["room"]["career"] is not None)
        teams = {t["name"]: t for t in state["room"]["career"]["teams"]}
        ws.send_json({"type": "choose_team", "team_id": teams["Red Bull"]["id"]})
        ws.send_json({"type": "start_career"})
        recv_until(ws, "room_state", check=lambda m: m["room"]["started"] is True)
        room = srv.ROOMS[code]
        career = room["career"]
        rbr = team_by_name(career, "Racing Bulls")
        ws.send_json({"type": "donate", "recipient_team_id": rbr["id"], "amount": 1.0})
        act = recv_until(ws, "action", check=lambda m: m.get("action") == "donate")
        assert act["ok"], act
        assert rbr["money"] == 5.0
        free = game.free_agents(career)[0]
        ws.send_json({"type": "set_wanted", "driver_id": free["id"], "reward": 1.5})
        act = recv_until(ws, "action", check=lambda m: m.get("action") == "set_wanted")
        assert act["ok"], act
        assert rbr["wanted"] == [{"driver_id": free["id"], "reward": 1.5}]
        # Winter break: propose a customer deal to Audi (a bot — it
        # accepts with p = 0.75 x 1.25 poor = 0.9375; seed the global
        # random so the roll, 0.134, accepts).
        career["phase"] = "winter_break"
        audi = team_by_name(career, "Audi")
        redbull = team_by_name(career, "Red Bull")
        random.seed(1)
        ws.send_json({"type": "propose_relation", "target_team_id": audi["id"],
                      "kind": "customer", "supplier_team_id": redbull["id"],
                      "years": 2})
        act = recv_until(ws, "action", check=lambda m: m.get("action") == "propose_relation")
        assert act["ok"], act
        assert "accepted" in act["message"]
        assert audi["sup_stage"] == 1
        assert audi["supplier_id"] == redbull["id"]


def test_relationships_tab_renders():
    """v0.6 (browser): the Relationships tab renders without JS
    errors for a supplier team (Mercedes) — current relation, teams
    supplied, donate form, propose form — and the Driver Market tab
    still renders with the WANTED-badge code path running."""
    pytest.importorskip("playwright")
    import subprocess
    import time
    import urllib.request
    port = 8807
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        from playwright.sync_api import sync_playwright
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/")
            page.fill("#home-nick", "Max")
            page.click("#btn-create")
            page.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
            page.click("#btn-new-career")
            page.wait_for_selector("[data-team]", timeout=15000)
            page.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.querySelector('b') && r.querySelector('b').textContent === 'Mercedes')
                    .querySelector('[data-team]').click();
            }""")
            page.wait_for_selector("#btn-start-career:not(.hidden)", timeout=15000)
            page.click("#btn-start-career")
            page.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            # The dashboard's one-line relation summary.
            my_team = page.inner_text("#my-team")
            assert "Supplies 3 teams" in my_team, my_team
            # The Relationships tab itself.
            page.click('[data-tab="relations"]')
            page.wait_for_selector("#tab-relations:not(.hidden)", timeout=15000)
            rel = page.inner_text("#tab-relations")
            assert "No supplier" in rel, rel
            assert "Teams you supply" in rel, rel
            for name in ("McLaren", "Williams", "Alpine"):
                assert name in rel, rel
            assert "Donate" in rel, rel
            assert "Propose a relation" in rel, rel
            # The Driver Market tab still renders (WANTED badge path).
            page.click('[data-tab="market"]')
            page.wait_for_selector("#tab-market:not(.hidden)", timeout=15000)
            market = page.inner_text("#tab-market")
            # v0.7.1: the market is grouped by source — the headers
            # are uppercased by CSS, so inner_text (rendered text)
            # shows them in caps.
            assert "F1 FREE AGENTS" in market, market
            assert "F2 (" in market, market
            browser.close()
        assert not errors, errors
    finally:
        proc.terminate()


# ---------------------------------------------------------------------------
# v0.6.5 — winter-break driver release; a customer/junior team
# cannot offer supply contracts; JUNIOR badge on junior teams;
# a customer/junior team can donate UP to its supplier.
# ---------------------------------------------------------------------------

def test_propose_blocked_when_supplier_has_supplier_v065():
    """v0.6.5 (Matthew's rule): a team that already HAS a supplier
    (customer or junior) cannot offer supply contracts to other
    teams — the server rejects the proposal. An independent team
    can still propose to supply."""
    career = game.build_new_career("Test", "Matthew")
    career["phase"] = "winter_break"
    mclaren = team_by_name(career, "McLaren")
    audi = team_by_name(career, "Audi")
    ferrari = team_by_name(career, "Ferrari")
    # McLaren is a customer of Mercedes — it cannot offer to supply
    # Audi, as a customer or as a junior.
    assert mclaren["supplier_id"] == team_by_name(career, "Mercedes")["id"]
    assert mclaren["sup_stage"] == 1
    ok, why = game.propose_relation(career, mclaren["id"], audi["id"],
                                    "customer", mclaren["id"], 2)
    assert not ok and "already has a supplier" in why
    ok, why = game.propose_relation(career, mclaren["id"], audi["id"],
                                    "junior", mclaren["id"], 2)
    assert not ok and "already has a supplier" in why
    assert career["relation_proposals"] == []
    assert audi["sup_stage"] == 0
    # Positive control: Ferrari is independent, so it CAN propose to
    # supply Audi (the bot accepts on the spot — Ferrari is faster).
    ok, why = game.propose_relation(career, ferrari["id"], audi["id"],
                                    "customer", ferrari["id"], 2)
    assert ok, why
    assert audi["sup_stage"] == 1
    assert audi["supplier_id"] == ferrari["id"]


def test_ws_winter_break_release_v065():
    """v0.6.5: a release_driver action sent over the socket in the
    winter break succeeds (it used to be blocked) — the full
    remaining contract is charged."""
    from fastapi.testclient import TestClient
    import server as srv

    client = TestClient(srv.app)

    def recv_until(ws, want, limit=60, check=None):
        for _ in range(limit):
            m = ws.receive_json()
            if m.get("type") == want and (check is None or check(m)):
                return m
        raise AssertionError(f"never got {want}")

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "create_room", "nickname": "Max", "colour": "#e74c3c"})
        joined = recv_until(ws, "joined")
        code = joined["code"]
        ws.send_json({"type": "new_career", "name": "WS WB Release"})
        state = recv_until(ws, "room_state",
                           check=lambda m: m["room"]["career"] is not None)
        teams = {t["name"]: t for t in state["room"]["career"]["teams"]}
        ws.send_json({"type": "choose_team", "team_id": teams["McLaren"]["id"]})
        ws.send_json({"type": "start_career"})
        recv_until(ws, "room_state", check=lambda m: m["room"]["started"] is True)
        career = srv.ROOMS[code]["career"]
        assert career["phase"] == "round"
        # Move into the winter break and drop a T&R driver.
        career["phase"] = "winter_break"
        tr = game.team_tr_drivers(career, team_by_name(career, "McLaren"))[0]
        assert tr["name"] == "Nyck de Vries"
        ws.send_json({"type": "release_driver", "driver_id": tr["id"]})
        act = recv_until(ws, "action",
                         check=lambda m: m.get("action") == "release_driver")
        assert act.get("ok") is True, act
        assert tr["status"] == "free"
        # Winter-break fee: 24 x 1.5/12 + 3.0 = $6.0M off $10.0M.
        assert team_by_name(career, "McLaren")["money"] == pytest.approx(4.0)


def test_junior_badge_and_relation_controls_v065():
    """v0.6.5 (browser): a junior team carries a JUNIOR badge
    wherever its name appears (Relationships tab, constructors
    standings) while a customer team does not; in the Relationships
    tab a customer team sees its supplier as a donate target and —
    in the winter break — no propose controls, while an
    independent team keeps them."""
    pytest.importorskip("playwright")
    import subprocess
    import time
    import urllib.request
    port = 8812
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        from playwright.sync_api import sync_playwright
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/")
            page.fill("#home-nick", "Max")
            page.click("#btn-create")
            page.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
            page.click("#btn-new-career")
            page.wait_for_selector("[data-team]", timeout=15000)
            page.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.querySelector('b') && r.querySelector('b').textContent === 'Red Bull')
                    .querySelector('[data-team]').click();
            }""")
            page.wait_for_selector("#btn-start-career:not(.hidden)", timeout=15000)
            page.click("#btn-start-career")
            page.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            # Relationships tab: Racing Bulls (Red Bull's junior)
            # shows the JUNIOR badge; independent Red Bull keeps the
            # propose controls.
            page.click('[data-tab="relations"]')
            page.wait_for_selector("#tab-relations:not(.hidden)", timeout=15000)
            rel = page.inner_text("#tab-relations")
            assert "Racing Bulls" in rel, rel
            assert "JUNIOR" in rel, rel
            assert "Propose a relation" in rel, rel
            assert page.locator("#tab-relations .jr-badge").count() >= 1
            # Constructors standings: the badge shows on the junior row.
            page.click('[data-tab="standings"]')
            page.wait_for_selector("#tab-standings:not(.hidden)", timeout=15000)
            st = page.inner_text("#tab-standings")
            assert "JUNIOR" in st, st
            # teamNameHtml itself: junior gets the badge, customer does not.
            jr = page.evaluate("() => window.F1UI.teamNameHtml({name:'Racing Bulls', colour:'#7091F8', sup_stage:2, supplier_name:'Red Bull', supplier_id:'nope'})")
            assert "JUNIOR" in jr and "jr-badge" in jr
            cu = page.evaluate("() => window.F1UI.teamNameHtml({name:'McLaren', colour:'#EF9B33', sup_stage:1, supplier_name:'Mercedes', supplier_id:'nope'})")
            assert "JUNIOR" not in cu
            # Same Relationships tab, but as McLaren in the winter
            # break: the supplier (Mercedes) is a donate target and
            # the propose controls are replaced by the can't-offer note.
            html = page.evaluate("""() => {
              const box = document.querySelector('#tab-relations');
              const savedTeam = myTeam, savedPhase = room.career.phase;
              myTeam = room.career.teams.find(t => t.name === 'McLaren');
              room.career.phase = 'winter_break';
              renderRelationsTab(box);
              const out = box.innerHTML;
              myTeam = savedTeam; room.career.phase = savedPhase;
              renderRelationsTab(box);
              return out;
            }""")
            assert "(your supplier)" in html, html
            assert "already has a supplier" in html, html
            assert "Propose</button>" not in html, html
            browser.close()
        assert not errors, errors
    finally:
        proc.terminate()


# ---------------------------------------------------------------------------
# v0.6.4 — Finances net gain; NEXT vote counts ALL humans; host
# override host-only; host crown; bot AI never acts for a
# disconnected human's team.
# ---------------------------------------------------------------------------

def test_finances_net_per_race_v064():
    """v0.6.4: the Finances tab shows one signed net number — the
    per-race gains minus the per-race salary cost minus the
    per-race extra charges. Positive = profit, negative = loss."""
    career = game.build_new_career("Test", "Matthew")
    for team in career["teams"]:
        f = game.finances_view(career, team)
        expected = round(f["eprg"] - f["salary_per_race"]
                         - f["extra_charges_per_race"], 2)
        assert f["net_per_race"] == expected
    # Positive case: a normal team is in profit per race.
    rb = next(t for t in career["teams"] if t["name"] == "Red Bull")
    assert game.finances_view(career, rb)["net_per_race"] > 0
    # Negative case: overload Aston Martin with T&R drivers — the
    # exponential T&R fine plus their salaries swamp the prize
    # money, exactly the trap Matthew fell into while testing the
    # Finances tab. The net must go negative.
    am = next(t for t in career["teams"] if t["name"] == "Aston Martin")
    assert game.finances_view(career, am)["net_per_race"] > 0
    pool = game.free_agents(career) + game.f2_drivers(career)
    for d in pool[:5]:
        ok, why = game.sign_tr_driver(career, am["id"], d["id"], contract_years=2)
        assert ok, why
    f = game.finances_view(career, am)
    assert f["net_per_race"] == round(
        f["eprg"] - f["salary_per_race"] - f["extra_charges_per_race"], 2)
    assert f["net_per_race"] < 0


def test_next_vote_waits_for_disconnected_human_v064():
    """v0.6.4: the NEXT vote counts ALL human players. A player who
    has not voted is pending even if their connection dropped —
    the vote must not complete on the other player's vote alone.
    (The live bug: the Round 2 race ran on Matthew's vote alone
    while Max's tab was down.)"""
    career = two_human_career()
    for t in list(game.human_teams(career)):
        game.advance_team(career, t["id"], rng=seeded())
    assert career["phase"] == "qual_results"
    assert sorted(game.results_voters(career)) == ["p-cousin", "p-matthew"]
    ok, adv, msg = game.vote_next(career, "p-matthew", rng=seeded())
    assert ok and not adv
    assert "1/2" in msg
    # The other voter is "disconnected" (has not voted): the vote
    # must NOT complete.
    assert not game.check_results_vote(career, rng=seeded())
    assert career["phase"] == "qual_results"
    # They reconnect and vote: now it completes.
    ok, adv, _ = game.vote_next(career, "p-cousin", rng=seeded())
    assert ok and adv
    assert career["phase"] == "race_results"


def test_advance_waits_for_all_humans_and_host_skip_advance_v064():
    """v0.6.4: the Advance waits for EVERY human team — a missing
    advance is pending, never excluded — and only the host can
    force the weekend with the host override. (The live bug: the
    Melbourne weekend started on Matthew's advance alone.)"""
    career = two_human_career()
    t1, t2 = career["teams"][0], career["teams"][1]
    ok, raced, _ = game.advance_team(career, t1["id"], rng=seeded())
    assert ok and not raced
    assert career["phase"] == "round"
    # Advancing twice does not help — the other human is still pending.
    ok, raced, _ = game.advance_team(career, t1["id"], rng=seeded())
    assert ok and not raced
    assert career["phase"] == "round"
    # A non-host cannot force the weekend.
    ok, raced, _ = game.host_skip_advance(career, "p-cousin",
                                          is_host=False, rng=seeded())
    assert not ok and not raced
    assert career["phase"] == "round"
    # The host can force it.
    ok, raced, _ = game.host_skip_advance(career, "p-matthew",
                                          is_host=True, rng=seeded())
    assert ok and raced
    assert career["phase"] == "qual_results"
    # The host override is only for the management phase.
    ok, raced, _ = game.host_skip_advance(career, "p-matthew",
                                          is_host=True, rng=seeded())
    assert not ok and not raced


def test_disconnected_human_team_untouched_by_bot_round_v064():
    """v0.6.4: the bot AI must NEVER act for a human-owned team,
    even when its player is temporarily disconnected. (Live bug:
    Red Bull's Tuning went 1 -> 3 and mystery research pools
    1.03/0.35 appeared while Max's tab was down — ai_act had run
    on his team during the race weekend.) The setup forces every
    ai_act trigger so the test would catch it if the bot ran."""
    from f1 import race as race_mod
    career = two_human_career()
    play_full_round(career, seeded(3))
    play_full_round(career, seeded(4))
    assert career["round"] == 3
    team = career["teams"][0]  # McLaren, p-matthew
    # Force the ai_act triggers: round >= 3, points > 0, lead
    # driver > 70% of team points, constructor pos < 4 (ai_act
    # would set tuning 3), and plenty of money (ai_act would
    # invest research and add an academy driver).
    team["points"] = 100
    drivers = game.team_drivers(career, team)
    drivers[0]["points"] = 90
    drivers[1]["points"] = 10
    career["teams"] = race_mod.constructor_standings(career)
    team = next(t for t in career["teams"] if t["id"] == team["id"])
    team["tuning"] = 1
    team["perf_res"] = 0.0
    team["reli_res"] = 0.0
    team["money"] = 50.0
    team["rel"] = 1.0  # no DNFs, so no repair bill muddies the money
    perf_before = team["perf"]
    academy_before = len(game.team_academy_drivers(career, team))
    pos_before = game.constructor_pos(career, team["id"])
    assert pos_before < 4
    # Race-flow money for this team: salaries + extra charges out,
    # EPRG in (the money block), PLUS the per-car prize money from
    # apply_race_results (1.95 - 0.025*pos per finishing car) minus
    # the DNF repair bill per DNF car. rel=1.0, so no DNFs here —
    # but the formula is written generally.
    from f1.race import DNF_REPAIR_COST
    salary = game.salary_per_round(career, team)
    extra = game.extra_charges_per_race(career, team)["total"]
    eprg = finance_estimates(pos_before)["eprg"]
    money_before = team["money"]
    # Both humans advance -> the weekend runs. The human team's
    # "disconnection" is irrelevant now: the bot must not touch it.
    for t in list(game.human_teams(career)):
        game.advance_team(career, t["id"], rng=seeded(9))
    assert career["phase"] == "qual_results"
    prize = 0.0
    for row in career["last_race"]:
        if row.get("team_id") == team["id"]:
            if row["pos"] == "DNF":
                prize -= DNF_REPAIR_COST
            else:
                prize += 1.95 - 0.025 * row["pos"]
    expected_money = money_before - salary - extra + eprg + prize
    assert team["tuning"] == 1  # ai_act would have set 3
    assert team["perf_res"] == 0.0  # ai_act would have invested
    assert team["reli_res"] == 0.0
    assert team["perf"] == perf_before  # ai_act would have developed
    assert len(game.team_academy_drivers(career, team)) == academy_before
    # Money changed ONLY by the race flow (salaries + charges +
    # gains + per-car prize money).
    assert abs(team["money"] - expected_money) < 0.02


def test_host_crown_grace_and_creator_restore_v064():
    """v0.6.4: a brief host disconnect does NOT move the crown; the
    crown only transfers after the 60s takeover grace, and the
    original host (room creator) regains it on rejoin."""
    import time as _time
    import server as srv

    room = srv.new_room("Matthew", "#e74c3c")
    host = srv.add_player(room, "Matthew", "#e74c3c")
    host_id = host["player_id"]
    assert room["creator_id"] == host_id
    assert room["host_id"] == host_id
    cousin = srv.add_player(room, "Cousin", "#3498db")
    cousin_id = cousin["player_id"]
    # Host disconnects briefly (< 60s): no promotion.
    host["connected"] = False
    host["disconnected_at"] = _time.time()
    srv.promote_host_if_needed(room)
    assert room["host_id"] == host_id
    # Host reconnects: crown is still theirs.
    host["connected"] = True
    host["disconnected_at"] = None
    srv.promote_host_if_needed(room)
    assert room["host_id"] == host_id
    # Host disconnects for a long time (> 60s): the crown moves to
    # the earliest-joined connected player.
    host["connected"] = False
    host["disconnected_at"] = _time.time() - 61
    srv.promote_host_if_needed(room)
    assert room["host_id"] == cousin_id
    # The original host (creator) rejoins: the crown returns to
    # them — the same restore the rejoin handler performs.
    host["connected"] = True
    host["disconnected_at"] = None
    if room.get("creator_id") == host["player_id"]:
        room["host_id"] = host["player_id"]
    srv.promote_host_if_needed(room)
    assert room["host_id"] == host_id


def test_ws_vote_and_advance_wait_for_disconnected_human_v064():
    """v0.6.4 end to end over real WebSockets: a disconnected
    human's advance/vote is pending, never excluded — the weekend
    and the vote must not complete until they reconnect and act."""
    import time as _time
    from fastapi.testclient import TestClient
    import server as srv
    from server import app

    client = TestClient(app)

    def recv_until(ws, want, limit=60, check=None):
        for _ in range(limit):
            m = ws.receive_json()
            if m.get("type") == want and (check is None or check(m)):
                return m
        raise AssertionError(f"never got {want}")

    with client.websocket_connect("/ws") as host_ws:
        host_ws.send_json({"type": "create_room", "nickname": "Matthew", "colour": "#e74c3c"})
        joined = recv_until(host_ws, "joined")
        code, host_id = joined["code"], joined["player_id"]
        guest_ws = client.websocket_connect("/ws").__enter__()
        guest_ws.send_json({"type": "join_room", "code": code,
                            "nickname": "Cousin", "colour": "#3498db"})
        g_joined = recv_until(guest_ws, "joined")
        guest_id = g_joined["player_id"]
        host_ws.send_json({"type": "new_career", "name": "Vote Career v064"})
        state = recv_until(host_ws, "room_state",
                           check=lambda m: m["room"]["career"] is not None)
        teams = state["room"]["career"]["teams"]
        host_ws.send_json({"type": "choose_team", "team_id": teams[0]["id"]})
        guest_ws.send_json({"type": "choose_team", "team_id": teams[1]["id"]})
        host_ws.send_json({"type": "start_career"})
        recv_until(host_ws, "room_state",
                   check=lambda m: m["room"]["started"] is True)
        # Host advances; guest disconnects before advancing. The
        # weekend must NOT start on the host's advance alone.
        host_ws.send_json({"type": "advance"})
        guest_ws.__exit__(None, None, None)
        state = recv_until(host_ws, "room_state",
                           check=lambda m: m["room"]["career"]["phase"] == "round")
        assert state["room"]["career"]["round"] == 1
        # Guest reconnects (same player) and advances: now it starts.
        guest_ws = client.websocket_connect("/ws").__enter__()
        guest_ws.send_json({"type": "rejoin", "code": code, "player_id": guest_id})
        recv_until(guest_ws, "joined")
        guest_ws.send_json({"type": "advance"})
        state = recv_until(host_ws, "room_state",
                           check=lambda m: m["room"]["career"]["phase"] == "qual_results")
        assert state["room"]["career"]["round"] == 1
        # Host votes NEXT; guest disconnects before voting. The vote
        # must NOT complete on the host's vote alone.
        host_ws.send_json({"type": "vote_next"})
        act = recv_until(host_ws, "action")
        assert "NEXT vote recorded (1/2)" in act["message"]
        guest_ws.__exit__(None, None, None)
        state = recv_until(host_ws, "room_state",
                           check=lambda m: m["room"]["career"]["phase"] == "qual_results")
        assert state["room"]["career"]["phase"] == "qual_results"
        # Guest reconnects and votes: now the race page appears.
        guest_ws = client.websocket_connect("/ws").__enter__()
        guest_ws.send_json({"type": "rejoin", "code": code, "player_id": guest_id})
        recv_until(guest_ws, "joined")
        guest_ws.send_json({"type": "vote_next"})
        state = recv_until(host_ws, "room_state",
                           check=lambda m: m["room"]["career"]["phase"] == "race_results")
        assert state["room"]["career"]["results_stage"] == "race"
        guest_ws.__exit__(None, None, None)


def test_ws_host_crown_returns_to_creator_on_rejoin_v064():
    """v0.6.4 over real WebSockets: a brief host disconnect does not
    move the crown; after a long absence the crown transfers, and
    the original host (creator) regains it on rejoin."""
    import time as _time
    from fastapi.testclient import TestClient
    import server as srv
    from server import app

    client = TestClient(app)

    def recv_until(ws, want, limit=60, check=None):
        for _ in range(limit):
            m = ws.receive_json()
            if m.get("type") == want and (check is None or check(m)):
                return m
        raise AssertionError(f"never got {want}")

    with client.websocket_connect("/ws") as host_ws:
        host_ws.send_json({"type": "create_room", "nickname": "Matthew", "colour": "#e74c3c"})
        joined = recv_until(host_ws, "joined")
        code, host_id = joined["code"], joined["player_id"]
        guest_ws = client.websocket_connect("/ws").__enter__()
        guest_ws.send_json({"type": "join_room", "code": code,
                            "nickname": "Cousin", "colour": "#3498db"})
        g_joined = recv_until(guest_ws, "joined")
        guest_id = g_joined["player_id"]
        # Host disconnects briefly: the crown must stay with them.
        host_ws.__exit__(None, None, None)
        state = recv_until(guest_ws, "room_state",
                           check=lambda m: m["room"]["host_id"] == host_id)
        assert state["room"]["host_id"] == host_id
        # Host rejoins: still host.
        host_ws = client.websocket_connect("/ws").__enter__()
        host_ws.send_json({"type": "rejoin", "code": code, "player_id": host_id})
        rejoined = recv_until(host_ws, "joined")
        assert rejoined["room"]["host_id"] == host_id
        # Host disconnects again; age the disconnect past the 60s
        # grace, then the guest rejoins — the crown transfers.
        host_ws.__exit__(None, None, None)
        room = srv.ROOMS[code]
        host_player = next(p for p in room["players"] if p["player_id"] == host_id)
        host_player["disconnected_at"] = _time.time() - 61
        guest_ws.__exit__(None, None, None)
        guest_ws = client.websocket_connect("/ws").__enter__()
        guest_ws.send_json({"type": "rejoin", "code": code, "player_id": guest_id})
        rejoined = recv_until(guest_ws, "joined")
        assert rejoined["room"]["host_id"] == guest_id
        # The original host (creator) rejoins: the crown returns.
        host_ws = client.websocket_connect("/ws").__enter__()
        host_ws.send_json({"type": "rejoin", "code": code, "player_id": host_id})
        rejoined = recv_until(host_ws, "joined")
        assert rejoined["room"]["host_id"] == host_id
        guest_ws.__exit__(None, None, None)
        host_ws.__exit__(None, None, None)


def _playwright_server():
    """Start the app on a random local port (same pattern as the
    other Playwright tests) and return (port, server, thread)."""
    import socket
    import threading
    import time as _time
    import urllib.request
    import uvicorn
    from server import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = _time.time() + 20
    while True:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    break
        except Exception:
            if _time.time() > deadline:
                raise
            _time.sleep(0.2)
    return port, server, thread


def test_playwright_host_skip_hidden_from_non_host_v064():
    """v0.6.4 client half: in a real browser, the host-override
    button ("skip vote & continue") is visible to the host and
    hidden from a non-host on the results page."""
    from playwright.sync_api import sync_playwright

    port, server, thread = _playwright_server()
    errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            # Host: create a room, start a career, take a team.
            host_ctx = browser.new_context()
            host = host_ctx.new_page()
            host.on("pageerror", lambda e: errors.append(str(e)))
            host.goto(f"http://127.0.0.1:{port}/")
            host.fill("#home-nick", "Max")
            host.click("#btn-create")
            host.locator("#screen-lobby").wait_for(state="visible", timeout=10000)
            host.click("#btn-new-career")
            host.locator("#team-list button[data-team]").first.wait_for(
                state="visible", timeout=10000)
            host.locator("#team-list button[data-team]").first.click()
            host.click("#btn-start-career")
            host.locator("#screen-game").wait_for(state="visible", timeout=10000)
            code = host.evaluate("() => room.code")
            assert len(code) == 6
            # Guest: join mid-career, take an AI team from the picker.
            guest_ctx = browser.new_context()
            guest = guest_ctx.new_page()
            guest.on("pageerror", lambda e: errors.append(str(e)))
            guest.goto(f"http://127.0.0.1:{port}/")
            guest.fill("#home-nick", "Matthew")
            guest.fill("#home-code", code)
            guest.click("#btn-join")
            guest.locator("#screen-picker").wait_for(state="visible", timeout=10000)
            guest.locator("#picker-team-list button[data-team]").first.click()
            guest.locator("#screen-game").wait_for(state="visible", timeout=10000)
            # Both advance -> the qualifying results page (vote page).
            host.click("#btn-advance")
            guest.click("#btn-advance")
            host.locator("#btn-vote-next").wait_for(state="visible", timeout=10000)
            guest.locator("#btn-vote-next").wait_for(state="visible", timeout=10000)
            # Host sees the override; the non-host does not.
            assert host.locator("#btn-host-skip").is_visible()
            assert not guest.locator("#btn-host-skip").is_visible()
            # The host force-advance button is likewise host-only on
            # the dashboard (visible here only after returning, so
            # check it is hidden for the guest right now).
            assert not guest.locator("#btn-host-advance").is_visible()
            browser.close()
        assert not errors, errors
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_playwright_finances_shows_net_per_race_v064():
    """v0.6.4 client half: in a real browser, the Finances tab
    shows one signed net figure ("Net per race: +$X.XXM") near the
    top, with its breakdown."""
    from playwright.sync_api import sync_playwright

    port, server, thread = _playwright_server()
    errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/")
            page.fill("#home-nick", "Max")
            page.click("#btn-create")
            page.locator("#screen-lobby").wait_for(state="visible", timeout=10000)
            page.click("#btn-new-career")
            page.locator("#team-list button[data-team]").first.wait_for(
                state="visible", timeout=10000)
            page.locator("#team-list button[data-team]").first.click()
            page.click("#btn-start-career")
            page.locator("#screen-game").wait_for(state="visible", timeout=10000)
            page.click('[data-tab="finances"]')
            page.locator("#tab-finances:not(.hidden)").wait_for(
                state="visible", timeout=10000)
            fin = page.inner_text("#finances-box")
            assert "Net per race:" in fin, fin
            assert "per-race gains" in fin, fin
            assert "salaries" in fin, fin
            browser.close()
        assert not errors, errors
    finally:
        server.should_exit = True
        thread.join(timeout=10)

# ---------------------------------------------------------------------------
# v0.7.0 — hidden team reputation, winter-break recap, season
# snapshots + clickable champion years (real 2010–2025 standings).
# ---------------------------------------------------------------------------

def _load_s3_save():
    """The real Season 3 save (v10, 2029 winter break), migrated —
    or None when it is not present in this checkout."""
    path = Path(__file__).parent.parent / "saves" / "matthew-max-wugynr-s3-complete.json"
    if not path.exists():
        return None
    return load_save(str(path.parent), "matthew-max-wugynr-s3-complete")


def test_team_rep_starts_at_50_and_migrates():
    """v0.7.0: every team starts at rep 50 in a fresh career; an
    old save without the field backfills to the neutral 50; the
    real Season 3 save (v10) migrates cleanly — and its file is
    never modified by loading it."""
    career = game.build_new_career("Test", "Matthew")
    assert all(t["team_rep"] == 50.0 for t in career["teams"])
    doc = json.loads(json.dumps(career))
    for t in doc["teams"]:
        t.pop("team_rep", None)
    doc["version"] = 10
    migrated = migrate_save(doc)
    assert all(t["team_rep"] == 50.0 for t in migrated["teams"])
    path = Path(__file__).parent.parent / "saves" / "matthew-max-wugynr-s3-complete.json"
    if not path.exists():
        pytest.skip("Season 3 save not present in this checkout")
    before = path.read_bytes()
    loaded = _load_s3_save()
    assert loaded is not None
    assert loaded["phase"] == "winter_break" and loaded["year"] == 2029
    assert all(t["team_rep"] == 50.0 for t in loaded["teams"])
    assert path.read_bytes() == before


def test_team_rep_never_leaves_the_server():
    """v0.7.0: hidden team reputation never reaches a client — not
    in the broadcast state (chart on or off), and not in the
    private my_team payload either."""
    career = two_human_career()
    viewer = career["teams"][0]["id"]
    for chart_on in (True, False):
        state = game.public_state(career, chart_on, viewer)
        assert all("team_rep" not in t for t in state["teams"])
    assert '"team_rep"' not in json.dumps(game.public_state(career, True))
    import server as srv
    room = srv.new_room("Matthew", "#e74c3c")
    room["career"] = career
    room["started"] = True
    mt = srv.my_team_public(room, "p-matthew")
    assert mt is not None
    assert "team_rep" not in mt


def test_team_rep_season_end_position_and_driver_quality():
    """v0.7.0: at the season rollover rep moves by final
    constructors' position — (6 - pos): P1 +5, P6 0, P11 -5 — and
    by the average hidden rep of the two main drivers, centred on
    2.5 and halved. A fresh career ends 2026 in 2025 order, so the
    numbers are exact: McLaren P1 with a 3.95-average lineup gains
    5 + 0.725; Cadillac P11 with a 2.8-average lineup loses
    5 - 0.15; Racing Bulls P6 with a 2.3-average lineup loses 0.1.
    The rollover also appends the 2026 season snapshot."""
    career = game.build_new_career("Test", "Matthew")
    game.end_of_season(career, rng=seeded(5))
    assert career["phase"] == "winter_break"
    rep = {t["name"]: t["team_rep"] for t in career["teams"]}
    assert rep["McLaren"] == pytest.approx(55.725)
    assert rep["Mercedes"] == pytest.approx(54.625)
    assert rep["Red Bull"] == pytest.approx(53.6)
    assert rep["Ferrari"] == pytest.approx(52.725)
    assert rep["Williams"] == pytest.approx(51.475)
    assert rep["Racing Bulls"] == pytest.approx(49.9)
    assert rep["Aston Martin"] == pytest.approx(49.2)
    assert rep["Haas"] == pytest.approx(48.1)
    assert rep["Audi"] == pytest.approx(47.175)
    assert rep["Alpine"] == pytest.approx(45.975)
    assert rep["Cadillac"] == pytest.approx(45.15)
    assert len(career["season_snapshots"]) == 1
    snap = career["season_snapshots"][0]
    assert snap["year"] == 2026
    assert snap["drivers"][0]["name"] == "Lando Norris"  # champion at 0 pts
    assert snap["teams"][0]["name"] == "McLaren"


def test_team_rep_race_win_bonus():
    """v0.7.0: a race win lifts the winning team's rep by +0.3, and
    no other team moves — after one played round exactly one team
    is at 50.3 and the rest are still at 50.0."""
    career = two_human_career()
    play_full_round(career, seeded(7))
    reps = {t["name"]: t["team_rep"] for t in career["teams"]}
    winners = [n for n, r in reps.items() if r == pytest.approx(50.3)]
    assert len(winners) == 1
    assert all(r == pytest.approx(50.0) for n, r in reps.items()
               if n != winners[0])


def test_team_rep_donation_flows():
    """v0.7.0 / v0.8.0: giving helps your reputation (+0.2 per $M,
    capped at +1.0 per transfer); a voluntary donation costs the
    recipient nothing — it is a gift. (v0.8.0: donation requests are
    gone, so there is no requester-reputation cost any more.)"""
    career = game.build_new_career("Test", "Matthew")
    mercedes = team_by_name(career, "Mercedes")
    mclaren = team_by_name(career, "McLaren")
    # Voluntary $2M gift: donor +0.4, recipient unchanged.
    ok, why = game.donate(career, mercedes["id"], mclaren["id"], 2.0)
    assert ok, why
    assert mercedes["money"] == 5.0 and mclaren["money"] == 12.0
    assert mercedes["team_rep"] == pytest.approx(50.4)
    assert mclaren["team_rep"] == pytest.approx(50.0)
    # Voluntary $10M gift: the donor's gain caps at +1.0.
    ok, why = game.donate(career, mclaren["id"], mercedes["id"], 10.0)
    assert ok, why
    assert mclaren["team_rep"] == pytest.approx(51.0)


def test_v080_bot_spontaneous_donations_still_work():
    """v0.8.0 (1): bots' spontaneous donations to a struggling
    supplied team survive the removal of donation requests — a rich
    bot parent still gives to a poor customer (no request needed),
    and a poor parent gives nothing."""
    career = game.build_new_career("Test", "Matthew")
    mercedes = team_by_name(career, "Mercedes")
    mclaren = team_by_name(career, "McLaren")  # Mercedes customer
    mercedes["money"] = 30.0
    mclaren["money"] = 1.0
    mclaren["team_rep"] = 80.0  # high rep: the generous tier
    # Spontaneous donations only go to a STRUGGLING supplied team
    # (bottom half of the constructors') — McLaren starts P1, so
    # move them to the back of the standings order first.
    career["teams"].remove(mclaren)
    career["teams"].append(mclaren)
    before = mercedes["money"]
    game.bot_spontaneous_donations(career, rng=_FixedRng(0.0))
    assert mclaren["money"] > 1.0, "rich bot parent should give"
    assert mercedes["money"] < before
    # A poor parent gives nothing.
    career2 = game.build_new_career("Test", "Matthew")
    mercedes2 = team_by_name(career2, "Mercedes")
    mclaren2 = team_by_name(career2, "McLaren")
    mercedes2["money"] = 5.0
    mclaren2["money"] = 1.0
    game.bot_spontaneous_donations(career2, rng=_FixedRng(0.0))
    assert mclaren2["money"] == 1.0
    assert mercedes2["money"] == 5.0


def test_team_rep_quiet_drop():
    """v0.7.0: the winter-break QUIET DROP — rep 50 -> 0, 35 -> 1,
    20 -> 2, 5 -> 3, 80 -> 0 — and it actually flips an offer: in
    the Season 3 career Red Bull (3rd) offering the same free agent
    as Aston Martin (4th) loses the driver when Red Bull's rep is
    5, and wins them when it is 50."""
    assert game.wb_rep_drop(50) == 0
    assert game.wb_rep_drop(80) == 0
    assert game.wb_rep_drop(35) == 1
    assert game.wb_rep_drop(20) == 2
    assert game.wb_rep_drop(5) == 3
    assert game.wb_rep_drop(0) == 3
    assert game.wb_rep_drop(49) == 0
    career = _load_s3_save()
    if career is None:
        pytest.skip("Season 3 save not present in this checkout")
    verstappen = next(d for d in career["drivers"]
                      if d["name"] == "Max Verstappen")
    assert verstappen["status"] == "free"
    redbull = team_by_name(career, "Red Bull")
    aston = team_by_name(career, "Aston Martin")
    offers = {
        redbull["id"]: [{"driver_id": verstappen["id"], "target": "seat",
                         "seat": 1, "years": 2}],
        aston["id"]: [{"driver_id": verstappen["id"], "target": "seat",
                       "seat": 1, "years": 2}],
    }
    # Low rep: Red Bull drops 3 (3rd -> effective 6th), Aston wins.
    redbull["team_rep"] = 5.0
    assert game._wb_offer_winners(career, offers) == {verstappen["id"]: aston["id"]}
    # Neutral rep: no drop, the better-placed team wins.
    redbull["team_rep"] = 50.0
    assert game._wb_offer_winners(career, offers) == {verstappen["id"]: redbull["id"]}
    # The stored constructors' order is never touched by the drop.
    assert game.wb_constructors_order(career)[:4] == [
        team_by_name(career, n)["id"]
        for n in ("Ferrari", "Mercedes", "Red Bull", "Aston Martin")]


def test_team_rep_proposal_factor():
    """v0.7.0: the customer/junior proposal multiplier — 1.0 at rep
    >= 25 (no extra rejection), 0.7 at rep 12.5, 0.4 at rep 0 (a 60%
    cut) — and it bites in the bot's decision: a rep-0 proposer
    whose proposal would otherwise be accepted is accepted on a
    0.0 roll and declined on a 0.5 roll."""
    assert game.proposal_rep_factor(25) == pytest.approx(1.0)
    assert game.proposal_rep_factor(50) == pytest.approx(1.0)
    assert game.proposal_rep_factor(12.5) == pytest.approx(0.7)
    assert game.proposal_rep_factor(0) == pytest.approx(0.4)

    def _winter_career():
        """A fresh career moved into the winter break (relation
        changes only happen there), with any bot proposals cleared."""
        c = game.build_new_career("Test", "Matthew")
        game.end_of_season(c, rng=seeded(5))
        assert c["phase"] == "winter_break"
        c["relation_proposals"] = []
        return c

    career = _winter_career()
    redbull = team_by_name(career, "Red Bull")
    aston = team_by_name(career, "Aston Martin")
    redbull["team_rep"] = 0.0
    # Red Bull (perf 175) proposing to supply Aston (perf 125):
    # the base rule accepts (supplier is faster), so the roll decides.
    ok, why = game.propose_relation(career, redbull["id"], aston["id"],
                                    "customer", redbull["id"], 2,
                                    rng=_FixedRng(0.0))
    assert ok and "accepted" in why, why
    assert aston["sup_stage"] == 1
    career2 = _winter_career()
    redbull2 = team_by_name(career2, "Red Bull")
    aston2 = team_by_name(career2, "Aston Martin")
    redbull2["team_rep"] = 0.0
    ok, why = game.propose_relation(career2, redbull2["id"], aston2["id"],
                                    "customer", redbull2["id"], 2,
                                    rng=_FixedRng(0.5))
    assert ok and "declined" in why, why
    assert aston2["sup_stage"] == 0
    # At rep 25 the factor is 1.0, so p = 0.75 and the roll decides
    # (a 0.5 roll accepts; a 0.99 roll would decline).
    career3 = _winter_career()
    redbull3 = team_by_name(career3, "Red Bull")
    aston3 = team_by_name(career3, "Aston Martin")
    redbull3["team_rep"] = 25.0
    ok, why = game.propose_relation(career3, redbull3["id"], aston3["id"],
                                    "customer", redbull3["id"], 2,
                                    rng=_FixedRng(0.5))
    assert ok and "accepted" in why, why


def test_winter_break_recap_2028():
    """v0.7.0: the winter-break recap for the Season 3 career —
    Top 8 drivers (Hamilton 143 first), Top 5 constructors
    (Ferrari 221 first), and 12 round winners starting with
    Antonelli in Bahrain."""
    career = _load_s3_save()
    if career is None:
        pytest.skip("Season 3 save not present in this checkout")
    rc = game.winter_break_recap(career)
    assert rc["year"] == 2028
    assert len(rc["drivers"]) == 8
    assert rc["drivers"][0]["name"] == "Lewis Hamilton"
    assert rc["drivers"][0]["points"] == 143
    assert rc["drivers"][1]["name"] == "Max Verstappen"
    assert len(rc["teams"]) == 5
    assert rc["teams"][0]["name"] == "Ferrari"
    assert rc["teams"][0]["points"] == 221
    assert rc["teams"][1]["name"] == "Mercedes"
    assert len(rc["winners"]) == 12
    assert rc["winners"][0] == {"round": 1, "circuit": "Bahrain Grand Prix",
                                "driver": "Kimi Antonelli", "team": "Mercedes",
                                "team_id": team_by_name(career, "Mercedes")["id"]}
    assert [w["driver"] for w in rc["winners"]] == [
        "Kimi Antonelli", "Fernando Alonso", "Fernando Alonso",
        "Fernando Alonso", "Kimi Antonelli", "George Russell",
        "Lewis Hamilton", "Oliver Bearman", "Oliver Bearman",
        "Oliver Bearman", "Charles Leclerc", "Isack Hadjar"]
    # The recap also rides along in the winter-break public state.
    state = game.public_state(career, True, None)
    assert state["wb_recap"]["year"] == 2028
    assert state["wb_recap"]["drivers"][0]["points"] == 143


def test_season_snapshots_reconstructed_from_history():
    """v0.7.0: migrating the Season 3 save rebuilds one snapshot per
    completed season — 2028 from last_season, 2026/2027 from
    race_history: Verstappen 261 in 2026 (Red Bull 329) and 210 in
    2027 (Red Bull 300), Hamilton 143 in 2028 (Ferrari 221)."""
    career = _load_s3_save()
    if career is None:
        pytest.skip("Season 3 save not present in this checkout")
    snaps = career["season_snapshots"]
    assert [s["year"] for s in snaps] == [2026, 2027, 2028]
    by_year = {s["year"]: s for s in snaps}
    s26, s27, s28 = by_year[2026], by_year[2027], by_year[2028]
    assert s26["drivers"][0]["name"] == "Max Verstappen"
    assert s26["drivers"][0]["points"] == 261
    assert s26["drivers"][0]["wins"] == 9
    assert s26["teams"][0]["name"] == "Red Bull"
    assert s26["teams"][0]["points"] == 329
    assert s27["drivers"][0]["name"] == "Max Verstappen"
    assert s27["drivers"][0]["points"] == 210
    assert s27["teams"][0]["name"] == "Red Bull"
    assert s27["teams"][0]["points"] == 300
    assert s28["drivers"][0]["name"] == "Lewis Hamilton"
    assert s28["drivers"][0]["points"] == 143
    assert s28["teams"][0]["name"] == "Ferrari"
    assert s28["teams"][0]["points"] == 221
    # A migrated save round-trips: migrating twice changes nothing.
    again = migrate_save(json.loads(json.dumps(career)))
    assert again["season_snapshots"] == career["season_snapshots"]


def test_real_standings_load_and_season_detail():
    """v0.7.0: the baked-in real 2010–2025 standings — 16 seasons,
    371 driver rows, 169 constructor rows. 2025 shows Norris 423
    and McLaren 833; Lotterer's 2014 row has a null pos; 2021 shows
    the .5 points (Verstappen 395.5). Unknown years and in-game
    years with no snapshot return None."""
    assert len(game.REAL_STANDINGS) == 16
    assert sum(len(v["drivers"]) for v in game.REAL_STANDINGS.values()) == 371
    assert sum(len(v["constructors"]) for v in game.REAL_STANDINGS.values()) == 169
    career = game.build_new_career("Test", "Matthew")
    d25 = game.get_season_detail(career, 2025)
    assert d25["source"] == "real"
    assert d25["drivers"][0] == {"pos": 1, "name": "Lando Norris",
                                 "team": "McLaren", "team_id": None,
                                 "points": 423.0, "wins": 7}
    assert d25["constructors"][0] == {"pos": 1, "name": "McLaren",
                                      "points": 833.0, "wins": 14}
    d14 = game.get_season_detail(career, 2014)
    lotterer = next(d for d in d14["drivers"] if d["name"] == "André Lotterer")
    assert lotterer["pos"] is None and lotterer["points"] == 0.0
    d21 = game.get_season_detail(career, 2021)
    verstappen = next(d for d in d21["drivers"] if d["name"] == "Max Verstappen")
    assert verstappen["points"] == 395.5
    assert game.get_season_detail(career, 2009) is None
    assert game.get_season_detail(career, 2026) is None  # no season played yet
    assert game.get_season_detail(career, "not a year") is None


def test_season_detail_in_game_after_rollover():
    """v0.7.0: after a season ends, its snapshot is the season
    detail — full drivers' and constructors' standings, in order."""
    career = game.build_new_career("Test", "Matthew")
    game.end_of_season(career, rng=seeded(5))
    d26 = game.get_season_detail(career, 2026)
    assert d26 is not None and d26["source"] == "game"
    assert d26["drivers"][0]["name"] == "Lando Norris"
    assert d26["drivers"][0]["pos"] == 1
    assert d26["constructors"][0]["name"] == "McLaren"
    assert d26["constructors"][0]["pos"] == 1
    assert len(d26["drivers"]) == 22 and len(d26["constructors"]) == 11
    assert game.get_season_detail(career, 2027) is None

def test_ws_season_detail_and_no_team_rep_on_wire():
    """v0.7.0: over the real WebSocket, a season_detail request for
    a real year returns the full standings (2025: Norris 423,
    McLaren 833); an in-game year with no snapshot yet errors; and
    no room_state broadcast ever carries team_rep."""
    from fastapi.testclient import TestClient
    from server import app

    client = TestClient(app)

    def recv_until(ws, want, limit=40, check=None):
        for _ in range(limit):
            m = ws.receive_json()
            if m.get("type") == want and (check is None or check(m)):
                return m
        raise AssertionError(f"never got {want}")

    with client.websocket_connect("/ws") as host_ws:
        host_ws.send_json({"type": "create_room", "nickname": "Matthew",
                           "colour": "#e74c3c"})
        joined = recv_until(host_ws, "joined")
        host_ws.send_json({"type": "new_career", "name": "WS Career"})
        state = recv_until(host_ws, "room_state",
                           check=lambda m: m["room"]["career"] is not None)
        career = state["room"]["career"]
        assert all("team_rep" not in t for t in career["teams"])
        assert "team_rep" not in json.dumps(career)
        team_id = career["teams"][0]["id"]
        host_ws.send_json({"type": "choose_team", "team_id": team_id})
        host_ws.send_json({"type": "start_career"})
        recv_until(host_ws, "room_state",
                   check=lambda m: m["room"]["started"] is True)
        # A real year: full standings, on demand.
        host_ws.send_json({"type": "season_detail", "year": 2025})
        detail = recv_until(host_ws, "season_detail")
        assert detail["detail"]["year"] == 2025
        assert detail["detail"]["source"] == "real"
        assert detail["detail"]["drivers"][0]["name"] == "Lando Norris"
        assert detail["detail"]["drivers"][0]["points"] == 423.0
        assert detail["detail"]["constructors"][0]["name"] == "McLaren"
        assert detail["detail"]["constructors"][0]["points"] == 833.0
        # An in-game year with no snapshot yet: a clean error.
        host_ws.send_json({"type": "season_detail", "year": 2026})
        err = recv_until(host_ws, "error")
        assert "No standings recorded" in err["message"]


# ---------------------------------------------------------------------------
# v0.7.1 — random driver illness / injury, the driver-reputation
# redesign, the F2 champion boost, F2 drivers promoted mid-season
# staying in the F2 standings, re-signing an expired driver from the
# Drivers tab, and the compact driver market.
# ---------------------------------------------------------------------------

class _ScriptedIllnessRng:
    """Feeds roll_illness a scripted random() sequence (one value per
    main driver, in the order roll_illness visits them), a fixed
    duration for randint, and the first option for choice — so an
    illness can be forced on exactly one driver, deterministically
    (the 0.5% roll itself is covered by the statistical test below)."""

    def __init__(self, rolls, duration=1):
        self.rolls = list(rolls)
        self.duration = duration

    def random(self):
        return self.rolls.pop(0) if self.rolls else 0.99

    def randint(self, a, b):
        return self.duration

    def choice(self, seq):
        return seq[0]


def _illness_rolls_for(career, ill_driver_id):
    """A rolls list for _ScriptedIllnessRng that falls ill only on
    ill_driver_id (0.0 there, 0.99 everywhere else), in the order
    roll_illness visits the main drivers (teams in order, seat 1
    then seat 2)."""
    rolls = []
    for team in career["teams"]:
        for seat in (1, 2):
            did = team["driver1_id"] if seat == 1 else team["driver2_id"]
            rolls.append(0.0 if did == ill_driver_id else 0.99)
    return rolls


def _raced_ids(career):
    return [r["driver_id"] for r in career.get("last_race") or []]


def test_illness_probability_about_half_percent():
    """v0.7.1: each F1 main driver has a 0.5% chance per race round —
    about one incident a season across the whole 22-driver grid.
    Over 200 seeded careers (4,400 driver-rounds) the count should
    sit near the expected 22, well inside 5–40."""
    total = 0
    for seed in range(200):
        career = game.build_new_career("Test", "Matthew")
        total += len(game.roll_illness(career, rng=random.Random(seed)))
    assert 5 <= total <= 40, total


def test_illness_blocks_driver_substitute_races_then_returns():
    """v0.7.1: a forced 2-race illness (McLaren's Norris, round 1)
    takes the driver out of the seat — 0 points, no f1_races, no rep
    change — while the best T&R driver stands in and races as a
    normal main driver; when the illness ends the driver is back in
    the seat and the substitute is back in T&R."""
    from f1.rules import ILLNESS_CHANCE
    assert ILLNESS_CHANCE == 0.005
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    norris = game.get_driver(career, mclaren["driver1_id"])
    assert norris["name"] == "Lando Norris"
    trs_before = game.team_tr_drivers(career, mclaren)
    assert len(trs_before) >= 2
    # The provisional stand-in is the highest-reputation fit T&R
    # driver (same order as the code: rep desc, then name).
    best_tr = sorted(trs_before, key=lambda d: (-d["rep"], d["name"]))[0]
    rep_before = norris["rep"]
    newly = game.roll_illness(
        career, rng=_ScriptedIllnessRng(
            _illness_rolls_for(career, norris["id"]), duration=2))
    assert newly == [norris]
    # The ill driver is out of the seat, in T&R, with the illness
    # recorded — and the announcement names them and the length.
    assert norris["status"] == "tr"
    assert norris["ill_until_round"] == 2
    assert norris["ill_label"] == "ill"
    assert norris["ill_seat"] == 1
    assert mclaren["driver1_id"] == best_tr["id"]
    assert best_tr["status"] == "f1"
    assert best_tr["ill_sub_for"] == norris["id"]
    assert best_tr["ill_sub_from"] == "tr"
    assert norris["ill_sub_id"] == best_tr["id"]
    assert any("Lando Norris is ill and will miss the next 2 races"
               in line for line in _ttexts(career))
    assert any(best_tr["name"] in line and "standing in" in line
               for line in _ttexts(career))
    # Round 1: the substitute races (finisher or DNF row), the ill
    # driver does not — no f1_races, no rep change for them.
    game.run_race_weekend(career, rng=seeded(11))
    assert best_tr["id"] in _raced_ids(career)
    assert norris["id"] not in _raced_ids(career)
    assert norris["f1_races"] == 0
    assert best_tr["f1_races"] == 1
    assert norris["rep"] == rep_before
    game.complete_race_weekend(career, rng=seeded(12))
    assert career["round"] == 2
    # Round 2: still out.
    assert norris["status"] == "tr" and norris["ill_until_round"] == 2
    game.run_race_weekend(career, rng=seeded(13))
    assert norris["id"] not in _raced_ids(career)
    assert best_tr["id"] in _raced_ids(career)
    assert norris["f1_races"] == 0
    assert best_tr["f1_races"] == 2
    game.complete_race_weekend(career, rng=seeded(14))
    # Round 3: back in the seat, the substitute back in T&R, and
    # every illness field cleared on both drivers.
    assert career["round"] == 3
    assert norris["status"] == "f1"
    # Either seat: after each race the two seats re-sort by
    # team_points, so the substitute (and the returning driver) may
    # sit in seat 2.
    assert norris["id"] in (mclaren["driver1_id"], mclaren["driver2_id"])
    assert norris["ill_until_round"] is None
    assert norris["ill_label"] is None
    assert norris["ill_seat"] is None
    assert norris["ill_sub_id"] is None
    assert best_tr["status"] == "tr"
    assert best_tr["ill_sub_for"] is None
    assert best_tr["ill_sub_from"] is None
    assert norris["contract_left"] > 0  # the contract was untouched


def test_illness_no_tr_auto_fills_from_worst_free_agents():
    """v0.7.1: a team with no T&R drivers fills an ill driver's seat
    with one of the five worst-reputation free agents (the same rule
    as the empty-seat auto-fill), on a 1-year contract — and when the
    driver returns, the fill goes back to the free-agent pool."""
    career = game.build_new_career("Test", "Matthew")
    aston = team_by_name(career, "Aston Martin")
    alonso = game.get_driver(career, aston["driver1_id"])
    # Strip Aston's T&R drivers (they leave the team entirely).
    for tr in game.team_tr_drivers(career, aston):
        tr["status"] = "free"
        tr["team_id"] = None
        tr["last_team_id"] = None
        tr["contract_left"] = 0
    assert not game.team_tr_drivers(career, aston)
    worst_rep = min(d["rep"] for d in game.free_agents(career))
    newly = game.roll_illness(
        career, rng=_ScriptedIllnessRng(
            _illness_rolls_for(career, alonso["id"]), duration=1))
    assert newly == [alonso]
    assert alonso["status"] == "tr" and alonso["ill_until_round"] == 1
    sub = game.get_driver(career, aston["driver1_id"])
    assert sub is not None and sub["id"] != alonso["id"]
    assert sub["status"] == "f1"
    assert sub["ill_sub_for"] == alonso["id"]
    assert sub["ill_sub_from"] == "free"
    # The scripted choice takes the first of the five worst-rep free
    # agents — the lowest-rep free agent of all.
    assert sub["rep"] == worst_rep
    assert sub["contract_left"] == 1
    # The fill races, then the driver returns and the fill is
    # released back to the free-agent pool.
    game.run_race_weekend(career, rng=seeded(21))
    assert sub["id"] in _raced_ids(career)
    assert alonso["id"] not in _raced_ids(career)
    game.complete_race_weekend(career, rng=seeded(22))
    assert career["round"] == 2
    assert alonso["status"] == "f1"
    assert aston["driver1_id"] == alonso["id"]
    assert sub["status"] == "free"
    assert sub["team_id"] is None
    assert sub["contract_left"] == 0
    assert sub["ill_sub_for"] is None


def test_illness_through_round_12_restored_at_season_end():
    """v0.7.1: an illness running to the final round does not carry
    across the season — the rollover brings the driver back into
    their seat (force restore), with every illness field cleared."""
    career = game.build_new_career("Test", "Matthew")
    for _ in range(11):
        game.run_race_weekend(career, rng=seeded(41))
        game.complete_race_weekend(career, rng=seeded(42))
    assert career["round"] == 12 and career["phase"] == "round"
    mclaren = team_by_name(career, "McLaren")
    norris = game.get_driver(career, mclaren["driver1_id"])
    sub = game.get_driver(career, mclaren["driver2_id"])
    game.roll_illness(career, rng=_ScriptedIllnessRng(
        _illness_rolls_for(career, norris["id"]), duration=1))
    assert norris["ill_until_round"] == 12
    assert norris["status"] == "tr"
    for d in career["drivers"]:
        d["age"] = 25
        d["cha"] = 0
    game.end_of_season(career, rng=seeded(43))
    assert career["phase"] == "winter_break"
    assert norris["status"] == "f1"
    assert norris["id"] in (mclaren["driver1_id"], mclaren["driver2_id"])
    assert norris["ill_until_round"] is None
    assert norris["ill_sub_id"] is None
    # The substitute is back in T&R (or signed on elsewhere — never
    # stranded in the wrong seat).
    assert sub["status"] in ("tr", "f1", "free")


def test_both_main_drivers_ill_at_once():
    """v0.7.1: both main drivers can fall ill at once (very rare) —
    both seats get substitutes by the same rules, and both drivers
    return when their illnesses end."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    norris = game.get_driver(career, mclaren["driver1_id"])
    piastri = game.get_driver(career, mclaren["driver2_id"])
    rolls = [0.0 if did in (norris["id"], piastri["id"]) else 0.99
             for team in career["teams"] for did in
             (team["driver1_id"], team["driver2_id"])]
    newly = game.roll_illness(career, rng=_ScriptedIllnessRng(rolls, duration=1))
    assert set(d["id"] for d in newly) == {norris["id"], piastri["id"]}
    assert norris["ill_until_round"] == 1 and piastri["ill_until_round"] == 1
    assert mclaren["driver1_id"] not in (norris["id"], piastri["id"])
    assert mclaren["driver2_id"] not in (norris["id"], piastri["id"])
    game.run_race_weekend(career, rng=seeded(51))
    assert norris["id"] not in _raced_ids(career)
    assert piastri["id"] not in _raced_ids(career)
    game.complete_race_weekend(career, rng=seeded(52))
    assert norris["status"] == "f1" and piastri["status"] == "f1"
    assert norris["id"] in (mclaren["driver1_id"], mclaren["driver2_id"])
    assert piastri["id"] in (mclaren["driver1_id"], mclaren["driver2_id"])


def test_illness_guards_and_human_sub_swap():
    """v0.7.1: an ill driver cannot be released, signed or swapped
    back into a seat while they recover; a human swapping the
    substitute for another T&R driver passes the stand-in role to
    the new driver (the ill driver's seat stays covered)."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    norris = game.get_driver(career, mclaren["driver1_id"])
    trs_before = game.team_tr_drivers(career, mclaren)
    best_tr = sorted(trs_before, key=lambda d: (-d["rep"], d["name"]))[0]
    other_tr = sorted(trs_before, key=lambda d: (d["rep"], d["name"]))[0]
    assert best_tr["id"] != other_tr["id"]
    game.roll_illness(career, rng=_ScriptedIllnessRng(
        _illness_rolls_for(career, norris["id"]), duration=1))
    ok, why = game.release_driver(career, mclaren["id"], norris["id"])
    assert not ok and "ill" in why
    ok, why = game.sign_driver(career, mclaren["id"], norris["id"], 1)
    assert not ok and "ill" in why
    ok, why = game.swap_main_tr(career, mclaren["id"], 1, norris["id"])
    assert not ok and "ill" in why
    # The human swaps the substitute for another T&R driver (free,
    # same mechanism as the normal T&R swap): the new driver becomes
    # the stand-in, the old one goes back to T&R.
    ok, why = game.swap_main_tr(career, mclaren["id"], 1, other_tr["id"])
    assert ok, why
    assert mclaren["driver1_id"] == other_tr["id"]
    assert other_tr["status"] == "f1"
    assert other_tr["ill_sub_for"] == norris["id"]
    assert other_tr["ill_sub_from"] == "tr"
    assert norris["ill_sub_id"] == other_tr["id"]
    assert best_tr["status"] == "tr"
    assert best_tr["ill_sub_for"] is None
    # ...and the ill driver still returns to the seat afterwards.
    game.run_race_weekend(career, rng=seeded(31))
    game.complete_race_weekend(career, rng=seeded(32))
    assert norris["id"] in (mclaren["driver1_id"], mclaren["driver2_id"])
    assert norris["status"] == "f1"
    assert other_tr["status"] == "tr"
    assert other_tr["ill_sub_for"] is None


def test_teammate_elo_matthew_example_zero_sum_and_quali():
    """v0.7.1 (a): the teammate Elo — a 2.12 driver losing to a 4.41
    teammate loses almost nothing (E ~ 0.995), but beating the 4.41
    gains almost the full K; the change is zero-sum; qualifying K is
    half the race K. And a pair where either teammate DNF'd moves
    neither rep (a mechanical DNF is not the driver's fault)."""
    from f1.rules import (
        DRIVER_REP_K_QUALI, DRIVER_REP_K_RACE, elo_expected,
        teammate_elo_delta,
    )
    from f1 import race as race_mod
    # The expectation: 2.12 vs 4.41 expects ~0.005.
    e = elo_expected(2.12, 4.41)
    assert 0.004 < e < 0.006
    # Losing to the 4.41 costs the 2.12 almost nothing.
    loss = teammate_elo_delta(2.12, 4.41, 0.0, DRIVER_REP_K_RACE)
    assert -0.002 < loss < 0.0
    # Beating the 4.41 gains almost the full K.
    win = teammate_elo_delta(2.12, 4.41, 1.0, DRIVER_REP_K_RACE)
    assert 0.07 < win < 0.08
    # Zero-sum: applied to two drivers, the gains cancel exactly.
    from f1.models import make_driver
    a = make_driver({"name": "A Driver", "age": 25, "perf": 90, "rep": 2.12,
                     "cha": 0, "contract": 2, "mv": 2.0, "salary": 2.0})
    b = make_driver({"name": "B Driver", "age": 25, "perf": 90, "rep": 4.41,
                     "cha": 0, "contract": 2, "mv": 2.0, "salary": 2.0})
    race_mod.apply_teammate_elo(a, b, 1, 2, DRIVER_REP_K_RACE)
    assert abs((a["rep"] - 2.12) + (b["rep"] - 4.41)) < 1e-12
    assert a["rep"] > 2.12 and b["rep"] < 4.41
    # Qualifying K is half the race K for the same outcome.
    a2 = make_driver({"name": "A Driver", "age": 25, "perf": 90, "rep": 2.12,
                      "cha": 0, "contract": 2, "mv": 2.0, "salary": 2.0})
    b2 = make_driver({"name": "B Driver", "age": 25, "perf": 90, "rep": 4.41,
                      "cha": 0, "contract": 2, "mv": 2.0, "salary": 2.0})
    race_mod.apply_teammate_elo(a2, b2, 1, 2, DRIVER_REP_K_QUALI)
    assert abs((a2["rep"] - 2.12) - (a["rep"] - 2.12) / 2) < 1e-12
    # Both teammates DNF: neither rep moves (through the real race
    # path, with other finishers so the race is not empty).
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    norris = game.get_driver(career, mclaren["driver1_id"])
    piastri = game.get_driver(career, mclaren["driver2_id"])
    norris["rep"] = piastri["rep"] = 3.0
    others = [d for d in game.race_mod.driver_standings(career)
              if d["id"] not in (norris["id"], piastri["id"])][:2]
    entries = [(others[0], team_by_name(career, "Ferrari")),
               (others[1], team_by_name(career, "Red Bull"))]
    game.race_mod.apply_race_results(
        career, entries, [25, 18], dnf_entries=[(norris, mclaren),
                                                (piastri, mclaren)])
    assert norris["rep"] == 3.0 and piastri["rep"] == 3.0


def test_finish_rep_vs_team_context():
    """v0.7.1 (b): the base gain by finish, multiplied by the team
    factor — a win from a 5th-place team is worth 1.6x a win from
    the 1st-place team, and the factor caps at 2.5x from an
    11th-place team. The teammate Elo is skipped by making the
    winner's teammate DNF, so the finish component is isolated."""
    from f1 import race as race_mod

    def _win_from(team_pos, rep=3.0):
        career = game.build_new_career("Test", "Matthew")
        teams = career["teams"]
        # Constructors' order going into the race: P1 gets 200, P2
        # 180, ... down to P11 with 0 — the winner's team sits at
        # team_pos.
        for i, t in enumerate(teams):
            t["points"] = (11 - (i + 1)) * 20.0
        winner_team = teams[team_pos - 1]
        winner = game.get_driver(career, winner_team["driver1_id"])
        team_mate = game.get_driver(career, winner_team["driver2_id"])
        winner["rep"] = rep
        others = [d for d in game.race_mod.driver_standings(career)
                  if d["id"] not in (winner["id"], team_mate["id"])][:1]
        other_team = game.get_team(career, others[0]["team_id"])
        entries = [(winner, winner_team), (others[0], other_team)]
        race_mod.apply_race_results(
            career, entries, [25, 18],
            dnf_entries=[(team_mate, winner_team)])
        return winner["rep"] - rep

    assert abs(_win_from(1) - 0.10) < 1e-9
    assert abs(_win_from(5) - 0.16) < 1e-9  # 0.10 * 1.6
    assert abs(_win_from(11) - 0.25) < 1e-9  # 0.10 * 2.5 (capped)
    # A P6 finish from a P5 team: base 0.01 * 1.6 = 0.016.
    career = game.build_new_career("Test", "Matthew")
    for i, t in enumerate(career["teams"]):
        t["points"] = (11 - (i + 1)) * 20.0
    team5 = career["teams"][4]
    d = game.get_driver(career, team5["driver1_id"])
    d["rep"] = 3.0
    mate = game.get_driver(career, team5["driver2_id"])
    others = [x for x in game.race_mod.driver_standings(career)
              if x["id"] not in (d["id"], mate["id"])][:5]
    entries = [(x, game.get_team(career, x["team_id"])) for x in others]
    entries.append((d, team5))
    game.race_mod.apply_race_results(
        career, entries, [25, 18, 15, 12, 10, 8],
        dnf_entries=[(mate, team5)])
    assert abs(d["rep"] - 3.0 - 0.016) < 1e-9


def _season_end_career(champion_team_pos, champion_rep=3.0):
    """A career ready for end_of_season with a known champion: the
    drivers' leader sits on the team at champion_team_pos (1 = the
    constructors' champions, 2 = a team that did not win them), all
    other drivers are young (no retirements) and on 0 rep movement
    from the F2 side. Returns (career, champion)."""
    career = game.build_new_career("Test", "Matthew")
    teams = career["teams"]
    # Constructors' order: team 0 first, team 1 second, ...
    for i, t in enumerate(teams):
        t["points"] = (11 - (i + 1)) * 20.0
    champ_team = teams[champion_team_pos - 1]
    others_team = teams[1 if champion_team_pos == 1 else 0]
    champ = game.get_driver(career, champ_team["driver1_id"])
    runner = game.get_driver(career, others_team["driver1_id"])
    champ["points"] = 100
    runner["points"] = 80
    for d in career["drivers"]:
        d["rep"] = 3.0
        d["age"] = 25
        d["cha"] = 0
    champ["rep"] = champion_rep
    return career, champ


def test_season_end_rep_standings_vs_team_and_champion_bonus():
    """v0.7.1 (c): standings vs team — a driver above their team's
    place gains, below loses, capped at +/-0.15 — and the champion
    gains +0.5 when their team won the constructors', +0.75 when
    they did not."""
    # Champion's team won the constructors': +0.5, no standings
    # term (P1 driver, P1 team).
    career, champ = _season_end_career(1)
    game.end_of_season(career, rng=seeded(7))
    assert abs(champ["rep"] - 3.5) < 1e-9
    # A driver P2 in a P5 team gains (5-2)*0.02 = +0.06; a driver
    # P10 in a P2 team loses (2-10)*0.02 = -0.16, capped at -0.15.
    career2 = game.build_new_career("Test", "Matthew")
    teams = career2["teams"]
    for i, t in enumerate(teams):
        t["points"] = (11 - (i + 1)) * 20.0
    drivers = [game.get_driver(career2, t["driver1_id"]) for t in teams]
    drivers[0]["points"] = 100  # P1, team P1
    # P2 driver on the P5 team, P10 driver on the P2 team.
    drivers[4]["points"] = 90
    drivers[1]["points"] = 10
    for i, d in enumerate(drivers):
        if i not in (0, 1, 4):
            d["points"] = 50 - i
    for d in career2["drivers"]:
        d["rep"] = 3.0
        d["age"] = 25
        d["cha"] = 0
    game.end_of_season(career2, rng=seeded(7))
    assert abs(drivers[4]["rep"] - 3.06) < 1e-9
    assert abs(drivers[1]["rep"] - 2.85) < 1e-9
    # Champion's team did NOT win the constructors': +0.75, plus
    # the standings term (P2 team, P1 driver) = +0.02.
    career3, champ3 = _season_end_career(2)
    game.end_of_season(career3, rng=seeded(7))
    assert abs(champ3["rep"] - 3.77) < 1e-9


def test_rep_clamp_one_to_five():
    """v0.7.1: rep is clamped to [1.0, 5.0] after every change — a
    champion at 4.99 does not pass 5.0, and a big season-end loss
    from 1.01 does not go below 1.0."""
    from f1 import race as race_mod
    d = {"rep": 4.99}
    race_mod.adjust_driver_rep(d, 0.75)
    assert d["rep"] == 5.0
    d = {"rep": 1.01}
    race_mod.adjust_driver_rep(d, -0.15)
    assert d["rep"] == 1.0
    # Through the real season end: a 4.99 champion stays at 5.0.
    career, champ = _season_end_career(1, champion_rep=4.99)
    game.end_of_season(career, rng=seeded(7))
    assert champ["rep"] == 5.0


def test_f2_champion_rep_boost():
    """v0.7.1: the F2 champion gets +0.5 rep at the rollover, on top
    of the F2 race-result rep earned during the season — and joins
    the free-agent pool."""
    career = game.build_new_career("Test", "Matthew")
    f2 = game.f2_drivers(career)
    champ = f2[0]
    champ["points"] = 100
    champ["rep"] = 3.0
    for d in f2[1:]:
        d["points"] = 0
    for d in career["drivers"]:
        d["age"] = 25
        d["cha"] = 0
    game.end_of_season(career, rng=seeded(9))
    assert champ["rep"] == 3.5
    assert champ["status"] == "free"


def test_f2_tr_driver_keeps_racing_f2_and_can_win_title():
    """v0.7.3 (replaces the v0.7.1 freeze): an F2 driver signed as
    Test & Reserve mid-season KEEPS racing in F2 — they stay in the
    F2 standings with a live tally, score more F2 points, and stay
    eligible for the F2 title. At the rollover the title goes to the
    best F2 driver INCLUDING the T&R driver, who is then forced out
    of F2 but keeps their F1 T&R contract. No frozen tally, no
    '↑ F1' tag anywhere in the page."""
    from f1 import race as race_mod
    career = game.build_new_career("Test", "Matthew")
    f2 = game.f2_drivers(career)
    # v0.8.0: f2[0] is an academy junior (a poach needs a meeting),
    # so promote a plain F2 driver instead — promoted and runner
    # are two DISTINCT plain drivers.
    plain = [d for d in f2 if not d.get("academy_team_id")]
    promoted, runner = plain[0], plain[1]
    for d in f2:
        d["points"] = 0
    promoted["points"] = 100
    promoted["season_wins"] = 3
    promoted["best_pos"] = 1
    promoted["best_finish_round"] = 1
    runner["points"] = 50
    mclaren = team_by_name(career, "McLaren")
    mclaren["money"] = 100.0
    ok, why = game.sign_tr_driver(career, mclaren["id"], promoted["id"], 2)
    assert ok, why
    assert promoted["status"] == "tr" and promoted.get("f2_tr") is True
    assert not promoted.get("f2_left")  # the v0.7.1 freeze field is gone
    # Still listed in F2, at the top, with the live tally.
    standings = race_mod.f2_standings(career)
    assert standings[0]["id"] == promoted["id"]
    assert standings[0]["points"] == 100
    assert standings[1]["id"] == runner["id"]
    # The F2 sim still races them — they keep scoring F2 points.
    game.run_race_weekend(career, rng=seeded(5))
    assert promoted["points"] >= 100
    assert promoted in race_mod.f2_standings(career)
    assert promoted.get("f2_tr") is True
    # At the rollover the title goes to the best F2 driver — the
    # T&R driver themselves, with 100+ points.
    for d in career["drivers"]:
        d["age"] = 25
        d["cha"] = 0
    rep_before = promoted["rep"]
    game.end_of_season(career, rng=seeded(3))
    assert any("won F2" in line and promoted["name"] in line
               for line in _ttexts(career))
    assert promoted["rep"] == rep_before + 0.5
    assert promoted["status"] == "tr"  # keeps the F1 T&R contract
    assert promoted.get("f2_tr") is False  # but leaves the feeder series
    assert promoted["points"] == 0  # fresh season tally
    # The old v0.7.1 tag is gone from the page.
    html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
    assert "↑ F1" not in html
    assert "f2_left" not in html


def test_resign_offer_counts_toward_limit_and_not_guaranteed():
    """v0.7.1: re-signing an expired driver is a normal winter-break
    offer — it counts toward the 3-offers-per-round limit (a 4th
    offer is refused), and it does NOT guarantee the signing: a
    better-placed constructors' team offering the same driver wins
    them."""
    career = game.build_new_career("Test", "Matthew")
    teams = career["teams"]
    # Constructors' order: McLaren P1, Aston Martin P5.
    for i, t in enumerate(teams):
        t["points"] = (11 - (i + 1)) * 20.0
    aston = team_by_name(career, "Aston Martin")
    mclaren = team_by_name(career, "McLaren")
    alonso = game.get_driver(career, aston["driver1_id"])
    norris = game.get_driver(career, mclaren["driver1_id"])
    alonso["age"] = 35  # no retirement at the rollover
    alonso["contract_left"] = 1  # expires at the rollover
    norris["contract_left"] = 1  # expires too, so McLaren has an
    # empty seat to offer Alonso (a winter-break offer to an occupied
    # seat is skipped at resolution — offers fill empty seats).
    for d in career["drivers"]:
        d["age"] = 25 if d is not alonso else 35
        d["cha"] = 0
    game.end_of_season(career, rng=seeded(11))
    assert career["phase"] == "winter_break"
    # Alonso expired: a free agent, still tied to Aston Martin, in
    # the seat he held. The rollover shifted Stroll up into Seat 1,
    # so Aston's empty seat is Seat 2.
    assert alonso["status"] == "free"
    assert alonso["last_team_id"] == aston["id"]
    assert alonso["last_slot"] == "seat1"
    assert alonso["id"] not in (aston["driver1_id"], aston["driver2_id"])
    assert aston["driver2_id"] is None
    assert norris["status"] == "free"
    assert mclaren["driver2_id"] is None
    # Aston's re-sign offer (into the empty Seat 2 — the v0.7.2 seat
    # picker offers every empty seat, and the driver lands in the one
    # picked) + two more offers = the 3-offer limit. A 4th is refused.
    free = game.free_agents(career)
    other1 = next(d for d in free if d["id"] != alonso["id"])
    other2 = next(d for d in free if d["id"] not in (alonso["id"], other1["id"]))
    # v0.8.0: f2[0] is an academy junior (a WB offer for one needs
    # a secret meeting) — the offer limit is the point here, so use
    # a plain F2 driver.
    f2d = next(d for d in game.f2_drivers(career)
               if not d.get("academy_team_id"))
    offers = [
        {"driver_id": alonso["id"], "target": "seat", "seat": 2, "years": 2},
        {"driver_id": other1["id"], "target": "seat", "seat": 1, "years": 2},
        {"driver_id": f2d["id"], "target": "tr", "seat": None, "years": 2},
    ]
    ok, why = game.submit_winter_offers(career, aston["id"], offers)
    assert ok, why
    ok, why = game.submit_winter_offers(
        career, aston["id"], offers + [
            {"driver_id": other2["id"], "target": "seat", "seat": 2, "years": 1}])
    assert not ok and "3 offers" in why
    # McLaren (P1) also offers Alonso for its empty Seat 2. The
    # round resolves in constructors' order — McLaren first — so the
    # re-sign does NOT keep Alonso at Aston Martin.
    ok, why = game.submit_winter_offers(career, mclaren["id"], [
        {"driver_id": alonso["id"], "target": "seat", "seat": 2, "years": 2}])
    assert ok, why
    game.resolve_winter_break_round(career, rng=seeded(1))
    assert alonso["team_id"] == mclaren["id"]
    assert alonso["status"] == "f1"
    assert mclaren["driver2_id"] == alonso["id"]
    assert aston["driver1_id"] != alonso["id"]
    assert aston["driver2_id"] != alonso["id"]
    # The Drivers tab offers the expired driver for re-signing with a
    # seat picker (Seat 1 / Seat 2 / T&R) — an occupied seat is a
    # disabled button naming its driver, empty seats are selectable,
    # and an unknown old seat says so instead of defaulting to Seat 1.
    html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
    assert "Expired — re-sign" in html
    assert "data-resign" in html
    # The seat buttons are built by a template ("Re-sign Seat ${s}"),
    # T&R is a literal button; an occupied seat is a disabled button
    # naming its driver, an unknown old seat says so.
    assert "Re-sign Seat ${s}" in html
    assert "Re-sign T&R" in html
    assert "Occupied by" in html
    assert "seat unknown" in html


def test_market_compact_grouped_headers():
    """v0.7.1: the driver market renders grouped by source — F1
    free agents, then F2, then other teams' drivers (poaching) —
    one line per driver, with the same columns as before.
    v0.8.1: the order flips — F1 grid (poaching) ON TOP, then free
    agents + F2 side-by-side beneath, heavily condensed (name / age
    / salary / cost only, no hidden ratings)."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright
    import subprocess, time, urllib.request
    port = 8795
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(f"http://127.0.0.1:{port}/")
            page.fill("#home-nick", "Max")
            page.click("#btn-create")
            page.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
            page.click("#btn-new-career")
            page.wait_for_selector("[data-team]", timeout=15000)
            page.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.querySelector('b') && r.querySelector('b').textContent === 'Red Bull')
                    .querySelector('[data-team]').click();
            }""")
            page.wait_for_selector("#btn-start-career:not(.hidden)", timeout=15000)
            page.click("#btn-start-career")
            page.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            page.click('[data-tab="market"]')
            page.wait_for_selector("#tab-market:not(.hidden)", timeout=15000)
            market = page.inner_text("#tab-market")
            # Group headers, uppercased by CSS (inner_text is the
            # rendered text).
            assert "F1 FREE AGENTS" in market, market
            assert "F2 (" in market, market
            assert "OTHER TEAMS' DRIVERS" in market, market
            # v0.8.1 order: the F1 grid (poaching) comes first, then
            # the free agents and F2 side-by-side beneath.
            assert market.index("F1 GRID") < market.index("F1 FREE AGENTS")
            assert market.index("F1 FREE AGENTS") < market.index("F2 (")
            assert "Cost to sign" in market
            assert "Salary/yr" in market
            # The per-row "Poaching needs a secret meeting first"
            # hint is gone (the meeting requirement itself stays —
            # the section intro still explains it once, above the
            # table). Check no driver ROW carries it.
            hint_in_rows = page.evaluate(
                """[...document.querySelectorAll('#tab-market td')]
                   .some(td => td.textContent.includes(
                       'Poaching needs a secret meeting first'))""")
            assert not hint_in_rows
            # Condensed free-agent/F2 tables: name/age/salary/cost
            # only — no Titles or Status columns; the poaching table
            # keeps its Team column.
            headers = page.evaluate(
                "[...document.querySelectorAll('.market-table')]"
                ".map(t => t.querySelector('tr').innerText)")
            assert len(headers) == 3, headers
            assert "Titles" in headers[0] and "Team" in headers[0]
            assert "Titles" not in headers[1] and "Team" not in headers[1]
            assert "Titles" not in headers[2] and "Team" not in headers[2]
            # Rows stay compact. The poaching table is full-width
            # (its rows carry 3-4 action buttons, so the bound is
            # looser than the old v0.3 rule); the condensed tables
            # sit side-by-side in narrower columns, so their action
            # buttons wrap — the bound there is looser still.
            row_h = page.evaluate(
                "document.querySelector('.market-table tr:nth-child(2)')"
                ".getBoundingClientRect().height")
            assert row_h < 150, row_h
            browser.close()
    finally:
        proc.terminate()


def test_v14_migration_adds_f2_tr_f1_tally_and_gender_fields():
    """v0.7.3 (save v14): an old save without the new driver fields
    backfills them — no T&R driver still racing F2, a zero F1 tally,
    gender M except the five real female drivers — and the frozen
    v0.7.1 F2 fields are dropped. The real Season 3 save loads with
    them (its file is never modified by loading it)."""
    from f1.saves import SAVE_VERSION, load_save, migrate_save
    assert SAVE_VERSION == 18
    career = game.build_new_career("Test", "Matthew")
    doc = json.loads(json.dumps(career))
    doc["version"] = 13
    for d in doc["drivers"]:
        for k in ("f2_tr", "f1_points", "f1_wins", "f1_podiums",
                  "f1_best_pos", "f1_best_finish_round", "gender"):
            d.pop(k, None)
        # An old save still carries the v0.7.1 frozen tally.
        d["f2_left"] = True
        d["f2_points"] = 12
        d["f2_wins"] = 1
        d["f2_best_pos"] = 3
        d["f2_best_finish_round"] = 2
    migrated = migrate_save(doc)
    for d in migrated["drivers"]:
        assert d["f2_tr"] is False
        assert d["f1_points"] == 0
        assert d["f1_wins"] == 0
        assert d["f1_podiums"] == 0
        assert d["f1_best_pos"] is None
        assert d["f1_best_finish_round"] is None
        assert "f2_left" not in d
        assert "f2_points" not in d
        assert "f2_wins" not in d
        assert "f2_best_pos" not in d
        assert "f2_best_finish_round" not in d
        expected = "F" if d["name"] in (
            "Mathilda Paatz", "Marta García", "Abbi Pulling",
            "Doriane Pin", "Payton Westcott") else "M"
        assert d["gender"] == expected, d["name"]
    path = Path(__file__).parent.parent / "saves" / "matthew-max-wugynr-s3-complete.json"
    if not path.exists():
        pytest.skip("Season 3 save not present in this checkout")
    before = path.read_bytes()
    loaded = _load_s3_save()
    assert loaded is not None
    assert loaded["phase"] == "winter_break" and loaded["year"] == 2029
    assert all(d["f2_tr"] is False for d in loaded["drivers"])
    assert all(d["gender"] in ("M", "F") for d in loaded["drivers"])
    paatz = next(d for d in loaded["drivers"]
                 if d["name"] == "Mathilda Paatz")
    assert paatz["gender"] == "F"
    boya = next(d for d in loaded["drivers"] if d["name"] == "Mari Boya")
    assert boya["gender"] == "M"  # male — verified via Wikipedia/FIA F2
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# v0.7.2 — snapshot relationships are year-correct everywhere (season
# detail, race history, champion table, winter recap), no supply
# chains, the re-sign seat picker, and the gentler retirement.
# ---------------------------------------------------------------------------

def test_v072_snapshot_carries_relationships_and_detail_uses_them():
    """v0.7.2 (1): a snapshot built at the rollover captures each
    team's supplier and supplied teams as they stood that season,
    and get_season_detail returns the SNAPSHOT's relationships even
    after the live relations have changed. A pre-v0.7.2 snapshot
    without relationship data renders with none (has_relations
    False, no supplier, no supplies) — never the live teams'."""
    career = game.build_new_career("Test", "Matthew")
    for d in career["drivers"]:
        d["age"] = 25
        d["cha"] = 0
    game.end_of_season(career, rng=seeded(11))
    assert career["phase"] == "winter_break"
    snap = next(s for s in career["season_snapshots"] if s["year"] == 2026)
    by_name = {t["name"]: t for t in snap["teams"]}
    # The 2026 grid, as it was: Mercedes supplies McLaren /
    # Williams / Alpine (customers), Red Bull has Racing Bulls
    # (junior), Ferrari supplies Haas / Cadillac (customers),
    # Aston Martin supplies nobody.
    assert {s["name"] for s in by_name["Mercedes"]["supplies"]} == {
        "McLaren", "Williams", "Alpine"}
    assert all(s["kind"] == "customer"
               for s in by_name["Mercedes"]["supplies"])
    assert by_name["Red Bull"]["supplies"] == [{
        "id": by_name["Racing Bulls"]["id"], "name": "Racing Bulls",
        "kind": "junior"}]
    assert {s["name"] for s in by_name["Ferrari"]["supplies"]} == {
        "Haas", "Cadillac"}
    assert by_name["McLaren"]["supplier_name"] == "Mercedes"
    assert by_name["McLaren"]["sup_stage"] == 1
    assert by_name["Cadillac"]["supplier_name"] == "Ferrari"
    assert by_name["Aston Martin"]["supplier_id"] is None
    assert by_name["Aston Martin"]["supplies"] == []
    # Now change the LIVE relations (Aston takes Cadillac, McLaren
    # goes independent) — the detail for 2026 must still show the
    # snapshot's relations, not the live ones.
    mclaren = team_by_name(career, "McLaren")
    cadillac = team_by_name(career, "Cadillac")
    mclaren["supplier_id"] = None
    mclaren["sup_stage"] = 0
    cadillac["supplier_id"] = team_by_name(career, "Aston Martin")["id"]
    cadillac["sup_stage"] = 1
    detail = game.get_season_detail(career, 2026)
    assert detail is not None and detail["has_relations"] is True
    det = {c["name"]: c for c in detail["constructors"]}
    assert det["McLaren"]["supplier_name"] == "Mercedes"
    assert det["Cadillac"]["supplier_name"] == "Ferrari"
    assert {s["name"] for s in det["Mercedes"]["supplies"]} == {
        "McLaren", "Williams", "Alpine"}
    assert det["Aston Martin"]["supplies"] == []
    # A pre-v0.7.2 snapshot (relationship fields stripped) renders
    # with no relationship info at all.
    career2 = game.build_new_career("Test", "Matthew")
    for d in career2["drivers"]:
        d["age"] = 25
        d["cha"] = 0
    game.end_of_season(career2, rng=seeded(11))
    snap2 = next(s for s in career2["season_snapshots"]
                 if s["year"] == 2026)
    for t in snap2["teams"]:
        for k in ("supplier_id", "supplier_name", "sup_stage", "supplies"):
            t.pop(k, None)
    detail2 = game.get_season_detail(career2, 2026)
    assert detail2 is not None and detail2["has_relations"] is False
    assert all(c["supplier_name"] is None
               for c in detail2["constructors"])
    assert all(c["supplies"] == [] for c in detail2["constructors"])


def test_v072_race_history_gp_uses_snapshot_relationships():
    """v0.7.2 (1, race history): the full-GP classification table
    renders a past race's team relationships from that year's
    season snapshot — a 2028 Cadillac shows Ferrari, never today's
    supplier — and a snapshot without relationship data renders a
    plain team name. The GP overlay, champion table and winter
    recap all resolve through the snapshot helpers."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright
    import subprocess, time, urllib.request
    port = 8792
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(f"http://127.0.0.1:{port}/")
            row = ("[{pos:1,driver:'Sergio Pérez',driver_id:'x',"
                   "team:'Cadillac',team_id:'t-cad',cha:0,gap:'LEADER',"
                   "points:25,gl:0}]")
            snap = ("{byId:{'t-cad':{id:'t-cad',name:'Cadillac',"
                    "supplier_id:'t-fer',supplier_name:'Ferrari',"
                    "sup_stage:1}}, byName:{'Cadillac':{id:'t-cad',"
                    "name:'Cadillac',supplier_id:'t-fer',"
                    "supplier_name:'Ferrari',sup_stage:1}}}")
            table = page.evaluate(
                f"window.F1UI.classificationTable({row},"
                f"{{showGL:true, teams:{snap}}})")
            assert "Cadillac" in table and "Ferrari" in table
            assert "Aston Martin" not in table
            plain = ("{byId:{'t-cad':{id:'t-cad',name:'Cadillac'}},"
                     " byName:{'Cadillac':{id:'t-cad',name:'Cadillac'}}}")
            table2 = page.evaluate(
                f"window.F1UI.classificationTable({row},"
                f"{{showGL:true, teams:{plain}}})")
            assert "Cadillac" in table2
            assert "Ferrari" not in table2
            assert "Aston Martin" not in table2
            browser.close()
    finally:
        proc.terminate()
    # The history surfaces resolve teams through the snapshot
    # helpers, not the live teams.
    html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
    assert "snapshotTeamMap(r.year)" in html
    assert "opts.teams" in html
    assert "snapshotForYear(h.year)" in html
    assert "snapshotForYear(rc.year)" in html


def test_v072_propose_to_supplier_rejected_and_stale_proposal_rejected():
    """v0.7.2 (2): a team that already supplies other teams cannot
    be proposed as a customer or junior — in either direction (as
    the customer, or as the would-be supplier's customer) — and a
    proposal sent before its target became a supplier is rejected
    when the target tries to accept it. The UI disables such
    targets with the reason."""
    career = game.build_new_career("Test", "Matthew")
    for d in career["drivers"]:
        d["age"] = 25
        d["cha"] = 0
    game.end_of_season(career, rng=seeded(11))
    assert career["phase"] == "winter_break"
    aston = team_by_name(career, "Aston Martin")
    mercedes = team_by_name(career, "Mercedes")
    audi = team_by_name(career, "Audi")
    cadillac = team_by_name(career, "Cadillac")
    # Mercedes supplies McLaren / Williams / Alpine in 2026, so
    # Aston cannot propose to supply it.
    ok, why = game.propose_relation(career, aston["id"], mercedes["id"],
                                    "customer", supplier_team_id=aston["id"],
                                    years=2)
    assert not ok and "already supplies" in why
    # The mirror direction: Mercedes cannot be Aston Martin's
    # customer either, because Mercedes itself supplies teams.
    ok, why = game.propose_relation(career, mercedes["id"], aston["id"],
                                    "customer", supplier_team_id=aston["id"],
                                    years=2)
    assert not ok and "already supplies" in why
    # Audi supplies nobody, so proposing to supply it is allowed —
    # and stays pending because Audi is a human team here.
    audi["is_ai"] = False
    ok, why = game.propose_relation(career, aston["id"], audi["id"],
                                    "customer", supplier_team_id=aston["id"],
                                    years=2)
    assert ok, why
    assert "waiting" in why
    proposal = career["relation_proposals"][-1]
    assert proposal["supplier_team_id"] == aston["id"]
    assert proposal["customer_team_id"] == audi["id"]
    # Audi then becomes a supplier itself (takes Cadillac) — the
    # pending proposal is now stale and must be rejected on accept.
    cadillac["supplier_id"] = audi["id"]
    cadillac["sup_stage"] = 1
    cadillac["relation_years"] = 2
    ok, why = game.respond_relation(career, audi["id"], proposal["id"],
                                    accept=True)
    assert not ok and "already supplies other teams now" in why
    # The UI disables targets that already supply others, with the
    # reason, for both roles.
    html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
    assert "already supplies teams" in html
    assert "data-supplies" in html
    assert "They supply me" in html
    assert "your team already supplies others" in html


def test_v072_resign_seat_picker_old_seat_occupied():
    """v0.7.2 (3): re-signing a driver whose old seat is now
    occupied lands them in the seat the player PICKS — here Seat 2,
    the empty one — not the old seat number."""
    career = game.build_new_career("Test", "Matthew")
    aston = team_by_name(career, "Aston Martin")
    alonso = game.get_driver(career, aston["driver1_id"])
    stroll = game.get_driver(career, aston["driver2_id"])
    alonso["age"] = 35  # no retirement at the rollover
    alonso["contract_left"] = 1  # expires at the rollover
    for d in career["drivers"]:
        d["age"] = 25 if d is not alonso else 35
        d["cha"] = 0
    game.end_of_season(career, rng=seeded(11))
    assert career["phase"] == "winter_break"
    # Alonso expired from Seat 1; the rollover moved Stroll up into
    # Seat 1, so Seat 1 is occupied and Seat 2 is the empty one.
    assert alonso["status"] == "free"
    assert alonso["last_slot"] == "seat1"
    assert aston["driver1_id"] == stroll["id"]
    assert aston["driver2_id"] is None
    ok, why = game.submit_winter_offers(career, aston["id"], [
        {"driver_id": alonso["id"], "target": "seat", "seat": 2,
         "years": 2}])
    assert ok, why
    game.resolve_winter_break_round(career, rng=seeded(1))
    assert alonso["team_id"] == aston["id"]
    assert alonso["status"] == "f1"
    assert aston["driver2_id"] == alonso["id"]
    assert aston["driver1_id"] == stroll["id"]
    assert alonso["contract_left"] == 2


def test_v13_migration_backfills_2026_snapshot_relationships():
    """v0.7.2 (save v13, now v15): the 2026 season snapshot backfills its
    relationship data from the 2026 grid — Mercedes -> McLaren /
    Williams / Alpine customers, Red Bull -> Racing Bulls junior,
    Ferrari -> Haas / Cadillac customers — while the 2027/2028
    snapshots keep NO relationship data (never fabricated). The
    real Season 3 save (v10) and the Season 4 Round 2 export (v12)
    migrate cleanly, and their files are never modified."""
    from f1.saves import SAVE_VERSION, load_save
    assert SAVE_VERSION == 18
    path = Path(__file__).parent.parent / "saves" / "matthew-max-wugynr-s3-complete.json"
    if not path.exists():
        pytest.skip("Season 3 save not present in this checkout")
    before = path.read_bytes()
    loaded = _load_s3_save()
    assert loaded is not None
    assert loaded["version"] == 18
    assert loaded["phase"] == "winter_break" and loaded["year"] == 2029
    snaps = {s["year"]: s for s in loaded["season_snapshots"]}
    assert {2026, 2027, 2028} <= set(snaps)
    by_name = {t["name"]: t for t in snaps[2026]["teams"]}
    assert {s["name"] for s in by_name["Mercedes"]["supplies"]} == {
        "McLaren", "Williams", "Alpine"}
    assert all(s["kind"] == "customer"
               for s in by_name["Mercedes"]["supplies"])
    assert {s["name"] for s in by_name["Red Bull"]["supplies"]} == {
        "Racing Bulls"}
    assert by_name["Red Bull"]["supplies"][0]["kind"] == "junior"
    assert {s["name"] for s in by_name["Ferrari"]["supplies"]} == {
        "Haas", "Cadillac"}
    assert by_name["McLaren"]["supplier_name"] == "Mercedes"
    assert by_name["McLaren"]["sup_stage"] == 1
    assert by_name["Racing Bulls"]["supplier_name"] == "Red Bull"
    assert by_name["Racing Bulls"]["sup_stage"] == 2
    assert by_name["Cadillac"]["supplier_name"] == "Ferrari"
    assert by_name["Aston Martin"]["supplier_id"] is None
    assert by_name["Aston Martin"]["supplies"] == []
    for y in (2027, 2028):
        for t in snaps[y]["teams"]:
            assert "supplier_id" not in t, (y, t["name"])
    assert path.read_bytes() == before
    # The Season 4 Round 2 export (v12) migrates the same way.
    path4 = Path(__file__).parent.parent / "saves" / "matthew-max-wugynr-s4-round2.json"
    if path4.exists():
        before4 = path4.read_bytes()
        loaded4 = load_save(str(path4.parent), "matthew-max-wugynr-s4-round2")
        assert loaded4 is not None
        assert loaded4["version"] == 18
        assert loaded4["year"] == 2029 and loaded4["round"] == 2
        assert loaded4["phase"] == "round"
        rb = team_by_name(loaded4, "Red Bull")
        assert abs(rb["money"] - 66.63) < 0.01
        snaps4 = {s["year"]: s for s in loaded4["season_snapshots"]}
        by4 = {t["name"]: t for t in snaps4[2026]["teams"]}
        assert by4["McLaren"]["supplier_name"] == "Mercedes"
        assert by4["Cadillac"]["supplier_name"] == "Ferrari"
        for y in (2027, 2028):
            for t in snaps4[y]["teams"]:
                assert "supplier_id" not in t, (y, t["name"])
        assert path4.read_bytes() == before4


# ---------------------------------------------------------------------------
# v0.7.3 — full-history snapshots, F2 T&R rework, team-contract matrix
# + bot probability rules, Develop message vs chart toggle, academy
# poaching fee + $3.0M poaching flat, retired-driver cleanup,
# grouped retirement rumours, winter-break-only contracts, gender
# indicators, regenerated custom colours. Save format v14.
# ---------------------------------------------------------------------------

def test_v073_snapshot_saves_full_registered_driver_list():
    """v0.7.3 (1): the season snapshot saves EVERY driver registered
    in the season's standings — main drivers, free agents with
    points, substitutes and mid-season movers (anyone who raced in
    F1 or scored) — not just the 22 main seats. Points, wins and
    team (last team) are preserved per driver."""
    from f1 import race as race_mod
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    # A free agent who raced and scored, then was dropped.
    fa = game.free_agents(career)[0]
    fa["points"] = 10
    fa["team_points"] = 10
    fa["f1_races"] = 2
    fa["season_wins"] = 0
    fa["last_team_id"] = mclaren["id"]
    # A T&R driver who substituted in a race but scored 0.
    tr = game.team_tr_drivers(career, mclaren)[0]
    tr["points"] = 0
    tr["f1_races"] = 2
    for d in career["drivers"]:
        d["age"] = 25
        d["cha"] = 0
    game.end_of_season(career, rng=seeded(9))
    rows = {r["id"]: r for r in career["last_season"]["drivers"]}
    # 22 main seats + the free agent + the substitute.
    assert len(rows) == 24
    assert rows[fa["id"]]["points"] == 10
    assert rows[fa["id"]]["team"] == "McLaren"
    assert rows[tr["id"]]["points"] == 0
    assert rows[tr["id"]]["team"] == "McLaren"
    # Order preserved: the free agent (10 pts) sits ahead of the
    # scoreless drivers.
    names = [r["name"] for r in career["last_season"]["drivers"]]
    assert names.index(fa["name"]) < names.index(tr["name"])


def test_v073_f2_tr_promotion_refused_midseason_illness_sub_only():
    """v0.7.3 (2): a T&R driver still racing in F2 cannot be
    promoted to a main seat mid-season — except as an illness
    substitute, the one in-season F1 race they may do. In the
    winter break the promotion is allowed and the F2 tally is
    cleared."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    # v0.8.0: f2[0] is an academy junior (a T&R signing is a poach
    # and needs a meeting) — use a plain F2 driver for the setup.
    f2 = next(d for d in game.f2_drivers(career)
              if not d.get("academy_team_id"))
    f2["points"] = 30
    mclaren["money"] = 100.0
    ok, why = game.sign_tr_driver(career, mclaren["id"], f2["id"], 2)
    assert ok, why
    # Mid-season promotion to an empty main seat: refused.
    piastri = game.get_driver(career, mclaren["driver2_id"])
    piastri["status"] = "free"
    piastri["team_id"] = None
    mclaren["driver2_id"] = None
    ok, why = game.promote_tr_to_main(career, mclaren["id"], f2["id"], 2)
    assert not ok and "F2" in why, why
    assert f2["status"] == "tr" and f2.get("f2_tr") is True
    # Illness stand-in: the main driver is ill, seat 1 is empty, the
    # T&R driver is the highest-rep reserve — they get the seat.
    norris = game.get_driver(career, mclaren["driver1_id"])
    f2["rep"] = 5.0
    norris["ill_until_round"] = 2
    norris["ill_seat"] = 1
    mclaren["driver1_id"] = None
    game.ensure_ill_substitutes(career, rng=seeded(1))
    assert mclaren["driver1_id"] == f2["id"]
    assert f2.get("ill_sub_for") == norris["id"]
    assert f2["status"] == "tr" and f2.get("f2_tr") is True
    # Winter break: the same driver may take a main seat for real.
    career["phase"] = "winter_break"
    mclaren["driver2_id"] = None
    ok, why = game.promote_tr_to_main(career, mclaren["id"], f2["id"], 2)
    assert ok, why
    assert f2["status"] == "f1" and f2.get("f2_tr") is False
    assert f2["points"] == 0


def test_v073_bot_prefers_high_rep_f2_drivers():
    """v0.7.3 (2, bot fix): bot promotion picks are
    probability-weighted by hidden reputation (weight = rep^2) —
    high-rep F2 drivers are favoured, but a lower-rep driver can
    still be picked (no hard cutoff)."""
    rng = seeded(7)
    top = {"id": "d-top", "name": "Top", "rep": 4.5}
    mid = {"id": "d-mid", "name": "Mid", "rep": 2.5}
    low = {"id": "d-low", "name": "Low", "rep": 1.0}
    picks = [game._rep_weighted_pick([top, mid, low], rng=rng)["id"]
             for _ in range(200)]
    assert picks.count("d-top") > 100  # ~74% expected (20.25 / 27.5)
    assert picks.count("d-low") < 20   # ~4% expected, never favoured
    # And the best-available helper draws from the same preference.
    career = game.build_new_career("Test", "Matthew")
    f2 = game.f2_drivers(career)
    f2[0]["rep"] = 4.5
    for d in f2[1:]:
        d["rep"] = 1.0
    best = game._best_available_driver(career, statuses=("f2",))
    assert best is not None and best["rep"] >= 4.0


def test_v073_supplied_team_cannot_be_proposed_to():
    """v0.7.3 (3): a team that already has a supplier cannot be
    proposed to in either role — as a customer/junior, or as a
    supplier — and a supplied team cannot offer supply either. They
    can only break their contract."""
    career = game.build_new_career("Test", "Matthew")
    career["phase"] = "winter_break"
    mercedes = team_by_name(career, "Mercedes")
    mclaren = team_by_name(career, "McLaren")  # a Mercedes customer
    aston = team_by_name(career, "Aston Martin")
    # McLaren as the customer side: already supplied.
    ok, why = game.propose_relation(career, aston["id"], mclaren["id"],
                                    "customer", aston["id"], 2)
    assert not ok and "already has a supplier" in why, why
    # McLaren as the supplier side: already supplied.
    ok, why = game.propose_relation(career, mclaren["id"], aston["id"],
                                    "customer", mclaren["id"], 2)
    assert not ok and "already has a supplier" in why, why
    # McLaren offering to supply Aston (the v0.6.5 rule): rejected.
    ok, why = game.propose_relation(career, mclaren["id"], aston["id"],
                                    "junior", mclaren["id"], 2)
    assert not ok
    # An unaffiliated pair still works (Aston is human-free here, so
    # the proposal goes to a bot target — pin the roll).
    ok, why = game.propose_relation(career, mercedes["id"], aston["id"],
                                    "customer", mercedes["id"], 2,
                                    rng=_FixedRng(0.0))
    assert ok, why


def test_v073_stale_proposal_supplier_side_rechecked():
    """v0.7.3 (3): a pending proposal is re-checked on the SUPPLIER
    side at accept time too — if the would-be supplier has since
    taken a supplier of their own (or the customer has since taken
    one), the accept is rejected as stale."""
    career = game.build_new_career("Test", "Matthew")
    career["phase"] = "winter_break"
    ok, _ = game.choose_team(career, team_by_name(career, "Aston Martin")["id"],
                             "p-matthew")
    mercedes = team_by_name(career, "Mercedes")
    aston = team_by_name(career, "Aston Martin")
    redbull = team_by_name(career, "Red Bull")
    ok, why = game.propose_relation(career, mercedes["id"], aston["id"],
                                    "customer", mercedes["id"], 2)
    assert ok, why
    proposal = career["relation_proposals"][0]
    # The supplier takes a supplier of its own in the meantime.
    mercedes["supplier_id"] = redbull["id"]
    mercedes["sup_stage"] = 1
    ok, why = game.respond_relation(career, aston["id"], proposal["id"], True)
    assert not ok and "already has a supplier" in why, why
    assert aston["sup_stage"] == 0
    # Same, customer side: the customer takes another supplier first.
    mercedes["supplier_id"] = None
    mercedes["sup_stage"] = 0
    ok, why = game.propose_relation(career, mercedes["id"], aston["id"],
                                    "customer", mercedes["id"], 2)
    assert ok, why
    proposal = career["relation_proposals"][0]
    aston["supplier_id"] = redbull["id"]
    aston["sup_stage"] = 1
    ok, why = game.respond_relation(career, aston["id"], proposal["id"], True)
    assert not ok and "already has a supplier" in why, why


def _v073_proposal(career, supplier_name, customer_name, kind="customer",
                   target_as="customer"):
    """A raw proposal dict for bot_accepts_proposal (bypasses
    propose_relation so the probability itself is tested)."""
    supplier = team_by_name(career, supplier_name)
    customer = team_by_name(career, customer_name)
    target = customer if target_as == "customer" else supplier
    return {"target_team_id": target["id"],
            "supplier_team_id": supplier["id"],
            "customer_team_id": customer["id"], "kind": kind}


def test_v073_bot_proposal_probability_factors():
    """v0.7.3 (3): the bot formation probability is built from
    standings-visible info only — beating the supplier, the supplier
    not being a frontrunner (top 4), bad reputation, and a customer
    becoming a threat (within 40 points, within 1 position, or top
    4). Never car perf. Each factor is pinned with a fixed roll."""
    from f1 import race as race_mod
    career = game.build_new_career("Test", "Matthew")
    # Base case: Mercedes (P2, frontrunner, rep 50) -> Alpine (P10,
    # not poor): p = 0.75.
    p = _v073_proposal(career, "Mercedes", "Alpine")
    assert game.bot_accepts_proposal(career, p, rng=_FixedRng(0.5)) is True
    assert game.bot_accepts_proposal(career, p, rng=_FixedRng(0.8)) is False
    # Bad supplier reputation (rep 0 -> x0.4): p = 0.30.
    mercedes = team_by_name(career, "Mercedes")
    mercedes["team_rep"] = 0
    assert game.bot_accepts_proposal(career, p, rng=_FixedRng(0.5)) is False
    assert game.bot_accepts_proposal(career, p, rng=_FixedRng(0.2)) is True
    mercedes["team_rep"] = 50
    # Beating the supplier AND a non-frontrunner supplier: McLaren
    # (P1) asked to be supplied by Aston (P7): p = 0.75 x 0.25 x 0.57
    # = 0.11.
    p = _v073_proposal(career, "Aston Martin", "McLaren")
    assert game.bot_accepts_proposal(career, p, rng=_FixedRng(0.5)) is False
    assert game.bot_accepts_proposal(career, p, rng=_FixedRng(0.05)) is True
    # Supplier side, no threat: Mercedes 100 pts, Alpine 10, Alpine
    # last (P11, 90-point gap, > 1 position): p = 0.75.
    for t, pts in (("Mercedes", 100), ("McLaren", 90), ("Red Bull", 80),
                   ("Ferrari", 70), ("Williams", 60), ("Racing Bulls", 50),
                   ("Aston Martin", 40), ("Haas", 30), ("Audi", 20),
                   ("Cadillac", 15), ("Alpine", 10)):
        team_by_name(career, t)["points"] = pts
    career["teams"] = race_mod.constructor_standings(career)
    p = _v073_proposal(career, "Mercedes", "Alpine", target_as="supplier")
    assert game.bot_accepts_proposal(career, p, rng=_FixedRng(0.5)) is True
    # Supplier side, threat (Alpine within 40 points): p = 0.75 x 0.35
    # = 0.26.
    team_by_name(career, "Alpine")["points"] = 80
    career["teams"] = race_mod.constructor_standings(career)
    assert game.bot_accepts_proposal(career, p, rng=_FixedRng(0.5)) is False
    assert game.bot_accepts_proposal(career, p, rng=_FixedRng(0.2)) is True


def test_v073_bot_spontaneous_donations():
    """v0.7.3 (3): a bot parent spontaneously donates to a struggling
    supplied team with a good reputation — small per-round chance,
    amount scaled by the customer's need and the parent's surplus.
    Low customer rep -> the parent will not willingly donate (p ~ 0);
    high rep -> low chance plus a slight size boost."""
    career = game.build_new_career("Test", "Matthew")
    mercedes = team_by_name(career, "Mercedes")
    alpine = team_by_name(career, "Alpine")  # P10, bottom half
    mercedes["money"] = 25.0
    alpine["money"] = 1.0  # need = (5 - 1) / 5 = 0.8
    # Neutralise the other parents so only Mercedes -> Alpine fires.
    team_by_name(career, "Red Bull")["money"] = 5.0
    team_by_name(career, "Ferrari")["money"] = 5.0
    # High rep: p = 0.12 x 0.8 = 0.096; amount = min(3, 20 x 0.05) x
    # 0.8 x 1.1 = $0.88.
    alpine["team_rep"] = 80
    made = game.bot_spontaneous_donations(career, rng=_FixedRng(0.0))
    assert made == [(mercedes["id"], alpine["id"], 0.88)], made
    assert alpine["money"] == pytest.approx(1.88)
    assert mercedes["money"] == pytest.approx(24.12)
    # Mid rep: same chance shape, no size boost — $0.80.
    mercedes["money"] = 25.0
    alpine["money"] = 1.0
    alpine["team_rep"] = 50
    made = game.bot_spontaneous_donations(career, rng=_FixedRng(0.0))
    assert made == [(mercedes["id"], alpine["id"], 0.80)], made
    # Low rep: no willing donation, even on a 0.0 roll.
    mercedes["money"] = 25.0
    alpine["money"] = 1.0
    alpine["team_rep"] = 10
    made = game.bot_spontaneous_donations(career, rng=_FixedRng(0.0))
    assert made == []
    assert alpine["money"] == 1.0 and mercedes["money"] == 25.0


def test_v073_develop_message_hides_perf_when_chart_off():
    """v0.7.3 (4): after developing parts the pop-up shows success +
    the amount invested, and EXCLUDES the perf gained when the perf
    bar chart is toggled off. Chart on = the current message."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    mclaren["perf_res"] = 5.0
    mclaren["reli_res"] = 5.0
    mclaren["money"] = 100.0
    ok, msg = game.do_develop(career, mclaren["id"], chart_on=True)
    assert ok and "Perf" in msg and "$1M spent" in msg, msg
    mclaren["perf_res"] = 5.0
    mclaren["reli_res"] = 5.0
    mclaren["money"] = 100.0
    ok, msg = game.do_develop(career, mclaren["id"], chart_on=False)
    assert ok, msg
    assert "Perf" not in msg and "$1M spent" in msg, msg
    assert "Reliability" in msg  # the one visible car stat stays


def test_v073_academy_poach_fee_and_3m_flat():
    """v0.7.3 (5): poaching an academy junior from another team costs
    50% of the salary term + $1.0M flat, shown upfront and charged
    on signing; your own academy junior is free. The general
    poaching flat is now $3.0M (shared formula — it also raises the
    seat-occupant break fee and release fees)."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    ferrari = team_by_name(career, "Ferrari")
    # Dino Beganovic is in Ferrari's academy (fresh career).
    junior = next(d for d in career["drivers"]
                  if d["name"] == "Dino Beganovic")
    assert junior["academy_team_id"] == ferrari["id"]
    junior["salary"] = 0.5
    junior["contract_left"] = 0
    # Round 1: 12 rounds left x $0.5M/12 x 0.5 + $1.0M = $1.25M.
    quote = game.sign_cost(career, mclaren["id"], junior["id"], ("tr", None))
    assert quote["poach_fee"] == pytest.approx(1.25), quote
    mclaren["money"] = 100.0
    # v0.8.0: poaching an academy junior needs a secret meeting
    # first — the fee itself is unchanged.
    ok, msg = game.request_meeting(
        career, mclaren["id"], junior["id"], ("tr", None),
        rng=_SeqRng([0.0, 0.99]))
    assert ok and "agreed" in msg, msg
    money_before = mclaren["money"]
    ok, msg = game.sign_tr_driver(career, mclaren["id"], junior["id"], 2)
    assert ok, msg
    assert mclaren["money"] == pytest.approx(money_before - 1.25)
    # Your own academy junior costs no poach fee.
    own = next(d for d in game.f2_drivers(career)
               if d["status"] == "f2" and not d.get("academy_team_id"))
    ok, why = game.add_academy_driver(career, mclaren["id"], own["id"])
    assert ok, why
    quote = game.sign_cost(career, mclaren["id"], own["id"], ("tr", None))
    assert quote["poach_fee"] == 0
    # The $3.0M flat: Norris, round 1, 2-year contract —
    # 24 x $5.2M/12 + $3.0M = $13.40M.
    norris = game.get_driver(career, mclaren["driver1_id"])
    assert norris["name"] == "Lando Norris"
    assert game.break_fee_for_driver(career, norris) == pytest.approx(13.4)


def test_v073_retired_tr_driver_cleaned_off_next_season_standings():
    """v0.7.3 (6): a driver who retires from Test & Reserve (or free
    agency) is wiped off the NEXT season's active standings, like a
    grid retiree — Matthew's Magnussen case (retired end of 2027 as
    Aston T&R, still listed in 2028). They stay in the previous
    season's history snapshot."""
    from f1 import race as race_mod
    retired = None
    for seed in range(1, 31):
        career = game.build_new_career("Test", "Matthew")
        cadillac = team_by_name(career, "Cadillac")
        tr = game.team_tr_drivers(career, cadillac)[0]
        tr["age"] = 60  # backmarker, low rep: a very high chance
        tr["rep"] = 1.0
        tr["points"] = 10
        tr["team_points"] = 10
        tr["f1_races"] = 3
        for d in career["drivers"]:
            if d is not tr:
                d["age"] = 25
                d["cha"] = 0
        game.end_of_season(career, rng=seeded(seed))
        if tr["name"] in (career.get("retired_this_season") or []):
            retired = (career, tr)
            break
    assert retired is not None, "no seed retired the T&R driver"
    career, tr = retired
    # Wiped from the new season's active standings: points and F1
    # starts reset, so driver_standings no longer lists them.
    assert tr["points"] == 0 and tr["f1_races"] == 0
    assert tr["id"] not in [d["id"] for d in race_mod.driver_standings(career)]
    # But they stay in the previous season's snapshot, with the
    # points they scored.
    snap = {r["id"]: r for r in career["last_season"]["drivers"]}
    assert snap[tr["id"]]["points"] == 10


def test_v073_retirement_rumours_grouped_and_zero_hidden():
    """v0.7.3 (6): retirement rumours cover main, T&R, academy and
    F2 drivers, grouped by category in that order, highest chance
    first within each group — and any driver at 0% chance is hidden
    (drivers under 30 are 0%, so the list stays short)."""
    career = game.build_new_career("Test", "Matthew")
    cadillac = team_by_name(career, "Cadillac")
    tr = game.team_tr_drivers(career, cadillac)[0]
    tr["age"] = 60
    tr["rep"] = 1.0
    junior = next(d for d in career["drivers"]
                  if d.get("academy_team_id"))
    junior["age"] = 45
    junior["rep"] = 1.0
    rows = game.retirement_rumours(career)
    assert rows, "expected at least the old T&R driver to be listed"
    assert all(r["chance"] > 0 for r in rows)
    assert all(r["gender"] in ("M", "F") for r in rows)
    cats = [r["category"] for r in rows]
    assert cats == sorted(cats, key=lambda c: {
        "Main": 0, "Test & Reserve": 1, "Academy": 2, "F2": 3}[c])
    assert any(r["driver"] == tr["name"] and r["category"] == "Test & Reserve"
               for r in rows)
    assert any(r["driver"] == junior["name"] and r["category"] == "Academy"
               for r in rows)
    # A 25-year-old main driver is 0% and hidden.
    young = next(d for d in career["drivers"]
                 if d["status"] == "f1" and d["age"] < 30)
    assert all(r["driver"] != young["name"] for r in rows)


def test_v073_relation_actions_winter_break_only():
    """v0.7.3 (7): offering, accepting and breaking a team contract
    happen only in the winter break — enforced server-side. A break
    notice started in the winter break is allowed; donations stay
    any-time (tested elsewhere)."""
    career = game.build_new_career("Test", "Matthew")
    career["phase"] = "winter_break"
    ok, _ = game.choose_team(career, team_by_name(career, "Aston Martin")["id"],
                             "p-matthew")
    mercedes = team_by_name(career, "Mercedes")
    aston = team_by_name(career, "Aston Martin")
    audi = team_by_name(career, "Audi")
    # A pending proposal to a human target, made in the winter break.
    ok, why = game.propose_relation(career, mercedes["id"], aston["id"],
                                    "customer", mercedes["id"], 2)
    assert ok, why
    proposal = career["relation_proposals"][0]
    # Outside the winter break, answering is rejected.
    career["phase"] = "round"
    ok, why = game.respond_relation(career, aston["id"], proposal["id"], True)
    assert not ok and "winter break" in why, why
    assert aston["sup_stage"] == 0
    # Back in the winter break, the same accept works.
    career["phase"] = "winter_break"
    ok, why = game.respond_relation(career, aston["id"], proposal["id"], True)
    assert ok, why
    assert aston["sup_stage"] == 1
    # Breaking is rejected mid-season, allowed in the winter break.
    career["phase"] = "round"
    ok, why = game.break_relation(career, mercedes["id"], aston["id"])
    assert not ok and "winter break" in why, why
    assert aston["sup_stage"] == 1  # relation untouched
    career["phase"] = "winter_break"
    ok, why = game.break_relation(career, mercedes["id"], aston["id"])
    assert ok, why
    assert aston.get("break_notice") is not None
    assert aston["sup_stage"] == 1  # active during the notice


def test_v073_gender_field_and_page():
    """v0.7.3 (8): every driver carries a gender field — "F" for the
    five real female drivers, "M" for everyone else (including
    Mari Boya, who is male, and all fictional drivers) — and the
    page renders a small M/F badge next to every name through the
    shared renderer."""
    from f1.models import FEMALE_DRIVERS
    career = game.build_new_career("Test", "Matthew")
    assert FEMALE_DRIVERS == {"Mathilda Paatz", "Marta García",
                              "Abbi Pulling", "Doriane Pin",
                              "Payton Westcott"}
    for d in career["drivers"]:
        expected = "F" if d["name"] in FEMALE_DRIVERS else "M"
        assert d["gender"] == expected, d["name"]
    boya = next(d for d in career["drivers"] if d["name"] == "Mari Boya")
    assert boya["gender"] == "M"
    html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
    assert "gender-badge" in html
    assert "genderBadge" in html
    assert "genderOf" in html


def test_v073_custom_colours_exact_list():
    """v0.7.3 (9): the custom-team colour list is exactly the ten
    Matthew approved — orange, yellow, purple, pink kept; brown,
    charcoal, lime, tan, deep magenta, olive new."""
    from f1.models import CUSTOM_COLOURS
    assert CUSTOM_COLOURS == ["#e67e22", "#f39c12", "#9b59b6", "#fd79a8",
                              "#8B4513", "#2D3436", "#7CB342", "#C3B091",
                              "#AD1457", "#827717"]
    assert len(set(CUSTOM_COLOURS)) == 10

# ---------------------------------------------------------------------------
# v0.7.4 — F2 T&R swap fix, Notes tab, cheats menu (perf / money /
# driver rating).
# ---------------------------------------------------------------------------


def _sign_f2_tr(career, team, rep=None, skip=0):
    """Sign an unsigned F2 driver as a T&R driver for `team` (the
    v0.7.3 rule: an F2 driver may join as T&R mid-season). Returns
    the driver."""
    # v0.8.0: another team's academy junior needs a secret meeting
    # to sign as T&R (a poach) — this helper signs a plain F2 driver.
    cands = [d for d in game.f2_drivers(career)
             if not d.get("f2_tr") and not d.get("academy_team_id")]
    f2 = cands[skip]
    team["money"] = 100.0
    ok, why = game.sign_tr_driver(career, team["id"], f2["id"], 2)
    assert ok, why
    assert f2.get("f2_tr") is True
    if rep is not None:
        f2["rep"] = rep
    return f2


def test_v074_f2_tr_swap_refused_midseason():
    """v0.7.4 (1): the manual Swap button was the F2 loophole —
    promoting an f2_tr driver into an empty seat was already blocked
    (v0.7.3), but swapping them with a main driver was not. Now the
    swap is refused mid-season too, and a normal T&R driver still
    swaps fine."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    f2 = _sign_f2_tr(career, mclaren)
    norris = game.get_driver(career, mclaren["driver1_id"])
    ok, why = game.swap_main_tr(career, mclaren["id"], 1, f2["id"])
    assert not ok and "F2" in why, why
    assert f2["status"] == "tr" and f2.get("f2_tr") is True
    assert mclaren["driver1_id"] == norris["id"]
    tr = next(d for d in game.team_tr_drivers(career, mclaren)
              if not d.get("f2_tr"))
    ok, why = game.swap_main_tr(career, mclaren["id"], 1, tr["id"])
    assert ok, why
    assert mclaren["driver1_id"] == tr["id"]


def test_v074_f2_tr_swap_allowed_as_ill_standin():
    """v0.7.4 (1): the one allowed mid-season swap — an f2_tr driver
    taking over a stand-in seat for an ill driver. They keep status
    "tr" + f2_tr (like the promote path) and return to T&R when the
    ill driver recovers."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    a = _sign_f2_tr(career, mclaren, rep=5.0)
    b = _sign_f2_tr(career, mclaren, rep=1.0)
    norris = game.get_driver(career, mclaren["driver1_id"])
    newly = game.roll_illness(
        career, rng=_ScriptedIllnessRng(
            _illness_rolls_for(career, norris["id"]), duration=2))
    assert newly == [norris]
    # A (highest-rep T&R) is the provisional stand-in.
    assert mclaren["driver1_id"] == a["id"]
    assert a.get("ill_sub_for") == norris["id"]
    assert a["status"] == "tr" and a.get("f2_tr") is True
    # B swaps in for A: allowed, and B takes over the stand-in role.
    ok, why = game.swap_main_tr(career, mclaren["id"], 1, b["id"])
    assert ok, why
    assert mclaren["driver1_id"] == b["id"]
    assert b["status"] == "tr" and b.get("f2_tr") is True
    assert b.get("ill_sub_for") == norris["id"]
    assert norris["ill_sub_id"] == b["id"]
    assert a["status"] == "tr" and a.get("ill_sub_for") is None
    # B races the two rounds as the stand-in, then Norris returns
    # and B is back in T&R (still racing F2).
    game.run_race_weekend(career, rng=seeded(31))
    assert b["id"] in _raced_ids(career)
    assert norris["id"] not in _raced_ids(career)
    game.complete_race_weekend(career, rng=seeded(32))
    game.run_race_weekend(career, rng=seeded(33))
    assert b["id"] in _raced_ids(career)
    game.complete_race_weekend(career, rng=seeded(34))
    assert career["round"] == 3
    assert norris["status"] == "f1"
    assert norris["id"] in (mclaren["driver1_id"], mclaren["driver2_id"])
    assert b["status"] == "tr" and b.get("f2_tr") is True
    assert b.get("ill_sub_for") is None
    assert a["status"] == "tr" and a.get("f2_tr") is True


def test_v074_f2_tr_swap_allowed_winter_break():
    """v0.7.4 (1): in the winter break the F2 season is over, so the
    swap is allowed — and the driver leaves F2 for the main seat
    (f2_tr cleared, F2 tally reset), like the promote path."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    f2 = _sign_f2_tr(career, mclaren)
    f2["points"] = 30
    f2["season_wins"] = 2
    f2["best_pos"] = 1
    career["phase"] = "winter_break"
    ok, why = game.swap_main_tr(career, mclaren["id"], 1, f2["id"])
    assert ok, why
    assert f2["status"] == "f1" and f2.get("f2_tr") is False
    assert f2["points"] == 0 and f2["season_wins"] == 0
    assert mclaren["driver1_id"] == f2["id"]


def test_v074_bot_does_not_swap_f2_tr_midseason():
    """v0.7.4 (1): the server-side guard covers bots too — the bot
    AI swaps through swap_main_tr, so a bot team with an f2_tr
    driver as its best T&R does not swap them into a main seat
    mid-season."""
    career = game.build_new_career("Test", "Matthew")
    career["round"] = 6
    cadillac = team_by_name(career, "Cadillac")
    bad = game.get_driver(career, cadillac["driver1_id"])
    bad["points"] = 0
    bad["finish_count"] = 5
    bad["finish_sum"] = 100  # average finish 20th
    f2 = _sign_f2_tr(career, cadillac, rep=5.0)
    game.bot_post_race(career, rng=seeded(41))
    assert cadillac["driver1_id"] == bad["id"]
    assert f2["status"] == "tr" and f2.get("f2_tr") is True
    assert f2["id"] not in (cadillac["driver1_id"], cadillac["driver2_id"])


def test_v074_notes_crud_and_limits():
    """v0.7.4 (2): Notes — add, edit, delete; max 15 per team, max
    200 characters per note; limits enforced server-side."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    ok, why = game.save_note(career, mclaren["id"], "  Remember to pit  ")
    assert ok, why
    assert mclaren["notes"] == [{"id": 1, "text": "Remember to pit"}]
    ok, why = game.save_note(career, mclaren["id"], "Second note")
    assert ok and mclaren["notes"][1]["id"] == 2
    # Edit by id.
    ok, why = game.save_note(career, mclaren["id"], "Edited", note_id=1)
    assert ok, why
    assert mclaren["notes"][0]["text"] == "Edited"
    # Unknown id / empty / too long.
    ok, why = game.save_note(career, mclaren["id"], "x", note_id=99)
    assert not ok and "No such note" in why
    ok, why = game.save_note(career, mclaren["id"], "   ")
    assert not ok
    ok, why = game.save_note(career, mclaren["id"], "x" * 201)
    assert not ok and "200" in why
    ok, why = game.save_note(career, mclaren["id"], "x" * 200)
    assert ok, why
    # Fill to the cap: 15 notes, the 16th is refused.
    for i in range(12):
        ok, why = game.save_note(career, mclaren["id"], f"note {i}")
        assert ok, why
    assert len(mclaren["notes"]) == 15
    ok, why = game.save_note(career, mclaren["id"], "one too many")
    assert not ok and "15" in why
    # Delete (immediate) and the id counter does not reuse ids.
    ok, why = game.delete_note(career, mclaren["id"], 1)
    assert ok, why
    assert len(mclaren["notes"]) == 14
    assert all(n["id"] != 1 for n in mclaren["notes"])
    ok, why = game.delete_note(career, mclaren["id"], 1)
    assert not ok and "No such note" in why
    ok, why = game.save_note(career, mclaren["id"], "fresh")
    assert ok and mclaren["notes"][-1]["id"] == 16


def test_v074_notes_privacy_and_migration():
    """v0.7.4 (2): Notes are private — public_state (the broadcast
    copy of the whole career) never carries them, and my_team_public
    only ever carries the viewer's own team's notes. A pre-v0.7.4
    save migrates with notes=[] on every team."""
    import server as srv
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    ferrari = team_by_name(career, "Ferrari")
    game.save_note(career, mclaren["id"], "McLaren secret")
    game.save_note(career, ferrari["id"], "Ferrari secret")
    # The broadcast copy: no notes anywhere, not even as a substring.
    state = game.public_state(career, True, mclaren["id"])
    assert "McLaren secret" not in json.dumps(state)
    assert "Ferrari secret" not in json.dumps(state)
    assert all("notes" not in t for t in state["teams"])
    # The private channel: each team sees only its own notes.
    mclaren["owner_player_id"] = "p-m"
    ferrari["owner_player_id"] = "p-f"
    room = {"career": career, "chart_on": True, "players": []}
    mine = srv.my_team_public(room, "p-m")
    assert [n["text"] for n in mine["notes"]] == ["McLaren secret"]
    theirs = srv.my_team_public(room, "p-f")
    assert [n["text"] for n in theirs["notes"]] == ["Ferrari secret"]
    # Migration: a v14 career with no notes field gains notes=[].
    career2 = game.build_new_career("Test", "Matthew")
    for t in career2["teams"]:
        t.pop("notes", None)
    career2["version"] = 14
    migrated = migrate_save(career2)
    assert migrated["version"] == 18
    assert all(t["notes"] == [] for t in migrated["teams"])


def test_v074_cheat_state_with_chart_off():
    """v0.7.4 (3): cheat_state returns the REAL perf values even
    when the perf chart is off (public_state strips them) — teams
    best-first, all registered drivers in championship order, all
    constructors in order."""
    career = game.build_new_career("Test", "Matthew", chart_on=False)
    state = game.public_state(career, False, career["teams"][0]["id"])
    assert all("perf" not in t for t in state["teams"])
    cheat = game.cheat_state(career)
    assert len(cheat["teams"]) == 11
    perfs = [t["perf"] for t in cheat["teams"]]
    assert perfs == sorted(perfs, reverse=True)
    assert all(p > 0 for p in perfs)
    assert len(cheat["drivers"]) == 22
    assert cheat["drivers"][0]["points"] >= cheat["drivers"][-1]["points"]
    assert all("rating" in d and "min_rating" in d and "max_rating" in d
               for d in cheat["drivers"])
    assert len(cheat["constructors"]) == 11
    assert cheat["constructors"][0]["points"] >= cheat["constructors"][-1]["points"]


def test_v074_cheat_adjust_clamps():
    """v0.7.4 (3): perf is clamped to [0, 400]; money is floored at
    $0 — and a team already in debt cannot go further down, only up."""
    career = game.build_new_career("Test", "Matthew")
    aston = team_by_name(career, "Aston Martin")
    aston["perf"] = 100.0
    aston["money"] = 10.0
    ok, why = game.cheat_adjust(career, aston["id"], perf_delta=500)
    assert ok, why
    assert aston["perf"] == 400.0
    ok, why = game.cheat_adjust(career, aston["id"], perf_delta=-500)
    assert ok, why
    assert aston["perf"] == 0.0
    ok, why = game.cheat_adjust(career, aston["id"], money_delta=-100)
    assert ok, why
    assert aston["money"] == 0.0
    # Already negative: a negative change is refused outright (no
    # change at all, perf included), a positive one is allowed and
    # may still leave the team in debt.
    aston["money"] = -5.0
    aston["perf"] = 100.0
    ok, why = game.cheat_adjust(career, aston["id"], perf_delta=10, money_delta=-1)
    assert not ok and "already" in why, why
    assert aston["perf"] == 100.0 and aston["money"] == -5.0
    ok, why = game.cheat_adjust(career, aston["id"], money_delta=3)
    assert ok, why
    assert aston["money"] == -2.0
    ok, why = game.cheat_adjust(career, aston["id"], money_delta=5)
    assert ok, why
    assert aston["money"] == 3.0
    # No log entry for a cheat change.
    assert not any("Aston Martin: perf" in line for line in _ttexts(career))


def test_v074_rating_bounds():
    """v0.7.4 (3, driver rating): the clamp range comes from the age
    rating table — the spec's examples, plus the out-of-table
    fallbacks (below 12 → age-12 row, 50+ → age-49 row)."""
    from f1.rules import rating_bounds
    assert rating_bounds(19) == (61, 85)
    assert rating_bounds(25) == (70, 100)
    assert rating_bounds(45) == (62, 86)
    assert rating_bounds(49) == (58, 86)
    assert rating_bounds(50) == (58, 86)
    assert rating_bounds(60) == (58, 86)
    assert rating_bounds(12) == (47, 55)
    assert rating_bounds(5) == (47, 55)


def test_v074_cheat_set_rating():
    """v0.7.4 (3, driver rating): setting a rating within range
    applies it; above the age max clamps to the max, below the age
    min clamps to the min; a 50+ driver clamps to the age-49 row;
    a driver who is not in the drivers' standings cannot be edited."""
    career = game.build_new_career("Test", "Matthew")
    norris = driver_by_name(career, "Lando Norris")
    assert norris["age"] == 26
    ok, why = game.cheat_set_rating(career, norris["id"], 90)
    assert ok, why
    assert norris["perf"] == 90
    ok, why = game.cheat_set_rating(career, norris["id"], 150)
    assert ok, why
    assert norris["perf"] == 100  # age-26 max
    assert "clamped" in why
    ok, why = game.cheat_set_rating(career, norris["id"], 10)
    assert ok, why
    assert norris["perf"] == 71  # age-26 min
    # A 19-year-old clamps to [61, 85] (the spec's example).
    antonelli = driver_by_name(career, "Kimi Antonelli")
    assert antonelli["age"] == 19
    ok, why = game.cheat_set_rating(career, antonelli["id"], 90)
    assert ok, why
    assert antonelli["perf"] == 85
    # A 50+ driver clamps to the age-49 row [58, 86].
    alonso = driver_by_name(career, "Fernando Alonso")
    alonso["age"] = 52
    ok, why = game.cheat_set_rating(career, alonso["id"], 200)
    assert ok, why
    assert alonso["perf"] == 86
    ok, why = game.cheat_set_rating(career, alonso["id"], 10)
    assert ok, why
    assert alonso["perf"] == 58
    # v0.8.0: an F2 driver CAN be edited now (extra cheats class),
    # clamped to the age range like everyone else.
    f2 = game.f2_drivers(career)[0]
    f2["age"] = 19
    ok, why = game.cheat_set_rating(career, f2["id"], 200)
    assert ok, why
    assert f2["perf"] == 85 and "clamped" in why
    # No log entry.
    assert not any("rating" in line for line in _ttexts(career))


def test_ws_v074_notes_and_cheats():
    """v0.7.4 over the real WebSocket (two clients): Notes CRUD via
    save_note/delete_note with the server-side caps; the trigger
    text opens the menu (cheat_state) and creates no note, while the
    lowercase variant creates a normal note; cheat_adjust and
    cheat_driver apply server-side and broadcast — the second client
    sees the new perf in the room state and the new rating in a
    cheat_state request."""
    from fastapi.testclient import TestClient
    import server as srv

    client = TestClient(srv.app)

    def recv_until(ws, want, limit=60, check=None):
        for _ in range(limit):
            m = ws.receive_json()
            if m.get("type") == want and (check is None or check(m)):
                return m
        raise AssertionError(f"never got {want}")

    with client.websocket_connect("/ws") as host_ws:
        host_ws.send_json({"type": "create_room", "nickname": "Matthew", "colour": "#e74c3c"})
        joined = recv_until(host_ws, "joined")
        code = joined["code"]
        host_ws.send_json({"type": "new_career", "name": "WS Notes"})
        state = recv_until(host_ws, "room_state",
                           check=lambda m: m["room"]["career"] is not None)
        teams = {t["name"]: t for t in state["room"]["career"]["teams"]}
        host_ws.send_json({"type": "choose_team", "team_id": teams["McLaren"]["id"]})
        host_ws.send_json({"type": "start_career"})
        recv_until(host_ws, "room_state", check=lambda m: m["room"]["started"] is True)
        room = srv.ROOMS[code]
        career = room["career"]
        mclaren = team_by_name(career, "McLaren")
        # A second client joins and takes Ferrari.
        with client.websocket_connect("/ws") as ws2:
            ws2.send_json({"type": "join_room", "code": code, "nickname": "Cousin",
                           "colour": "#3498db"})
            recv_until(ws2, "joined")
            ws2.send_json({"type": "choose_team", "team_id": teams["Ferrari"]["id"]})
            recv_until(ws2, "room_state", check=lambda m: m["room"]["started"] is True)
            # Notes: add, edit, delete.
            host_ws.send_json({"type": "save_note", "text": "Remember to pit"})
            act = recv_until(host_ws, "action", check=lambda m: m.get("action") == "save_note")
            assert act["ok"], act
            assert [n["text"] for n in mclaren["notes"]] == ["Remember to pit"]
            nid = mclaren["notes"][0]["id"]
            host_ws.send_json({"type": "save_note", "text": "Edited note", "note_id": nid})
            act = recv_until(host_ws, "action", check=lambda m: m.get("action") == "save_note")
            assert act["ok"], act
            assert mclaren["notes"][0]["text"] == "Edited note"
            host_ws.send_json({"type": "delete_note", "note_id": nid})
            act = recv_until(host_ws, "action", check=lambda m: m.get("action") == "delete_note")
            assert act["ok"], act
            assert mclaren["notes"] == []
            # The trigger: exact match opens the menu, creates no note.
            host_ws.send_json({"type": "save_note", "text": "chmenU"})
            cheat = recv_until(host_ws, "cheat_state")
            assert len(cheat["cheat"]["teams"]) == 11
            assert len(cheat["cheat"]["drivers"]) == 22
            assert mclaren["notes"] == []
            # The lowercase variant is just a normal note.
            host_ws.send_json({"type": "save_note", "text": "chmenu"})
            act = recv_until(host_ws, "action", check=lambda m: m.get("action") == "save_note")
            assert act["ok"], act
            assert [n["text"] for n in mclaren["notes"]] == ["chmenu"]
            host_ws.send_json({"type": "delete_note",
                               "note_id": mclaren["notes"][0]["id"]})
            recv_until(host_ws, "action", check=lambda m: m.get("action") == "delete_note")
            # Server-side caps over the wire: 15 notes, then the 16th
            # is refused; a 201-char note is refused.
            for i in range(15):
                host_ws.send_json({"type": "save_note", "text": f"n{i}"})
                act = recv_until(host_ws, "action",
                                 check=lambda m: m.get("action") == "save_note")
                assert act["ok"], act
            assert len(mclaren["notes"]) == 15
            host_ws.send_json({"type": "save_note", "text": "one too many"})
            err = recv_until(host_ws, "error")
            assert "15" in err["message"]
            host_ws.send_json({"type": "save_note", "text": "x" * 201})
            err = recv_until(host_ws, "error")
            assert "200" in err["message"]
            # Team adjust: the target is the team named in the
            # message, NOT the requester's own team — adjust Mercedes
            # (an AI team) and check McLaren is untouched. The second
            # client sees the new perf in the broadcast room state.
            mercedes = team_by_name(career, "Mercedes")
            host_ws.send_json({"type": "cheat_adjust", "team_id": mercedes["id"],
                               "perf_delta": 500})
            act = recv_until(host_ws, "action", check=lambda m: m.get("action") == "cheat_adjust")
            assert act["ok"], act
            assert mercedes["perf"] == 400.0
            assert mclaren["perf"] != 400.0
            recv_until(ws2, "room_state",
                       check=lambda m: any(t["name"] == "Mercedes" and t.get("perf") == 400.0
                                           for t in m["room"]["career"]["teams"]))
            # Driver rating: set above the age max, it clamps; the
            # second client sees the new rating in a cheat_state.
            norris = driver_by_name(career, "Lando Norris")
            host_ws.send_json({"type": "cheat_driver", "driver_id": norris["id"],
                               "rating": 150})
            act = recv_until(host_ws, "action", check=lambda m: m.get("action") == "cheat_driver")
            assert act["ok"], act
            assert norris["perf"] == 100
            ws2.send_json({"type": "cheat_state"})
            cheat2 = recv_until(ws2, "cheat_state")
            row = next(d for d in cheat2["cheat"]["drivers"] if d["id"] == norris["id"])
            assert row["rating"] == 100


def test_v074_notes_tab_and_cheats_menu_browser():
    """v0.7.4 (browser): the Notes tab renders (add box, 0/15 count),
    a note can be added, the trigger text opens the menu (perf bars,
    finances, all 22 drivers, all 11 constructors), a team pop-up
    opens and a preset applies, a driver pop-up opens and a rating
    clamps to the age max, and BACK returns to the Notes tab — with
    the trigger having created no note. No JS errors."""
    pytest.importorskip("playwright")
    import re
    import subprocess
    import time
    import urllib.request
    port = 8813
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        from playwright.sync_api import sync_playwright
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/")
            page.fill("#home-nick", "Max")
            page.click("#btn-create")
            page.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
            page.click("#btn-new-career")
            page.wait_for_selector("[data-team]", timeout=15000)
            page.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.textContent.includes('McLaren'))
                    .querySelector('[data-team]').click();
            }""")
            page.wait_for_selector("#btn-start-career:not(.hidden)", timeout=15000)
            page.click("#btn-start-career")
            page.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            # The Notes tab (right after Research).
            page.click('[data-tab="notes"]')
            page.wait_for_selector("#tab-notes:not(.hidden)", timeout=15000)
            notes = page.inner_text("#tab-notes")
            assert "Notes" in notes, notes
            assert "0/15 notes" in notes, notes
            # Add a note: it appears in the grid.
            page.fill("#note-text", "Remember to pit on lap 20")
            page.click("#btn-note-add")
            page.wait_for_selector("#notes-box .note-card", timeout=15000)
            assert "Remember to pit on lap 20" in page.inner_text("#notes-box")
            assert "1/15 notes" in page.inner_text("#tab-notes")
            # The trigger opens the menu.
            page.fill("#note-text", "chmenU")
            page.click("#btn-note-add")
            page.wait_for_selector("#screen-cheats:not(.hidden)", timeout=15000)
            # The menu: perf bars, finances, all 22 drivers, all 11 teams.
            menu = page.inner_text("#screen-cheats")
            assert "Car performance" in menu, menu
            assert "Finances" in menu, menu
            assert "Drivers' standings" in menu, menu
            assert "Constructors' standings" in menu, menu
            assert page.locator("#cheats-drivers [data-cheat-driver]").count() == 22
            assert page.locator("#cheats-teams [data-cheat-team]").count() == 11
            # Team pop-up: tap a team name, apply a +10 perf preset.
            # Presets apply immediately and the pop-up stays open
            # (so more presets can be tapped), showing the new value.
            page.locator("#cheats-perf [data-cheat-team]").first.click()
            page.wait_for_selector(".ov-panel:not(.hidden)", timeout=15000)
            ov = page.inner_text(".ov-body")
            assert "Perf change" in ov and "Money change" in ov, ov
            m = re.search(r"perf ([\d.]+)", ov)
            before = float(m.group(1))
            page.click('[data-cheat-preset="perf:10"]')
            page.wait_for_function(
                """(before) => {
                    const b = document.querySelector('.ov-body');
                    return b && b.textContent.includes('perf ' + (before + 10).toFixed(1));
                }""", arg=before, timeout=15000)
            # Close the team pop-up, then open a driver pop-up.
            page.click('[data-ov-close="cheat-team"]')
            page.locator("#cheats-drivers [data-cheat-driver]").first.click()
            page.wait_for_function(
                """() => {
                    const b = document.querySelector('.ov-body');
                    return b && b.textContent.includes('New rating');
                }""", timeout=15000)
            ov = page.inner_text(".ov-body")
            mm = re.search(r"min (\d+) – max (\d+)", ov)
            assert mm, ov
            max_rating = int(mm.group(2))
            page.fill("#cheat-rating", str(max_rating + 10))
            page.click("[data-cheat-driver-apply]")
            page.wait_for_function(
                """(mx) => {
                    const b = document.querySelector('.ov-body');
                    return b && b.textContent.includes('rating ' + mx);
                }""", arg=max_rating, timeout=15000)
            # BACK returns to the game screen, Notes tab selected —
            # and the trigger created no note (still exactly 1).
            page.click('[data-ov-close="cheat-driver"]')
            page.click("#btn-cheats-back")
            page.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            page.wait_for_selector("#tab-notes:not(.hidden)", timeout=15000)
            assert "1/15 notes" in page.inner_text("#tab-notes")
            assert page.locator("#notes-box .note-card").count() == 1
            assert "Remember to pit on lap 20" in page.inner_text("#notes-box")
            browser.close()
        assert not errors, errors
    finally:
        proc.terminate()


def test_v074_f2_swap_button_disabled_browser():
    """v0.7.4 (1, browser): sign an F2 driver as T&R from the Driver
    Market, then the Drivers tab shows their Swap buttons disabled
    with the F2 reason (the server refuses the swap too)."""
    pytest.importorskip("playwright")
    import subprocess
    import time
    import urllib.request
    port = 8815
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        from playwright.sync_api import sync_playwright
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/")
            page.fill("#home-nick", "Max")
            page.click("#btn-create")
            page.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
            page.click("#btn-new-career")
            page.wait_for_selector("[data-team]", timeout=15000)
            page.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.textContent.includes('McLaren'))
                    .querySelector('[data-team]').click();
            }""")
            page.wait_for_selector("#btn-start-career:not(.hidden)", timeout=15000)
            page.click("#btn-start-career")
            page.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            # Market tab: sign an F2 driver as T&R. (v0.8.0: the
            # first F2 driver is an academy junior — a poach, whose
            # overlay shows the meeting UI, not the contract picker
            # — so pick an F2 driver with no academy team. v0.8.1:
            # the condensed rows no longer carry the per-row meeting
            # hint or an academy badge, so the pick comes from the
            # client state, not the row text.)
            page.click('[data-tab="market"]')
            page.wait_for_selector("#tab-market:not(.hidden)", timeout=15000)
            page.evaluate("""() => {
                const d = room.career.drivers
                    .find(x => x.status === "f2" && !x.academy_team_id);
                document.querySelector(
                    `[data-sign="${d.id}"][data-target="tr"]`).click();
            }""")
            page.wait_for_selector(".ov-panel:not(.hidden)", timeout=15000)
            page.click('[data-contract-years="2"]')
            page.wait_for_selector(".ov-panel", state="detached", timeout=15000)
            # Drivers tab: the new T&R driver's Swap buttons are
            # disabled with the F2 reason (one per seat).
            page.click('[data-tab="drivers"]')
            page.wait_for_selector("#tab-drivers:not(.hidden)", timeout=15000)
            page.wait_for_function(
                """() => [...document.querySelectorAll('#tab-drivers button[disabled]')]
                    .some(b => b.textContent.includes('still racing in F2'))""",
                timeout=15000)
            disabled = page.locator('#tab-drivers button[disabled]',
                                    has_text="still racing in F2")
            assert disabled.count() == 2
            browser.close()
        assert not errors, errors
    finally:
        proc.terminate()

# ---------------------------------------------------------------------------
# v0.7.5 — cheats-menu bar alignment, coloured Perf Chart, retirement
# toggles, reg research rework, winter-break retired list (name + age).
# ---------------------------------------------------------------------------


class _ZeroChaos:
    """A stand-in rng for reg_change_v2: chaos 0, so the gain is exact."""
    def uniform(self, a, b):
        return 0.0


def test_v075_reg_research_new_curve():
    """v0.7.5 (4): the reworked reg curve — minor cap 15 with the
    optimum at $10M, major cap 24.5 with the optimum at $20M. $10M
    buys more than $1M bought under the old formula (12 * ln 2
    = 8.32)."""
    import math
    from f1.rules import reg_change_v2
    z = _ZeroChaos()
    # Minor (target 12): $1M -> ~+4.3, $5M -> ~+11.2, $10M -> +15.
    assert reg_change_v2(100.0, 1.0, 12.0, rng=z) == pytest.approx(104.34, abs=0.05)
    assert reg_change_v2(100.0, 5.0, 12.0, rng=z) == pytest.approx(111.21, abs=0.05)
    assert reg_change_v2(100.0, 10.0, 12.0, rng=z) == pytest.approx(115.0, abs=0.05)
    # Major (target 35): $1M -> ~+5.6, $10M -> ~+19.3, $20M -> +24.5.
    assert reg_change_v2(100.0, 1.0, 35.0, rng=z) == pytest.approx(105.58, abs=0.05)
    assert reg_change_v2(100.0, 10.0, 35.0, rng=z) == pytest.approx(119.30, abs=0.05)
    assert reg_change_v2(100.0, 20.0, 35.0, rng=z) == pytest.approx(124.5, abs=0.05)
    # More money keeps helping until the optimum, then flat.
    assert reg_change_v2(100.0, 50.0, 12.0, rng=z) == pytest.approx(115.0, abs=0.05)
    assert reg_change_v2(100.0, 100.0, 35.0, rng=z) == pytest.approx(124.5, abs=0.05)
    # Nothing adapted and nothing researched is still exactly 0.
    assert reg_change_v2(0.0, 0.0, 12.0, rng=z) == 0.0
    assert reg_change_v2(0.0, 0.0, 35.0, rng=z) == 0.0
    # $10M in a minor year buys more than $1M bought under the old
    # formula.
    assert 15.0 > 12 * math.log1p(1.0)


def _old_backmarker(career, team_name="Cadillac"):
    """A main driver with a very high retirement chance (old,
    backmarker, low rep) — the Magnussen-test shape. Every other
    driver is made young so no other random retirement can fire."""
    team = team_by_name(career, team_name)
    d = game.get_driver(career, team["driver1_id"])
    d["age"] = 60
    d["rep"] = 1.0
    d["points"] = 0
    d["team_points"] = 0
    d["season_wins"] = 0
    for other in career["drivers"]:
        if other is not d:
            other["age"] = 25
    return d


def test_v075_custom_retirements_master_on_only_toggled():
    """v0.7.5 (3): master ON — a toggled driver retires at season
    close (forced, even at 0% random chance), and NO random
    retirements happen at all, not even for a high-chance untoggled
    driver."""
    from f1 import race as race_mod
    # (a) A young toggled driver retires — forced, not a roll.
    career = game.build_new_career("Test", "Matthew")
    norris = driver_by_name(career, "Lando Norris")
    ok, why = game.set_custom_retirements(career, True)
    assert ok, why
    assert career["custom_retirements"] is True
    ok, why = game.set_retire_toggle(career, norris["id"], True)
    assert ok, why
    assert norris["retire_at_season_end"] is True
    game.end_of_season(career, rng=seeded(7))
    assert norris["status"] == "retired"
    assert career.get("retired_this_season") == ["Lando Norris"]
    assert norris["id"] not in [d["id"] for d in race_mod.driver_standings(career)]
    # The flag is spent once the driver retires.
    assert norris["retire_at_season_end"] is False
    # (b) With the master on, a high-chance untoggled driver NEVER
    # retires — the random rolls are skipped entirely.
    for seed in range(1, 31):
        career = game.build_new_career("Test", "Matthew")
        old = _old_backmarker(career)
        game.set_custom_retirements(career, True)
        game.end_of_season(career, rng=seeded(seed))
        assert old["status"] != "retired", f"seed {seed}"
        assert not career.get("retired_this_season"), \
            f"seed {seed}: {career.get('retired_this_season')}"


def test_v075_custom_retirements_master_off_random_and_clear():
    """v0.7.5 (3): master OFF — the normal random retirements run
    (a high-chance driver retires in some seed) and every toggle is
    cleared at season close."""
    retired_any = False
    for seed in range(1, 31):
        career = game.build_new_career("Test", "Matthew")
        old = _old_backmarker(career)
        # A toggle left over from an earlier master-ON spell is
        # cleared by the season close when the master is off — and
        # it does not force the retirement (the roll does that).
        old["retire_at_season_end"] = True
        assert career.get("custom_retirements") is False
        game.end_of_season(career, rng=seeded(seed))
        assert old["retire_at_season_end"] is False
        if old["status"] == "retired":
            retired_any = True
            break
    assert retired_any, "no seed retired the high-chance driver"


def test_v075_retire_toggle_master_gating():
    """v0.7.5 (3): turning the master OFF clears every driver toggle
    immediately, and setting a toggle while the master is off is
    refused server-side. cheat_state carries the master flag and the
    per-driver toggles."""
    career = game.build_new_career("Test", "Matthew")
    norris = driver_by_name(career, "Lando Norris")
    alonso = driver_by_name(career, "Fernando Alonso")
    # Master off: setting a toggle is refused.
    ok, why = game.set_retire_toggle(career, norris["id"], True)
    assert not ok and "Custom retirements" in why, why
    assert norris["retire_at_season_end"] is False
    # Master on: toggles can be set...
    ok, why = game.set_custom_retirements(career, True)
    assert ok, why
    ok, why = game.set_retire_toggle(career, norris["id"], True)
    assert ok, why
    ok, why = game.set_retire_toggle(career, alonso["id"], True)
    assert ok, why
    assert norris["retire_at_season_end"] is True
    assert alonso["retire_at_season_end"] is True
    # ...and turning the master off clears them all immediately.
    ok, why = game.set_custom_retirements(career, False)
    assert ok, why
    assert career["custom_retirements"] is False
    assert not any(d.get("retire_at_season_end") for d in career["drivers"])
    # cheat_state carries the master flag and per-driver toggles.
    game.set_custom_retirements(career, True)
    game.set_retire_toggle(career, norris["id"], True)
    cheat = game.cheat_state(career)
    assert cheat["custom_retirements"] is True
    row = next(d for d in cheat["drivers"] if d["id"] == norris["id"])
    assert row["retire_at_season_end"] is True
    row2 = next(d for d in cheat["drivers"] if d["id"] == alonso["id"])
    assert row2["retire_at_season_end"] is False
    # Only a driver in the registered standings can be toggled.
    ok, why = game.set_retire_toggle(career, "no-such-driver", True)
    assert not ok


def test_v075_retirement_toggles_migration():
    """v0.7.5 (3): a v15 career with no retirement flags migrates
    with the master OFF and every driver toggle OFF, at version 16."""
    career = game.build_new_career("Test", "Matthew")
    career.pop("custom_retirements", None)
    for d in career["drivers"]:
        d.pop("retire_at_season_end", None)
    career["version"] = 15
    migrated = migrate_save(career)
    assert migrated["version"] == 18
    assert migrated["custom_retirements"] is False
    assert all(d["retire_at_season_end"] is False
               for d in migrated["drivers"])


def test_v075_cheats_menu_retirements_chart_and_winter_browser():
    """v0.7.5 (browser): the Perf Chart bars + names are coloured
    (no default blue); the cheats-menu perf bars line up (every
    bar-track starts at the same x, name slot 130px); the master
    toggle sits at the top of the menu (default off); the driver
    pop-up toggle is disabled while the master is off and works
    once it is on (a RET marker appears); then a full season with
    Alonso toggled ends in the winter break with his surname
    uppercase, bold, in Aston Martin green, plus his age. No JS
    errors."""
    pytest.importorskip("playwright")
    import re
    import subprocess
    import time
    import urllib.request
    port = 8817
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise AssertionError("server did not come up")
        from playwright.sync_api import sync_playwright
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            p1 = browser.new_page(viewport={"width": 1280, "height": 800})
            p1.on("pageerror", lambda e: errors.append(str(e)))
            p1.goto(f"http://127.0.0.1:{port}/")
            p1.fill("#home-nick", "Max")
            p1.click("#btn-create")
            p1.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
            p1.click("#btn-new-career")
            p1.wait_for_selector("[data-team]", timeout=15000)
            p1.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.textContent.includes('McLaren'))
                    .querySelector('[data-team]').click();
            }""")
            p1.wait_for_selector("#btn-start-career:not(.hidden)", timeout=15000)
            code = re.search(r"\b[A-Z0-9]{6}\b",
                             p1.inner_text("#screen-lobby")).group(0)
            p2 = browser.new_page(viewport={"width": 1280, "height": 800})
            p2.on("pageerror", lambda e: errors.append(str(e)))
            p2.goto(f"http://127.0.0.1:{port}/?code={code}")
            p2.fill("#home-nick", "Matthew")
            p2.click("#btn-join")
            p2.wait_for_selector("[data-team]", timeout=15000)
            p2.evaluate("""() => {
                const rows = [...document.querySelectorAll('#team-list > *')];
                rows.find(r => r.textContent.includes('Ferrari'))
                    .querySelector('[data-team]').click();
            }""")
            p1.click("#btn-start-career")
            p1.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            # (2) Perf Chart: every bar and name carries a team
            # colour — no default blue #58a6ff anywhere.
            p1.click('[data-tab="chart"]')
            p1.wait_for_selector("#tab-chart:not(.hidden)", timeout=15000)
            colours = p1.evaluate("""() => {
                const rows = [...document.querySelectorAll('#chart-box .bar-row')];
                return rows.map(r => ({
                    bar: (r.querySelector('.bar-fill') || {}).style.background,
                    name: (r.querySelector('span span') || {}).style.color,
                }));
            }""")
            assert colours, "no chart rows"
            assert all(c["bar"] for c in colours), colours
            assert len({c["bar"] for c in colours}) > 1, colours
            assert all(c["bar"] != "rgb(88, 166, 255)" for c in colours), colours
            assert all(c["name"] for c in colours), colours
            assert len({c["name"] for c in colours}) > 1, colours
            # Open the cheats menu (the Notes-tab trigger).
            p1.click('[data-tab="notes"]')
            p1.wait_for_selector("#tab-notes:not(.hidden)", timeout=15000)
            p1.fill("#note-text", "chmenU")
            p1.click("#btn-note-add")
            p1.wait_for_selector("#screen-cheats:not(.hidden)", timeout=15000)
            # (1) Perf bars line up: same x for every bar-track and
            # a fixed 130px name slot.
            align = p1.evaluate("""() => ({
                lefts: [...document.querySelectorAll('#cheats-perf .bar-track')]
                    .map(el => el.getBoundingClientRect().left),
                widths: [...document.querySelectorAll('#cheats-perf .bar-row > button')]
                    .map(el => el.getBoundingClientRect().width),
            })""")
            assert align["lefts"], "no cheats perf rows"
            assert max(align["lefts"]) - min(align["lefts"]) < 1, align["lefts"]
            assert all(abs(w - 130) < 1 for w in align["widths"]), align["widths"]
            # (3) Master toggle: at the top of the menu, default off.
            assert p1.is_visible("#cheat-custom-retirements")
            assert not p1.is_checked("#cheat-custom-retirements")
            menu_text = p1.inner_text("#screen-cheats")
            assert "Custom retirements" in menu_text
            # Driver pop-up: the toggle is disabled while the master
            # is off, with a note saying so.
            p1.locator("#cheats-drivers [data-cheat-driver]", has_text="ALONSO").click()
            p1.wait_for_function(
                "() => document.querySelector('.ov-body') && "
                "document.querySelector('.ov-body').textContent.includes('Retire at season end')",
                timeout=15000)
            assert p1.is_disabled("#cheat-retire-toggle")
            ov = p1.inner_text(".ov-body")
            assert "Turn on Custom retirements" in ov
            # Alonso's age, for the winter-break assertion later.
            # The rollover ages every driver by one year before the
            # winter-break screen renders, so the list shows the
            # post-rollover age (44 in-season -> 45 at the break).
            age = re.search(r"age (\d+)", ov).group(1)
            # Turn the master on and toggle Alonso: the marker shows.
            p1.click('[data-ov-close="cheat-driver"]')
            p1.check("#cheat-custom-retirements")
            p1.wait_for_function(
                "() => document.getElementById('cheat-custom-retirements').checked",
                timeout=15000)
            p1.locator("#cheats-drivers [data-cheat-driver]", has_text="ALONSO").click()
            p1.wait_for_function(
                "() => document.querySelector('.ov-body') && "
                "document.querySelector('.ov-body').textContent.includes('Retire at season end')",
                timeout=15000)
            assert not p1.is_disabled("#cheat-retire-toggle")
            p1.check("#cheat-retire-toggle")
            p1.wait_for_function(
                "() => document.getElementById('cheats-drivers').textContent.includes('RET')",
                timeout=15000)
            p1.click('[data-ov-close="cheat-driver"]')
            p1.click("#btn-cheats-back")
            p1.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)
            # Play the whole season (two humans, both advance + vote).
            for rnd in range(12):
                p1.click("#btn-advance")
                p2.click("#btn-advance")
                p1.wait_for_selector("#screen-results:not(.hidden)", timeout=30000)
                p1.click("#btn-vote-next")
                p2.click("#btn-vote-next")
                p1.wait_for_selector("text=Race Results", timeout=30000)
                p1.click("#btn-vote-next")
                p2.click("#btn-vote-next")
                if rnd < 11:
                    p1.wait_for_selector("#screen-game:not(.hidden)", timeout=30000)
                else:
                    p1.wait_for_selector("#winter-card:not(.hidden)", timeout=30000)
            # (5)(6) The winter-break line: Alonso's surname
            # uppercase and bold (the .surname class), in Aston
            # Martin green, with the '|' bar and his age.
            winter = p1.inner_html("#winter-text")
            assert "ALONSO" in winter, winter
            assert "Retired:" in winter, winter
            assert "#4b9774" in winter.lower(), winter
            assert f"({int(age) + 1})" in winter, winter
            assert 'class="surname"' in winter, winter
            assert 'class="team-bar"' in winter, winter
            browser.close()
        assert not errors, errors
    finally:
        proc.terminate()

# ---------------------------------------------------------------------------
# v0.8.0 — donation requests removed; F2 T&R colour/badge + mutual
# exclusion; happiness + loyalty + activities + bribes + media rumours
# + tab split; secret meetings; tab groups; advance gate; cheats-menu
# extra driver classes; winter-break academy poaches gated on meetings.
# ---------------------------------------------------------------------------


class _SeqRng:
    """Deterministic RNG returning a scripted sequence of random()
    values (then falling back to a seeded Random). request_meeting
    rolls acceptance first, then the leak — so [0.0, 0.99] means
    accept + no leak, [0.99, 0.0] means decline + leak."""

    def __init__(self, values, seed=1):
        self.values = list(values)
        self._fallback = random.Random(seed)

    def random(self):
        if self.values:
            return self.values.pop(0)
        return self._fallback.random()

    def choice(self, seq):
        return seq[0]

    def gauss(self, mu, sigma):
        return self._fallback.gauss(mu, sigma)

    def randint(self, a, b):
        return self._fallback.randint(a, b)

    def uniform(self, a, b):
        return self._fallback.uniform(a, b)


def _set_relation(d, team_id, happiness, loyalty):
    """Pin a driver's relation with a team (both the per-team dicts
    and the current values, kept in sync)."""
    d.setdefault("happiness_by_team", {})[team_id] = happiness
    d.setdefault("loyalty_by_team", {})[team_id] = loyalty
    d["happiness"] = happiness
    d["loyalty"] = loyalty


def _poach_target(career, team_name="Red Bull", seat=1):
    """A main driver of a bot team, ready to be poached: (team, driver)."""
    team = team_by_name(career, team_name)
    did = team["driver1_id"] if seat == 1 else team["driver2_id"]
    return team, game.get_driver(career, did)


def test_v080_happiness_starts_neutral_and_resets_on_new_team():
    """v0.8.0 (3): every driver associated with a team starts at
    neutral happiness (3.0) with loyalty 3.0 at career start; a free
    agent sits at neutral until signed, and signing with a team
    they have never been at resets them to neutral (3.0) with fresh
    loyalty (1.0 — they just arrived)."""
    from f1 import rules as rules_mod
    career = game.build_new_career("Test", "Matthew")
    for d in career["drivers"]:
        if d.get("status") in ("f1", "tr") and d.get("team_id"):
            assert game.happiness_with(d, d["team_id"]) == 3.0
            assert game.loyalty_with(d, d["team_id"]) == 3.0
        elif d.get("academy_team_id"):
            assert game.happiness_with(d, d["academy_team_id"]) == 3.0
        else:
            assert game.happiness_with(d, "no-such-team") == 3.0
    # A free agent signed to a brand-new team resets to neutral.
    mclaren = team_by_name(career, "McLaren")
    free = next(d for d in game.free_agents(career) if not d.get("academy_team_id"))
    _set_relation(free, "somewhere-else", 1.0, 5.0)
    mclaren["money"] = 100.0
    ok, why = game.sign_tr_driver(career, mclaren["id"], free["id"], 2)
    assert ok, why
    assert free["happiness"] == 3.0
    assert free["loyalty"] == 1.0
    assert rules_mod.happiness_level(free["happiness"]) == 3
    assert rules_mod.happiness_word(free["happiness"]) == "Neutral"


def test_v080_happiness_averages_on_return_to_old_team():
    """v0.8.0 (3): re-signing with a team the driver was at before
    averages the current happiness with the happiness from the last
    stint there (.5 rounds to nearest, halves up) — Happy/4 +
    Unhappy/2 -> Neutral/3. Loyalty averages the same way."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    d = game.get_driver(career, mclaren["driver1_id"])
    _set_relation(d, mclaren["id"], 2.0, 4.0)  # last stint: unhappy, loyal
    d["happiness"] = 4.0  # current: happy
    d["loyalty"] = 2.0
    game.apply_signing_relation(career, d, mclaren["id"])
    assert d["happiness"] == 3.0  # (4 + 2) / 2
    assert d["loyalty"] == 3.0  # (2 + 4) / 2
    # Halves round up: (4 + 3) / 2 = 3.5 -> 4.
    _set_relation(d, mclaren["id"], 3.0, 3.0)
    d["happiness"] = 4.0
    game.apply_signing_relation(career, d, mclaren["id"])
    assert d["happiness"] == 4.0


def test_v080_tune3_makes_driver_unhappy_within_a_few_races():
    """v0.8.0 (3b): tuning against a driver (-0.03 x |tuning| x
    (1 + 0.12 x streak) per race) — tune 3 makes the disadvantaged
    driver unhappy within ~6 races, while the favoured driver gains
    a small bump (+0.015 x |tuning| per race)."""
    from f1 import rules as rules_mod
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    mclaren["tuning"] = 3  # for driver 1 (Norris), against driver 2
    d1 = game.get_driver(career, mclaren["driver1_id"])
    d2 = game.get_driver(career, mclaren["driver2_id"])
    for _ in range(6):
        game.update_driver_happiness(career, rng=seeded(1))
    assert rules_mod.happiness_level(game.happiness_with(d2, mclaren["id"])) <= 2
    assert game.happiness_with(d2, mclaren["id"]) < 2.5  # ~2.23 by hand
    assert game.happiness_with(d1, mclaren["id"]) > 3.0  # favoured bump


def test_v080_tune1_is_a_slow_drift():
    """v0.8.0 (3b): tune 1 is a slow drift over ~a full season —
    still neutral after 6 races, unhappy only after ~12."""
    from f1 import rules as rules_mod
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    mclaren["tuning"] = 1
    d2 = game.get_driver(career, mclaren["driver2_id"])
    for _ in range(6):
        game.update_driver_happiness(career, rng=seeded(1))
    assert rules_mod.happiness_level(game.happiness_with(d2, mclaren["id"])) == 3
    assert game.happiness_with(d2, mclaren["id"]) > 2.6  # ~2.74 by hand
    for _ in range(6):
        game.update_driver_happiness(career, rng=seeded(1))
    assert rules_mod.happiness_level(game.happiness_with(d2, mclaren["id"])) == 2
    assert game.happiness_with(d2, mclaren["id"]) < 2.5  # ~2.36 by hand


def test_v080_kept_despite_poor_performance_raises_happiness():
    """v0.8.0 (3c): a main driver kept in their seat despite poor
    performances (big points + finishing gaps to their teammate over
    12 races) gets happier — gratitude for being kept. The teammate
    ahead gets nothing."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    d1 = game.get_driver(career, mclaren["driver1_id"])
    d2 = game.get_driver(career, mclaren["driver2_id"])
    d2["rep"] = 4.0  # higher-rep driver behind = bigger gratitude
    d1["rep"] = 2.0
    career["race_history"] = [
        {"year": 2026, "round": i + 1, "circuit": "Test",
         "race": [{"driver_id": d2["id"], "team_id": mclaren["id"],
                   "points": 0, "pos": 20},
                  {"driver_id": d1["id"], "team_id": mclaren["id"],
                   "points": 10, "pos": 1}]}
        for i in range(12)]
    for _ in range(3):
        game.update_driver_happiness(career, rng=seeded(1))
    assert game.happiness_with(d2, mclaren["id"]) >= 3.3  # +0.128/race
    assert game.happiness_with(d1, mclaren["id"]) == 3.0


def test_v080_rating_boost_is_per_race_only():
    """v0.8.0 (3): the happiness rating adjustment (very unhappy -2,
    unhappy -1, neutral 0, happy +1, very happy +2) applies per race
    only and never accumulates — a very happy driver rated 86 races
    at 88 for one race and is back to 86 afterwards."""
    from f1 import race as race_mod
    from f1 import rules as rules_mod
    assert rules_mod.happiness_rating_adj(1.0) == -2
    assert rules_mod.happiness_rating_adj(2.0) == -1
    assert rules_mod.happiness_rating_adj(3.0) == 0
    assert rules_mod.happiness_rating_adj(4.0) == 1
    assert rules_mod.happiness_rating_adj(5.0) == 2
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    d = game.get_driver(career, mclaren["driver1_id"])
    d["perf"] = 86
    other = json.loads(json.dumps(career))
    _set_relation(d, mclaren["id"], 5.0, 3.0)
    d2 = game.get_driver(other, mclaren["driver1_id"])
    assert game.happiness_with(d2, mclaren["id"]) == 3.0
    assert race_mod._happiness_adj(d, mclaren["id"]) == 2
    assert race_mod._happiness_adj(d2, mclaren["id"]) == 0
    race_mod.simulate_qual(career, rng=seeded(7))
    race_mod.simulate_qual(other, rng=seeded(7))
    assert d["qual_time"] < d2["qual_time"]  # +2 rating = 0.08s a lap
    assert d["perf"] == 86 and d2["perf"] == 86
    # And after a full round the rating is still 86 — never banked.
    career = two_human_career()
    mclaren = team_by_name(career, "McLaren")
    d = game.get_driver(career, mclaren["driver1_id"])
    d["perf"] = 86
    _set_relation(d, mclaren["id"], 5.0, 3.0)
    play_full_round(career, seeded(11))
    assert d["perf"] == 86


def test_v080_unhappy_resign_mostly_declined():
    """v0.8.0 (3): an unhappy driver mostly turns down their old
    team's re-signing offer in the winter break — 70% when unhappy,
    90% when very unhappy (pinned here with scripted rolls)."""
    def _resign_scenario(level, roll):
        career = game.build_new_career("Test", "Matthew")
        mclaren = team_by_name(career, "McLaren")
        d = game.get_driver(career, mclaren["driver1_id"])
        mclaren["driver1_id"] = None
        d["status"] = "free"
        d["last_team_id"] = mclaren["id"]
        d["last_slot"] = "seat1"
        _set_relation(d, mclaren["id"], float(level), 3.0)
        career["phase"] = "winter_break"
        career["wb_round"] = 1
        mclaren["money"] = 100.0
        ok, why = game.submit_winter_offers(
            career, mclaren["id"],
            [{"driver_id": d["id"], "target": "seat", "seat": 1, "years": 2}])
        assert ok, why
        game.resolve_winter_break_round(career, rng=_SeqRng([roll]))
        return career, d, mclaren

    # Unhappy (70%): a 0.5 roll declines, a 0.95 roll accepts.
    career, d, mclaren = _resign_scenario(2, 0.5)
    assert d["status"] == "free"
    assert any("turned down" in t for t in _ttexts(career))
    assert any("rejected a new deal" in m["text"]
               for m in career["media_rumours"])
    career, d, mclaren = _resign_scenario(2, 0.95)
    assert d["status"] == "f1" and mclaren["driver1_id"] == d["id"]
    # Very unhappy (90%): even a 0.85 roll declines.
    career, d, mclaren = _resign_scenario(1, 0.85)
    assert d["status"] == "free"


def test_v080_happy_driver_refuses_adjacent_team_outright():
    """v0.8.0 (3/4): a happy driver turns down an approach from a
    neighbouring team outright — no meeting, no $0.5M charge, no
    cooldown, no leak."""
    career = two_human_career()
    mclaren = team_by_name(career, "McLaren")  # P1
    mercedes = team_by_name(career, "Mercedes")  # P2 — adjacent
    d = game.get_driver(career, mercedes["driver1_id"])
    _set_relation(d, mercedes["id"], 4.5, 3.0)
    before = mclaren["money"]
    ok, why = game.request_meeting(
        career, mclaren["id"], d["id"], ("seat", 1), rng=_SeqRng([0.0, 0.0]))
    assert not ok and "happy" in why and "neighbouring" in why
    assert mclaren["money"] == before
    assert not career.get("meetings")
    assert not career.get("meeting_cooldowns")
    assert not career.get("media_rumours")


def test_v081_activity_any_level_with_declines():
    """v0.8.1 (6): activities are open at ANY happiness level.
    Unhappy drivers turn one down 60% of the time, very unhappy 85%;
    neutral and above always accept (unless ill). A declined activity
    costs nothing and does not use up the one-per-round slot. One
    activity per driver per round still applies, and they cost money
    and raise happiness + loyalty."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    d = game.get_driver(career, mclaren["driver1_id"])
    d2 = game.get_driver(career, mclaren["driver2_id"])
    mclaren["money"] = 100.0
    # Neutral: accepts — the simulator session costs $0.3M, +0.15/+0.05.
    ok, why = game.do_activity(career, mclaren["id"], d["id"], "simulator")
    assert ok, why
    assert mclaren["money"] == pytest.approx(99.7)
    assert d["happiness"] == pytest.approx(3.15)
    assert d["loyalty"] == pytest.approx(3.05)
    # A second activity this round is refused.
    ok, why = game.do_activity(career, mclaren["id"], d["id"], "team_dinner")
    assert not ok and "already did an activity" in why
    # Unhappy: a 0.5 roll declines (60%), a 0.7 roll accepts — and
    # the decline costs nothing and leaves the slot free.
    _set_relation(d2, mclaren["id"], 2.0, 3.0)
    ok, why = game.do_activity(career, mclaren["id"], d2["id"],
                               "simulator", rng=_SeqRng([0.5]))
    assert not ok and "turned the activity down" in why
    assert mclaren["money"] == pytest.approx(99.7)
    assert d2.get("activity_round") is None
    ok, why = game.do_activity(career, mclaren["id"], d2["id"],
                               "simulator", rng=_SeqRng([0.7]))
    assert ok, why
    assert mclaren["money"] == pytest.approx(99.4)
    assert d2["happiness"] == pytest.approx(2.15)
    # Very unhappy: a 0.5 roll declines (85%), a 0.9 roll accepts.
    career["round"] = 2
    _set_relation(d2, mclaren["id"], 1.0, 3.0)
    ok, why = game.do_activity(career, mclaren["id"], d2["id"],
                               "team_dinner", rng=_SeqRng([0.5]))
    assert not ok and "turned the activity down" in why
    assert mclaren["money"] == pytest.approx(99.4)
    ok, why = game.do_activity(career, mclaren["id"], d2["id"],
                               "team_dinner", rng=_SeqRng([0.9]))
    assert ok, why
    assert mclaren["money"] == pytest.approx(99.2)
    # Ill: refused outright, no charge.
    career["round"] = 3
    d2["ill_until_round"] = 5
    ok, why = game.do_activity(career, mclaren["id"], d2["id"], "simulator")
    assert not ok and "ill" in why
    assert mclaren["money"] == pytest.approx(99.2)
    # The catalogue has 12+ realistic activities, each with a cost.
    assert len(game.ACTIVITIES) >= 12
    assert len({a["id"] for a in game.ACTIVITIES}) == len(game.ACTIVITIES)
    for a in game.ACTIVITIES:
        assert a["cost"] > 0 and a["happiness"] > 0 and a["loyalty"] > 0


def test_v080_bribe_want_values():
    """v0.8.0 (3): the bribe want — very happy $2M base, x2 at high
    loyalty (4-5), x1 at medium (3), x0.5 at low (1-2); unhappy $0.
    Pinned: very happy + high loyalty $4M, + low loyalty $1M."""
    from f1 import rules as rules_mod
    assert rules_mod.bribe_want(5.0, 4.5) == 4.0
    assert rules_mod.bribe_want(5.0, 1.5) == 1.0
    assert rules_mod.bribe_want(5.0, 3.0) == 2.0
    assert rules_mod.bribe_want(4.0, 4.5) == 2.0
    assert rules_mod.bribe_want(4.0, 3.0) == 1.0
    assert rules_mod.bribe_want(4.0, 1.0) == 0.5
    assert rules_mod.bribe_want(2.0, 5.0) == 0.0
    assert rules_mod.bribe_want(3.0, 5.0) == 0.0
    assert rules_mod.bribe_want(1.0, 1.0) == 0.0
    # Through the career helper: a poach prices the driver's relation
    # with their CURRENT team, not the approaching team.
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    rb, d = _poach_target(career)
    _set_relation(d, rb["id"], 5.0, 4.5)
    assert game.bribe_want_for(career, mclaren["id"], d) == 4.0


def test_v080_happiness_hidden_from_broadcast_and_market():
    """v0.8.0 (3): happiness/loyalty never leave the server in the
    public broadcast or the Driver Market — only the viewer's own
    team sees its drivers' values, in the private my_team message
    (the bribe want in the market is computed server-side)."""
    import server as srv
    career = two_human_career()
    mclaren = team_by_name(career, "McLaren")
    d = game.get_driver(career, mclaren["driver1_id"])
    _set_relation(d, mclaren["id"], 4.0, 4.0)
    state = game.public_state(career, chart_on=True, viewer_team_id=mclaren["id"])
    for sd in state["drivers"]:
        for k in ("happiness", "loyalty", "happiness_by_team",
                  "loyalty_by_team", "tuning_against_streak", "tr_bench_races"):
            assert k not in sd, (sd["name"], k)
    costs = state["market_costs"]
    assert costs  # the market exists for the viewer
    assert all("happiness" not in str(v) for v in costs.values())
    # The private message carries the relation — own team only.
    room = {"career": career, "chart_on": True}
    mine = srv.my_team_public(room, "p-matthew")
    got = next(x for x in mine["drivers"] if x["id"] == d["id"])
    assert got["happiness"] == 4.0 and got["happiness_word"] == "Happy"
    assert got["loyalty"] == 4.0
    assert "happiness_by_team" not in got
    theirs = srv.my_team_public(room, "p-cousin")
    assert all(x["id"] != d["id"] for x in theirs["drivers"])


def test_v080_meeting_required_before_poach():
    """v0.8.0 (4): no secret meeting, no poach — main-seat, T&R and
    academy signings of another team's driver are all refused until
    a meeting for that target has been agreed."""
    career = two_human_career()
    mclaren = team_by_name(career, "McLaren")
    mclaren["money"] = 100.0
    rb, d = _poach_target(career)
    ok, why = game.sign_driver(career, mclaren["id"], d["id"], 1)
    assert not ok and "secret meeting" in why
    ok, why = game.sign_tr_driver(career, mclaren["id"], d["id"], 2)
    assert not ok and "secret meeting" in why
    # An academy junior of another team is a poach too.
    ferrari = team_by_name(career, "Ferrari")
    junior = next(x for x in game.free_agents(career)
                  if not x.get("academy_team_id") and x["age"] <= 25)
    ok, why = game.add_academy_driver(career, ferrari["id"], junior["id"])
    assert ok, why
    ok, why = game.sign_tr_driver(career, mclaren["id"], junior["id"], 2)
    assert not ok and "secret meeting" in why


def test_v080_meeting_cost_and_unhappy_top_team_accepts():
    """v0.8.0 (4): a meeting costs $0.5M when requested; a very
    unhappy driver approached by a top team very likely accepts
    (probability >= 0.9 here) — and the agreed meeting is stored
    with its target and bribe."""
    career = two_human_career()
    mclaren = team_by_name(career, "McLaren")  # P1
    aston = team_by_name(career, "Aston Martin")  # P7
    d = game.get_driver(career, aston["driver1_id"])
    d["rep"] = 2.5
    _set_relation(d, aston["id"], 1.0, 3.0)
    p = game.meeting_accept_probability(career, mclaren, d, ("seat", 1))
    assert p >= 0.9
    before = mclaren["money"]
    ok, why = game.request_meeting(
        career, mclaren["id"], d["id"], ("seat", 1), bribe=1.0,
        rng=_SeqRng([0.5, 0.99]))
    assert ok, why
    assert mclaren["money"] == pytest.approx(before - 0.5)
    meeting = game.agreed_meeting(career, mclaren["id"], d["id"], ("seat", 1))
    assert meeting is not None and meeting["bribe"] == 1.0
    # A meeting for Seat 1 does not cover Seat 2.
    assert game.agreed_meeting(career, mclaren["id"], d["id"], ("seat", 2)) is None


def test_v080_meeting_decline_cooldown_1_to_5():
    """v0.8.0 (4): a failed meeting means a 1-5 round wait before
    that team may try the driver again — happier / more loyal means
    longer. The cooldown ticks down per round and blocks retries."""
    def _decline(happiness, loyalty, approach_team="Cadillac"):
        career = two_human_career()
        team = team_by_name(career, approach_team)
        mclaren = team_by_name(career, "McLaren")  # P1
        d = game.get_driver(career, mclaren["driver1_id"])
        d["rep"] = 2.5
        _set_relation(d, mclaren["id"], happiness, loyalty)
        team["money"] = 100.0
        ok, why = game.request_meeting(
            career, team["id"], d["id"], ("seat", 1),
            rng=_SeqRng([0.99, 0.99]))
        # request_meeting returns True whenever the request itself
        # went through (the $0.5M was spent) — the decline is in
        # the message, not the ok flag.
        assert "turned the meeting down" in why, why
        return career, team, d

    # Very unhappy + disloyal: 1 round. Neutral: 3. Very happy +
    # loyal (approached by a bottom team, so the RNG declines): 5.
    career, team, d = _decline(1.0, 1.0)
    assert game.meeting_cooldown_left(career, team["id"], d["id"]) == 1
    career, team, d = _decline(3.0, 3.0)
    assert game.meeting_cooldown_left(career, team["id"], d["id"]) == 3
    career, team, d = _decline(5.0, 5.0)
    assert game.meeting_cooldown_left(career, team["id"], d["id"]) == 5
    # Retrying during the cooldown is refused, naming the wait.
    ok, why = game.request_meeting(
        career, team["id"], d["id"], ("seat", 1), rng=_SeqRng([0.0, 0.99]))
    assert "5 round" in why
    # It ticks down a round at a time.
    game.tick_meeting_cooldowns(career)
    assert game.meeting_cooldown_left(career, team["id"], d["id"]) == 4


def test_v080_main_seat_beats_tr_offer():
    """v0.8.0 (4): drivers always prefer a main seat over a T&R role
    — the meeting chance for a seat is exactly 0.30 higher than for
    a T&R slot, all else equal."""
    career = two_human_career()
    red_bull = team_by_name(career, "Red Bull")  # P3
    aston = team_by_name(career, "Aston Martin")  # P7
    d = game.get_driver(career, aston["driver1_id"])
    p_seat = game.meeting_accept_probability(career, red_bull, d, ("seat", 1))
    p_tr = game.meeting_accept_probability(career, red_bull, d, ("tr", None))
    assert p_seat - p_tr == pytest.approx(0.30)
    assert p_seat > p_tr


def test_v080_meeting_leak():
    """v0.8.0 (4): a leaked meeting (15%) costs both sides
    reputation — approaching team -2.0, driver -0.1 — drops the
    leaking driver's happiness/loyalty with their current team by
    0.5 each, raises the OTHER driver's loyalty by 0.3, and a BOT
    current team shifts its tuning in favour of the other driver."""
    career = two_human_career()
    mclaren = team_by_name(career, "McLaren")
    red_bull = team_by_name(career, "Red Bull")  # bot team
    assert red_bull.get("is_ai") or not red_bull.get("owner_player_id")
    d = game.get_driver(career, red_bull["driver2_id"])
    other = game.get_driver(career, red_bull["driver1_id"])
    _set_relation(d, red_bull["id"], 3.0, 3.0)
    _set_relation(other, red_bull["id"], 3.0, 3.0)
    rep_before = game.team_rep_of(mclaren)
    d_rep_before = d["rep"]
    mclaren["money"] = 100.0
    ok, why = game.request_meeting(
        career, mclaren["id"], d["id"], ("seat", 1), rng=_SeqRng([0.0, 0.0]))
    assert ok, why  # accepted AND leaked
    assert any(m["kind"] == "leak" and "LEAKED" in m["text"]
               for m in career["media_rumours"])
    assert game.team_rep_of(mclaren) == pytest.approx(rep_before - 2.0)
    assert d["rep"] == pytest.approx(d_rep_before - 0.1)
    assert game.happiness_with(d, red_bull["id"]) == 2.5
    assert game.loyalty_with(d, red_bull["id"]) == 2.5
    assert game.loyalty_with(other, red_bull["id"]) == pytest.approx(3.3)
    # The other driver is Seat 1, so tuning moves +1 in their favour.
    assert red_bull["tuning"] == 1


def test_v080_poach_notifies_losing_team_three_places():
    """v0.8.0 (4): a successful poach notifies the losing team in
    THREE places — the Transfers feed, a Media entry, and a notice
    in their Drivers tab — consumes the meeting and pays the bribe
    on top of the (unchanged) poaching costs."""
    career = two_human_career()
    mclaren = team_by_name(career, "McLaren")
    rb, d = _poach_target(career)
    _set_relation(d, rb["id"], 1.0, 3.0)  # very unhappy: accepts
    mclaren["money"] = 100.0
    quote = game.sign_cost(career, mclaren["id"], d["id"], ("seat", 1))
    assert quote["poach_fee"] > 0
    ok, why = game.request_meeting(
        career, mclaren["id"], d["id"], ("seat", 1), bribe=2.0,
        rng=_SeqRng([0.0, 0.99]))
    assert ok, why
    money_after_meeting = mclaren["money"]
    ok, why = game.sign_driver(career, mclaren["id"], d["id"], 1)
    assert ok, why
    # Poaching costs + bribe, all paid.
    assert mclaren["money"] == pytest.approx(
        money_after_meeting - quote["total"] - 2.0)
    assert not game.agreed_meeting(career, mclaren["id"], d["id"], ("seat", 1))
    assert any(f"left {rb['name']} (poached by {mclaren['name']})" in t
               for t in _ttexts(career))
    assert any("secret meeting sealed the move" in m["text"]
               for m in career["media_rumours"])
    assert any(n["team_id"] == rb["id"]
               and d["name"] in n["text"]
               for n in career["team_notices"])
    assert d["team_id"] == mclaren["id"] and d["status"] == "f1"


def test_v080_poaching_costs_unchanged():
    """v0.8.0 (4): the transfer + poaching costs are UNCHANGED by
    the meeting flow — a main-driver poach costs their break fee,
    an academy poach costs half the salary term + $1.0M."""
    career = two_human_career()
    mclaren = team_by_name(career, "McLaren")
    rb, d = _poach_target(career)
    quote = game.sign_cost(career, mclaren["id"], d["id"], ("seat", 1))
    assert quote["poach_fee"] == pytest.approx(game.break_fee_for_driver(career, d))
    ferrari = team_by_name(career, "Ferrari")
    junior = next(x for x in game.free_agents(career)
                  if not x.get("academy_team_id") and x["age"] <= 25)
    ok, why = game.add_academy_driver(career, ferrari["id"], junior["id"])
    assert ok, why
    quote = game.sign_cost(career, mclaren["id"], junior["id"], ("tr", None))
    assert quote["poach_fee"] == pytest.approx(game.academy_poach_fee(career, junior))
    assert quote["poach_fee"] == pytest.approx(
        game.rounds_remaining_for(career, junior) * junior["salary"] / 12 * 0.5 + 1.0)


def test_v080_academy_tr_mutual_exclusion():
    """v0.8.0 (2): a T&R driver cannot be signed as academy, and an
    academy driver cannot be signed as T&R — except through the
    poach path (a secret meeting + the fee)."""
    career = two_human_career()
    mclaren = team_by_name(career, "McLaren")
    ferrari = team_by_name(career, "Ferrari")
    # A T&R driver cannot join an academy (status "tr" is not unsigned).
    tr = next(d for d in game.team_tr_drivers(career, mclaren))
    ok, why = game.add_academy_driver(career, ferrari["id"], tr["id"])
    assert not ok and "unsigned" in why
    # An academy junior cannot be signed as T&R without a meeting…
    junior = next(x for x in game.free_agents(career)
                  if not x.get("academy_team_id") and x["age"] <= 25)
    ok, why = game.add_academy_driver(career, ferrari["id"], junior["id"])
    assert ok, why
    mclaren["money"] = 100.0
    ok, why = game.sign_tr_driver(career, mclaren["id"], junior["id"], 2)
    assert not ok and "secret meeting" in why
    # …but the poach path (meeting + fee) still allows it.
    _set_relation(junior, ferrari["id"], 1.0, 3.0)
    ok, why = game.request_meeting(
        career, mclaren["id"], junior["id"], ("tr", None),
        rng=_SeqRng([0.0, 0.99]))
    assert ok, why
    ok, why = game.sign_tr_driver(career, mclaren["id"], junior["id"], 2)
    assert ok, why
    assert junior["status"] == "tr" and junior["team_id"] == mclaren["id"]
    assert junior.get("academy_team_id") is None


def test_v080_wb_academy_poach_needs_meeting():
    """v0.8.0 (4): winter-break offers CAN poach an academy junior
    (the only poachable class in the break) — so the meeting gates
    those offers too. The meeting is requestable in the break; the
    signing charges the academy poach fee + the meeting bribe and
    notifies the losing academy team in three places."""
    career = two_human_career()
    mclaren = team_by_name(career, "McLaren")
    ferrari = team_by_name(career, "Ferrari")
    junior = next(x for x in game.free_agents(career)
                  if not x.get("academy_team_id") and x["age"] <= 25)
    ok, why = game.add_academy_driver(career, ferrari["id"], junior["id"])
    assert ok, why
    # McLaren needs an empty seat for the offer to land.
    old = game.get_driver(career, mclaren["driver1_id"])
    mclaren["driver1_id"] = None
    old["status"] = "free"
    old["last_team_id"] = mclaren["id"]
    career["phase"] = "winter_break"
    career["wb_round"] = 1
    mclaren["money"] = 100.0
    offer = {"driver_id": junior["id"], "target": "seat", "seat": 1, "years": 2}
    ok, why = game.submit_winter_offers(career, mclaren["id"], [offer])
    assert not ok and "secret meeting" in why
    # The meeting IS requestable in the winter break.
    _set_relation(junior, ferrari["id"], 1.0, 3.0)
    ok, why = game.request_meeting(
        career, mclaren["id"], junior["id"], ("seat", 1), bribe=1.0,
        rng=_SeqRng([0.0, 0.99]))
    assert ok, why
    ok, why = game.submit_winter_offers(career, mclaren["id"], [offer])
    assert ok, why
    fee = game.academy_poach_fee(career, junior)
    money_before = mclaren["money"]
    game.resolve_winter_break_round(career, rng=seeded(3))
    assert junior["status"] == "f1" and mclaren["driver1_id"] == junior["id"]
    assert mclaren["money"] == pytest.approx(money_before - fee - 1.0)
    assert not game.agreed_meeting(career, mclaren["id"], junior["id"], ("seat", 1))
    assert any(f"left {ferrari['name']}'s academy (poached by {mclaren['name']})" in t
               for t in _ttexts(career))
    assert any("secret meeting sealed the move" in m["text"]
               for m in career["media_rumours"])
    assert any(n["team_id"] == ferrari["id"] and junior["name"] in n["text"]
               for n in career["team_notices"])


def test_v080_bot_winter_offers_skip_academy_juniors():
    """v0.8.0 (4): bots cannot hold secret meetings, so they cannot
    poach another team's academy junior — bot winter-break offers
    never pick one, even when they are the best driver available."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    ferrari = team_by_name(career, "Ferrari")
    junior = next(x for x in game.free_agents(career)
                  if not x.get("academy_team_id") and x["age"] <= 25)
    ok, why = game.add_academy_driver(career, mclaren["id"], junior["id"])
    assert ok, why
    junior["rep"] = 5.0  # the best available driver by reputation
    # Ferrari has an empty seat to fill.
    old = game.get_driver(career, ferrari["driver1_id"])
    ferrari["driver1_id"] = None
    old["status"] = "free"
    ferrari["money"] = 100.0
    game.bot_winter_offers(career, ferrari, rng=seeded(2))
    offers = (career.get("wb_offers") or {}).get(ferrari["id"]) or []
    assert offers, "bot with an empty seat should still offer someone"
    assert all(o["driver_id"] != junior["id"] for o in offers)


def test_v080_bot_midseason_fill_skips_academy_juniors():
    """v0.8.0 (4): the same for the mid-season bot manager — an
    empty main seat is filled from the best UNAFFILIATED free agent,
    never another team's academy junior (the meeting gate would make
    the signing fail and leave the seat empty)."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    ferrari = team_by_name(career, "Ferrari")
    junior = next(x for x in game.free_agents(career)
                  if not x.get("academy_team_id") and x["age"] <= 25)
    ok, why = game.add_academy_driver(career, mclaren["id"], junior["id"])
    assert ok, why
    junior["rep"] = 5.0
    best = game._best_available_driver(career, statuses=("free",),
                                       team_id=ferrari["id"])
    assert best is not None and best["id"] != junior["id"]
    # The full bot turn fills Ferrari's empty seat with someone else.
    old = game.get_driver(career, ferrari["driver1_id"])
    ferrari["driver1_id"] = None
    old["status"] = "free"
    ferrari["money"] = 100.0
    game.ai_act(career, ferrari, rng=seeded(4))
    assert ferrari["driver1_id"] is not None
    assert ferrari["driver1_id"] != junior["id"]
    assert junior["status"] == "free" and junior.get("academy_team_id") == mclaren["id"]


def test_v080_advance_gate():
    """v0.8.0 (6): a team with an empty main seat cannot advance —
    the refusal names the seat — and the host override is blocked
    too while ANY team is short. Both succeed once every team has 2."""
    career = two_human_career()
    mclaren = team_by_name(career, "McLaren")
    old = game.get_driver(career, mclaren["driver1_id"])
    mclaren["driver1_id"] = None
    old["status"] = "free"
    ok, ran, why = game.advance_team(career, mclaren["id"], rng=seeded(1))
    assert not ok and "McLaren Seat 1" in why and "empty" in why
    assert career["phase"] == "round"
    ok, ran, why = game.host_skip_advance(career, "p-matthew", True, rng=seeded(1))
    assert not ok and "McLaren Seat 1" in why
    # Fill the seat: both the advance and the host override work.
    free = next(d for d in game.free_agents(career) if not d.get("academy_team_id"))
    mclaren["driver1_id"] = free["id"]
    free["status"] = "f1"
    free["team_id"] = mclaren["id"]
    ok, ran, why = game.advance_team(career, mclaren["id"], rng=seeded(1))
    assert ok, why
    assert career["phase"] == "round"  # Mercedes has not advanced yet
    ok, ran, why = game.host_skip_advance(career, "p-matthew", True, rng=seeded(1))
    assert ok and ran, why
    assert career["phase"] == "qual_results"


def test_v080_cheats_extra_driver_classes():
    """v0.8.0 (7): the cheats menu lists T&R, free agents, academy
    juniors and F2 drivers, each editable (rating clamped to the age
    range) with the retirement toggle — like the mini standings."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    state = game.cheat_state(career)
    extra = state["extra"]
    assert set(extra) == {"tr", "free", "academy", "f2"}
    assert extra["tr"] and extra["free"] and extra["f2"]
    # An academy junior exists once one is added.
    junior = next(x for x in game.free_agents(career)
                  if not x.get("academy_team_id") and x["age"] <= 25)
    ok, why = game.add_academy_driver(career, mclaren["id"], junior["id"])
    assert ok, why
    extra = game.cheat_state(career)["extra"]
    assert any(r["id"] == junior["id"] for r in extra["academy"])
    # Every class edits with clamping (age 19 -> [61, 85]).
    for section in ("tr", "free", "academy", "f2"):
        row = game.cheat_state(career)["extra"][section][0]
        d = game.get_driver(career, row["id"])
        d["age"] = 19
        ok, why = game.cheat_set_rating(career, d["id"], 200)
        assert ok, why
        assert d["perf"] == 85 and "clamped" in why
    # The retirement toggle works on a T&R driver while the master is on.
    game.set_custom_retirements(career, True)
    tr = game.cheat_state(career)["extra"]["tr"][0]
    ok, why = game.set_retire_toggle(career, tr["id"], True)
    assert ok, why
    assert game.get_driver(career, tr["id"])["retire_at_season_end"] is True


def test_v080_toggled_tr_driver_retires_at_season_close():
    """v0.8.0 (7): a toggled T&R driver retires at season close when
    the custom-retirements master is on — and only toggled drivers
    retire (the random rolls are off)."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = team_by_name(career, "McLaren")
    tr = game.team_tr_drivers(career, mclaren)[0]
    game.set_custom_retirements(career, True)
    ok, why = game.set_retire_toggle(career, tr["id"], True)
    assert ok, why
    game.end_of_season(career, rng=seeded(5))
    assert tr["name"] in (career.get("retired_this_season") or [])
    assert tr not in game.team_tr_drivers(career, mclaren)
    # Nobody else retired — only the toggled driver.
    assert len(career.get("retired_this_season") or []) == 1


def test_v080_media_rumours_generated_per_race():
    """v0.8.0 (3): two PlanetF1-style rumours are generated after
    every race — one grounded, one random — naming a driver and
    their team."""
    career = two_human_career()
    play_full_round(career, seeded(11))
    media = career.get("media_rumours") or []
    assert len(media) == 2
    for m in media:
        assert m["text"] and m["kind"] in ("rumour", "transfer", "performance",
                                           "happy", "unhappy", "leak")
        assert m["year"] == career["year"]


def test_v080_tab_groups_and_media_split_markup():
    """v0.8.0 (3/5): the dashboard has 11 tabs in 4 groups (Team,
    Money & Deals, Results, System), each group titled and shaded;
    History and Media & Rumours are separate tabs; the tab bar never
    scrolls horizontally."""
    import re
    html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
    tabs = re.findall(r'<button data-tab="([^"]+)"', html)
    assert tabs == ["drivers", "research", "notes", "finances", "relations",
                    "market", "standings", "chart", "history", "media", "saves"]
    for title in ("Team", "Money &amp; Deals", "Results", "System"):
        assert title in html
    assert 'id="tab-media"' in html and 'id="media-box"' in html
    assert "History &amp; Rumours" not in html
    assert ".tabs {" not in html and ".tabs button" not in html
    m = re.search(r"\.tab-groups \{[^}]*\}", html)
    assert m and "overflow" not in m.group(0)  # the tab bar never scrolls
    assert "tab-groups" in html and "tab-group-title" in html
    # F2 T&R: the small badge + the T&R-team colour wiring.
    assert "f2-tr-badge" in html
    assert "trTeam" in html


def test_v080_s4_save_migrates_to_v18():
    """v0.8.0 (migration): the Season 4 Round 2 save (v12) migrates
    to v18 with happiness neutral and loyalty defaulted for every
    current relation — and the save file itself is never modified."""
    path = Path(__file__).parent.parent / "saves" / "matthew-max-wugynr-s4-round2.json"
    if not path.exists():
        pytest.skip("Season 4 save not present in this checkout")
    before = path.read_bytes()
    career = load_save(str(path.parent), "matthew-max-wugynr-s4-round2")
    assert career is not None
    from f1.saves import SAVE_VERSION
    assert SAVE_VERSION == 18
    assert career["version"] == 18
    assert "donation_requests" not in career
    for key in ("meetings", "meeting_cooldowns", "media_rumours",
                "driver_messages", "team_notices"):
        assert key in career
    for d in career["drivers"]:
        assert "happiness" in d and "loyalty" in d
        assert "happiness_by_team" in d and "loyalty_by_team" in d
        if d.get("status") in ("f1", "tr") and d.get("team_id"):
            assert game.happiness_with(d, d["team_id"]) == 3.0
            assert game.loyalty_with(d, d["team_id"]) == 3.0
    # The file on disk is byte-identical.
    assert path.read_bytes() == before


def test_v080_tab_groups_no_scroll_browser():
    """v0.8.0 (5, browser): all 11 tabs are visible without
    horizontal scrolling at desktop width and at a narrow (mobile)
    width, and the four group titles exist. No JS errors."""
    pytest.importorskip("playwright")
    import re
    import subprocess
    import time
    import urllib.request
    port = 8818
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise AssertionError("server did not come up")
        from playwright.sync_api import sync_playwright
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                p1 = browser.new_page(viewport={"width": 1280, "height": 800})
                p1.on("pageerror", lambda e: errors.append(str(e)))
                p1.goto(f"http://127.0.0.1:{port}/")
                p1.fill("#home-nick", "Max")
                p1.click("#btn-create")
                p1.wait_for_selector("#screen-lobby:not(.hidden)", timeout=15000)
                p1.click("#btn-new-career")
                p1.wait_for_selector("[data-team]", timeout=15000)
                p1.evaluate("""() => {
                    const rows = [...document.querySelectorAll('#team-list > *')];
                    rows.find(r => r.textContent.includes('McLaren'))
                        .querySelector('[data-team]').click();
                }""")
                p1.wait_for_selector("#btn-start-career:not(.hidden)", timeout=15000)
                code = re.search(r"\b[A-Z0-9]{6}\b",
                                 p1.inner_text("#screen-lobby")).group(0)
                p2 = browser.new_page(viewport={"width": 1280, "height": 800})
                p2.on("pageerror", lambda e: errors.append(str(e)))
                p2.goto(f"http://127.0.0.1:{port}/?code={code}")
                p2.fill("#home-nick", "Matthew")
                p2.click("#btn-join")
                p2.wait_for_selector("[data-team]", timeout=15000)
                p2.evaluate("""() => {
                    const rows = [...document.querySelectorAll('#team-list > *')];
                    rows.find(r => r.textContent.includes('Ferrari'))
                        .querySelector('[data-team]').click();
                }""")
                p1.click("#btn-start-career")
                p1.wait_for_selector("#screen-game:not(.hidden)", timeout=15000)

                def _check_tabs(page, label):
                    info = page.evaluate("""() => {
                        const g = document.querySelector('.tab-groups');
                        const tabs = [...document.querySelectorAll('.tab-group button')];
                        const titles = [...document.querySelectorAll('.tab-group-title')]
                            .map(t => t.textContent.trim());
                        return {scrollW: g.scrollWidth, clientW: g.clientWidth,
                                overflowX: getComputedStyle(g).overflowX,
                                tabCount: tabs.length,
                                allVisible: tabs.every(b => {
                                    const r = b.getBoundingClientRect();
                                    return r.width > 0 && r.height > 0; }),
                                titles};
                    }""")
                    assert info["tabCount"] == 11, (label, info)
                    assert info["allVisible"], (label, info)
                    assert info["scrollW"] <= info["clientW"], (label, info)
                    assert info["overflowX"] in ("visible", "clip"), (label, info)
                    assert info["titles"] == ["Team", "Money & Deals",
                                              "Results", "System"], (label, info)

                _check_tabs(p1, "desktop")
                # Narrow (mobile) width: the groups wrap, still no scroll.
                p1.set_viewport_size({"width": 390, "height": 844})
                p1.wait_for_timeout(300)
                _check_tabs(p1, "mobile")
            finally:
                browser.close()
        assert not errors, errors
    finally:
        proc.terminate()


# ---------------------------------------------------------------------------
# v0.8.1 — happiness fixes, grounded rumours, calmer bots, junior
# promotion fix, reg cost $0.30M, activities at any level, 2-lap
# qualifying, driver-market redesign.
# ---------------------------------------------------------------------------


def test_v081_ferrari_sim_disadvantaged_driver_drops_despite_climb():
    """v0.8.1 (1): the full-season Ferrari case from Matthew's
    playtest — tuning 3 for Hamilton, Leclerc disadvantaged. Even
    when the team CLIMBS the constructors' (a positive trend, which
    used to mask the tuning signal) and the gap is large (Leclerc's
    rating dropped), the disadvantaged driver still drops to unhappy
    within ~6-7 races, while the favoured driver stays neutral or
    better and the tuning keeps favouring Hamilton."""
    career = game.build_new_career("Test", "Matthew", chart_on=True)
    ok, _ = game.choose_team(career, team_by_name(career, "Ferrari")["id"],
                             "p-matthew")
    assert ok
    ferrari = team_by_name(career, "Ferrari")
    ham = game.get_driver(career, ferrari["driver1_id"])
    lec = game.get_driver(career, ferrari["driver2_id"])
    assert ham["name"] == "Lewis Hamilton"
    assert lec["name"] == "Charles Leclerc"
    lec["perf"] = 62  # a large gap to Hamilton
    ferrari["tuning"] = 3
    rng = random.Random(42)
    pos_r3 = None
    for r in range(1, 8):
        if r == 4:
            # Mid-season development: Ferrari climbs the order.
            ferrari["perf"] = 385
        game.run_race_weekend(career, rng=rng)
        game.complete_race_weekend(career, rng=rng)
        ferrari = team_by_name(career, "Ferrari")
        if r == 3:
            pos_r3 = game.constructor_pos(career, ferrari["id"])
    pos_r7 = game.constructor_pos(career, ferrari["id"])
    # Ferrari climbed (a positive team trend).
    assert pos_r7 < pos_r3, (pos_r3, pos_r7)
    # ... and the disadvantaged driver still dropped to unhappy.
    assert lec["happiness"] < 2.5, lec["happiness"]
    # The favoured driver did not drop.
    assert ham["happiness"] >= 3.0, ham["happiness"]
    # Tuning still favours Hamilton (he is still driver 1).
    assert ferrari["driver1_id"] == ham["id"] and ferrari["tuning"] == 3


def test_v081_seat_swap_keeps_tuning_on_same_driver():
    """v0.8.1 (1a): tuning follows the DRIVER across a D1/D2 seat
    swap — when the seats flip by team points, the tuning sign
    flips too, so it keeps favouring the same driver, and the
    disadvantaged driver's tuning_against_streak keeps accumulating
    instead of resetting."""
    career = game.build_new_career("Test", "Matthew", chart_on=True)
    ok, _ = game.choose_team(career, team_by_name(career, "Ferrari")["id"],
                             "p-matthew")
    assert ok
    ferrari = team_by_name(career, "Ferrari")
    ham = game.get_driver(career, ferrari["driver1_id"])
    lec = game.get_driver(career, ferrari["driver2_id"])
    ferrari["tuning"] = 3
    rng = random.Random(42)
    game.run_race_weekend(career, rng=rng)
    game.complete_race_weekend(career, rng=rng)
    # After one race the disadvantaged driver has a streak of 1.
    assert ferrari["driver1_id"] == ham["id"] and ferrari["tuning"] == 3
    assert lec.get("tuning_against_streak") == 1
    # Force a swap next race: Leclerc ahead on team points.
    lec["team_points"] = 200
    ham["team_points"] = 0
    game.run_race_weekend(career, rng=rng)
    game.complete_race_weekend(career, rng=rng)
    # The seats flipped, the tuning sign flipped with them — it
    # still favours Hamilton (now driver 2) — and Leclerc's streak
    # kept accumulating instead of resetting.
    assert ferrari["driver1_id"] == lec["id"]
    assert ferrari["driver2_id"] == ham["id"]
    assert ferrari["tuning"] == -3
    assert lec.get("tuning_against_streak") == 2


def _v081_positions_series_for_year(career, year):
    """Per-round constructors' positions for one season, rebuilt
    from race_history (mirrors game._season_positions_by_round,
    which only covers the CURRENT year — after the rollover that is
    the next season)."""
    series, cum = [], {}
    for entry in career.get("race_history") or []:
        if entry.get("year") != year:
            continue
        for row in entry.get("race") or []:
            tid = row.get("team_id")
            if tid:
                cum[tid] = cum.get(tid, 0) + (row.get("points") or 0)
        ranked = sorted(career["teams"],
                        key=lambda t: (-cum.get(t["id"], 0), t["name"]))
        series.append({t["id"]: i + 1 for i, t in enumerate(ranked)})
    return series


def test_v081_media_rumours_never_contradict_state():
    """v0.8.1 (2): every rumour traces to a verified real condition.
    Over a full seeded season, a \"without points\" rumour only ever
    names a team that truly scored 0 points in the last 3 races as
    of that round, and a \"board wants results\" rumour only ever
    names a team that truly dropped 2+ constructors' places over
    the last 4 rounds as of that round — never the v0.8.0 nonsense
    (a scoreless claim about a team fresh off two podiums, a board
    demanding results from a team on a winning streak)."""
    career = game.build_new_career("Test", "Matthew", chart_on=True)
    rng = random.Random(7)
    for _ in range(12):
        game.run_race_weekend(career, rng=rng)
        game.complete_race_weekend(career, rng=rng)
    series = _v081_positions_series_for_year(career, 2026)

    def team_named(text):
        for t in career["teams"]:
            if t["name"] in text:
                return t
        return None

    def points_in_rounds(team_id, year, rounds):
        total = 0
        for e in career.get("race_history") or []:
            if e.get("year") == year and e.get("round") in rounds:
                for row in e.get("race") or []:
                    if row.get("team_id") == team_id:
                        total += row.get("points") or 0
        return total

    scoreless = pressure = 0
    for m in career.get("media_rumours") or []:
        text, rnd, year = m["text"], m["round"], m["year"]
        if "without points" in text:
            scoreless += 1
            t = team_named(text)
            assert t is not None, text
            pts = points_in_rounds(t["id"], year, {rnd - 2, rnd - 1, rnd})
            assert pts == 0, (text, pts)
        if "board wants results" in text:
            pressure += 1
            t = team_named(text)
            assert t is not None, text
            s = series[:rnd]
            back = s[rnd - 4] if rnd >= 4 else s[0]
            move = back.get(t["id"], s[-1][t["id"]]) - s[-1][t["id"]]
            assert move <= -2, (text, move)
    # The season produced rumours, and every scoreless / pressure
    # claim in it checked out (no minimum count is asserted — the
    # race outcomes vary with the global random state, so some
    # seasons produce no scoreless claim at all; the gate itself is
    # pinned by test_v081_media_scoreless_gate below).
    assert len(career.get("media_rumours") or []) >= 2


def test_v081_media_scoreless_gate():
    """v0.8.1 (2): the scoreless gate, pinned directly — with three
    rounds of fabricated history in which Cadillac scored 0 points
    and Ferrari scored in every race, the generator may name
    Cadillac in a \"without points\" rumour but NEVER Ferrari, over
    20 calls with different seeds. (The v0.8.0 bug: the claim was
    applied to a random team regardless of form.)"""
    career = game.build_new_career("Test", "Matthew")
    career["round"] = 4
    ferrari = team_by_name(career, "Ferrari")
    cadillac = team_by_name(career, "Cadillac")
    fer_d = game.get_driver(career, ferrari["driver1_id"])
    cad_d = game.get_driver(career, cadillac["driver1_id"])
    career["race_history"] = [
        {"year": 2026, "round": r, "circuit": f"Round {r}",
         "race": [{"driver_id": fer_d["id"], "team_id": ferrari["id"],
                   "pos": 1, "points": 25},
                  {"driver_id": cad_d["id"], "team_id": cadillac["id"],
                   "pos": 20, "points": 0}]}
        for r in (1, 2, 3)]
    assert game._team_points_last_n(career, cadillac["id"], 3) == 0
    assert game._team_points_last_n(career, ferrari["id"], 3) > 0
    for seed in range(20):
        before = len(career.get("media_rumours") or [])
        game.generate_media_rumours(career, rng=random.Random(seed))
        for m in (career.get("media_rumours") or [])[before:]:
            if "without points" in m["text"]:
                # Ferrari scored in every race — it must never be
                # named here. (Any other team may be: in this
                # fabricated history every team but Ferrari scored
                # 0, so a scoreless claim about them is true.)
                assert "Ferrari" not in m["text"], m["text"]


def test_v081_plausible_rival_never_links_leader_to_backmarker():
    """v0.8.1 (2): a championship leader's plausible rivals are the
    frontrunners only — never a backmarker (the v0.8.0 bug:
    Antonelli to Haas while leading the championship at Mercedes).
    A mid-field driver's rivals stay within 3 places or the top 4."""
    career = game.build_new_career("Test", "Matthew")
    names = ["Mercedes", "Ferrari", "McLaren", "Red Bull",
             "Aston Martin", "Alpine", "Williams", "Racing Bulls",
             "Haas", "Audi", "Cadillac"]
    by = {t["name"]: t for t in career["teams"]}
    career["teams"] = [by[n] for n in names]
    leader = game.get_driver(career, by["Mercedes"]["driver1_id"])
    leader["points"] = 200  # championship leader, at the P1 team
    rival = game._plausible_rival(career, leader)
    assert rival is not None
    assert game.constructor_pos(career, rival["id"]) <= 4
    assert rival["name"] not in ("Haas", "Cadillac", "Audi")
    # A mid-field driver (Alpine, P6): rivals within 3 places or
    # the frontrunner band.
    mid = game.get_driver(career, by["Alpine"]["driver1_id"])
    rival = game._plausible_rival(career, mid)
    assert rival is not None
    rpos = game.constructor_pos(career, rival["id"])
    assert rpos <= 4 or abs(rpos - 6) <= 3, rpos


def test_v081_junior_acceptance_weighs_position():
    """v0.8.1 (4): a top-4 customer basically never accepts junior
    status, and the chance rises the further back they finish. A P3
    customer asked by the P1 supplier: p = 0.75 x 0.05 = 0.0375
    (pinned with fixed rolls either side of it). A P11 backmarker:
    p = 0.75 x 1.0 = 0.75."""
    career = game.build_new_career("Test", "Matthew")
    names = ["Mercedes", "Ferrari", "McLaren", "Red Bull",
             "Aston Martin", "Alpine", "Williams", "Racing Bulls",
             "Haas", "Audi", "Cadillac"]
    by = {t["name"]: t for t in career["teams"]}
    career["teams"] = [by[n] for n in names]
    assert game.constructor_pos(career, by["McLaren"]["id"]) == 3
    assert game.constructor_pos(career, by["Mercedes"]["id"]) == 1
    assert game.constructor_pos(career, by["Cadillac"]["id"]) == 11
    p3 = _v073_proposal(career, "Mercedes", "McLaren",
                        kind="junior", target_as="customer")
    assert game.bot_accepts_proposal(career, p3, rng=_FixedRng(0.0))
    assert game.bot_accepts_proposal(career, p3, rng=_FixedRng(0.037))
    assert not game.bot_accepts_proposal(career, p3, rng=_FixedRng(0.038))
    assert not game.bot_accepts_proposal(career, p3, rng=_FixedRng(0.99))
    p11 = _v073_proposal(career, "Mercedes", "Cadillac",
                         kind="junior", target_as="customer")
    assert game.bot_accepts_proposal(career, p11, rng=_FixedRng(0.0))
    assert game.bot_accepts_proposal(career, p11, rng=_FixedRng(0.7))
    assert not game.bot_accepts_proposal(career, p11, rng=_FixedRng(0.75))
    assert not game.bot_accepts_proposal(career, p11, rng=_FixedRng(0.99))


def test_v081_junior_offer_gate_skips_frontrunners():
    """v0.8.1 (4): the supplier's offer gate weighs the customer's
    position too — a top-4 customer is never offered junior status
    in the winter break, while a backmarker customer can be (and,
    with a roll of 0, is). The pick is a weighted random draw, not
    the deterministic highest-perf customer."""
    career = game.build_new_career("Test", "Matthew")
    career["phase"] = "winter_break"
    career["wb_round"] = 1
    names = ["Mercedes", "Ferrari", "McLaren", "Red Bull",
             "Aston Martin", "Alpine", "Williams", "Racing Bulls",
             "Haas", "Audi", "Cadillac"]
    by = {t["name"]: t for t in career["teams"]}
    career["teams"] = [by[n] for n in names]
    mercedes = by["Mercedes"]
    for t in (by["McLaren"], by["Cadillac"]):
        t["supplier_id"] = mercedes["id"]
        t["sup_stage"] = 1
        t["relation_years"] = 2
        t["perf"] = mercedes["perf"] - 10
    # The other supplied teams sit outside the perf window, so only
    # the P11 backmarker is eligible.
    by["Alpine"]["perf"] = mercedes["perf"] - 100
    by["Williams"]["perf"] = mercedes["perf"] - 100
    mercedes["money"] = 50.0
    game.bot_winter_relations(career, mercedes, rng=_FixedRng(0.0))
    assert by["McLaren"]["sup_stage"] == 1  # P3: never offered
    assert by["Cadillac"]["sup_stage"] == 2  # P11: offered + accepted


def test_v081_bot_season_plan_cuts_tr_and_academy_decisions():
    """v0.8.1 (3d): bots only plan a T&R or academy signing in ~40%
    of team-seasons (a stable per-team-per-year coin flip, no new
    saved field) — the rest sit the season out, which is what makes
    the transfers list readable. A team whose plan says no does not
    sign in ai_act; a team whose academy plan says yes does."""
    career = game.build_new_career("Test", "Matthew")
    # Distribution: roughly 40% across teams and seasons (20
    # seasons, so the sample is large enough that 0.30-0.50 is a
    # comfortable band around the 40% target).
    for kind in ("tr", "academy"):
        total = true = 0
        for y in range(2026, 2046):
            career["year"] = y
            for t in career["teams"]:
                total += 1
                if game._ai_season_plan(career, t, kind):
                    true += 1
        assert 0.30 <= true / total <= 0.50, (kind, true, total)
    # Stability: the same team+year always gives the same answer.
    career["year"] = 2026
    t0 = career["teams"][0]
    assert (game._ai_season_plan(career, t0, "tr")
            == game._ai_season_plan(career, t0, "tr"))
    # ai_act: a team whose plan says no does not sign — even when
    # it has room to (its T&R drivers are released first, so the
    # no-signing is the plan's doing, not a full roster). A fresh
    # career per part, so one team's ai_act cannot consume another
    # team's once-a-season flag.
    career = game.build_new_career("Test", "Matthew")
    career["round"] = 3
    no_tr = next(t for t in career["teams"]
                 if not game._ai_season_plan(career, t, "tr"))
    for d in game.team_tr_drivers(career, no_tr):
        d["status"] = "free"
        d["team_id"] = None
    no_tr["money"] = 100.0
    game.ai_act(career, no_tr, rng=random.Random(1))
    assert len(game.team_tr_drivers(career, no_tr)) == 0
    no_ac = next(t for t in career["teams"]
                 if not game._ai_season_plan(career, t, "academy")
                 and len(game.team_academy_drivers(career, t)) < 3)
    no_ac["money"] = 100.0
    ac0 = len(game.team_academy_drivers(career, no_ac))
    game.ai_act(career, no_ac, rng=random.Random(1))
    assert len(game.team_academy_drivers(career, no_ac)) == ac0
    # ... while a team whose academy plan says yes does sign one.
    career = game.build_new_career("Test", "Matthew")
    career["round"] = 3
    yes_ac = next(t for t in career["teams"]
                  if game._ai_season_plan(career, t, "academy")
                  and len(game.team_academy_drivers(career, t)) < 3)
    yes_ac["money"] = 100.0
    ac0 = len(game.team_academy_drivers(career, yes_ac))
    game.ai_act(career, yes_ac, rng=random.Random(1))
    assert len(game.team_academy_drivers(career, yes_ac)) == ac0 + 1


def test_v081_qual_two_laps_best_counts(monkeypatch):
    """v0.8.1 (7): each driver runs 2 laps in qualifying and the
    best lap counts (one lap was too hectic). The per-session form
    draw stays once per driver per session, applied to the best lap."""
    from f1 import race as race_mod
    career = game.build_new_career("Test", "Matthew")
    calls = []
    laps = iter([100.0 + i for i in range(44)])
    def fake_lap_sim(w, t_perf, max_perf, min_perf, session, err, rng=None):
        v = next(laps)
        calls.append(v)
        return v
    monkeypatch.setattr(race_mod, "lap_sim", fake_lap_sim)
    forms = []
    class _ZeroForm:
        def random(self):
            return 0.5
        def gauss(self, mu, sigma):
            forms.append((mu, sigma))
            return 0.0
    race_mod.simulate_qual(career, rng=_ZeroForm())
    # Two laps per driver (22 drivers).
    assert len(calls) == 44
    # One form draw per driver, applied to the best lap.
    assert len(forms) == 22
    # Each driver's qual time is the BEST of their two laps: the
    # laps were dealt in order (100,101), (102,103), ... per team
    # (driver 1, then driver 2), so driver k's best is 100 + 2k.
    k = 0
    for team in career["teams"]:
        for did in (team["driver1_id"], team["driver2_id"]):
            if not did:
                continue
            d = game.get_driver(career, did)
            assert d["qual_time"] == pytest.approx(100.0 + 2 * k), \
                (d["name"], d["qual_time"], k)
            k += 1
    assert k == 22


def test_v081_transfers_structured_with_labels_and_migration():
    """v0.8.1 (3): transfers are structured dicts with a round label
    (\"R8 2026\" in a round, \"WB2 2026\" in the winter break) so the
    UI can show when each move happened and style the names, kept
    to the newest 100. Old saves (plain strings) migrate: strings
    are wrapped (no label known) and junior-share payment lines are
    dropped. Save version 18."""
    from f1.saves import SAVE_VERSION, migrate_save
    assert SAVE_VERSION == 18
    # Live entries: dicts with labels.
    career = game.build_new_career("Test", "Matthew")
    game.log_transfer(career, "Test move in round 1")
    assert career["transfers"] == [{
        "text": "Test move in round 1", "year": 2026,
        "round": 1, "label": "R1 2026"}]
    career["phase"] = "winter_break"
    career["wb_round"] = 2
    game.log_transfer(career, "Test move in the break")
    assert career["transfers"][1]["label"] == "WB2 2026"
    # The cap keeps the newest 100.
    for i in range(105):
        game.log_transfer(career, f"move {i}")
    assert len(career["transfers"]) == 100
    assert career["transfers"][0]["text"] == "move 5"
    assert career["transfers"][-1]["text"] == "move 104"
    # Migration: a v17 save with string transfers.
    doc = json.loads(json.dumps(game.build_new_career("Test", "Matthew")))
    doc["version"] = 17
    doc["transfers"] = [
        "Lewis Hamilton left Mercedes (poached by Ferrari)",
        "Williams paid $0.30M of its race prize to its parent "
        "(15% junior share)",
        "Max Verstappen signed for Red Bull",
    ]
    migrated = migrate_save(doc)
    assert migrated["version"] == 18
    assert len(migrated["transfers"]) == 2
    assert all(isinstance(t, dict) for t in migrated["transfers"])
    assert migrated["transfers"][0]["text"].startswith("Lewis Hamilton")
    assert migrated["transfers"][0]["label"] is None
    assert migrated["transfers"][1]["text"].startswith("Max Verstappen")
    assert not any("junior share" in t["text"]
                   for t in migrated["transfers"])
