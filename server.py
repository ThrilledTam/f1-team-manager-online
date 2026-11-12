"""
F1 Team Manager Online — v0.6.3 (beta)
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
    reg_adapt    {points?}             # adapt unadapted parts, $0.25M/pt;
                                       # no points = adapt all
    sign_driver  {driver_id, seat, contract_years}  # round only (winter
                                                     # break uses offers);
                                                     # contract_years 1-4,
                                                     # picked by the signer;
                                                     # may poach another
                                                     # team's driver
    sign_tr      {driver_id, contract_years}  # sign a T&R driver (1-4 yrs)
    release_driver {driver_id}            # v0.4.3: release a main or T&R
                                          # driver — break fee = main-driver
                                          # formula, shown upfront, round only
    add_academy  {driver_id}                  # add a free/F2 driver (25 or
                                              # under) to your Driver Academy
                                              # (1/3 salary, round + winter)
    release_academy {driver_id}               # release an academy driver (free)
    swap_driver  {seat, tr_driver_id}  # swap a main driver with a T&R driver
    promote_tr   {tr_driver_id, seat}  # promote a T&R driver to an empty seat
    winter_offers {offers}             # winter break: [{driver_id, target,
                                       # seat, years}] — max 3, max 2 main,
                                       # max 1 T&R
    winter_next  {}                    # winter break: ready for the round
    advance      {}
    start_next_season {}                  # winter break, when the 3 rounds
                                          # are done and seats are filled
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


app = FastAPI(title="F1 Team Manager Online v0.6.3", lifespan=lifespan)

ROOMS = {}          # code -> room dict
MAX_PLAYERS = 10
EMPTY_HOLD_SECONDS = 120
# v0.6.2: a disconnected human's team becomes takeable by another
# player once they have been away this long. A quick reload (same
# player_id auto-rejoin) is well inside the grace period, so a
# reconnecting player is never beaten to their own team.
TAKEOVER_GRACE_SECONDS = 60
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
        # v0.6.2: when the player last disconnected (None while
        # connected). Drives the takeover grace period for their team.
        "disconnected_at": None,
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


def team_takeable(room, team, now=None):
    """Whether a player with no team may take this team (v0.6.2).

    AI teams are always takeable. A team owned by a CONNECTED human
    never is. A team owned by a disconnected human becomes takeable
    once TAKEOVER_GRACE_SECONDS have passed since they left, so a
    quick reload (same player_id auto-rejoin) is never beaten to
    the team. Returns (ok, reason)."""
    owner_id = team.get("owner_player_id")
    if not owner_id:
        return True, ""
    owner = get_player(room, owner_id)
    if owner is not None and owner.get("connected"):
        return False, "That team is taken by a player who is still connected."
    disc = owner.get("disconnected_at") if owner else None
    if disc is None:
        # No disconnect timestamp (owner record gone, or an older
        # room): the owner is not here, so the team is not stranded.
        return True, ""
    now = time.time() if now is None else now
    elapsed = now - disc
    if elapsed >= TAKEOVER_GRACE_SECONDS:
        return True, ""
    left = int(TAKEOVER_GRACE_SECONDS - elapsed) + 1
    return False, (f"That team's manager disconnected recently — "
                   f"it becomes available in {left}s.")


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
        # v0.6.2: the takeover grace period, so the client picker can
        # show "available in Xs" for a disconnected owner's team.
        "takeover_grace_seconds": TAKEOVER_GRACE_SECONDS,
        "players": [
            {"player_id": p["player_id"], "nickname": p["nickname"],
             "colour": p["colour"], "connected": p["connected"],
             "disconnected_at": p.get("disconnected_at"),
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
    # v0.4: raw regulation numbers never reach a client — only the
    # percentages + cost in reg_view. Strip the raw fields here too.
    out.pop("reg_total", None)
    out.pop("reg_adapted", None)
    out.pop("reg_unadapted", None)
    out["reg_view"] = game.reg_view(career, team)
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
            # v0.3: reputation is never shown to players, in any payload.
            d.pop("rep", None)
            d.pop("qual_time", None)
            d.pop("race_time", None)
            # v0.4.3: the contract-break fee for this driver, so the
            # Drivers tab can show the Drop cost upfront (same
            # formula as a main-driver replacement).
            d["break_fee"] = game.break_fee_for_driver(career, d)
            # v0.5: development contribution (same helper do_develop
            # uses) — the Drivers tab shows it per driver.
            d["dev_contrib"] = game.driver_dev_contrib(d)
            out["drivers"].append(d)
    # v0.4: your Test & Reserve drivers, with the same stripping.
    out["tr_drivers"] = []
    for d in game.team_tr_drivers(career, team):
        d = dict(d)
        if not room["chart_on"]:
            d.pop("perf", None)
        d.pop("rep", None)
        d.pop("qual_time", None)
        d.pop("race_time", None)
        # v0.4.3: same break fee — releasing a T&R driver costs
        # exactly what breaking a main driver's contract costs.
        d["break_fee"] = game.break_fee_for_driver(career, d)
        # v0.5: development contribution (same helper do_develop
        # uses) — the Drivers tab shows it per driver.
        d["dev_contrib"] = game.driver_dev_contrib(d)
        out["tr_drivers"].append(d)
    # v0.4.2: your Driver Academy juniors, with the same stripping.
    out["academy_drivers"] = []
    for d in game.team_academy_drivers(career, team):
        d = dict(d)
        if not room["chart_on"]:
            d.pop("perf", None)
        d.pop("rep", None)
        d.pop("qual_time", None)
        d.pop("race_time", None)
        out["academy_drivers"].append(d)
    # v0.5: the team's driver development factor (1 + main 2 + T&R
    # contributions) — the Research tab shows it near Develop, and
    # do_develop uses the same helper, so display and mechanic agree.
    out["dev_factor"] = game.team_dev_factor(career, team)
    out["finances"] = game.finances_view(career, team)
    out["empty_seats"] = game.empty_seats(career, team)
    return out


def ensure_winter_break_open(career, rng=None):
    """v0.4.1: when a winter-break career STARTS (start_career, after
    the humans have picked their teams), make sure the current round
    has been opened — bot teams make their offers and do last-minute
    R&D when the round opens. It must NOT run at load time: a loaded
    career has every team flagged AI (ownership was just cleared),
    so opening the round then would auto-generate offers — and spend
    R&D money — for teams a human is about to take over. A round is
    opened when every AI team has an entry in wb_offers (even an
    empty one); if any AI team is missing, open (or re-open) the
    round. Safe to call on every start: it never re-runs for an
    already-open round, so bots never double-spend on R&D."""
    if career.get("phase") != "winter_break":
        return
    offers = career.get("wb_offers") or {}
    ai_ids = [t["id"] for t in career["teams"] if t["is_ai"]]
    if any(tid not in offers for tid in ai_ids):
        game.start_winter_break_round(career, rng=rng)


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
        # v0.6.2: a send failure is a disconnect too — start the
        # takeover grace period for this player's team.
        p["disconnected_at"] = time.time()
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
    return {"ok": True, "rooms": len(ROOMS), "version": "v0.6.3"}


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
                # v0.6.2: same-player auto-rejoin clears the disconnect
                # timestamp — the player keeps their team, and the
                # takeover grace period is cancelled.
                player["disconnected_at"] = None
                player["ws"] = ws
                room["empty_since"] = None
                promote_host_if_needed(room)
                await ws.send_json({"type": "joined", "code": room["code"],
                                    "player_id": player["player_id"],
                                    "room": room_public(room, player["player_id"])})
                await broadcast(room)
                continue

            if mtype == "create_from_save":
                # v0.4: "Continue a saved career" from the home screen —
                # creates a room with that career loaded, with the host
                # in it; friends join by the room code/link as usual.
                doc = load_save(SAVES_DIR, msg.get("save_id"))
                if doc is None:
                    await send_error(ws, "Could not load that save.")
                    continue
                if doc.get("phase") == "finished":
                    await send_error(ws, "That career is finished (30 seasons played).")
                    continue
                room = new_room(msg.get("nickname"), msg.get("colour"))
                player = add_player(room, msg.get("nickname"), msg.get("colour"))
                player["ws"] = ws
                for t in doc["teams"]:
                    t["is_ai"] = True
                    t["owner_player_id"] = None
                doc["advanced"] = []
                doc["save_id"] = doc.get("save_id")
                room["career"] = doc
                room["career_mode"] = "loaded"
                room["chart_on"] = bool(doc.get("settings", {}).get("chart_on", True))
                # v0.4.1: do NOT open the winter-break round here — every
                # team is still flagged AI at load time, so bot offers
                # would be auto-generated for teams a human is about to
                # take over. The round opens at start_career, once the
                # humans have picked their teams.
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
                                # v0.4.1: the team is AI again — if the
                                # winter break is running, the bot
                                # manager takes over its offers for
                                # this round (offers only: no R&D
                                # spending on top of what the human
                                # may already have done). Any offers
                                # the kicked player submitted are
                                # replaced by the bot's.
                                if career.get("phase") == "winter_break":
                                    game.bot_winter_offers(career, t, rng=rng)
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
                        # v0.4.1: do NOT open the winter-break round here
                        # (see create_from_save) — it opens at
                        # start_career, once the humans have picked
                        # their teams.
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
                    target = next((t for t in career["teams"]
                                   if t["id"] == msg.get("team_id")), None)
                    if not target:
                        await send_error(ws, "No such team.")
                    else:
                        # v0.6.2: a team owned by a connected human is
                        # never takeable; a disconnected owner's team
                        # only becomes takeable after the grace period.
                        # The server re-validates here — the team may
                        # have been retaken by its owner reconnecting
                        # after the picker rendered.
                        ok, why = team_takeable(room, target)
                        if not ok:
                            await send_error(ws, why)
                        else:
                            ok, why = game.choose_team(
                                career, msg.get("team_id"), player["player_id"],
                                allow_takeover=bool(target.get("owner_player_id")))
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
                        # v0.4.1: open the winter-break round now that
                        # the humans have picked their teams — bot
                        # offers (and bot R&D) are generated only for
                        # teams that are still AI.
                        ensure_winter_break_open(career, rng=rng)
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

            elif mtype in ("add_academy", "release_academy"):
                # v0.4.2: academy actions work between races AND during
                # the winter break — adding a junior to the academy is
                # not a signing, so the winter-break signing block
                # does not apply to it.
                if not room["started"] or not career:
                    await send_error(ws, "The career has not started yet.")
                elif not team:
                    await send_error(ws, "Pick a team first (take over an AI team).")
                elif career.get("phase") not in ("round", "winter_break"):
                    await send_error(ws, "You cannot do that right now.")
                else:
                    if mtype == "add_academy":
                        ok, why = game.add_academy_driver(
                            career, team["id"], msg.get("driver_id"))
                    else:
                        ok, why = game.release_academy_driver(
                            career, team["id"], msg.get("driver_id"))
                    if not ok:
                        await send_error(ws, why)
                    else:
                        await ws.send_json({"type": "action", "action": mtype,
                                            "ok": True, "message": why})
                    await broadcast(room)

            elif mtype in ("donate", "propose_relation", "respond_relation",
                           "break_relation", "set_wanted", "remove_wanted",
                           "set_research_cap"):
                # v0.6: team relationships. Donations and WANTED marks
                # work any time (round or winter break); relation
                # proposals / answers / breaks and research caps are
                # winter-break only — the game functions enforce that
                # and return a clear message otherwise.
                if not room["started"] or not career:
                    await send_error(ws, "The career has not started yet.")
                elif not team:
                    await send_error(ws, "Pick a team first (take over an AI team).")
                elif career.get("phase") not in ("round", "winter_break"):
                    await send_error(ws, "You cannot do that right now.")
                else:
                    if mtype == "donate":
                        ok, why = game.donate(career, team["id"],
                                              msg.get("recipient_team_id"),
                                              msg.get("amount"))
                    elif mtype == "propose_relation":
                        ok, why = game.propose_relation(
                            career, team["id"], msg.get("target_team_id"),
                            msg.get("kind"), msg.get("supplier_team_id"),
                            msg.get("years", 2))
                    elif mtype == "respond_relation":
                        ok, why = game.respond_relation(
                            career, team["id"], msg.get("proposal_id"),
                            msg.get("accept"))
                    elif mtype == "break_relation":
                        ok, why = game.break_relation(
                            career, team["id"], msg.get("other_team_id"))
                    elif mtype == "set_wanted":
                        ok, why = game.set_wanted(career, team["id"],
                                                  msg.get("driver_id"),
                                                  msg.get("reward"))
                    elif mtype == "remove_wanted":
                        ok, why = game.remove_wanted(career, team["id"],
                                                     msg.get("driver_id"))
                    elif mtype == "set_research_cap":
                        ok, why = game.set_research_cap(career, team["id"],
                                                        msg.get("cap"))
                    if not ok:
                        await send_error(ws, why)
                    else:
                        await ws.send_json({"type": "action", "action": mtype,
                                            "ok": True, "message": why})
                    await broadcast(room)

            elif mtype in ("research", "develop", "tuning", "reg_research",
                           "reg_adapt", "sign_driver", "sign_tr",
                           "swap_driver", "promote_tr", "release_driver",
                           "advance",
                           "winter_offers", "winter_next"):
                if not room["started"] or not career:
                    await send_error(ws, "The career has not started yet.")
                elif not team:
                    await send_error(ws, "Pick a team first (take over an AI team).")
                elif mtype in ("winter_offers", "winter_next") \
                        and career.get("phase") != "winter_break":
                    await send_error(ws, "That is only for the winter break.")
                elif mtype in ("research", "develop", "tuning", "advance") \
                        and career.get("phase") == "winter_break":
                    await send_error(ws, "Winter break: make your offers, then hit NEXT.")
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
                    elif mtype == "reg_adapt":
                        # v0.4: adapt unadapted parts ($0.25M/pt); no
                        # points = adapt everything still unadapted.
                        ok, why = game.do_reg_adapt(career, team["id"], msg.get("points"))
                    elif mtype == "sign_driver":
                        ok, why = game.sign_driver(career, team["id"],
                                                   msg.get("driver_id"), msg.get("seat"),
                                                   msg.get("contract_years", 2))
                    elif mtype == "sign_tr":
                        # v0.4: sign a Test & Reserve driver (1–4 yrs).
                        ok, why = game.sign_tr_driver(career, team["id"],
                                                      msg.get("driver_id"),
                                                      msg.get("contract_years", 2))
                    elif mtype == "release_driver":
                        # v0.4.3: release (drop) a main or T&R driver —
                        # break fee = main-driver formula, round only.
                        ok, why = game.release_driver(career, team["id"],
                                                      msg.get("driver_id"))
                    elif mtype == "swap_driver":
                        # v0.4: swap a main driver with a T&R driver.
                        ok, why = game.swap_main_tr(career, team["id"],
                                                    msg.get("seat"), msg.get("tr_driver_id"))
                    elif mtype == "promote_tr":
                        # v0.4: promote a T&R driver to an empty seat.
                        ok, why = game.promote_tr_to_main(career, team["id"],
                                                         msg.get("tr_driver_id"),
                                                         msg.get("seat"))
                    elif mtype == "winter_offers":
                        # v0.4: submit this round's offers (max 3).
                        ok, why = game.submit_winter_offers(career, team["id"],
                                                           msg.get("offers") or [])
                    elif mtype == "winter_next":
                        # v0.4: ready for the round — resolves when every
                        # human team is ready (bots are always ready).
                        ok, _advanced, why = game.winter_next(career, team["id"], rng=rng)
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
                    ok, why = game.start_next_season(career, rng=rng)
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
            # v0.6.2: record when the player left — their team becomes
            # takeable by another player once the grace period passes.
            player["disconnected_at"] = time.time()
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
