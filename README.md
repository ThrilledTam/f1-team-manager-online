# F1 Team Manager Online — v0.3.1 (beta)

A clean online rebuild of Matthew's PyCharm F1 team-manager simulator, built
for playing together at the same time (not pass-and-play). v0.3.1 is a beta:
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
   History. Every action confirms with a toast (e.g. "Tuning set to +2 ✓")
   and the updated value shows immediately; a failed action shows an error
   toast instead of failing silently. Hit **Advance to Race Weekend** when
   you are done. The dashboard labels who is ready and who it is waiting
   for ("Waiting for: …" / "Ready: …"). When every connected human has
   advanced, the AI teams act and the server runs qualifying then the race.
5. **Results pages (v0.1):** after a race weekend runs, everyone sees a full
   **Qualifying Results** page first (POS, driver with team-coloured `|`
   bar, CAR, GAP from pole — never absolute times), then a unanimous **NEXT**
   vote (all connected humans must vote; the host has a skip button only
   they can see) takes everyone to the full **Race Results** page (same
   style, plus G/L vs qualifying and 🥇🥈🥉 medals). A second NEXT vote
   returns everyone to their dashboards for the next round. Votes are stored
   by player, so reconnecting mid-vote keeps your vote; if a voter
   disconnects they drop out of the vote and the rest can finish it. Any
   previous Grand Prix can be reopened from the dashboard's Race History
   with its full qualifying + race tables.
6. **Seasons:** 12 rounds per season, up to 30 seasons. At season end:
   champion, prize money, retirements, driver ageing, transfers, and a
   winter break where humans fill any empty seats before the next season.
   Each round is a named Grand Prix from the 12-round calendar (see below);
   the circuit name shows on the dashboard header, both results pages, and
   in Race History.
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
  `1.95 − 0.025*pos` per driver, rep updates. **v0.1 race:** the race itself
  is a real full-distance sum — see "Race time (v0.1)" below.
- **2026 circuit calendar (v0.1, `f1/data.py` CALENDAR):** Bahrain 57,
  Jeddah 50, Melbourne 58, Suzuka 53, Shanghai 56, Miami 57, Imola 63,
  Monaco 78, Barcelona 66, Montreal 70, Spielberg 71, Silverstone 52.
  The same 12 Grands Prix repeat every season; the circuit and lap count
  are stored per round in saves and in race history.
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
- **Race time (v0.1 — changed with Matthew's approval):** `Race.py`'s
  80-lap loop overwrote the race time every lap, so v0 races totalled
  ~137s. v0.1 sums a real Grand Prix: each driver runs the circuit's lap
  count, every lap is `95 − (driver_perf/20 + team_perf/80) ± tuning
  (0.1s/lap per point, helps Driver 1 / hurts Driver 2) + N(0, 0.12)`,
  plus a one-off 0.5s per qualifying position. A 57-lap race totals
  ~5,000s (80+ min), P22 finishes ~80s behind, qualifying stays a single
  `lapSim` draw per driver (gaps ~0–3s). The per-lap perf spread is
  deliberately compressed (driver/20 + team/80) so a full race does not
  produce gaps of hundreds of seconds. Absolute times are kept server-side
  only for ordering — results payloads and history carry gaps only
  (`LEADER` / `+x.xxxs`), never times. Qualifying keeps the original
  `lapSim` maths.
- **Session form (v0.2 — randomness):** v0.1 was too predictable — the
  per-lap noise (σ 0.12) averages out over 57 laps while the perf spread
  is ~1.8s/lap, so the best driver+car combo won every race. v0.2 adds
  one random pace offset per driver per session, drawn once (not per
  lap): qualifying gets +N(0, 0.35s) on the single lap; the race gets
  +N(0, 0.22s) per lap, the same offset every lap (total σ ~12.5s over
  57 laps), drawn fresh for the race. Measured over 120 seeded races
  with the 2026 grid: the best combo (Antonelli/Mercedes) wins 45%
  (54/120) instead of ~100%, with 5 different winners (Russell 42,
  Leclerc 11, Hamilton 7, Verstappen 6) and 9 different poles
  (Antonelli 38%); qual P22 gap median 3.0s, race P22 gap median ~96s,
  winner ~5,010s. No crashes/DNFs or weather — only lap-time randomness.
- **Driver-name colour (v0.1):** surnames are bold, UPPERCASED, and coloured
  by team everywhere a driver name appears (results, standings, market,
  lineup, history, rumours), with a coloured `|` bar in classification
  tables and 🏆 + title count for champions; team names are coloured, with
  the supplier suffix in the supplier's colour (e.g. McLaren-Mercedes).
  Free agents / F2 drivers are bold + uppercase with no team colour.
- **Save format v2 (v0.1):** saves now store the circuit per round, the
  results stage/vote, and full qual+race tables per Grand Prix in
  `race_history`. v0 saves (version 1) still load: the loader fills in the
  circuit for the saved round, marks old history entries as having no full
  tables, and strips any stored absolute times.
- **Save format v3 (v0.2):** saves now also store `prev_driver_order`
  (last season's driver finishing order) and each team's `points_kept`
  (constructor points banked from departed drivers). v0/v0.1 saves still
  load: the loader fills in both fields with safe defaults.
- **Save format v4 (v0.2.1):** saves now also store each driver's
  `team_points` (points scored for their current team) and
  `last_team_id` (last team raced for). v0/v0.1/v0.2 saves still load:
  the loader fills in both fields with safe defaults (`team_points`
  from championship points for current F1 drivers, 0 otherwise).
- **Save format v5 (v0.3):** saves now also store the champion
  history (pre-filled real 2010–2025 table, plus one row per
  in-game season completed) and `team_id` on race-history top-3
  entries. Older saves still load: the loader adds the 2010–2025
  table for any year it does not already have, keeping any in-game
  champion rows.
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

## Out of scope for v0.1 (not built)

Driver/team affiliates, Power Unit menu, contract extensions beyond the
basic Driver Market signing above, 2025 season, 2027+ seasons. **No live
animated race view / lap-by-lap progression:** results still resolve on
Advance, just as proper full-distance results on separate pages — that was
deliberately left out of v0.1.

## v0.1.1 — WebSocket connect race (live blocker, fixed)

Live v0.1 served pages fine but Create Room / Join Room always failed in
a real browser with "Not connected yet — try again in a second." Root
cause: the home screen called `connect()` and then sent `create_room`
after a fixed 300ms timeout; whenever the WebSocket handshake took
longer than 300ms (common on Render's free tier, especially after a
cold start) `send()` saw `readyState === 0` and errored, and every
retry created a new orphaned socket and repeated the same race. The
server even logged "connection open" for each attempt — the handshake
did complete, just too late — and a Render restart did not help. Fix:
messages now queue while the socket is connecting and flush on `open`
(no fixed send timeouts anywhere); the socket reconnects with backoff
(0.5s → 4s cap) even before the player has a room; only one socket is
ever live (re-connecting closes the old one, and there is a single ping
interval); the home screen shows the connection status ("Connecting…"
→ "Connected ✓" / "Connection lost — retrying…", with a "still
connecting — the free server can take ~50s to wake up" note after 10s);
the page connects on load so the handshake is usually done before the
player clicks; and a double-click on Create/Join cannot create two
rooms. Also fixed the premature Advance toast: it said "Race weekend
complete" before the race results page had been shown — it now says
"Qualifying complete — results are in."

## v0.2 — session randomness, replacement rules, standings carry-over

- **Randomness (the big one):** per-driver, per-session form — see
  "Session form (v0.2)" above. The fastest combo still wins most often
  (Antonelli/Mercedes 45% of 120 seeded races) but not every time.
- **Mid-season replacement (revised):** the replaced driver KEEPS their
  points and stays in the driver standings, like real F1. The
  replacement driver begins at 0 points (points, wins, best finish all
  reset on signing — this matters for a free agent with points from an
  earlier stint). Constructor points stay with the team: when a driver
  leaves, the points they scored for that team are banked on the team
  (`points_kept`) and still count toward its constructor total after
  the next race. Note the consequence of the two rules together: a
  driver who leaves one team with points and is signed by another team
  mid-season restarts at 0 for their new team — Matthew's stated rule
  for incoming drivers, even though real F1 would let them keep their
  points across the switch.
- **Standings carry into the next season:** at season end the final
  driver order is recorded (`prev_driver_order`) and used as the
  tie-breaker in `driver_standings()` — so the new season opens in last
  season's order with everyone on 0 points, and as the season
  progresses points sort first and the old order only breaks ties.
  Drivers with no previous order (promoted F2 drivers, new signings)
  go after the carried-over drivers, ordered by best finish, then
  driver perf, then name. Driver standings only — constructor
  standings are unchanged (points, then best finish).
- **Nickname placeholder:** the home nickname input now says
  "e.g. [name]" instead of "e.g. Matthew".
- **Save format v3:** see above — `prev_driver_order` and per-team
  `points_kept`, with safe defaults when loading v0/v0.1 saves.

## v0.3.1 — standings '|' bar for free agents

Small fix found in a live real-browser playtest of v0.3 (2026-11-10):
a replaced driver who keeps points as a free agent (e.g. Piastri after
being replaced at McLaren) showed in the driver standings with their old
team in the team column but WITHOUT the coloured '|' bar every other row
had. `driverNameHtml` now takes a separate bar colour, and the standings
renderer passes the standing team's colour (current team, or last team
for a free agent) as the bar colour while the surname stays uncoloured
for free agents, per the v0.1 rule. Constructors' standings already had
the bar on every row — no change there.

Also in v0.3.1, every pop-up can be minimized: the full-GP view, the
contract picker, confirm dialogs (delete save, skip vote) and the home
settings panel all have a minimize ("—") and a close ("×") button.
Minimizing collapses the pop-up to a floating chip at the bottom-left
showing its title; clicking the chip restores it exactly as it was,
and the dashboard underneath stays usable while a pop-up is minimized.
Escape closes the topmost open pop-up, and clicking the backdrop
outside a modal pop-up dismisses it too.

## v0.3 — hidden reputation, compact market, contract choice, free signing, new break cost, 2025 order, history grid + champions

Matthew's "iron out a few things before I forget them" round (2026-11-10):

- **Reputation hidden from players:** `rep` is only for the bot teams
  to judge who's good. It is stripped from every payload sent to the
  browser (`public_state` and `my_team_public`, chart on or off) and
  from every UI surface (Driver Market column, My Team line, custom
  team picker). It stays in the server-side data and the AI still
  reads it (`ai_visible_score`, `ai_best_candidate`) exactly as
  before.
- **Compact Driver Market:** dense rows (13px, 4px padding, both seat
  buttons side by side, 40px tap targets) so 55+ drivers scroll in
  about half the space. The Rep column is gone; Salary/yr replaces MV.
- **Contract length on every signing:** player or bot, F2 / free
  agency / F1 — the signing manager picks 1–4 years. The player
  picks in a confirm step after tapping a seat (1/2/3/4 buttons);
  bots pick by `ai_contract_years()` — rep 4+ gets 4 years, rep 3+
  gets 2–4, rep 2+ gets 1–3, below that 1–2. The server validates
  1–4 and stores the choice as the new driver's `contract_left`.
  (Custom-team creation still gives 1-year contracts, per the
  original game's rule.)
- **No upfront signing cost:** a driver's MV is their salary per
  season, paid salary/12 directly after each race (the existing
  per-race deduction, verified: `salary_per_round()` =
  sum(salaries)/12). `sign_driver()` no longer charges a signing
  fee at all.
- **New break-cost formula** (replaces 1 + salary × contract_left):
  `rounds_remaining = (12 - round + 1) + max(0, contract_years - 1) × 12`
  (in winter break: `contract_years × 12`), and
  `break_cost = rounds_remaining × salary/12 + $2.0M`. Matthew's
  example passes as a test: Gasly, contract 3, start of round 9,
  salary $4.0M → 28 × $4.0M/12 + $2.0M = $11.33M. `break_cost()` is
  the single source of truth that `sign_driver()` charges. The
  market shows "Seat N — Free" for an empty seat and the break fee
  only for an occupied seat ("Break $X.XXM" underneath); unaffordable
  seats stay disabled with "Need $X.XXM"; the sign toast states the
  break fee charged (or Free).
- **Standings open in last season's order:** season 1 (2026) uses
  the real 2025 final standings — drivers Norris, Verstappen,
  Piastri, Russell, Leclerc, Hamilton, Antonelli, Albon, Sainz,
  Alonso, Hülkenberg, Hadjar, Bearman, Lawson, Ocon, Stroll, Gasly,
  Bortoleto, Colapinto, then Pérez, Bottas, Lindblad (did not race
  in F1 in 2025, ordered by perf); constructors McLaren, Mercedes,
  Red Bull, Ferrari, Williams, Racing Bulls, Aston Martin, Haas,
  Audi (Sauber's slot), Alpine, Cadillac (new, last). Stored as
  `PREV_SEASON_DRIVER_ORDER` / `PREV_SEASON_TEAM_ORDER` in
  `f1/data.py` and applied when a new career is created (all on 0
  points). Later seasons keep the v0.2 carry-over (`prev_driver_order`
  from the season just finished). Standings rows now carry the
  coloured '|' bar, like qualifying/race results (drivers and
  constructors).
- **Race history grid:** the History & Rumours tab now mirrors
  Matthew's PyCharm grid — rows = seasons (2026 first, a new row
  appears as each season starts), columns = R1..R12, each cell the
  top 3 finishers' initials (L.HAM), each coloured by the team they
  raced for at the time. Unplayed rounds show a dash. Clicking a
  cell opens the full Grand Prix (qualifying + race tables).
- **Champion history table:** YEAR, DRIVER, AGE (at season start),
  CAR, PTS, TROPHIES (🏆 xN, cumulative career championships at
  that year), WINNING TEAM (constructors champion). Pre-filled with
  the real 2010–2025 table (Hamilton's counts include his 2008
  title, so 2014 is x2; 2021 and 2024 winning teams differ from the
  drivers' champion's team, intentionally). In-game seasons append
  a row as they complete, from the actual sim results. 2026 is NOT
  pre-filled — it has not been played yet online.
- **Save format v5:** old saves gain the 2010–2025 champion table
  on load (in-game champion rows are kept), and race-history top-3
  entries gain `team_id`.

## v0.2.1 — points follow the driver, last team shown, cost upfront

A correction patch to v0.2's replacement rules (Matthew's clarification):

- **Points follow the driver across teams (Flag 1, refined):** v0.2
  reset every incoming driver's tally to 0, which was wrong for a
  driver moving between teams. Now a free agent who already has F1
  points from an earlier stint this season KEEPS them when signed by
  a new team — e.g. a driver with 44 points at Alpine who moves to
  Audi mid-season keeps the 44 in the driver standings. Only an F2
  driver starts at 0 (their F2 points must not carry into F1).
- **Points scored stay with the team:** three separate numbers are
  now tracked — `driver["points"]` (championship total, follows the
  driver), `driver["team_points"]` (points scored for the CURRENT
  team, reset to 0 whenever the driver joins a new team), and
  `team["points_kept"]` (points banked from departed drivers). A
  constructor total is `points_kept + current drivers' team_points`,
  so the 44 Alpine points stay at Alpine, and only points the driver
  scores from now on for Audi count toward Audi. When a driver
  leaves, only their `team_points` are banked — never their
  championship total. `apply_race_results()` awards both tallies,
  `_recompute_team_points()` uses `team_points`, and the Driver 1 /
  Driver 2 order inside a team is now sorted by `team_points` too, so
  a newcomer does not become Driver 1 on points scored elsewhere.
- **Last team shown in the standings (Flag 2):** a replaced driver
  now records `last_team_id` when they leave a team (replacement,
  contract end, retirement), and the standings show that team in the
  team column — in team colours — even though they no longer race for
  them. Their name stays uncoloured (free-agent rule from v0.1).
- **Replacement cost shown upfront:** the Driver Market now shows the
  TOTAL cost of signing each driver into each seat — signing fee
  (market value) + contract-break fee (1 + salary × years left) when
  the seat is occupied — with the breakdown underneath. The cost is
  computed server-side by `signing_cost()`, the same helper
  `sign_driver()` charges with, and sent to the client as
  `market_costs`, so the displayed number is always the number
  charged. The sign confirmation toast also states the amount
  charged. Seats the team cannot afford are disabled and show the
  amount needed instead of erroring only after the click.
- **Save format v4:** see above — `team_points` and `last_team_id`
  on drivers, with safe defaults when loading v0/v0.1/v0.2 saves
  (old saves take `team_points` from championship points for current
  F1 drivers, 0 otherwise).

## Mobile + mouse

The UI works the same with a mouse click or a mobile tap: every action is a
real `<button>` (or select/input) with a standard click handler — no
hover-only controls, no `mouseover`/`mousedown`-only logic, no touch-only
handlers, no keyboard-only input, and `touch-action: manipulation` so taps
do not double-fire. Layout is responsive: viewport meta tag, a 600px
breakpoint with full-width action buttons, 16px input text (so iOS does not
auto-zoom), tables in horizontal-scroll containers, and all buttons/inputs
at least 44px tall.

**Mobile checks run for v0.1:** `tests/test_f1.py::test_mobile_friendly_markup`
asserts the viewport meta, 44px tap targets, responsive breakpoint,
scrollable tables, and no hover/mouse/touch-only handlers. In addition, a
headless-Chromium check at 360px wide (see below) loaded the home screen,
tapped Create Room with a real touchscreen tap, opened the lobby, started a
new career, picked a team, started the season and advanced to a race, and
measured `scrollWidth` vs viewport width plus the rendered height of every
visible button on the home, lobby, dashboard, standings and race-results
screens — no horizontal overflow, all measured tap targets ≥ 44px.
For v0.1 the same check (`/tmp/mobile_check_v01.py`, two real players)
drives the whole results flow: qualifying page → unanimous NEXT vote →
race page → NEXT vote → dashboard round 2 → reopening the Bahrain GP from
Race History, checking overflow and tap targets on each screen.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

`tests/test_f1.py` covers: new 2026 career (11 teams, correct drivers),
simultaneous advance (race waits for all humans, then the qualifying
results page appears and the round only advances after the NEXT votes),
reg-change properties (research helps but cannot cancel, floor holds,
major > minor), save/load round-trip (v0.1 format + old v0 saves still
load), chart-off hiding perf and results-based AI, lobby kick / host
promotion / mid-career takeover over a real WebSocket, finances/research/
develop/tuning/retirement formulas, a full 12-round season rollover, the
mobile markup checks, and v0.1: results payloads never contain absolute
times, full-distance race totals (~5,000s) with plausible gaps, the NEXT
vote flow (unanimous, host override host-only, disconnect exclusion, vote
persistence), reopening a previous GP from history with full tables,
action confirmations (e.g. "Tuning set to +2 ✓"), the 12-round calendar,
and driver-surname markup (bold/uppercase/team-coloured, free agents
neutral) rendered in headless Chromium, and v0.1.1: Create Room succeeds
even when the WebSocket handshake takes 1.5s (queued while connecting,
exactly one room per player, double-click safe), plus the corrected
Advance toast wording, and v0.2: winner/pole diversity over 120 seeded
races (best combo wins ~45%, not every race), mid-season replacement
(outgoing keeps points and stays in the standings, incoming starts at 0,
constructor total keeps the banked points), standings carry-over into
the next season (exact order before round 1, points sort takes over),
old saves still loading with the new fields defaulted, and the nickname
placeholder reading exactly "e.g. [name]"; and v0.2.1: a driver moving
teams keeps their points while the points stay with the old team, an
F2 signing starts at 0, the market shows the exact cost sign_driver()
charges, and the replaced driver's old team shows in the standings
(verified in a real browser); and v0.3: the Gasly $11.33M break-cost
example, free signing into an empty seat, contract length 1–4
validated and stored, rep absent from every client payload while the
AI still uses it, season 1 standings in the real 2025 order, driver
initials, champion history pre-filled 2010–2025 and appended on
season end, save format v5 migration, and the market/history/standings
smoke check in headless Chromium (compact market rows, break-cost /
Free display, no rep, standings '|' bar, clickable history grid,
champions table).

## Known limits

- Rooms and saves live in server memory / `saves/` on the server: a Render
  free restart or sleep wipes them. Export your save to keep it.
- Free-tier Render sleeps after inactivity: the first visit can take
  ~50 seconds to wake up.
- One career per room; a room holds up to 10 human players.
- The F2 championship runs in parallel (points + rep), but has no separate UI.
