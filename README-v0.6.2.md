# AI Video Studio v0.6.2 — Audio Timeline

Upgrade from v0.6.1; preserve your existing `studio-data` directory and `.env`.

## Changes

- `studio audio-add PROJECT --file assets/sfx/bird.wav --at 12.5 --duration 2.5 --gain 0.65 --fade-in 0.1 --fade-out 0.3`
- `studio audio-list PROJECT`
- `studio audio-remove PROJECT EVENT_ID`
- `studio export-film PROJECT --video /path/to/final-i2v.mp4 --burn-subtitles --ducking`
- Chinese subtitle font: `STUDIO_SUBTITLE_FONT=Noto Sans CJK SC`, `STUDIO_SUBTITLE_SIZE=24`.

Sound effects are saved in `studio-data/PROJECT/audio-events.json` and layered at specified times. `--ducking` lowers music dynamically in response to narration using FFmpeg `sidechaincompress`; it only applies when both narration and music are configured. Existing `soundtrack-set` remains supported. Every export creates a new MP4 and manifest; existing outputs are not overwritten.

Note: effects are not copied into the project; keep source files in place. `audio-add` references local audio and does not generate sound effects. Peak limiting and loudness normalization are not yet implemented; avoid excessive combined gains. `--ducking` is experimental and depends on FFmpeg sidechaincompress support.
