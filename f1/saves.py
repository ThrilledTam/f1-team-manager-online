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

SAVE_VERSION = 4  # v0.2.1: per-team points (team_points) + last team
# (last_team_id) on drivers. v0.2: prev_driver_order (standings
# carry-over). v0.1: results stages, circuit per round, full GP history.


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
