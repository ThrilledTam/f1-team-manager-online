# F1 Team Manager Online — v0 (beta)

A clean online rebuild of Matthew's PyCharm F1 team-manager simulator, built
for playing together at the same time (not pass-and-play). v0 is a beta:
expect lots of tweaking before a playable official game.

**Source of truth:** `Main.py` in the original `Project_Manager` repo is the
current game. `MainMod.py` was a middle refactor attempt and is **ignored**.
The original repo is untouched — this is a separate clean rebuild that takes
the ideas and formulas from `Main.py` / `Race.py` / `Rules.py` /
`YearsData.py` / `Team.py` / `Driver.py` / `ContractOffer.py` and rebuilds
them properly. It does not copy the messy globals or `input()` code.

**Stack:** Python 3.12, FastAPI + WebSockets, single-page frontend in
`static/index.html` — the same pattern as the Party Game Hub. All game logic
is server-side, rooms live in memory, no accounts, no paid services.

## Run locally

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000

## Deploy on Render (free)

The repo includes `render.yaml` for a free Web Service:

- Build: `pip install -r requirements.txt`
- Start: `uvicorn server:app --host 0.0.0.0 --port $PORT`
- Health check: `/api/health`

## How to play

1. **Home:** Create Room, Join Room (6-letter code), Settings, Exit. Exit
   just returns to the home screen with a note — a browser tab cannot be
   force-closed from inside the page, so close the tab when you are done.
2. **Lobby:** player list, host crown, room code + copyable join link.
   The host can kick players (lobby only). If the host disconnects, the
   longest-connected remaining player becomes host. Rooms are held for 120s
   when empty; reconnect with the same player (stored in the browser) to
   keep your team.
3. **Career setup (host):** Start New Career (2026 only — 2027/2028 are
   shown as coming later, disabled) or Continue a Saved Career from the
   saves list. Each human picks an existing team or, for new careers only,
   creates a custom team (name, colour, 2 drivers from the free-agent / F2
   pool). Remaining teams are AI. 1–10 humans, 11-team base grid.
   Joining a career that already started means taking over an AI team.
4. **Race rounds (simultaneous turns):** every human acts at once on their
   own dashboard — Finances, Research, Develop Parts, Tuning, Reg Research,
   Driver Market, Lineup, Standings, Retirement Rumours, Race/Champion
   History. Hit **Advance to Race Weekend** when you are done. When every
   connected human has advanced, the AI teams act and the server runs
   qualifying then the race, and shows who has / has not advanced yet.
5. **Seasons:** 12 rounds per season, up to 30 seasons. At season end:
   champion, prize money, retirements, driver ageing, transfers, and a
   winter break where humans fill any empty seats before the next season.
6. **Regulation research:** a minor reg change every year, a major one every
   4 years (2030, 2034, …). The dashboard always shows the next reg-change
   year and whether it is Minor or Major. Reg Research is a separate pool
   from normal research — invest during the season and it softens the
   change at rollover (research can recover at most 70% of the loss).
7. **Performance chart toggle (host setting):** when ON, a bar chart and perf
   numbers are shown. When OFF, **no** perf numbers reach any human client
   and AI/bot decisions are based on results/standings only.

## Saves

- In-game (host): Save Career, list / load / delete saves.
- `GET /api/export/{save_id}` downloads the save JSON;
  `POST /api/import` uploads one. The save document is plain JSON
  (`version`, `save_id`, `name`, `host`, `year`, `round`, `teams`,
  `drivers`, `race_history`, `champion_history`, `settings`, …) — the exact
  same shape could later be stored in Firebase Firestore or Supabase
  without changing it. **No Firebase, Supabase or other database is set up
  in v0, and no account is needed.**
- ⚠️ **Render free wipes the server filesystem (and in-memory rooms) on
  restart/sleep.** Saves in `saves/` are best-effort. Export/Import is the
  durable backup: export a save before you stop playing, import it to
  continue later.

## Formulas preserved from the original

- 2026 grid only: McLaren, Mercedes, Red Bull, Ferrari, Williams, Racing
  Bulls, Aston Martin, Haas, Audi, Alpine, Cadillac — drivers, money, perf
  (110–210), reliability (0.89–0.955), suppliers, salaries/ages from
  `YearsData.py` 2026. No 2025 season.
- Points: top 10, `[25, 18, 15, 12, 10, 8, 6, 4, 2, 1]`.
- Race sim (`Race.py`): `lapSim` baseline 75.000, qual then race, supplier
  perf blending `(2*own + supplier)/3`, tuning + for driver 1 / − for
  driver 2 in qual (×4 in the race), race prize money
  `1.95 − 0.025*pos` per driver, rep updates.
- Finances (`Main.py`): post-season injection `9.0 − 0.50*pos` (+1.50 if
  1st); per-race gains `(1.75−0.025*(pos*2)) + (1.75−0.025*((pos*2)−1))`;
  per-race driver cost `salary / 12`.
- Research: Performance invest adds PerfRes and `−invest/3` ReliRes;
  Reliability invest adds ReliRes and `−invest/5` PerfRes. Develop Parts
  costs $1M and converts banked research via `Team.py`'s diminishing
  `addPerf` / `addRel` formulas.
- Retirement: `Main.py isRetiring()` (age, titles, points share, hard cap 49).
- Driver ageing: `Rules.py DSUS()`.
- Reg change: `Rules.py regChange()` — base loss, `12*log1p(research)`
  capped at 70% of target, catch-up bonus, chaos ±4, floor at 72% of grid avg.
- AI: `Main.py`'s AI spending/tuning logic, and `ContractOffer`'s
  results-based candidate scoring (points, best finish, rep, titles) —
  never team perf.

## Decisions made where Main.py was ambiguous

- **Reg targets:** the original reg-change call was commented out with a
  TODO and a target formula that would have wiped the grid. v0 uses
  Minor target 12 / Major target 35.
- **Reg change timing:** the 2026 grid already reflects the 2026 regs, so
  the first change applies at the rollover into 2027 (Minor), then 2028/2029
  Minor, 2030 Major, and so on.
- **Finances applied, not just displayed:** in `Main.py`, EPSCI/EPRG were
  only shown as estimates. In v0, EPRG is paid after each race and EPSCI at
  season end, so the Finances screen is real.
- **retMinAge stays 42:** `Main.py`'s decrements were `==` comparisons
  (no-ops), so the retirement age never actually dropped.
- **Custom teams are appended** to the grid (as in `Main.py`), and a custom
  team's reliability is 0.90 — the original passed `rel=6`, which is a bug.
- **Custom-team perf** follows `Main.py`: `(best + 5 × worst) / 6`.
- **Race time:** `Race.py`'s 80-lap loop overwrote the race time every lap,
  so the effective result is one `lapSim` draw, then `(time + qualPos) * 3`.
  v0 keeps that exact behaviour rather than inventing a new race model.
- **Reliability** is a developed/displayed stat; the original race sim never
  used it for DNFs, so v0 does not either.
- **Disconnected humans:** their team is AI-driven for that round so the
  race is never blocked; reconnecting gives the team back.
- **Season points reset** at rollover (the original did this at lines
  ~1685–1693 of `Main.py`); F2 champion is promoted to the free-agent pool
  and the F2 grid refills from queued/pool rookies.
- **Signing a driver** costs their market value, plus a contract-break fee
  of `1 + salary × years left` if the seat is occupied (the original's
  winter-break break cost). New contract: 2 years.
- **AI is always results-based** (points/position/points-share/rep/titles),
  in both chart modes — that guarantees the chart-off requirement.
- **Winter break is interactive:** humans must fill empty seats (retirements
  / expired contracts) before the next season; AI teams fill theirs
  automatically. The career ends after 30 seasons.

## Out of scope for v0 (not built)

Driver/team affiliates, Power Unit menu, contract extensions beyond the
basic Driver Market signing above, 2025 season, 2027+ seasons.

## Mobile + mouse

The UI works the same with a mouse click or a mobile tap: every action is a
real `<button>` (or select/input) with a standard click handler — no
hover-only controls, no `mouseover`/`mousedown`-only logic, no touch-only
handlers, no keyboard-only input, and `touch-action: manipulation` so taps
do not double-fire. Layout is responsive: viewport meta tag, a 600px
breakpoint with full-width action buttons, 16px input text (so iOS does not
auto-zoom), tables in horizontal-scroll containers, and all buttons/inputs
at least 44px tall.

**Mobile checks run for v0:** `tests/test_f1.py::test_mobile_friendly_markup`
asserts the viewport meta, 44px tap targets, responsive breakpoint,
scrollable tables, and no hover/mouse/touch-only handlers. In addition, a
headless-Chromium check at 360px wide (see below) loaded the home screen,
tapped Create Room with a real touchscreen tap, opened the lobby, started a
new career, picked a team, started the season and advanced to a race, and
measured `scrollWidth` vs viewport width plus the rendered height of every
visible button on the home, lobby, dashboard, standings and race-results
screens — no horizontal overflow, all measured tap targets ≥ 44px.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

`tests/test_f1.py` covers: new 2026 career (11 teams, correct drivers),
simultaneous advance (race waits for all humans, then qual+race run and
points change), reg-change properties (research helps but cannot cancel,
floor holds, major > minor), save/load round-trip, chart-off hiding perf
and results-based AI, lobby kick / host promotion / mid-career takeover
over a real WebSocket, finances/research/develop/tuning/retirement
formulas, a full 12-round season rollover, and the mobile markup checks.

## Known limits

- Rooms and saves live in server memory / `saves/` on the server: a Render
  free restart or sleep wipes them. Export your save to keep it.
- Free-tier Render sleeps after inactivity: the first visit can take
  ~50 seconds to wake up.
- One career per room; a room holds up to 10 human players.
- The F2 championship runs in parallel (points + rep), but has no separate UI.
