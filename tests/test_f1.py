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
    retirement_chance,
)
from f1.saves import delete_save, list_saves, load_save, migrate_save, save_career


def seeded(seed=1234):
    return random.Random(seed)


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
    assert retirement_chance(49, 0, 1, 0, 0) == 100.0
    # Older + fewer titles + backmarker team share => higher chance.
    assert retirement_chance(45, 0, 11, 0, 100) > retirement_chance(45, 7, 1, 80, 100)


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
        assert loaded["version"] == 9  # v0.6.2 save format
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
        # v0.2 form widens qual gaps a little (sim median 3.0s, max 4.1s
        # over 120 races) — still a few seconds, not tens.
        assert qtimes[-1] - qtimes[0] <= 4.5
        race_entries, _dnfs = race_mod.simulate_race(c, laps=57, rng=rng)
        rtimes = [d["race_time"] for d, _ in race_entries]
        winner = rtimes[0]
        assert 4000 <= winner <= 7000, f"winner total {winner:.0f}s is not a GP distance"
        # v0.6.2: the last row of race_entries is the last FINISHER
        # (DNFs are returned separately) — its gap is still plausible.
        assert 10 <= rtimes[-1] - winner <= 150  # last finisher's gap
        assert 5 <= rtimes[9] - winner <= 120    # P10 gap plausible
    # The played round agrees (server-side times are what the gaps came from).
    play_full_round(career, seeded(5))
    assert career["last_race"][0]["gap"] == "LEADER"
    # v0.6.2: DNF rows (gap "DNF") sit after the finishers — the gap
    # check is on the last FINISHER, not the last row.
    finishers = [r for r in career["last_race"] if r["pos"] != "DNF"]
    assert finishers[-1]["gap"].startswith("+")
    last_gap = float(finishers[-1]["gap"][1:-1])
    assert 10 <= last_gap <= 150


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
    Antonelli/Mercedes wins 54 (45%) — in the 30-50% band — with 5
    different winners (Russell 42, Leclerc 11, Hamilton 7, Verstappen 6),
    while races stay a Grand Prix distance with plausible gaps."""
    wins, _poles, _q, r_gaps, totals = _simulate_many_races()
    assert 36 <= wins["Kimi Antonelli"] <= 60  # 30-50% of 120
    assert len(wins) >= 4
    assert all(4000 <= t <= 7000 for t in totals)
    assert all(10 <= g <= 150 for g in r_gaps)


def test_session_form_pole_diversity():
    """Qualifying is less deterministic too: over the same 120 seeded
    sessions Antonelli takes pole 45 times (38%), with 9 different poles,
    and the P22 qual gap stays a few seconds (median 3.0s, max 4.1s)."""
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
    """An F2 driver signed mid-season starts at 0: F2 championship
    points must not carry into F1."""
    career = two_human_career()
    f2 = game.f2_drivers(career)[0]
    f2["points"] = 30  # F2 championship points
    team = career["teams"][0]
    team["money"] = 100.0
    ok, msg = game.sign_driver(career, team["id"], f2["id"], 1)
    assert ok, msg
    assert f2["status"] == "f1" and f2["team_id"] == team["id"]
    assert f2["points"] == 0 and f2["team_points"] == 0
    assert f2["season_wins"] == 0 and f2["best_pos"] is None


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
    expected = (12 + 12) * occupant["salary"] / 12 + 2.0
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
            # Play one round so the McLaren drivers have points.
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
            # so either driver can be in seat 1 after round 1.
            my_team = p1.inner_text("#my-team")
            seats = re.findall(r"\b([A-Z][a-z]+)\s+([A-Z]+)\b", my_team)
            assert len(seats) >= 2, my_team
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
    28 x $4.0M/12 + $2.0M = $11.33M."""
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
    assert quote["total"] == pytest.approx(11.33, abs=0.01)
    assert quote["break_fee"] == pytest.approx(11.33, abs=0.01)
    # Seat 2 (Colapinto, salary 1.5, 1 year left): only the 4 rounds
    # left this season count -> 4 x 1.5/12 + 2 = $2.50M.
    quote2 = game.break_cost(career, alpine["id"], fa["id"], 2)
    assert quote2["total"] == pytest.approx(2.50, abs=0.01)


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
    # 2 full years left: 24 x 4/12 + 2 = $10.00M.
    assert quote["total"] == pytest.approx(10.00, abs=0.01)
    career["phase"] = "round"
    career["round"] = 12
    gasly["contract_left"] = 1
    quote = game.break_cost(career, alpine["id"], fa["id"], 1)
    # 1 round left: 1 x 4/12 + 2 = $2.33M.
    assert quote["total"] == pytest.approx(2.33, abs=0.01)


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
        assert loaded["version"] == 9
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
            row_h = page.evaluate(
                "document.querySelector('#market-box .market-table tr:nth-child(2)')"
                ".getBoundingClientRect().height")
            assert row_h < 90, f"market row {row_h}px is not compact"
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


def test_health_reports_v065():
    """The live version string is v0.6.5 everywhere it is shown."""
    from fastapi.testclient import TestClient
    from server import app
    client = TestClient(app)
    assert client.get("/api/health").json()["version"] == "v0.6.5"
    html = client.get("/").text
    assert "<title>F1 Team Manager Online — v0.6.5</title>" in html
    assert "Online · v0.6.5 beta" in html


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
                seat = page.evaluate("""() => {
                    const btns = [...document.querySelectorAll('#market-box button[data-sign][data-seat]')]
                      .filter(b => (b.dataset.seat === '1' || b.dataset.seat === '2') && !b.disabled);
                    return btns.length ? btns[0].dataset.seat : null;
                }""")
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
    perf − bottom team's perf; major = full perf; $0.25M/pt; 0 perf
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
    # Adaptation costs $0.25M per point.
    team["money"] = 100.0
    unadapted = team["reg_unadapted"]
    ok, msg = game.do_reg_adapt(career, team["id"], points=10)
    assert ok, msg
    assert team["money"] == pytest.approx(100.0 - 10 * 0.25)
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
    assert SAVE_VERSION == 9
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
    assert any("dropped" in t and bad["name"] in t for t in career["transfers"])
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
    assert any("swapped" in t for t in career["transfers"])
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
    break round 1 must not move McLaren's bank — it stays exactly
    the save file's value ($150.19M) through load, takeover and
    round-1 resolution."""
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
        # The human has no auto-offers (v0.4.1), so they submit their
        # own for the empty seat, then hit NEXT to resolve round 1.
        piastri = next(d for d in loaded["drivers"] if d["name"] == "Oscar Piastri")
        ok, why = game.submit_winter_offers(
            loaded, mclaren["id"],
            [{"driver_id": piastri["id"], "target": "seat", "seat": 2, "years": 2}])
        assert ok, why
        ok, _adv, why = game.winter_next(loaded, mclaren["id"], rng=random.Random(4))
        assert ok, why
        assert loaded["wb_round"] == 2
        assert mclaren["money"] == pytest.approx(saved_money)


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
    mercedes["driver1_id"] = None
    mercedes["money"] = 100.0
    ok, msg = game.sign_driver(career, mercedes["id"], camara["id"], 1)
    assert ok, msg
    assert camara["status"] == "f1" and camara["academy_team_id"] is None
    assert game.team_academy_drivers(career, ferrari) == [
        d for d in game.team_academy_drivers(career, ferrari)
        if d["name"] == "Dino Beganovic"]
    # T&R signing frees it too.
    dunne = next(d for d in career["drivers"] if d["name"] == "Alex Dunne")
    alpine = next(t for t in career["teams"] if t["name"] == "Alpine")
    assert dunne["academy_team_id"] == alpine["id"]
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
    go over 3 academy drivers (they avoid the fine entirely)."""
    career = game.build_new_career("Test", "Matthew")
    rb = next(t for t in career["teams"] if t["name"] == "Racing Bulls")
    rb["money"] = 100.0
    career["round"] = 3
    game.ai_act(career, rb, rng=random.Random(1))
    assert len(game.team_academy_drivers(career, rb)) == 1
    assert rb["ai_academy_signed"] is True
    game.ai_act(career, rb, rng=random.Random(2))
    assert len(game.team_academy_drivers(career, rb)) == 1
    # A team already at 3 academy drivers gets no 4th from the bot.
    alpine = next(t for t in career["teams"] if t["name"] == "Alpine")
    alpine["money"] = 100.0
    assert len(game.team_academy_drivers(career, alpine)) == 3
    game.ai_act(career, alpine, rng=random.Random(1))
    assert len(game.team_academy_drivers(career, alpine)) == 3


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
    $2.0M — shown upfront and charged on confirm. The driver becomes
    a free agent, keeps championship points, and the points they
    scored for the team stay with the team."""
    career = game.build_new_career("Test", "Matthew")
    mclaren = next(t for t in career["teams"] if t["name"] == "McLaren")
    trs = game.team_tr_drivers(career, mclaren)
    assert len(trs) == 4
    d = next(x for x in trs if x["name"] == "Nyck de Vries")
    d["points"] = 10  # a T&R driver can hold points from an earlier stint
    fee = game.break_fee_for_driver(career, d)
    # Round 1, 2-year contract: (12 + 12) x 1.5/12 + 2.0 = $5.0M.
    assert fee == pytest.approx(5.0)
    money_before = mclaren["money"]
    ok, msg = game.release_driver(career, mclaren["id"], d["id"])
    assert ok, msg
    assert mclaren["money"] == pytest.approx(money_before - fee)
    assert d["status"] == "free" and d["team_id"] is None
    assert d["points"] == 10  # championship points follow the driver
    assert d not in game.team_tr_drivers(career, mclaren)
    assert any("released by McLaren" in t and "Nyck de Vries" in t
               for t in career["transfers"])


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
    mclaren["money"] = 100.0  # Norris's fee ($12.4M) needs a full bank
    fee = game.break_fee_for_driver(career, norris)
    assert fee == pytest.approx(12.4)
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
    # contract, $1.5M/yr: 24 x 1.5/12 + 2.0 = $5.0M.
    mclaren["money"] = 100.0
    career["phase"] = "winter_break"
    assert d["name"] == "Nyck de Vries"
    fee = game.break_fee_for_driver(career, d)
    assert fee == pytest.approx(5.0)
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
               for t in career["transfers"])
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
            assert "Breaking their contract costs $5.00M" in confirm_text
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
# WANTED drivers, research caps, bot parity, v9 save migration
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
        assert t["research_spent_season"] == 0.0
        assert t["wanted"] == []
        assert t["research_cap"] is None


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
    # McLaren + Ferrari as the two humans (no relation between them —
    # two_human_career picks McLaren + Mercedes, who already have one).
    # Mercedes plays the supplier role: it is independent, while
    # McLaren — a Mercedes customer — cannot offer supply contracts
    # at all under v0.6.5.
    ok, _ = game.choose_team(career, team_by_name(career, "McLaren")["id"], "p-matthew")
    assert ok
    ok, _ = game.choose_team(career, team_by_name(career, "Ferrari")["id"], "p-cousin")
    assert ok
    mclaren = team_by_name(career, "McLaren")
    ferrari = team_by_name(career, "Ferrari")
    mercedes = team_by_name(career, "Mercedes")
    # Outside the winter break: rejected.
    ok, why = game.propose_relation(career, mercedes["id"], ferrari["id"],
                                    "customer", mercedes["id"], 3)
    assert not ok and "winter break" in why
    career["phase"] = "winter_break"
    # Direct nothing -> junior: rejected.
    ok, why = game.propose_relation(career, mercedes["id"], ferrari["id"],
                                    "junior", mercedes["id"], 2)
    assert not ok and "must be a customer" in why
    # Customer formation: Ferrari is human, so the proposal stays pending.
    ok, why = game.propose_relation(career, mercedes["id"], ferrari["id"],
                                    "customer", mercedes["id"], 3)
    assert ok, why
    assert len(career["relation_proposals"]) == 1
    assert ferrari["sup_stage"] == 0  # not applied until accepted
    proposal = career["relation_proposals"][0]
    # Only the target may answer.
    ok, why = game.respond_relation(career, mclaren["id"], proposal["id"], True)
    assert not ok and "Only the team" in why
    ok, why = game.respond_relation(career, ferrari["id"], proposal["id"], True)
    assert ok, why
    assert ferrari["sup_stage"] == 1
    assert ferrari["supplier_id"] == mercedes["id"]
    assert ferrari["relation_years"] == 3
    assert career["relation_proposals"] == []
    # A second supplier for the same team is rejected.
    redbull = team_by_name(career, "Red Bull")
    ok, why = game.propose_relation(career, redbull["id"], ferrari["id"],
                                    "customer", redbull["id"], 2)
    assert not ok and "already has a supplier" in why
    # Upgrade to junior, then a second junior for Mercedes is rejected.
    ok, why = game.propose_relation(career, mercedes["id"], ferrari["id"],
                                    "junior", mercedes["id"], 2)
    assert ok, why
    proposal = career["relation_proposals"][0]
    ok, why = game.respond_relation(career, ferrari["id"], proposal["id"], True)
    assert ok, why
    assert ferrari["sup_stage"] == 2
    assert ferrari["relation_years"] == 2
    # Audi becomes Mercedes' customer first (AI target: Mercedes is
    # faster, so the bot accepts on the spot) — then a second junior
    # for Mercedes is rejected.
    audi = team_by_name(career, "Audi")
    ok, why = game.propose_relation(career, mercedes["id"], audi["id"],
                                    "customer", mercedes["id"], 2)
    assert ok, why
    assert audi["sup_stage"] == 1 and audi["supplier_id"] == mercedes["id"]
    ok, why = game.propose_relation(career, mercedes["id"], audi["id"],
                                    "junior", mercedes["id"], 2)
    assert not ok and "already has a junior" in why
    # Downgrade: junior -> customer, years unchanged. The supplier
    # proposes it to human Ferrari so it stays pending (an AI target
    # would decide on the spot).
    ok, why = game.propose_relation(career, mercedes["id"], ferrari["id"],
                                    "downgrade")
    assert ok, why
    proposal = career["relation_proposals"][0]
    ok, why = game.respond_relation(career, ferrari["id"], proposal["id"], True)
    assert ok, why
    assert ferrari["sup_stage"] == 1
    assert ferrari["relation_years"] == 2
    # End: customer -> nothing.
    ok, why = game.propose_relation(career, mercedes["id"], ferrari["id"], "end")
    assert ok, why
    proposal = career["relation_proposals"][0]
    ok, why = game.respond_relation(career, ferrari["id"], proposal["id"], True)
    assert ok, why
    assert ferrari["sup_stage"] == 0
    assert ferrari["supplier_id"] is None
    assert ferrari["relation_years"] == 0
    # Junior -> nothing directly (skip customer).
    for kind in ("customer", "junior"):
        ok, why = game.propose_relation(career, mercedes["id"], ferrari["id"],
                                        kind, mercedes["id"], 2)
        assert ok, why
        proposal = career["relation_proposals"][0]
        ok, why = game.respond_relation(career, ferrari["id"], proposal["id"], True)
        assert ok, why
    assert ferrari["sup_stage"] == 2
    ok, why = game.propose_relation(career, mercedes["id"], ferrari["id"], "end")
    assert ok, why
    proposal = career["relation_proposals"][0]
    ok, why = game.respond_relation(career, ferrari["id"], proposal["id"], True)
    assert ok, why
    assert ferrari["sup_stage"] == 0
    # Declining leaves everything as it was.
    ok, why = game.propose_relation(career, mercedes["id"], ferrari["id"],
                                    "customer", mercedes["id"], 2)
    assert ok, why
    proposal = career["relation_proposals"][0]
    ok, why = game.respond_relation(career, ferrari["id"], proposal["id"], False)
    assert ok, why
    assert ferrari["sup_stage"] == 0
    assert career["relation_proposals"] == []


def test_contracts_rollover_renew_break_lapse():
    """v0.6: relation years tick down one per season at season end;
    an expired deal renews by agreement (new 1–4 year term) or lapses
    at the next season start; breaking early costs $1M per remaining
    year, blocked when unaffordable, and free once expired."""
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
    # Early break: $1M per remaining year.
    money_before = mclaren["money"]
    ok, why = game.break_relation(career, mclaren["id"], mercedes["id"])
    assert ok, why
    assert mclaren["money"] == round(money_before - 3.0, 2)
    assert mclaren["sup_stage"] == 0
    assert mclaren["supplier_id"] is None
    # Blocked when unaffordable; free once expired.
    career2 = game.build_new_career("Test", "Matthew")
    ok, _ = game.choose_team(career2, team_by_name(career2, "McLaren")["id"], "p-matthew")
    assert ok
    mclaren2 = team_by_name(career2, "McLaren")
    mercedes2 = team_by_name(career2, "Mercedes")
    career2["phase"] = "winter_break"
    mclaren2["money"] = 1.0
    ok, why = game.break_relation(career2, mclaren2["id"], mercedes2["id"])
    assert not ok and "Need $2.00M" in why
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
    assert all(t["research_spent_season"] == 0.0 for t in career3["teams"])
    assert career3["relation_proposals"] == []


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
    # Junior signs them into a main seat: Lawson's break fee is $3.0M.
    ok, why = game.sign_driver(career, rbr["id"], ricciardo["id"], 1, 2)
    assert ok, why
    assert rbr["money"] == 4.0  # 4.0 - 3.0 fee + 3.0 reward
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
    assert not ok and "don't supply a junior team" in why
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


def test_research_caps():
    """v0.6: a parent sets a hard cap on the junior team's season
    research spend (Performance + Reliability) in the winter break;
    do_research enforces it, the spend resets at season rollover,
    Develop and reg research are NOT capped, and a team with no cap
    is uncapped."""
    career = game.build_new_career("Test", "Matthew")
    redbull = team_by_name(career, "Red Bull")
    rbr = team_by_name(career, "Racing Bulls")
    aston = team_by_name(career, "Aston Martin")
    mclaren = team_by_name(career, "McLaren")
    # Caps are winter-break only, and only for a parent of a junior team.
    ok, why = game.set_research_cap(career, redbull["id"], 5.0)
    assert not ok and "winter break" in why
    career["phase"] = "winter_break"
    ok, why = game.set_research_cap(career, aston["id"], 5.0)
    assert not ok and "don't supply a junior team" in why
    ok, why = game.set_research_cap(career, redbull["id"], 5.0)
    assert ok, why
    assert rbr["research_cap"] == 5.0
    rbr["money"] = 20.0
    ok, why = game.do_research(career, rbr["id"], "perf", 3.0)
    assert ok, why
    assert rbr["research_spent_season"] == 3.0
    ok, why = game.do_research(career, rbr["id"], "reliability", 3.0)
    assert not ok and "Research cap reached" in why
    assert rbr["research_spent_season"] == 3.0
    assert rbr["money"] == 17.0
    ok, why = game.do_research(career, rbr["id"], "perf", 2.0)
    assert ok, why  # exactly at the cap is allowed
    assert rbr["research_spent_season"] == 5.0
    # Develop is NOT capped (still at the cap, $1M in the bank).
    ok, why = game.do_develop(career, rbr["id"])
    assert ok, why
    # Reg research is NOT capped either.
    rbr["reg_pending"] = True
    ok, why = game.do_reg_research(career, rbr["id"], 2.0)
    assert ok, why
    # A team with no cap is uncapped.
    ok, why = game.do_research(career, mclaren["id"], "perf", 3.0)
    assert ok, why
    # The spend resets at season rollover; the cap itself stays for
    # the season (it is fixed for that season).
    career["wb_done"] = True
    ok, why = game.start_next_season(career, rng=seeded(7))
    assert ok, why
    assert rbr["research_spent_season"] == 0.0
    assert rbr["research_cap"] == 5.0
    # Clearing the cap.
    career["phase"] = "winter_break"
    ok, why = game.set_research_cap(career, redbull["id"], None)
    assert ok, why
    assert rbr["research_cap"] is None


def test_migration_v8_to_v9():
    """v0.6: an old v8 save backfills the 2026 relations (Mercedes ->
    McLaren/Williams/Alpine customer, Red Bull -> Racing Bulls
    junior, Ferrari -> Haas/Cadillac customer, 2 years) and the new
    fields default correctly; a save round-trips at version 9."""
    from f1.saves import SAVE_VERSION, load_save, migrate_save, save_career
    career = game.build_new_career("Test", "Matthew")
    doc = json.loads(json.dumps(career))
    doc["version"] = 8
    doc.pop("relation_proposals", None)
    for t in doc["teams"]:
        t.pop("relation_years", None)
        t.pop("research_spent_season", None)
        t.pop("wanted", None)
        t.pop("research_cap", None)
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
        assert t["research_spent_season"] == 0.0
        assert t["wanted"] == []
        assert t["research_cap"] is None
    # A v9 save round-trips (migrating twice changes nothing).
    assert SAVE_VERSION == 9
    migrated2 = migrate_save(json.loads(json.dumps(migrated)))
    assert team_by_name(migrated2, "McLaren")["relation_years"] == 2
    with tempfile.TemporaryDirectory() as tmp:
        meta = save_career(career, "v9 test", tmp)
        loaded = load_save(tmp, meta["save_id"])
        assert loaded is not None
        assert team_by_name(loaded, "Racing Bulls")["sup_stage"] == 2
        assert team_by_name(loaded, "Racing Bulls")["relation_years"] == 2
        assert loaded.get("relation_proposals") == []


def test_bot_relations():
    """v0.6 bot parity: a rich bot supplier donates to a poor supplied
    team; a bot parent sets its junior team's research cap ($15M) and
    marks a WANTED driver in the winter break; a bot junior prefers a
    WANTED driver when signing; bot targets accept/decline proposals
    by the simple rule. v0.6.5: a customer/junior team cannot offer
    supply contracts at all."""
    # Donation: Mercedes (> $20M) -> McLaren (< $5M), once per round.
    career = game.build_new_career("Test", "Matthew")
    mercedes = team_by_name(career, "Mercedes")
    mclaren = team_by_name(career, "McLaren")
    mercedes["money"] = 25.0
    mclaren["money"] = 3.0
    game.ai_act(career, mercedes, rng=random.Random(3))
    assert any("donated" in line and "Mercedes" in line and "McLaren" in line
               for line in career["transfers"])
    assert 4.0 < mclaren["money"] <= 5.0
    # Winter break round 1: bot parents set the cap and a WANTED mark.
    career = game.build_new_career("Test", "Matthew")
    career["phase"] = "winter_break"
    career["wb_round"] = 1
    game.start_winter_break_round(career, rng=random.Random(4))
    rbr = team_by_name(career, "Racing Bulls")
    assert rbr["research_cap"] == 15.0
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
    # Bot proposal decisions: a poor team accepts a supplier; a
    # comfortable team declines a slower supplier; a close customer
    # accepts becoming a junior.
    career = game.build_new_career("Test", "Matthew")
    career["phase"] = "winter_break"
    aston = team_by_name(career, "Aston Martin")
    audi = team_by_name(career, "Audi")
    ok, why = game.propose_relation(career, aston["id"], audi["id"],
                                    "customer", aston["id"], 2)
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
    # A comfortable team declines a slower supplier: Aston
    # (independent, 125 perf) offers to supply Mercedes (210 perf,
    # $7.0M — comfortable, so it says no).
    mercedes = team_by_name(career, "Mercedes")
    ok, why = game.propose_relation(career, aston["id"], mercedes["id"],
                                    "customer", aston["id"], 2)
    assert ok and "declined" in why, why
    assert mercedes["sup_stage"] == 0
    assert career["relation_proposals"] == []
    career = game.build_new_career("Test", "Matthew")
    career["phase"] = "winter_break"
    mercedes = team_by_name(career, "Mercedes")
    alpine = team_by_name(career, "Alpine")
    ok, why = game.propose_relation(career, mercedes["id"], alpine["id"],
                                    "junior", mercedes["id"], 2)
    assert ok and "accepted" in why, why  # within 60 perf, no junior yet
    assert alpine["sup_stage"] == 2
    assert alpine["supplier_id"] == mercedes["id"]


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
        # accepts on the spot, Red Bull is much faster than Audi).
        career["phase"] = "winter_break"
        audi = team_by_name(career, "Audi")
        redbull = team_by_name(career, "Red Bull")
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
            assert "Free agents" in market, market
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
        # Winter-break fee: 24 x 1.5/12 + 2.0 = $5.0M off $10.0M.
        assert team_by_name(career, "McLaren")["money"] == pytest.approx(5.0)


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
