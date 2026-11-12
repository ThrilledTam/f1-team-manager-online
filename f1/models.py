"""Clean data models for the F1 manager — plain dicts, fully JSON-serializable.

The save document is exactly the output of Career.to_dict(): the same shape
could later be stored in Firebase Firestore or Supabase without changing it.
No matplotlib, no ANSI colours, no input() anywhere.
"""
import copy
import uuid

CUSTOM_COLOURS = [
    "#e74c3c", "#e67e22", "#f39c12", "#2ecc71", "#1abc9c",
    "#3498db", "#9b59b6", "#fd79a8", "#00cec9", "#6c5ce7",
]


def new_driver_id():
    return "d-" + uuid.uuid4().hex[:10]


def make_driver(data, status="free", team_id=None):
    """data: one entry from GRID_2026 (name, age, perf, rep, cha, contract,
    mv, salary). contract 'DNA' in the original means no contract — stored 0.
    """
    return {
        "id": new_driver_id(),
        "name": data["name"],
        "age": int(data["age"]),
        "perf": float(data["perf"]),
        "rep": float(data["rep"]),
        "cha": int(data["cha"]),
        "contract_left": int(data["contract"]) if isinstance(data["contract"], int) else 0,
        "mv": float(data["mv"]),
        "salary": float(data["salary"]),
        "points": 0,           # championship points this season (follow
                               # the driver across teams, like real F1)
        "team_points": 0,      # v0.2.1: points scored for the CURRENT team
                               # this season — reset whenever the driver
                               # joins a new team; this is what counts
                               # toward a constructor total, never the
                               # driver's championship total
        "last_team_id": None,  # v0.2.1: team the driver last raced for
                               # (shown in the standings once they are a
                               # free agent, like real F1)
        "season_wins": 0,
        "best_pos": None,
        # v0.4: standings tie-breaks (points -> wins -> best finish ->
        # whoever got there first). best_finish_round is the round in
        # which best_pos was achieved (earliest wins a tie).
        "best_finish_round": None,
        "season_podiums": 0,   # v0.4: P1-P3 finishes this season
        "season_qual_h2h": 0,  # v0.4: times they out-qualified their
                               # teammate this season (head-to-head)
        "finish_sum": 0,       # v0.4: sum of race finishing positions
                               # this season (bot drop rule uses the
                               # average: finish_sum / finish_count)
        "finish_count": 0,
        # v0.4.4: F1 Grands Prix started this season (the F1 race loop
        # only — F2 races do not count). A driver who raced in F1
        # stays registered in the Drivers' Championship even with 0
        # points, even if they are no longer a main driver (swapped to
        # T&R, dropped to free agency) — like real F1, where a replaced
        # driver stays in the table. Reset with the season; an F2
        # driver signed into F1 starts at 0 (their F2 starts do not
        # carry over).
        "f1_races": 0,
        # v0.5: CAREER F1 totals — Grand Prix starts and wins over the
        # driver's whole career. These NEVER reset: not at season end,
        # not on signing, not on a team change. They drive the
        # development contribution (game.driver_dev_contrib): the more
        # a driver has raced and won, the more they help develop the
        # car. Start-of-2026 values come from data.CAREER_STATS (real
        # totals at the end of 2025); juniors default to (0, 0). Only
        # the F1 race loop increments them.
        "gp_starts": 0,
        "gp_wins": 0,
        "qual_time": None,
        "qual_pos": None,
        "race_time": None,
        "race_pos": None,
        # v0.4: status gains "tr" (Test & Reserve). A T&R driver is
        # affiliated with a team (team_id set, contract_left 1-4,
        # full salary paid) but does not race. v0.4.4: a T&R driver
        # who raced earlier this season DOES appear in the F1
        # standings (their f1_races keep them registered, like real
        # F1) — only a T&R driver who has never raced in F1 is absent.
        # See game.py team_tr_drivers().
        "status": status,          # "f1" | "free" | "f2" | "queued" | "pool" | "retired" | "tr"
        "team_id": team_id,
        # v0.4.2: Driver Academy affiliation — the team whose junior
        # programme the driver belongs to (None = no academy). The
        # driver keeps their status ("f2" or "free") and still races
        # in F2 / sits in the free-agent pool; the academy is an
        # affiliation on top: the team pays 1/3 of the salary, the
        # driver weighs that team's offers more heavily, and their
        # rating growth is weighted toward the top of the range.
        # Cleared the moment they sign a main or T&R contract.
        "academy_team_id": None,
    }


def make_team(data, team_id=None):
    return {
        "id": team_id or ("t-" + uuid.uuid4().hex[:10]),
        "name": data["name"],
        "colour": data["colour"],
        "money": float(data["money"]),
        "c_wins": int(data["c_wins"]),
        "perf": float(data["perf"]),
        "rel": float(data["rel"]),
        "supplier_id": None,       # filled in after all teams exist
        "supplier_name": data.get("supplier"),
        "sup_stage": int(data.get("sup_stage") or 0),
        # v0.6: team relationships — the supplier link above is now
        # dynamic and mechanical (donations, contracts, WANTED
        # drivers, research caps). relation_years is the contract
        # years left (starting relations get 2); research_spent_
        # season tracks this season's Performance + Reliability
        # research $M (the research cap counts against it); wanted
        # and research_cap live on a JUNIOR team's dict and are set
        # by its parent (supplier).
        "relation_years": 2 if int(data.get("sup_stage") or 0) > 0 else 0,
        "research_spent_season": 0.0,
        "wanted": [],               # [{driver_id, reward}] — max 3,
                                    # set by the parent of a junior team
        "research_cap": None,       # $M/season cap on Perf+Reli
                                    # research, set by the parent
        "tuning": 0,
        "perf_res": 0.0,           # $M of performance research banked
        "reli_res": 0.0,           # $M of reliability research banked
        "reg_research": 0.0,       # $M invested toward the NEXT reg change
        # v0.4: regulation adaptation state (feature 7). Initialised
        # at the start of round 7 for next season's regulations:
        # reg_total is the car perf at round 7, reg_unadapted is the
        # part still to adapt (own perf - bottom team's perf in a
        # minor year, the full perf in a major year), reg_adapted is
        # the part already fitted. Next season's car perf is
        # reg_adapted + research gains, applied when the season
        # starts. reg_pending marks a change waiting to be applied.
        "reg_total": 0.0,
        "reg_adapted": 0.0,
        "reg_unadapted": 0.0,
        "reg_pending": False,
        "driver1_id": None,
        "driver2_id": None,
        "is_ai": True,
        "owner_player_id": None,
        "points": 0,
        "points_kept": 0,  # v0.2: points scored for this team by drivers
                           # who have since left (stay with the team)
        "best_pos": None,
        # v0.4: constructors' wins this season (race wins by either
        # driver) and the round the team's best finish was achieved
        # — both feed the new standings sort (feature 1).
        "season_wins": 0,
        "best_finish_round": None,
        # v0.4.2: bot academy bookkeeping — an AI team adds at most
        # one academy driver per season (see game.py ai_act); reset
        # at season end.
        "ai_academy_signed": False,
    }


def new_career_dict(name, host_nickname, chart_on=True):
    return {
        "version": 1,
        "save_id": None,
        "name": name,
        "host": host_nickname,
        "year": 2026,
        "season_index": 0,         # 0-based; 2026 is season 0, max 30 seasons
        "round": 1,                # 1..12 within the season
        "phase": "round",          # round | qual_results | race_results |
                                   # winter_break | finished
        "settings": {"chart_on": bool(chart_on)},
        "teams": [],
        "drivers": [],
        "advanced": [],            # team ids that hit Advance this round
        "results_vote": [],        # v0.1: player ids that voted NEXT at the
                                   # current results stage (qual/race)
        "results_stage": None,     # v0.1: None | "qual" | "race" while the
                                   # room is looking at results pages
        "circuit": None,           # v0.1: current round's Grand Prix name
        "circuit_laps": None,      # v0.1: laps for the current circuit
        "race_history": [],        # v0.1: [{year, round, circuit, laps,
                                   #   qual: [full rows], race: [full rows],
                                   #   top3: [{driver, team}]}]
        "champion_history": [],    # [{year, driver, team, points}]
        "prev_driver_order": [],   # v0.2: driver ids in last season's
                                   # finishing order, best first — the
                                   # tie-breaker for driver_standings(),
                                   # so the new season opens in last
                                   # season's order (all on 0 points)
        "prev_team_order": [],     # v0.4: same for constructors — team
                                   # ids in last season's finishing
                                   # order, so the constructors' table
                                   # opens in last season's order too
        # v0.4: winter break (feature 6). After a season ends the
        # winter break runs 3 rounds of simultaneous driver offers:
        # wb_round is the current offer round (1-3), wb_offers maps
        # team id -> [{driver_id, target, contract_years}], wb_ready
        # is the team ids that have submitted offers / hit NEXT this
        # round, wb_done is True once all 3 rounds + auto-fill are
        # complete and the next season can start. last_season is a
        # snapshot of the season just finished (final standings +
        # per-driver stats) for the winter-break review.
        "wb_round": 0,
        "wb_offers": {},
        "wb_ready": [],
        "wb_done": False,
        # v0.6: pending relation proposals — {id, proposer_team_id,
        # target_team_id, kind, supplier_team_id, customer_team_id,
        # years}. Proposals are winter-break only and expire when the
        # next season starts (see start_next_season).
        "relation_proposals": [],
        "last_season": None,
        "transfers": [],           # ["Driver from X to Y"] this season
        "retired_this_season": [],
        "last_qual": None,         # v0.1: [{pos, driver, driver_id, team,
                                   #   team_id, cha, gap}] — gaps only, no times
        "last_race": None,         # v0.1: [{pos, driver, driver_id, team,
                                   #   team_id, cha, gap, gl, points}]
        "last_reg_change": None,   # {year, kind, changes: [{team, old, new}]}
        "next_reg_year": 2027,
        "next_reg_kind": "Minor",
        "created_at": None,
        "saved_at": None,
    }


def career_copy(career):
    return copy.deepcopy(career)
