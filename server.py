"""
F1 Team Manager Online — v0 (beta)
===================================
A clean online rebuild of Matthew's PyCharm F1 team-manager simulator
(Main.py is the source of truth; MainMod.py is ignored).

Stack: Python 3.12, FastAPI + WebSockets, single-page frontend in static/.
No accounts, no sign-in. All game logic lives server-side in f1/ so every
player in a room sees the same state at the same time.

v0 scope:
- Home: Create Room / Join Room (6-letter code) / Settings / Exit (returns
  to home; a browser tab cannot be force-closed).
- Lobby: player list, host crown, room code + join link, host kick (lobby),
  host promotion, reconnect with the same player_id, room held 120s empty,
  5s heartbeat.
- Career setup (host): Start New Career (2026 only) or Continue a Saved
  Career from saves/. Each human picks an existing team or creates a custom
  team (new careers only); joining a mid-career room means taking over an
  AI team. 1-10 humans, 11-team base grid.
- Simultaneous turns: all humans act at once on their own dashboard. When
  every connected human has hit Advance, AI teams act and the server runs
  qualifying then the race.
- Season: 12 rounds, then rollover (ageing, retirements, transfers, prize
  money) and a winter break to refill empty seats. Up to 30 seasons.
- Regulation research every year: Minor every year, Major every 4 years
  (2030, 2034, ...). Reg Research is a separate pool, spent at rollover.
- Perf chart toggle: when OFF, no perf numbers reach any human client, and
  AI decisions are results-based only.
- Saves: JSON in saves/, plus Export (download) / Import (upload). Render
  free wipes the filesystem on restart, so Export/Import is the durable
  backup. No Firebase / Supabase in v0.

Run locally:
    python -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
Then open http://localhost:8000

Protocol (JSON over one WebSocket per player, endpoint /ws):
  Client -> server:
    create_room  {nickname, colour}
    join_room    {code, nickname, colour}
    rejoin       {code, player_id}
    update_settings {chart_on}            # host, lobby only
    kick_player  {player_id}              # host, lobby only
    list_saves   {}
    save_career  {name}                   # host, career must exist
    load_save    {save_id}                # host, lobby only
    delete_save  {save_id}                # host
    new_career   {name?}                  # host, lobby only
    choose_team  {team_id}                # lobby / takeover
    create_custom_team {name, colour, driver1_id, driver2_id}
    start_career {}                       # host, lobby only
    research     {priority, amount}       # "perf" | "reliability"
    develop      {}
    tuning       {value}                  # -3..3
    reg_research {amount}
    sign_driver  {driver_id, seat}        # round or winter break
    advance      {}
    start_next_season {}                  # winter break, when seats are filled
    ping         {}
  Server -> client:
    joined       {code, player_id, room}  # this player only
    room_state   {room}                   # broadcast; career is the public
                                          # state (perf stripped when chart off)
    my_team      {team}                   # this player only: own team incl.
                                          # research pools, never perf
    saves_list   {saves}
    error        {message}
    kicked       {message}                # to the kicked player only
    pong         {}
"""
import asyncio
import json
import os
import random
import secrets
import string
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from f1 import game
from f1.models import CUSTOM_COLOURS
from f1.saves import (
    delete_save, import_save, list_saves, load_save, save_career, save_meta,
)

BASE_DIR = Path(__file__).parent
SAVES_DIR = BASE_DIR / "saves"
STATIC_DIR = BASE_DIR / "static"

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="F1 Team Manager Online v0", lifespan=lifespan)

ROOMS = {}          # code -> room dict
MAX_PLAYERS = 10
EMPTY_HOLD_SECONDS = 120
HEARTBEAT_TIMEOUT = 60

PALETTE = [
    "#e74c3c", "#e67e22", "#f1c40f", "#2ecc71", "#1abc9c",
    "#3498db", "#9b59b6", "#fd79a8", "#00cec9", "#6c5ce7",
]


def new_code():
    while True:
        code = "".join(secrets.choice(string.ascii_uppercase) for _ in range(6))
        if code not in ROOMS:
            return code


def new_room(host_nickname, host_colour):
    code = new_code()
    room = {
        "code": code,
        "host_id": None,
        "players": [],          # {player_id, nickname, colour, connected, joined_at}
        "career": None,        # career dict or None
        "career_mode": None,   # None | "new" | "loaded"
        "started": False,
        "chart_on": True,
        "created_at": time.time(),
        "empty_since": None,
    }
    ROOMS[code] = room
    return room


def add_player(room, nickname, colour, player_id=None):
    pid = player_id or uuid.uuid4().hex[:12]
    player = {
        "player_id": pid,
        "nickname": (nickname or "Player").strip()[:20] or "Player",
        "colour": colour if colour in PALETTE + CUSTOM_COLOURS else random.choice(PALETTE),
        "connected": True,
        "joined_at": time.time(),
        "last_seen": time.time(),
    }
    room["players"].append(player)
    if room["host_id"] is None:
        room["host_id"] = pid
    room["empty_since"] = None
    return player


def get_player(room, player_id):
    return next((p for p in room["players"] if p["player_id"] == player_id), None)


def connected_ids(room):
    return {p["player_id"] for p in room["players"] if p["connected"]}


def my_team_for(room, player_id):
    """The team a player owns in this room's career (or None)."""
    career = room.get("career")
    if not career:
        return None
    return next((t for t in career["teams"]
                 if t.get("owner_player_id") == player_id), None)


def room_public(room, viewer_player_id=None):
    """Room state for broadcast. The career is the public state (perf
    stripped when the chart is off); each player's own team details go in a
    separate private my_team message."""
    career = room.get("career")
    viewer_team = my_team_for(room, viewer_player_id) if viewer_player_id else None
    return {
        "code": room["code"],
        "host_id": room["host_id"],
        # v0.1: the host override button is only ever sent to the host
        # client — the frontend shows it when this flag is true.
        "is_host": viewer_player_id == room["host_id"],
        "players": [
            {"player_id": p["player_id"], "nickname": p["nickname"],
             "colour": p["colour"], "connected": p["connected"],
             "is_host": p["player_id"] == room["host_id"],
             "team_id": (my_team_for(room, p["player_id"]) or {}).get("id"),
             "team_name": (my_team_for(room, p["player_id"]) or {}).get("name")}
            for p in room["players"]
        ],
        "career": game.public_state(career, room["chart_on"],
                                    viewer_team["id"] if viewer_team else None) if career else None,
        "career_mode": room["career_mode"],
        "started": room["started"],
        "chart_on": room["chart_on"],
    }


def my_team_public(room, player_id):
    """Private view of your own team: money, research pools, tuning — never
    perf, even when the chart is on this is the same data the dashboard uses
    for actions. (Perf itself is only in the broadcast when chart is on.)"""
    team = my_team_for(room, player_id)
    if not team:
        return None
    career = room["career"]
    out = {k: v for k, v in team.items() if k != "perf"}
    # Copy the driver dicts: never mutate the live career state here.
    out["drivers"] = []
    for did in (team["driver1_id"], team["driver2_id"]):
        if not did:
            continue
        d = game.get_driver(career, did)
        if d:
            d = dict(d)
            if not room["chart_on"]:
                d.pop("perf", None)
            d.pop("qual_time", None)
            d.pop("race_time", None)
            out["drivers"].append(d)
    out["finances"] = game.finances_view(career, team)
    out["empty_seats"] = game.empty_seats(career, team)
    return out


async def broadcast(room, exclude=None):
    """Send room_state to everyone, plus a private my_team to each player."""
    dead = []
    for player in list(room["players"]):
        ws = player.get("ws")
        if not ws:
            continue
        try:
            await ws.send_json({"type": "room_state",
                                "room": room_public(room, player["player_id"])})
            mt = my_team_public(room, player["player_id"])
            if mt:
                await ws.send_json({"type": "my_team", "team": mt})
        except Exception:
            dead.append(player)
    for p in dead:
        p["connected"] = False
        p.pop("ws", None)


async def send_error(ws, message):
    try:
        await ws.send_json({"type": "error", "message": message})
    except Exception:
        pass


def promote_host_if_needed(room):
    if any(p["player_id"] == room["host_id"] and p["connected"]
           for p in room["players"]):
        return
    for p in room["players"]:
        if p["connected"]:
            room["host_id"] = p["player_id"]
            return
    room["host_id"] = room["players"][0]["player_id"] if room["players"] else None


async def cleanup_loop():
    while True:
        await asyncio.sleep(10)
        now = time.time()
        for code in list(ROOMS):
            room = ROOMS[code]
            if not room["players"] or not any(p["connected"] for p in room["players"]):
                if room["empty_since"] is None:
                    room["empty_since"] = now
                elif now - room["empty_since"] > EMPTY_HOLD_SECONDS:
                    del ROOMS[code]
            else:
                room["empty_since"] = None

# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "rooms": len(ROOMS), "version": "v0.1"}


@app.get("/api/saves")
async def api_saves():
    return {"saves": list_saves(SAVES_DIR)}


@app.get("/api/export/{save_id}")
async def api_export(save_id: str):
    path = SAVES_DIR / f"{save_id}.json"
    if not path.exists():
        return JSONResponse({"error": "No such save."}, status_code=404)
    return FileResponse(path, filename=f"f1-career-{save_id}.json",
                        media_type="application/json")


@app.post("/api/import")
async def api_import(file: UploadFile = File(...)):
    try:
        doc = json.loads((await file.read()).decode("utf-8"))
    except Exception:
        return JSONResponse({"error": "That file is not valid JSON."}, status_code=400)
    meta, msg = import_save(doc, SAVES_DIR)
    if meta is None:
        return JSONResponse({"error": msg}, status_code=400)
    return {"save": meta}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    room = None
    player = None
    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")
            if mtype == "ping":
                if player:
                    player["last_seen"] = time.time()
                await ws.send_json({"type": "pong"})
                continue

            if mtype == "create_room":
                room = new_room(msg.get("nickname"), msg.get("colour"))
                player = add_player(room, msg.get("nickname"), msg.get("colour"))
                player["ws"] = ws
                await ws.send_json({"type": "joined", "code": room["code"],
                                    "player_id": player["player_id"],
                                    "room": room_public(room, player["player_id"])})
                await broadcast(room)
                continue

            if mtype == "join_room":
                code = (msg.get("code") or "").upper().strip()
                room = ROOMS.get(code)
                if not room:
                    await send_error(ws, f"No room with code {code}. It may have "
                                         "expired — ask the host to create a new one.")
                    room = None
                    continue
                if len(room["players"]) >= MAX_PLAYERS:
                    await send_error(ws, "That room is full (10 players max).")
                    room = None
                    continue
                player = add_player(room, msg.get("nickname"), msg.get("colour"))
                player["ws"] = ws
                await ws.send_json({"type": "joined", "code": room["code"],
                                    "player_id": player["player_id"],
                                    "room": room_public(room, player["player_id"])})
                await broadcast(room)
                continue

            if mtype == "rejoin":
                code = (msg.get("code") or "").upper().strip()
                room = ROOMS.get(code)
                player = get_player(room, msg.get("player_id")) if room else None
                if not room or not player:
                    await send_error(ws, "Could not rejoin that room — it may have expired.")
                    room, player = None, None
                    continue
                player["connected"] = True
                player["last_seen"] = time.time()
                player["ws"] = ws
                room["empty_since"] = None
                promote_host_if_needed(room)
                await ws.send_json({"type": "joined", "code": room["code"],
                                    "player_id": player["player_id"],
                                    "room": room_public(room, player["player_id"])})
                await broadcast(room)
                continue

            # Everything below needs a room + player.
            if room is None or player is None:
                await send_error(ws, "Join or create a room first.")
                continue
            player["last_seen"] = time.time()
            career = room.get("career")
            is_host = player["player_id"] == room["host_id"]
            team = my_team_for(room, player["player_id"])
            rng = random

            if mtype == "update_settings":
                if not is_host:
                    await send_error(ws, "Only the host can change settings.")
                elif room["started"]:
                    await send_error(ws, "Settings can only change in the lobby.")
                else:
                    if "chart_on" in msg:
                        room["chart_on"] = bool(msg["chart_on"])
                        if room.get("career"):
                            room["career"]["settings"]["chart_on"] = room["chart_on"]
                    await broadcast(room)

            elif mtype == "kick_player":
                if not is_host:
                    await send_error(ws, "Only the host can kick players.")
                elif room["started"]:
                    await send_error(ws, "Players can only be kicked in the lobby.")
                else:
                    target = get_player(room, msg.get("player_id"))
                    if not target or target["player_id"] == player["player_id"]:
                        await send_error(ws, "Cannot kick that player.")
                    else:
                        if career:
                            t = my_team_for(room, target["player_id"])
                            if t:
                                t["is_ai"] = True
                                t["owner_player_id"] = None
                        tgt_ws = target.pop("ws", None)
                        room["players"] = [p for p in room["players"]
                                           if p["player_id"] != target["player_id"]]
                        if tgt_ws:
                            try:
                                await tgt_ws.send_json(
                                    {"type": "kicked",
                                     "message": "The host removed you from the room."})
                            except Exception:
                                pass
                        promote_host_if_needed(room)
                        await broadcast(room)

            elif mtype == "list_saves":
                await ws.send_json({"type": "saves_list",
                                    "saves": list_saves(SAVES_DIR)})

            elif mtype == "rumours":
                if not career:
                    await send_error(ws, "No career running.")
                else:
                    await ws.send_json({"type": "rumours",
                                        "rumours": game.retirement_rumours(career)})

            elif mtype == "save_career":
                if not is_host:
                    await send_error(ws, "Only the host can save the career.")
                elif not career:
                    await send_error(ws, "No career to save yet.")
                else:
                    meta = save_career(career, msg.get("name") or career.get("name"),
                                       SAVES_DIR)
                    career["save_id"] = meta["save_id"]
                    await ws.send_json({"type": "saved", "save": meta})
                    await broadcast(room)

            elif mtype == "load_save":
                if not is_host:
                    await send_error(ws, "Only the host can load a career.")
                elif room["started"]:
                    await send_error(ws, "Load a career from the lobby, before starting.")
                else:
                    doc = load_save(SAVES_DIR, msg.get("save_id"))
                    if doc is None:
                        await send_error(ws, "Could not load that save.")
                    elif doc.get("phase") == "finished":
                        await send_error(ws, "That career is finished (30 seasons played).")
                    else:
                        # Clear old ownership; everyone takes over an AI team.
                        for t in doc["teams"]:
                            t["is_ai"] = True
                            t["owner_player_id"] = None
                        doc["advanced"] = []
                        doc["save_id"] = doc.get("save_id")
                        room["career"] = doc
                        room["career_mode"] = "loaded"
                        room["chart_on"] = bool(doc.get("settings", {}).get("chart_on", True))
                        await broadcast(room)

            elif mtype == "delete_save":
                if not is_host:
                    await send_error(ws, "Only the host can delete saves.")
                elif not delete_save(SAVES_DIR, msg.get("save_id")):
                    await send_error(ws, "Could not delete that save.")
                else:
                    await ws.send_json({"type": "saves_list",
                                        "saves": list_saves(SAVES_DIR)})

            elif mtype == "new_career":
                if not is_host:
                    await send_error(ws, "Only the host can start a career.")
                elif room["started"]:
                    await send_error(ws, "A career is already running.")
                else:
                    room["career"] = game.build_new_career(
                        msg.get("name") or "My F1 Career",
                        player["nickname"], chart_on=room["chart_on"])
                    room["career_mode"] = "new"
                    await broadcast(room)

            elif mtype == "choose_team":
                if not career:
                    await send_error(ws, "Set up a career first (new or loaded).")
                elif room["started"] and career.get("phase") not in ("round", "winter_break"):
                    await send_error(ws, "You cannot change team now.")
                elif team:
                    await send_error(ws, "You already have a team in this career.")
                else:
                    ok, why = game.choose_team(career, msg.get("team_id"),
                                               player["player_id"])
                    if not ok:
                        await send_error(ws, why)
                    else:
                        await ws.send_json({"type": "action", "action": "choose_team",
                                            "ok": True, "message": why})
                        await broadcast(room)

            elif mtype == "create_custom_team":
                if not career:
                    await send_error(ws, "Set up a career first (new or loaded).")
                elif room["career_mode"] != "new":
                    await send_error(ws, "Custom teams are only for new careers. "
                                         "In a loaded career, take over an existing team.")
                elif room["started"]:
                    await send_error(ws, "Custom teams are picked before the career starts.")
                elif team:
                    await send_error(ws, "You already have a team in this career.")
                else:
                    ok, why, _t = game.create_custom_team(
                        career, player["player_id"], msg.get("name"),
                        msg.get("colour"), msg.get("driver1_id"), msg.get("driver2_id"))
                    if not ok:
                        await send_error(ws, why)
                    else:
                        await ws.send_json({"type": "action", "action": "create_custom_team",
                                            "ok": True, "message": why})
                        await broadcast(room)

            elif mtype == "start_career":
                if not is_host:
                    await send_error(ws, "Only the host can start the career.")
                elif room["started"]:
                    await send_error(ws, "The career has already started.")
                elif not career:
                    await send_error(ws, "Set up a career first (new or loaded).")
                else:
                    without_team = [p for p in room["players"]
                                    if p["connected"] and not my_team_for(room, p["player_id"])]
                    if not game.human_teams(career):
                        await send_error(ws, "At least one player needs to pick a team first.")
                    elif without_team:
                        names = ", ".join(p["nickname"] for p in without_team)
                        await send_error(ws, f"Waiting for {names} to pick a team.")
                    else:
                        room["started"] = True
                        if career.get("phase") not in ("round", "winter_break"):
                            career["phase"] = "round"
                        await broadcast(room)

            elif mtype == "vote_next":
                # v0.1: unanimous NEXT vote on a results page.
                if not room["started"] or not career:
                    await send_error(ws, "The career has not started yet.")
                else:
                    ok, _advanced, why = game.vote_next(
                        career, player["player_id"], connected_ids(room), rng=rng)
                    if not ok:
                        await send_error(ws, why)
                    else:
                        await ws.send_json({"type": "action", "action": "vote_next",
                                            "ok": True, "message": why})
                    await broadcast(room)

            elif mtype == "host_skip":
                # v0.1: host-only override — advances the results stage now.
                if not room["started"] or not career:
                    await send_error(ws, "The career has not started yet.")
                else:
                    ok, _advanced, why = game.host_skip_results(
                        career, player["player_id"], connected_ids(room),
                        is_host, rng=rng)
                    if not ok:
                        await send_error(ws, why)
                    else:
                        await ws.send_json({"type": "action", "action": "host_skip",
                                            "ok": True, "message": why})
                    await broadcast(room)

            elif mtype in ("research", "develop", "tuning", "reg_research",
                           "sign_driver", "advance"):
                if not room["started"] or not career:
                    await send_error(ws, "The career has not started yet.")
                elif not team:
                    await send_error(ws, "Pick a team first (take over an AI team).")
                elif career.get("phase") == "winter_break" and mtype != "sign_driver":
                    await send_error(ws, "Winter break: fill empty seats first.")
                elif career.get("phase") not in ("round", "winter_break"):
                    await send_error(ws, "You cannot do that right now.")
                else:
                    if mtype == "research":
                        ok, why = game.do_research(career, team["id"],
                                                   msg.get("priority"), msg.get("amount"))
                    elif mtype == "develop":
                        ok, why = game.do_develop(career, team["id"])
                    elif mtype == "tuning":
                        ok, why = game.do_tuning(career, team["id"], msg.get("value"))
                    elif mtype == "reg_research":
                        ok, why = game.do_reg_research(career, team["id"], msg.get("amount"))
                    elif mtype == "sign_driver":
                        ok, why = game.sign_driver(career, team["id"],
                                                   msg.get("driver_id"), msg.get("seat"))
                    elif mtype == "advance":
                        ok, _raced, why = game.advance_team(
                            career, team["id"], connected_ids(room), rng=rng)
                    if not ok:
                        await send_error(ws, why)
                    else:
                        # v0.1: confirm every action back to the actor so
                        # the frontend can toast it (e.g. "Tuning set to +2 ✓").
                        await ws.send_json({"type": "action", "action": mtype,
                                            "ok": True, "message": why})
                    await broadcast(room)

            elif mtype == "start_next_season":
                if not room["started"] or not career:
                    await send_error(ws, "The career has not started yet.")
                else:
                    ok, why = game.start_next_season(career)
                    if not ok:
                        await send_error(ws, why)
                    else:
                        await ws.send_json({"type": "action", "action": "start_next_season",
                                            "ok": True,
                                            "message": f"Season {career['year']} starts ✓"})
                    await broadcast(room)

            else:
                await send_error(ws, f"Unknown message: {mtype}")
    except WebSocketDisconnect:
        pass
    finally:
        if player is not None:
            player["connected"] = False
            player.pop("ws", None)
        if room is not None:
            promote_host_if_needed(room)
            # v0.1: a disconnect shrinks the NEXT-vote voter set, so a
            # stalled vote can now complete (the disconnected team is AI).
            career = room.get("career")
            if career and career.get("phase") in ("qual_results", "race_results"):
                game.check_results_vote(career, connected_ids(room))
            if not any(p["connected"] for p in room["players"]):
                room["empty_since"] = time.time()
            await broadcast(room)
