# AI Video Studio v0.6 — Soundtrack and subtitles

Non-destructive post-production for v0.5 timeline renders. All features are local FFmpeg operations; no paid AI API is called.

## Quick start

```bash
cd ai-video-studio
uv sync --extra dev
PID=3a5b4d58f96b
uv run studio captions-draft "$PID"
# Edit studio-data/$PID/shot-captions.srt manually. These are placeholders, not generated narration.
uv run studio soundtrack-set "$PID" --narration /path/to/voice.wav --music /path/to/music.mp3 --ambience /path/to/forest.wav --subtitles "studio-data/$PID/shot-captions.srt"
uv run studio soundtrack-show "$PID"
uv run studio export-film "$PID" --video "studio-data/$PID/final-i2v.mp4" --burn-subtitles
# Or let v0.5 render selected timeline first:
uv run studio export-film "$PID" --source selected --burn-subtitles
```

Optional `--narration-gain`, `--music-gain`, `--ambience-gain`, `--fade-seconds` are available on `soundtrack-set`. A missing audio input is omitted. Music and ambience are looped to the full film duration. Narration plays once, starting at 0 seconds. This version does **not** synthesize speech, generate music, align subtitles to voice, or implement dynamic voice-aware ducking. Music is attenuated using a fixed gain, with fade-in and fade-out. If no audio tracks are configured, a silent AAC track is added.

By default export preserves the video bitstream and encodes AAC audio. `--burn-subtitles` requires an FFmpeg build with libass and re-encodes video to H.264. A fresh output path and JSON manifest are created; existing files are never overwritten. Explicit output via `--output` is supported. Audio and subtitle paths are stored in `studio-data/PROJECT_ID/soundtrack.json`. Keep media files at those locations.

## Safety

Back up `studio-data/studio.sqlite3` before upgrade. Keep your previous `studio-data` and `STUDIO_HOME`. Do not commit `.env` or secrets. Use properly licensed audio tracks. Exporting existing footage does not change shot assets or timeline selection.
