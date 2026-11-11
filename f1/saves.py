"""Career saves — JSON files in saves/.

The save document is the career dict itself (see models.py): the exact same
document could later be stored in Firebase Firestore or Supabase without
changing its shape. No external database is used in v0.

Durability warning (also in the README): on Render free tier the server
filesystem is wiped on restart/sleep, so saves/ is best-effort. The durable
backup is Export (download the JSON) / Import (upload it back).
"""
import copy
import json
import os
import uuid
from datetime import datetime, timezone

SAVE_VERSION = 6  # v0.4: standings sort (best finish + earliest round),
# F2 standings, constructors' wins, Test & Reserve drivers, winter
# break 3-round offer flow, regulation adaptation/research model.
# v0.3: champion history pre-filled 2010–2025 + appended per in-game
# season (age / trophies / winning team); no upfront signing cost,
# break-cost formula, contract choice. v0.2.1: per-team points
# (team_points) + last team (last_team_id) on drivers. v0.2:
# prev_driver_order (standings carry-over). v0.1: results stages,
# circuit per round, full GP history.


def _reconstruct_last_season(doc):
    """Best-effort last-season snapshot for a v5 winter-break save,
    rebuilt from race_history (the season just finished is year - 1).
    The v0.3 end_of_season already reset every driver's points to 0,
    so the race tables are the only record of the final standings.
    Qual head-to-head is rebuilt from the qual tables too. Returns
    {year, drivers, teams} in final order, or None when there is no
    race history for that year."""
    year = (doc.get("year") or 1) - 1
    entries = [e for e in doc.get("race_history") or []
               if e.get("year") == year and e.get("race")]
    if not entries:
        return None
    drivers = {d["id"]: d for d in doc.get("drivers") or []}
    teams = {t["id"]: t for t in doc.get("teams") or []}
    dstat = {}
    tstat = {}
    qual_h2h = {}
    for e in entries:
        rnd = e.get("round") or 99
        for row in e.get("race") or []:
            did, tid, pos = row.get("driver_id"), row.get("team_id"), row.get("pos")
            pts = row.get("points") or 0
            if did and did in drivers and pos is not None:
                s = dstat.setdefault(did, {"points": 0, "wins": 0, "podiums": 0,
                                           "best_pos": None, "best_round": None,
                                           "finish_sum": 0, "finish_count": 0})
                s["points"] += pts
                # v0.4.1: the team the driver raced for — from the race
                # rows, not the driver's current team_id (a driver who
                # left at contract end, e.g. Piastri, has team_id None
                # in the save, but still raced for the team last
                # season). Overwritten per row, so it ends as the team
                # of their last race of the season.
                if tid:
                    s["team_id"] = tid
                if pos == 1:
                    s["wins"] += 1
                if pos <= 3:
                    s["podiums"] += 1
                if s["best_pos"] is None or pos < s["best_pos"]:
                    s["best_pos"] = pos
                    s["best_round"] = rnd
                s["finish_sum"] += pos
                s["finish_count"] += 1
            if tid and tid in teams and pos is not None:
                s = tstat.setdefault(tid, {"points": 0, "wins": 0,
                                           "best_pos": None, "best_round": None})
                s["points"] += pts
                if pos == 1:
                    s["wins"] += 1
                if s["best_pos"] is None or pos < s["best_pos"]:
                    s["best_pos"] = pos
                    s["best_round"] = rnd
        # Qualifying head-to-head, from the qual table of this round.
        by_team = {}
        for row in e.get("qual") or []:
            if row.get("driver_id") and row.get("team_id") and row.get("pos") is not None:
                by_team.setdefault(row["team_id"], []).append(row)
        for rows in by_team.values():
            if len(rows) >= 2:
                best = min(rows, key=lambda r: r["pos"])
                qual_h2h[best["driver_id"]] = qual_h2h.get(best["driver_id"], 0) + 1
    prev_d = {did: i for i, did in enumerate(doc.get("prev_driver_order") or [])}

    def dkey(item):
        did, s = item
        return (-s["points"], -s["wins"],
                s["best_pos"] if s["best_pos"] is not None else 99,
                s["best_round"] if s["best_round"] is not None else 99,
                prev_d.get(did, len(prev_d)),
                drivers[did].get("name", ""))

    drv_list = []
    for did, s in sorted(dstat.items(), key=dkey):
        d = drivers[did]
        team = teams.get(s.get("team_id")) or {}
        drv_list.append({
            "id": did, "name": d["name"],
            "team": team.get("name", ""), "team_id": s.get("team_id"),
            "points": s["points"], "wins": s["wins"],
            "podiums": s["podiums"], "best_pos": s["best_pos"],
            "qual_h2h": qual_h2h.get(did, 0),
            "avg_finish": round(s["finish_sum"] / s["finish_count"], 1)
                          if s["finish_count"] else None,
        })

    def tkey(item):
        tid, s = item
        return (-s["points"], -s["wins"],
                s["best_pos"] if s["best_pos"] is not None else 99,
                s["best_round"] if s["best_round"] is not None else 99,
                teams[tid].get("name", ""))

    team_list = [{"id": tid, "name": teams[tid]["name"],
                  "points": s["points"], "wins": s["wins"],
                  "best_pos": s["best_pos"]}
                 for tid, s in sorted(tstat.items(), key=tkey)]
    return {"year": year, "drivers": drv_list, "teams": team_list}


def migrate_save(doc):
    """Fill in v0.1 fields missing from a v0 save (version 1), so old saves
    still load. Rounds played under v0 have no circuit and no full qual/race
    tables in history — those entries keep their top3 and show as
    'Round N' with no tables to revisit."""
    if not isinstance(doc, dict):
        return doc
    doc.setdefault("results_vote", [])
    doc.setdefault("results_stage", None)
    # v0.2: standings carry-over — old saves open in tie-break order.
    doc.setdefault("prev_driver_order", [])
    # v0.2: constructor points banked from departed drivers.
    for team in doc.get("teams") or []:
        team.setdefault("points_kept", 0)
    # v0.2.1: per-team points + last team on every driver. Old saves
    # have neither: a driver's championship points are the best
    # available stand-in for points scored for their current team,
    # and their last team is their current team (free agents have none).
    for d in doc.get("drivers") or []:
        if "team_points" not in d:
            d["team_points"] = d.get("points", 0) if d.get("status") == "f1" else 0
        d.setdefault("last_team_id", d.get("team_id"))
    # v0.3: champion history. A save from before v0.3 has none, or only
    # in-game rows (v0.2 appended {year, driver, team, points} per
    # completed season). Either way it gains the real 2010–2025 table
    # for any year it does not already have, keeping any in-game rows
    # (e.g. a simulated 2026) in place, sorted by year. Rows that
    # predate the new fields show "—" for age / trophies / winning
    # team in the UI.
    from .data import CHAMPION_HISTORY_2010_2025
    existing = doc.get("champion_history") or []
    have_years = {r.get("year") for r in existing}
    merged = ([dict(r) for r in CHAMPION_HISTORY_2010_2025
               if r["year"] not in have_years]
              + [dict(r) for r in existing])
    for r in merged:
        r.setdefault("age", None)
        r.setdefault("trophies", None)
        r.setdefault("winning_team", None)
    merged.sort(key=lambda r: r.get("year") or 0)
    doc["champion_history"] = merged
    # v0.3: race-history top-3 entries gain team_id (for the grid's
    # team colours). Old entries resolve it from the team name.
    by_name = {t.get("name"): t.get("id") for t in doc.get("teams") or []}
    for entry in doc.get("race_history") or []:
        for item in entry.get("top3") or []:
            if "team_id" not in item:
                item["team_id"] = by_name.get(item.get("team"))
    # v0.4: standings tie-break tracking (best finish + the round it
    # was achieved, podiums, qualifying head-to-head, finishing
    # positions) on every driver; regulation state + season wins on
    # every team. T&R drivers are just a driver status ('tr') — no
    # separate migration needed.
    for d in doc.get("drivers") or []:
        d.setdefault("best_finish_round", None)
        d.setdefault("season_podiums", 0)
        d.setdefault("season_qual_h2h", 0)
        d.setdefault("finish_sum", 0)
        d.setdefault("finish_count", 0)
    for team in doc.get("teams") or []:
        team.setdefault("reg_total", 0.0)
        team.setdefault("reg_adapted", 0.0)
        team.setdefault("reg_unadapted", 0.0)
        team.setdefault("reg_pending", False)
        team.setdefault("season_wins", 0)
        team.setdefault("best_finish_round", None)
    # v0.4: constructors' carry-over order, the last-season snapshot
    # (winter-break review + offer resolution order), and the
    # winter-break flow state.
    doc.setdefault("prev_team_order", [])
    doc.setdefault("wb_round", 0)
    doc.setdefault("wb_offers", {})
    doc.setdefault("wb_ready", [])
    doc.setdefault("wb_done", False)
    doc.setdefault("pending_reg_year", None)
    doc.setdefault("pending_reg_kind", None)
    if "last_season" not in doc or doc.get("last_season") is None:
        doc["last_season"] = _reconstruct_last_season(doc)
    if doc.get("phase") == "winter_break" and not doc.get("wb_done"):
        # A v5 winter-break save enters the new 3-round offer flow at
        # round 1 (its reg change was already applied under v0.3, so
        # reg_pending stays False and no reg change is applied again
        # when the break ends). Bot offers for round 1 are generated
        # when the career starts (server.py start_career), after the
        # humans have picked their teams — never at load, when every
        # team is still flagged AI (v0.4.1).
        if not doc.get("wb_round"):
            doc["wb_round"] = 1
        doc.setdefault("wb_offers", {})
        doc.setdefault("wb_ready", [])
        if not doc.get("prev_team_order") and doc.get("last_season"):
            doc["prev_team_order"] = [t["id"] for t in doc["last_season"]["teams"]]
    # A career saved mid-results under v0 never existed (v0 had no results
    # stages), but guard anyway: drop back to the dashboard round.
    if doc.get("phase") == "race_results":
        doc["phase"] = "round"
    if "circuit" not in doc or doc.get("circuit") is None:
        from .data import circuit_for_round
        gp = circuit_for_round(doc.get("round") or 1)
        doc["circuit"] = gp["name"]
        doc["circuit_laps"] = gp["laps"]
    for entry in doc.get("race_history") or []:
        entry.setdefault("circuit", f"Round {entry.get('round')}")
        entry.setdefault("laps", None)
        entry.setdefault("qual", None)   # None = no full table recorded (v0)
        entry.setdefault("race", None)
    # Strip any absolute times that slipped into stored results rows.
    for key in ("last_qual", "last_race"):
        for row in doc.get(key) or []:
            row.pop("time", None)
    for entry in doc.get("race_history") or []:
        for key in ("qual", "race"):
            for row in entry.get(key) or []:
                row.pop("time", None)
    return doc


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _path(saves_dir, save_id):
    return os.path.join(saves_dir, f"{save_id}.json")


def save_career(career, name, saves_dir):
    """Write the career to saves/{save_id}.json. Returns the save meta."""
    os.makedirs(saves_dir, exist_ok=True)
    doc = copy.deepcopy(career)
    save_id = doc.get("save_id") or uuid.uuid4().hex[:12]
    doc["save_id"] = save_id
    doc["version"] = SAVE_VERSION
    doc["name"] = name or doc.get("name") or "Untitled career"
    doc["saved_at"] = _now_iso()
    with open(_path(saves_dir, save_id), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    return save_meta(doc)


def save_meta(doc):
    return {
        "save_id": doc.get("save_id"),
        "name": doc.get("name"),
        "host": doc.get("host"),
        "year": doc.get("year"),
        "round": doc.get("round"),
        "season_index": doc.get("season_index"),
        "phase": doc.get("phase"),
        "saved_at": doc.get("saved_at"),
    }


def list_saves(saves_dir):
    os.makedirs(saves_dir, exist_ok=True)
    metas = []
    for fname in sorted(os.listdir(saves_dir)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(saves_dir, fname), encoding="utf-8") as f:
                doc = json.load(f)
            metas.append(save_meta(doc))
        except (OSError, ValueError):
            continue
    metas.sort(key=lambda m: m.get("saved_at") or "", reverse=True)
    return metas


def load_save(saves_dir, save_id):
    """Returns the career dict, or None."""
    path = _path(saves_dir, save_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or "teams" not in doc or "drivers" not in doc:
        return None
    return migrate_save(doc)


def delete_save(saves_dir, save_id):
    path = _path(saves_dir, save_id)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def import_save(doc, saves_dir):
    """Validate an uploaded save JSON and store it. A colliding save_id gets
    a fresh one so an import never overwrites an existing save."""
    if not isinstance(doc, dict) or "teams" not in doc or "drivers" not in doc:
        return None, "That file is not a valid career save."
    os.makedirs(saves_dir, exist_ok=True)
    doc = copy.deepcopy(doc)
    save_id = doc.get("save_id") or uuid.uuid4().hex[:12]
    if os.path.exists(_path(saves_dir, save_id)):
        save_id = uuid.uuid4().hex[:12]
        doc["save_id"] = save_id
    doc = migrate_save(doc)
    doc["version"] = SAVE_VERSION
    doc["saved_at"] = _now_iso()
    with open(_path(saves_dir, save_id), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    return save_meta(doc), "ok"
