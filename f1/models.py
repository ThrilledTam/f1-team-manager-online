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
        "points": 0,
        "season_wins": 0,
        "best_pos": None,
        "qual_time": None,
        "qual_pos": None,
        "race_time": None,
        "race_pos": None,
        "status": status,          # "f1" | "free" | "f2" | "queued" | "pool" | "retired"
        "team_id": team_id,
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
        "tuning": 0,
        "perf_res": 0.0,           # $M of performance research banked
        "reli_res": 0.0,           # $M of reliability research banked
        "reg_research": 0.0,       # $M invested toward the NEXT reg change
        "driver1_id": None,
        "driver2_id": None,
        "is_ai": True,
        "owner_player_id": None,
        "points": 0,
        "best_pos": None,
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
