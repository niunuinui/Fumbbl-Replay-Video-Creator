"""CLI entry point.

Usage:

    python -m fumbbl_replay <replay-ref> [--json] [--dump-replay PATH] [--no-replay] [--no-rosters]

`<replay-ref>` is any of:
    * a FUMBBL replay URL, e.g. https://fumbbl.com/ffblive.jnlp?replay=1901135
    * a local path to a saved .jnlp file
    * a bare match id, e.g. 1901135

The default pipeline pulls the match summary, fetches the gzipped
replay event log over HTTP, identifies pivotal plays (TDs, kills,
injuries) with their player names and turn numbers, and prints a
text report. `--json` switches to structured JSON for downstream
tooling. `--no-replay` skips the event-log step and reports
summary-level totals only.
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import json
import logging
import sys
from pathlib import Path

from . import analyzer, events, field_state, fumbbl_api, jnlp_loader, sprites


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fumbbl-replay")
    parser.add_argument("replay_ref", help="FUMBBL replay URL, .jnlp path, or numeric match id")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a text report")
    parser.add_argument("--no-replay", action="store_true",
                        help="Skip the replay event log, use summary totals only")
    parser.add_argument("--no-rosters", action="store_true",
                        help="Skip the per-team roster fetch (omits player names from headlines)")
    parser.add_argument("--dump-replay", type=Path, default=None,
                        help="Save the raw replay JSON (gzipped) to this path")
    parser.add_argument("--tableaux", type=Path, default=None,
                        help="Render a PNG tableau per pivotal play into this directory")
    parser.add_argument("--gifs", type=Path, default=None,
                        help="Render an animated GIF per pivotal play into this directory")
    parser.add_argument("--no-sprites", action="store_true",
                        help="Skip the FUMBBL position sprite fetch; render plain coloured tokens")
    parser.add_argument("--orientation", choices=("vertical", "horizontal"), default="vertical",
                        help="Pitch orientation in tableaux/GIFs (default: vertical)")
    parser.add_argument("--pitch", choices=("auto", "nice", "sunny", "heat", "rain", "blizzard"),
                        default="auto",
                        help="Pitch background (default: auto - pick by replay weather)")
    parser.add_argument("--commentary", action="store_true",
                        help="Generate one whimsical commentary line per pivotal play")
    parser.add_argument("--commentary-backend", choices=("template", "ollama", "openai", "claude"),
                        default=None,
                        help="Commentary backend (default: template - local templates, no LLM, no install; env: FUMBBL_COMMENTARY_BACKEND)")
    parser.add_argument("--commentary-model", default=None,
                        help="Model name for the chosen backend (default per backend; env: FUMBBL_COMMENTARY_MODEL)")
    parser.add_argument("--commentary-base-url", default=None,
                        help="Override base URL for ollama/openai backends (e.g. http://localhost:11434)")
    parser.add_argument("--tts", type=Path, default=None,
                        help="Generate per-play TTS audio into this directory (forces --commentary)")
    parser.add_argument("--sounds", type=Path, default=None,
                        help="Copy FFB game-event SFX (cheers, thuds, whistles) into this directory")
    parser.add_argument("--mix", type=Path, default=None,
                        help="Mix per-play TTS + SFX into a single mp3 (auto-runs --commentary/--tts/--sounds; requires ffmpeg)")
    parser.add_argument("--video", type=Path, default=None,
                        help="Compose final highlight MP4 from per-play GIFs + mixed MP3s (auto-runs --gifs/--mix; requires ffmpeg)")
    parser.add_argument("--tts-backend", choices=("kokoro", "say", "pyttsx3", "openai"), default=None,
                        help="TTS backend (default: kokoro - local neural TTS; env: FUMBBL_TTS_BACKEND)")
    parser.add_argument("--tts-voice", default=None,
                        help="Voice for the chosen TTS backend (default per backend; env: FUMBBL_TTS_VOICE)")
    parser.add_argument("--tts-meme", action="store_true",
                        help="Use af_nicole (ASMR-style) as the voice — comedy mode")
    parser.add_argument("--kill", action="store_true",
                        help="Only show kill (RIP) events in the report")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("fumbbl_replay")

    ref = jnlp_loader.resolve(args.replay_ref)
    log.info("resolved %s -> match_id=%s replay_id=%s",
             args.replay_ref, ref.match_id, ref.replay_id)

    # Resolution paths:
    #   match_id only  -> fetch summary, derive replay_id from it
    #   replay_id only -> fetch replay, synthesize summary from it
    #   both           -> use both directly
    summary: dict | None = None
    replay = None
    replay_id = ref.replay_id

    if ref.match_id is not None:
        summary = fumbbl_api.fetch_match_summary(ref.match_id)
        if replay_id is None:
            replay_id = fumbbl_api.resolve_replay_id(ref.match_id, summary)

    if not args.no_replay and replay_id is not None:
        replay = fumbbl_api.fetch_replay(replay_id)

    if summary is None:
        if replay is None:
            raise SystemExit("can't proceed: only a replay id was provided AND --no-replay was set")
        summary = fumbbl_api.synthesize_summary_from_replay(replay)
        log.info("synthesized summary from replay (no match id available)")

    team_home = team_away = None
    if not args.no_rosters and summary["team1"]["id"] and summary["team2"]["id"]:
        team_home = fumbbl_api.fetch_team(int(summary["team1"]["id"]))
        team_away = fumbbl_api.fetch_team(int(summary["team2"]["id"]))

    event_list = None
    player_lookup = None
    if replay is not None:
        event_list = events.extract_events(replay)
        player_lookup = events.roster_from_replay(replay)
        log.info("extracted %d events from replay %d (%d in-game players)",
                 len(event_list), replay_id, len(player_lookup))
        if args.dump_replay:
            args.dump_replay.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(args.dump_replay, "wt", encoding="utf-8") as f:
                json.dump(replay, f)
            log.info("dumped raw replay to %s", args.dump_replay)

    analysis = analyzer.analyze(
        summary, team_home, team_away,
        events=event_list, player_lookup=player_lookup,
    )

    # --video implies the full pipeline below it: we need gifs +
    # mixed audio. Auto-derive the intermediate dirs next to the
    # video output so a one-flag invocation works.
    if args.video:
        if not args.gifs:
            args.gifs = args.video.parent / args.orientation / "gifs"
        if not args.mix:
            args.mix = args.video.parent / "mixed"

    # When --mix is requested we auto-derive intermediate output dirs
    # for commentary/TTS next to the mix dir so the user doesn't have
    # to wire up multiple flags by hand. SFX are served from the
    # shared cache root by default - we only physically copy them
    # into a per-match directory when --sounds DIR is set.
    want_commentary = bool(args.commentary or args.tts or args.mix)
    want_tts_dir = args.tts or (args.mix.parent / "audio" if args.mix else None)
    kinds = {i: p.kind for i, p in enumerate(analysis.pivotal, 1)}

    commentary_lines: dict[int, str] = {}
    if want_commentary:
        from . import commentary
        try:
            commentary_lines = commentary.generate_commentary(
                analysis,
                backend=args.commentary_backend,
                model=args.commentary_model,
                base_url=args.commentary_base_url,
            )
            log.info("got commentary for %d/%d plays", len(commentary_lines), len(analysis.pivotal))
        except Exception as e:
            log.warning("commentary generation failed: %s", e)

    tts_paths: dict[int, Path] = {}
    tts = None
    voice_a: str | None = None
    if want_tts_dir:
        from . import tts

        # Pick the play-by-play voice. Seed by match id so a given
        # match gets a stable booth across reruns. --tts-meme swaps in
        # the ASMR-style nicole.
        voice_a = args.tts_voice
        if (args.tts_backend or tts.DEFAULT_BACKEND) == "kokoro" and not voice_a:
            seed = ref.match_id or ref.replay_id or 0
            voice_a, _ = tts.pick_voice_pair(seed, meme=args.tts_meme)
            log.info("picked voice for match %s: %s", seed, voice_a)

        try:
            tts_paths = tts.generate_audio(
                commentary_lines, want_tts_dir,
                pivotal_kinds=kinds,
                backend=args.tts_backend,
                voice=voice_a,
            )
            log.info("rendered %d audio clips to %s", len(tts_paths), want_tts_dir)
        except Exception as e:
            log.warning("TTS generation failed: %s", e)

    sfx_paths: dict[int, list[Path]] = {}
    if args.sounds or args.mix:
        from . import sounds as sounds_mod
        try:
            if args.sounds:
                # User wants a self-contained per-match copy.
                sfx_paths = sounds_mod.install_play_sounds(analysis.pivotal, args.sounds)
                log.info("copied SFX for %d plays into %s (also cached at root)",
                         len(sfx_paths), args.sounds)
            else:
                # Reference the shared cache directly.
                sfx_paths = sounds_mod.resolve_play_sounds(analysis.pivotal)
                log.info("resolved cached SFX for %d plays", len(sfx_paths))
        except Exception as e:
            log.warning("SFX resolution failed: %s", e)

    # Mix is deferred until AFTER gifs render so it can use each
    # play's impact_ms (when the visual climax lands) and total_ms.
    # When --gifs isn't requested, mix still runs with default
    # offsets (everything from t=0, as before).

    if args.json:
        out = dataclasses.asdict(analysis)
        if commentary_lines:
            out["commentary"] = {str(k): v for k, v in commentary_lines.items()}
        print(json.dumps(out, indent=2))
    else:
        print(analyzer.format_report(analysis, commentary=commentary_lines,
                                       only_kills=args.kill))

    if (args.tableaux or args.gifs) and replay is None:
        log.warning("--tableaux/--gifs require the replay event log; skipping (--no-replay was set)")
        return 0

    player_sprites: dict = {}
    if (args.tableaux or args.gifs) and not args.no_sprites and player_lookup:
        idx = sprites.position_icon_index_from_replay(replay)
        player_sprites = sprites.build_player_sprites(player_lookup, idx)
        log.info("loaded sprites for %d/%d players", len(player_sprites), len(player_lookup))

    # Team labels + logos for the endzones / pitch watermark.
    home_name = analysis.home.name
    away_name = analysis.away.name
    home_logo_img = None
    away_logo_img = None
    pitch_bg = None
    weather: str | None = None
    if args.tableaux or args.gifs:
        home_logo_id = _logo_id_from_team(team_home) or _logo_id_from_replay_team(replay, "home")
        away_logo_id = _logo_id_from_team(team_away) or _logo_id_from_replay_team(replay, "away")
        home_logo_img = sprites.fetch_team_logo(home_logo_id)
        away_logo_img = sprites.fetch_team_logo(away_logo_id)
        # Pitch background. Auto = use replay weather; otherwise force.
        from . import pitches
        weather = pitches.weather_from_replay(replay)
        if args.pitch == "auto":
            pitch_bg = pitches.fetch_pitch(weather)
            if pitch_bg is not None:
                log.info("loaded pitch background for weather %r", weather)
        else:
            pitch_bg = pitches.fetch_pitch_by_short_name(args.pitch)
            if pitch_bg is not None:
                log.info("loaded forced pitch %r", args.pitch)

    if args.tableaux or args.gifs:
        from . import dice as dice_mod

    if args.tableaux:
        from . import tableau  # local import: pillow only loaded when needed
        n = 0
        # TDs: stop before the post-score cleanup that sweeps players
        # to the dugout (cleanup happens after the score event in the
        # same command).
        # Casualties: the victim is removed from the pitch BEFORE
        # the casualty trigger fires within the same command, so
        # snapshot the previous command instead.
        for i, p in enumerate(analysis.pivotal, 1):
            if p.command_nr is None:
                continue
            if p.kind == "touchdown":
                state = field_state.reconstruct_at(
                    replay, p.command_nr, stop_at={"teamResultSetScore"},
                )
            elif p.kind == "casualty":
                state = field_state.reconstruct_at(replay, p.command_nr - 1)
            else:
                state = field_state.reconstruct_at(replay, p.command_nr)
            out = args.tableaux / f"{i:02d}_{p.kind}_{p.command_nr}.png"
            # Casualties: the originating block fired in an earlier cmd, so
            # look back. Blunders (double/triple skull, clutch fail) have the
            # roll in the event cmd itself. TDs and interceptions: scan a
            # short window for the dodges/GFIs that set up the play.
            lookback = {"casualty": 8, "touchdown": 8, "interception": 4,
                         "self_kill": 4, "clutch_fail": 0,
                         "double_skull": 0, "triple_skull": 0}.get(p.kind, 0)
            dice_for_play = dice_mod.extract_for_command(replay, p.command_nr, lookback=lookback)
            tableau.render_tableau(
                p, state, player_lookup or {}, out,
                sprites=player_sprites,
                orientation=args.orientation,
                home_name=home_name, away_name=away_name,
                home_logo=home_logo_img, away_logo=away_logo_img,
                dice=dice_for_play,
                pitch_background=pitch_bg,
                weather=weather,
            )
            n += 1
        log.info("rendered %d tableaux to %s", n, args.tableaux)

    gif_paths: dict[int, Path] = {}
    frames_dirs: dict[int, Path] = {}
    impact_offsets_ms: dict[int, int] = {}
    target_durations_ms: dict[int, int] = {}
    if args.gifs:
        from . import animate
        # When --video is set we also dump a full-res PNG frame sequence
        # per play so the video encoder skips the GIF palette pass.
        frames_root = (args.video.parent / args.orientation / "frames") if args.video else None
        n = 0
        for i, p in enumerate(analysis.pivotal, 1):
            if p.command_nr is None:
                continue
            out = args.gifs / f"{i:02d}_{p.kind}_{p.command_nr}.gif"
            per_play_frames_dir = (
                frames_root / f"{i:02d}_{p.kind}_{p.command_nr}"
                if frames_root else None
            )
            result = animate.render_play_gif(
                replay, p, player_lookup or {}, out,
                sprites=player_sprites,
                orientation=args.orientation,
                home_name=home_name, away_name=away_name,
                home_logo=home_logo_img, away_logo=away_logo_img,
                pitch_background=pitch_bg,
                weather=weather,
                frames_dir=per_play_frames_dir,
            )
            gif_paths[i] = result.path
            if result.frames_dir:
                frames_dirs[i] = result.frames_dir
            impact_offsets_ms[i] = result.impact_ms
            target_durations_ms[i] = result.total_ms
            n += 1
        log.info("rendered %d gifs to %s", n, args.gifs)

    mix_paths: dict[int, Path] = {}
    if args.mix:
        from . import mix as mix_mod
        try:
            mix_paths = mix_mod.mix_match_audio(
                tts_paths, sfx_paths, kinds, args.mix,
                impact_offsets_ms=impact_offsets_ms,
                target_durations_ms=target_durations_ms,
            )
            log.info("mixed %d per-play clips into %s", len(mix_paths), args.mix)
        except Exception as e:
            log.warning("audio mix failed: %s", e)

    if args.video:
        from . import compose, framing
        import shutil as _shutil
        if not gif_paths:
            log.warning("--video needs --gifs output; nothing to compose")
        elif not mix_paths:
            log.warning("--video needs --mix output; nothing to compose")
        else:
            # Render intro + outro title cards and their narrated audio,
            # then encode as still-image MP4 clips that the compose
            # step will prepend / append to the per-play sequence.
            intro_clip_path: Path | None = None
            outro_clip_path: Path | None = None
            try:
                framing_root = args.video.parent / args.orientation / "framing"
                framing_root.mkdir(parents=True, exist_ok=True)
                match_stats = framing.compute_match_stats(replay, analysis)

                intro_png = framing_root / "intro.png"
                framing.render_intro_slide(
                    analysis, player_lookup or {}, intro_png,
                    orientation=args.orientation,
                    home_logo=home_logo_img, away_logo=away_logo_img,
                )
                outro_png = framing_root / "outro.png"
                framing.render_outro_slide(
                    analysis, match_stats, outro_png,
                    orientation=args.orientation,
                    home_logo=home_logo_img, away_logo=away_logo_img,
                )

                # TTS narration for intro + outro using the existing voice.
                if want_tts_dir:
                    intro_line = framing.generate_intro_line(analysis, player_lookup or {})
                    outro_line = framing.generate_outro_line(analysis)
                    intro_audio_dir = want_tts_dir.parent / "framing_tts"
                    intro_audio_paths = tts.generate_audio(
                        {0: intro_line}, intro_audio_dir,
                        pivotal_kinds={0: "intro"},
                        backend=args.tts_backend,
                        voice=voice_a,
                    )
                    outro_audio_paths = tts.generate_audio(
                        {0: outro_line}, intro_audio_dir,
                        pivotal_kinds={0: "outro"},
                        backend=args.tts_backend,
                        voice=voice_a,
                    )
                    intro_audio = intro_audio_paths.get(0)
                    outro_audio = outro_audio_paths.get(0)

                    clip_work = args.video.parent / f"_clips_{args.video.stem}"
                    clip_work.mkdir(parents=True, exist_ok=True)
                    if intro_audio and intro_audio.exists():
                        intro_clip_path = clip_work / "00_intro.mp4"
                        if not compose.encode_still_clip(intro_png, intro_audio,
                                                          intro_clip_path,
                                                          min_duration_ms=4000):
                            intro_clip_path = None
                    if outro_audio and outro_audio.exists():
                        outro_clip_path = clip_work / "99_outro.mp4"
                        if not compose.encode_still_clip(outro_png, outro_audio,
                                                          outro_clip_path,
                                                          min_duration_ms=4000):
                            outro_clip_path = None
            except Exception as e:
                log.warning("intro/outro generation failed: %s", e)

            try:
                result = compose.compose_highlight_reel(
                    gif_paths, mix_paths, kinds, args.video,
                    frames_dirs_by_play=frames_dirs,
                    intro_clip=intro_clip_path,
                    outro_clip=outro_clip_path,
                )
                if result:
                    log.info("composed highlight video at %s", result)
                # Clean up the PNG frame sequence (~250 MB for a typical
                # match). We keep the per-play GIFs since users often
                # want those standalone.
                for d in frames_dirs.values():
                    if d.exists():
                        _shutil.rmtree(d, ignore_errors=True)
                parent = (args.video.parent / args.orientation / "frames")
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except Exception as e:
                log.warning("video compose failed: %s", e)


def _logo_id_from_team(team: dict | None) -> int | None:
    if not team:
        return None
    bio = team.get("bio") or {}
    return bio.get("image")


def _logo_id_from_replay_team(replay: dict, side: str) -> int | None:
    """Fallback: pull the logo image id out of `logoUrl` in the replay's roster."""
    team = (replay.get("game") or {}).get(f"team{side.capitalize()}") or {}
    url = team.get("logoUrl")
    if not url:
        return None
    # logoUrl is typically "i/12345"; the trailing integer is the image id.
    import re
    m = re.search(r"(\d+)", url)
    return int(m.group(1)) if m else None

    return 0


if __name__ == "__main__":
    sys.exit(main())
