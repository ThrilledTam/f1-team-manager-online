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
    add_perf_gain, finance_estimates, reg_change, retirement_chance,
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


def play_full_round(career, connected, rng):
    """Drive one round to completion the v0.1 way: all humans Advance,
    then everyone votes NEXT on the qual page, then on the race page."""
    for t in list(game.human_teams(career)):
        game.advance_team(career, t["id"], connected, rng=rng)
    assert career["phase"] == "qual_results"
    for pid in sorted(connected):
        game.vote_next(career, pid, connected, rng=rng)
    assert career["phase"] == "race_results"
    for pid in sorted(connected):
        game.vote_next(career, pid, connected, rng=rng)
    assert career["phase"] in ("round", "winter_break")


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
    assert len([x for x in career["drivers"] if x["status"] == "free"]) == 33
    assert len([x for x in career["drivers"] if x["status"] == "f2"]) == 22
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
    connected = {"p-matthew", "p-cousin"}
    t1, t2 = career["teams"][0], career["teams"][1]
    ok, raced, _ = game.advance_team(career, t1["id"], connected, rng=seeded())
    assert ok and not raced
    assert career["round"] == 1
    assert career["last_race"] is None
    ok, raced, _ = game.advance_team(career, t2["id"], connected, rng=seeded(7))
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
    ok, advanced, msg = game.vote_next(career, "p-matthew", connected, rng=seeded(7))
    assert ok and not advanced
    assert career["phase"] == "qual_results"
    ok, advanced, msg = game.vote_next(career, "p-cousin", connected, rng=seeded(7))
    assert ok and advanced
    assert career["phase"] == "race_results"
    assert career["results_stage"] == "race"
    # Second vote returns everyone to their dashboards for round 2.
    ok, advanced, _ = game.vote_next(career, "p-matthew", connected, rng=seeded(7))
    assert ok and not advanced
    ok, advanced, _ = game.vote_next(career, "p-cousin", connected, rng=seeded(7))
    assert ok and advanced
    assert career["phase"] == "round"
    assert career["round"] == 2


def test_race_points_and_money_change():
    career = two_human_career()
    connected = {"p-matthew", "p-cousin"}
    money_before = {t["id"]: t["money"] for t in career["teams"]}
    game.advance_team(career, career["teams"][0]["id"], connected, rng=seeded())
    game.advance_team(career, career["teams"][1]["id"], connected, rng=seeded(7))
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
    team = career["teams"][0]
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
        play_full_round(career, connected, rng)
    assert career["phase"] == "winter_break"
    assert career["year"] == 2027
    assert career["last_reg_change"]["year"] == 2027
    assert career["last_reg_change"]["kind"] == "Minor"
    assert career["next_reg_year"] == 2028
    # Points reset for the new season.
    assert all(t["points"] == 0 for t in career["teams"])
    assert all(d["points"] == 0 for d in career["drivers"] if d["status"] == "f1")
    # Winter break can be completed: fill any empty human seats, then start.
    for team in game.human_teams(career):
        for seat in game.empty_seats(career, team):
            pool = game.free_agents(career) + game.f2_drivers(career)
            assert pool, "no drivers left to fill a seat"
            ok, why = game.sign_driver(career, team["id"], pool[0]["id"], seat)
            assert ok, why
    ok, why = game.start_next_season(career)
    assert ok, why
    assert career["phase"] == "round" and career["round"] == 1


# ---------------------------------------------------------------------------
# 5. Save / load round-trip + JSON shape
# ---------------------------------------------------------------------------

def test_save_load_round_trip_preserves_state():
    career = two_human_career()
    connected = {"p-matthew", "p-cousin"}
    play_full_round(career, connected, seeded(7))
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
        assert loaded["version"] == 5  # v0.3 save format
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
    assert '"rel"' not in blob
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

            # Host disconnects -> the cousin is promoted to host.
    # (host_ws context exits here)
    with client.websocket_connect("/ws") as cousin_ws:
        cousin_ws.send_json({"type": "rejoin", "code": code,
                             "player_id": cousin_id})
        rejoined = recv_until(cousin_ws, "joined")
        assert rejoined["room"]["host_id"] == cousin_id
        assert rejoined["room"]["started"] is True


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
    connected = {"p-matthew", "p-cousin"}
    play_full_round(career, connected, seeded(5))
    for key in ("last_qual", "last_race"):
        rows = career[key]
        assert len(rows) == 22
        for r in rows:
            assert "time" not in r, f"absolute time leaked in {key}: {r}"
            assert r["gap"] == "LEADER" or r["gap"].startswith("+")
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
    connected = {"p-matthew", "p-cousin"}
    for seed in (1, 7, 42):
        c = game.build_new_career("T", "H")
        rng = random.Random(seed)
        from f1 import race as race_mod
        qual = race_mod.simulate_qual(c, rng=rng)
        qtimes = [d["qual_time"] for d, _ in qual]
        # v0.2 form widens qual gaps a little (sim median 3.0s, max 4.1s
        # over 120 races) — still a few seconds, not tens.
        assert qtimes[-1] - qtimes[0] <= 4.5
        race_entries = race_mod.simulate_race(c, laps=57, rng=rng)
        rtimes = [d["race_time"] for d, _ in race_entries]
        winner = rtimes[0]
        assert 4000 <= winner <= 7000, f"winner total {winner:.0f}s is not a GP distance"
        assert 10 <= rtimes[-1] - winner <= 150  # P22 gap plausible, not hundreds
        assert 5 <= rtimes[9] - winner <= 120    # P10 gap plausible
    # The played round agrees (server-side times are what the gaps came from).
    play_full_round(career, connected, seeded(5))
    assert career["last_race"][0]["gap"] == "LEADER"
    assert career["last_race"][-1]["gap"].startswith("+")
    last_gap = float(career["last_race"][-1]["gap"][1:-1])
    assert 10 <= last_gap <= 150


def test_next_vote_flow_host_override_and_disconnect():
    career = two_human_career()
    connected = {"p-matthew", "p-cousin"}
    for t in list(game.human_teams(career)):
        game.advance_team(career, t["id"], connected, rng=seeded())
    assert career["phase"] == "qual_results"
    # A player with no team cannot vote.
    ok, _adv, _msg = game.vote_next(career, "p-stranger", connected, rng=seeded())
    assert not ok
    # The host override is host-only.
    ok, _adv, _msg = game.host_skip_results(career, "p-cousin", connected,
                                            is_host=False, rng=seeded())
    assert not ok
    assert career["phase"] == "qual_results"
    ok, adv, _msg = game.host_skip_results(career, "p-matthew", connected,
                                           is_host=True, rng=seeded())
    assert ok and adv
    assert career["phase"] == "race_results"
    # Second vote: one vote is not enough, both votes return to dashboards.
    ok, adv, _ = game.vote_next(career, "p-matthew", connected, rng=seeded())
    assert ok and not adv
    assert career["phase"] == "race_results"
    ok, adv, _ = game.vote_next(career, "p-cousin", connected, rng=seeded())
    assert ok and adv
    assert career["phase"] == "round" and career["round"] == 2
    # Disconnected humans are excluded: one remaining voter can pass alone,
    # and a recorded vote survives a disconnect/reconnect.
    career2 = two_human_career()
    for t in list(game.human_teams(career2)):
        game.advance_team(career2, t["id"], connected, rng=seeded())
    ok, adv, _ = game.vote_next(career2, "p-matthew", connected, rng=seeded())
    assert ok and not adv  # cousin has not voted yet
    assert "p-matthew" in career2["results_vote"]  # vote is recorded
    connected2 = {"p-matthew"}  # cousin disconnected
    assert game.check_results_vote(career2, connected2, rng=seeded())
    assert career2["phase"] == "race_results"


def test_history_gp_revisit_returns_full_tables():
    career = two_human_career()
    connected = {"p-matthew", "p-cousin"}
    play_full_round(career, connected, seeded(3))
    play_full_round(career, connected, seeded(4))
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
    team = career["teams"][0]
    ok, msg = game.do_tuning(career, team["id"], 2)
    assert ok and team["tuning"] == 2
    assert msg == "Tuning set to +2 ✓"
    ok, msg = game.do_research(career, team["id"], "perf", 1.0)
    assert ok and "Invested $1.00M in Performance research" in msg
    assert team["perf_res"] == 1.0
    # A failed action says so clearly instead of failing silently.
    ok, msg = game.do_research(career, team["id"], "perf", 10**9)
    assert not ok and "Not enough money" in msg
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
    connected = {"p-matthew", "p-cousin"}
    # (two_human_career already picks teams; use a fresh one here)
    career = two_human_career()
    play_full_round(career, connected, seeded(11))
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
    connected = {"p-matthew", "p-cousin"}
    rng = seeded()
    ok, ran, _msg = game.advance_team(
        career, career["teams"][0]["id"], connected, rng=rng)
    assert ok and not ran
    ok, ran, msg = game.advance_team(
        career, career["teams"][1]["id"], connected, rng=rng)
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
        entries = race_mod.simulate_race(c, laps=57, rng=rng)
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
    connected = {"p-matthew", "p-cousin"}
    play_full_round(career, connected, seeded(5))
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
    play_full_round(career, connected, seeded(6))
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
        play_full_round(career, connected, rng)
    assert career["phase"] == "winter_break"
    order = career["prev_driver_order"]
    assert len(order) == 22
    # Fill empty human seats, then start the next season.
    for team in game.human_teams(career):
        for seat in game.empty_seats(career, team):
            pool = game.free_agents(career) + game.f2_drivers(career)
            ok, why = game.sign_driver(career, team["id"], pool[0]["id"], seat)
            assert ok, why
    ok, why = game.start_next_season(career)
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
    play_full_round(career, connected, seeded(21))
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
    connected = {"p-matthew", "p-cousin"}
    play_full_round(career, connected, seeded(5))
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
    play_full_round(career, connected, seeded(6))
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
    must have shown the per-seat replacement cost before the click."""
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
            # Who is in McLaren seat 1? (Their surname must stay visible
            # with McLaren in the standings after replacement.)
            my_team = p1.inner_text("#my-team")
            seat1_surname = re.search(r"\b([A-Z][a-z]+)\s+([A-Z]+)\b", my_team)
            assert seat1_surname, my_team
            surname = seat1_surname.group(2)
            # Market tab: per-seat costs are shown before committing.
            p1.click("button[data-tab='market']")
            market = p1.inner_text("#market-box")
            assert "Cost to sign" in market
            assert re.search(r"Seat 1 — \$\d+\.\d\dM", market), market
            assert "Break $" in market
            assert "Signing $" not in market  # v0.3: no signing fee
            assert "Rep" not in market  # v0.3: reputation is hidden
            # Replace seat 1 with the first listed free agent: tapping
            # the seat opens the contract-length picker (v0.3), then
            # picking 2 years signs the driver.
            p1.click("#market-box button[data-sign][data-seat='1']")
            p1.wait_for_selector("[data-contract-years]", timeout=15000)
            assert "Pick the contract length" in p1.inner_text("#market-box")
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
    game.fill_empty_seats_ai(career, team, rng=random.Random(3))
    assert team["driver2_id"] is not None
    picked = game.get_driver(career, team["driver2_id"])
    assert 1 <= picked["contract_left"] <= 4
    assert picked["status"] == "f1"


def test_salary_paid_per_race_after_each_race():
    """MV is the salary per season, paid salary/12 after each race.
    The exact money change for a team over one round is prize money
    (1.95 - 0.025*pos per driver) + per-race gains - salaries/12."""
    from f1.rules import finance_estimates
    career = two_human_career()
    connected = {"p-matthew", "p-cousin"}
    team = career["teams"][0]  # McLaren
    assert game.salary_per_round(career, team) == pytest.approx((5.2 + 4.6) / 12)
    pos_before = game.constructor_pos(career, team["id"])
    money_before = team["money"]
    salaries = sum(d["salary"] for d in game.team_drivers(career, team))
    game.advance_team(career, team["id"], connected, rng=seeded())
    game.advance_team(career, career["teams"][1]["id"], connected, rng=seeded(7))
    prize = sum(1.95 - 0.025 * d["race_pos"]
                for d in game.team_drivers(career, team))
    expected = money_before + prize + finance_estimates(pos_before)["eprg"] \
        - salaries / 12
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
    connected = {"p-matthew", "p-cousin"}
    play_full_round(career, connected, seeded(3))
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
        play_full_round(career, connected, rng)
    for t in list(game.human_teams(career)):
        game.advance_team(career, t["id"], connected, rng=rng)
    assert career["phase"] == "qual_results"
    for pid in sorted(connected):
        game.vote_next(career, pid, connected, rng=rng)
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
        game.vote_next(career, pid, connected, rng=rng)
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
    # And a save round-trip writes version 5 with the pre-filled table.
    career = two_human_career()
    with tempfile.TemporaryDirectory() as tmp:
        meta = save_career(career, "v5 test", tmp)
        loaded = load_save(tmp, meta["save_id"])
        assert loaded["version"] == 5
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
            # Clicking the R1 cell opens the full Grand Prix.
            page.click("#history-box .hist-cell:not(.empty)")
            page.wait_for_selector("#btn-gp-back", timeout=15000)
            gp = page.inner_text("#history-box")
            assert "Qualifying —" in gp and "Race —" in gp
            browser.close()
    finally:
        proc.terminate()


def test_health_reports_v03():
    """The live version string is v0.3 everywhere it is shown."""
    from fastapi.testclient import TestClient
    from server import app
    client = TestClient(app)
    assert client.get("/api/health").json()["version"] == "v0.3"
    html = client.get("/").text
    assert "<title>F1 Team Manager Online — v0.3</title>" in html
    assert "Online · v0.3 beta" in html
