# FUMBBL Replay Video Creator

A toolchain for turning a [FUMBBL](https://fumbbl.com) Blood Bowl match
into a 3-5 minute whimsical highlight reel with retro pixel-art
visuals, AI-written commentary, TTS narration, chiptune SFX, and stat
cards.

## Current state: pivotal-play analyzer

A CLI that takes a FUMBBL replay reference and prints the plays that
mattered most - with player names, half, and turn number.

```
$ python -m fumbbl_replay 1901135

  #1901135 (2007-10-28, Ranked) Neck Snappers [Orc, Ballcrusher] 1 - 2 Torcy United [Orc, Tathar]
  Winner: Torcy United (by 1)
  ...
  Pivotal plays (6, from replay event log):
     1. [1.00] Skurrrk-Gnash (Neck Snappers) scored a touchdown (turn 8, half 1)
     2. [0.50] Gozzax (Neck Snappers) was seriously injured by Egill Ólison (Torcy United) (turn 1, half 1) - Head Injury (-AV)
     3. [0.50] Jón Hávarrs (Torcy United) was seriously injured by Skurrrk-Gnash (Neck Snappers) (turn 2, half 2) - Serious Injury (NI)
     ...
```

Headlines pick up context tags too: a TD is rendered as "scored the
game-winning touchdown", "scored a tying touchdown", or "scored a
comeback touchdown" when applicable. Casualty headlines name the
inflicter and distinguish blocked / fouled / crowd-pushed.

`<replay-ref>` accepts:
* a bare match id, e.g. `1901135`
* a FUMBBL replay URL, e.g. `https://fumbbl.com/ffblive.jnlp?replay=1901135`
* a path to a local saved `.jnlp` file

### How it gets the event log

There are two FUMBBL data sources we use:

1. `GET /api/match/get/{id}` returns the final scoreboard, casualties,
   coaches, division, and team metadata. No event log, no auth.
2. `GET /api/replay/get/{replayId}/gz` returns the full game log as a
   gzipped JSON. Same per-turn deltas the official FFB Java client
   sees over its websocket on port 22223, but as plain HTTP, so no
   firewalled-port surprises. (The Python `fumbbl_replays` package by
   gsverhoeven uses the same endpoint.)

The replay's `gameLog.commandArray` is a stream of `serverModelSync`
commands. Each command's `modelChangeArray` carries small typed
deltas; we walk them, hold onto sticky state (current half, per-team
turn number), and emit a typed event whenever a counter changes
(`teamResultSetScore` -> TD, `teamResultSetRipSuffered` -> kill,
`teamResultSetSeriousInjurySuffered` -> SI, `teamResultSetBadlyHurtSuffered`
-> BH). The scorer's playerId rides along in the same command on a
companion `playerResultSetTouchdowns`; the casualty victim's
playerId rides on `playerResultSetSeriousInjury`. We resolve those
to player names via the replay's *own* in-game roster (under
`game.teamHome.playerArray` and `game.teamAway.playerArray`) - the
persistent player ids from `/api/team` would not have matched.

### Usage

```bash
pip install -r requirements.txt

# Text report
python -m fumbbl_replay 1901135

# JSON for downstream tooling
python -m fumbbl_replay 1901135 --json

# Save the raw gzipped replay alongside the report
python -m fumbbl_replay 1901135 --dump-replay out/1901135.json.gz

# Render a PNG tableau per pivotal play (spike-quality)
python -m fumbbl_replay 4700842 --tableaux out/tableaux

# Render an animated GIF of each pivotal play's run-up
python -m fumbbl_replay 4700842 --gifs out/gifs

# Add commentary lines per pivotal play (default: local deterministic templates, no LLM)
python -m fumbbl_replay 4700842 --commentary
python -m fumbbl_replay 4700842 --commentary --commentary-backend ollama  # or claude / openai

# Synthesise commentary into per-play audio clips (default: macOS `say`)
python -m fumbbl_replay 4700842 --tts out/audio
python -m fumbbl_replay 4700842 --tts out/audio --tts-voice "Reed"
python -m fumbbl_replay 4700842 --tts out/audio --tts-backend openai --tts-voice nova

# Pull FFB game SFX (cheers, thuds, whistles, crowd boos) per pivotal play
python -m fumbbl_replay 4700842 --sounds out/sfx

# Skip the FUMBBL position sprite fetch (use plain coloured tokens instead)
python -m fumbbl_replay 4700842 --tableaux out/tableaux --no-sprites

# Skip the replay step, use just summary totals (no player names, no turn)
python -m fumbbl_replay 1901135 --no-replay

# kills report
python -m fumbbl_replay 1901135 --kill
```

A self-contained project showcase (with embedded sample tableaux and
a sample animated drive) lives at [`docs/overview.html`](docs/overview.html);
regenerate with `python -m scripts.build_overview`.

### Module layout

```
fumbbl_replay/
  __main__.py     - CLI
  jnlp_loader.py  - resolve URL / .jnlp file / bare id -> match id
  fumbbl_api.py   - HTTP client: /api/match, /api/team, /api/replay/.../gz
  events.py       - parse gameLog.commandArray -> typed event timeline + in-game roster
  analyzer.py     - score pivotal plays; fall back to summary totals if no events
  field_state.py  - reconstruct player + ball positions at any commandNr
  tableau.py      - render a single pivotal play to PNG (uses sprites when available)
  animate.py      - render an animated GIF of a play's run-up
  sprites.py      - fetch + crop FUMBBL position icon sheets per player (cached on disk)
  commentary.py   - generate one whimsical commentary line per pivotal play (template / Ollama / Claude / OpenAI)
  commentary_templates.py - local, deterministic template pools per play kind/tags (default backend)
  dice.py         - extract + render block / armor / injury dice; fetches FFB's PNGs
  pitches.py     - weather-themed pitch backgrounds from FFB's Default pitch set
  tts.py          - synthesise commentary lines to audio (macOS say / pyttsx3 / OpenAI)
  sounds.py       - per-play FFB game SFX (td.ogg / specCheer / injury / specHurt / ...)
scripts/
  build_overview.py - regenerate docs/overview.html
```

## Roadmap

| Stage                                                                   | Status |
|-------------------------------------------------------------------------|--------|
| Resolve replay reference (URL / file / id) to a match id                | done   |
| Fetch the gzipped replay over HTTP                                       | done   |
| Parse server commands into a typed event timeline                       | done   |
| Pivotal plays with player names + half + turn                            | done   |
| Context-aware scoring (game-winning, tying, comeback, foul tags)        | done   |
| Casualty inflicter + reason (blocked / fouled / crowd-pushed)            | done   |
| Match summary fallback analyzer                                          | done   |
| Pixel-art tableau spike (pitch + tokens at saved coords)                 | done   |
| Animated GIFs of pivotal plays                                            | done   |
| FUMBBL position sprites in tableaux                                       | done   |
| Epic-fail events (self-kill, triple/double skull streaks, clutch fumble)  | done   |
| LLM commentary script (one line per pivotal play, Claude API)             | done   |
| TTS narration                                                             | done   |
| ffmpeg compose final mp4                                                 | done   |
| Discord bot + YouTube uploader (`services/`)                              | done   |

## Discord bot + YouTube uploader

Two long-running services live alongside the `fumbbl_replay` library:

- **`services.bot`** (one process) — listens on Discord, exposes
  `/generate-highlight <match-ref>` plus admin slash commands. Validates,
  dedup-checks, rate-limits and writes a job file to `jobs/queue/`.
- **`services.worker`** (one process) — drains `jobs/queue/`, runs
  `fumbbl_replay.main()` to produce the MP4, uploads to YouTube, records
  the result in SQLite, cleans up the intermediates, and writes a
  `jobs/done/` marker the bot picks up.

The shared SQLite at `data/app.sqlite3` carries:
- `bot_defaults` — the operator's encrypted YouTube refresh token
- `guild_config` — per-server YouTube overrides (also encrypted)
- `processed_replays` — dedup table so a re-request returns the cached
  YouTube link instantly
- `rate_log` — append-only per-guild invocation log for the
  3-per-10-minute rate limit

YouTube refresh tokens are encrypted at rest with Fernet
(`cryptography` + `FERNET_MASTER_KEY` env). Per-guild OAuth is wired
through a small aiohttp callback that the bot exposes on port `38080`
of the host — admins run `/highlight-config set-youtube`, the bot
replies with a one-time Google auth URL, the admin authorizes in their
browser, and the bot's callback handler encrypts + stores the refresh
token under that guild.

### First-time bring-up

Full step-by-step from a fresh clone to your first `/generate-highlight`
is in [**`docs/bring-up-checklist.md`**](docs/bring-up-checklist.md).
For just the Google side (creating an OAuth client and downloading
`client_secret.json`), see
[**`docs/google-oauth-setup.md`**](docs/google-oauth-setup.md).

The condensed version:

```bash
# 1. Get the Google OAuth client (see docs/google-oauth-setup.md)
mkdir -p data && mv ~/Downloads/client_secret_*.json data/client_secret.json

# 2. Configure
cp deploy/.env.example deploy/.env
# Generate a Fernet master key (stdlib, no project deps required):
python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
# Paste into FERNET_MASTER_KEY in deploy/.env, plus DISCORD_BOT_TOKEN
# and DISCORD_APPLICATION_ID from your Discord developer-portal app.

# 3. Launch
cd deploy && docker compose up --build -d
docker compose logs -f bot   # confirm "logged in as ..."

# 4. Mint the default YouTube refresh token (one-time)
docker compose exec worker python -m services.worker.youtube_upload --bootstrap-default

# 5. Invite the bot to your Discord server with applications.commands + bot
#    scopes, then try /generate-highlight 4700552 in any channel.
```

### Local dev (without docker)

```bash
pip install -r requirements.txt -r requirements-services.txt
cp deploy/.env.example .env && $EDITOR .env

# terminal 1
python -m services.bot

# terminal 2
python -m services.worker
```

`services.bot` and `services.worker` share state through `./data/`,
`./jobs/`, and `./out/` by default (override via env vars).

### Failure handling

- **Render fails** → `jobs/done/<uuid>.json` carries `status: "error",
  phase: "render"`. Bot posts the error tail to the invocation channel.
  The `out/<job_id>/` directory is kept on disk for forensics.
- **Upload fails** → same shape; the MP4 stays at
  `out/<job_id>/highlight.mp4` for manual retry. No auto-retry in v1.
- **Bot dies mid-job** → systemd / docker restarts in 5s. The worker
  keeps running; when the bot comes back it scans `jobs/done/` for
  fresh entries and delivers any orphans via `channel.send`.
- **Worker dies mid-job** → on restart, any `jobs/in-progress/*.json`
  older than 30 min is moved to `done/` as `status: "error", phase:
  "worker_crash"`.

## License

See `LICENSE`.
