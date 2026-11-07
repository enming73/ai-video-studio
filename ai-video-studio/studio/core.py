"""SQLite-backed deterministic storyboard and local FFmpeg renderer."""
from __future__ import annotations
import json
import math
import os
import shutil
import sqlite3
import subprocess
import uuid
import time
import httpx
from video_provider_core import PROVIDERS
from dotenv import load_dotenv
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("STUDIO_HOME", "./studio-data")).resolve()

def now():
    return datetime.now(timezone.utc).isoformat()

def connect():
    HOME.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(HOME / "studio.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS projects (
      id TEXT PRIMARY KEY, prompt TEXT NOT NULL, duration INTEGER NOT NULL,
      provider TEXT NOT NULL, budget REAL NOT NULL, status TEXT NOT NULL,
      created TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS shots (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, seq INTEGER NOT NULL,
      prompt TEXT NOT NULL, duration INTEGER NOT NULL, status TEXT NOT NULL,
      file TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
      UNIQUE(project_id, seq), FOREIGN KEY(project_id) REFERENCES projects(id));
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(shots)")}
    for name, kind in [("remote_id", "TEXT"), ("video_url", "TEXT"), ("reserved_usd", "REAL NOT NULL DEFAULT 0")]:
        if name not in columns:
            db.execute(f"ALTER TABLE shots ADD COLUMN {name} {kind}")
    db.execute("""CREATE TABLE IF NOT EXISTS storyboards (
      project_id TEXT PRIMARY KEY, bible_json TEXT NOT NULL,
      planner TEXT NOT NULL, updated TEXT NOT NULL)""")
    db.commit()
    return db

def project(db, pid):
    p = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if p is None:
        raise ValueError(f"Project not found: {pid}")
    return p

def create(prompt, duration, provider, budget):
    if not prompt.strip() or duration <= 0 or budget < 0:
        raise ValueError("Prompt must be nonempty, duration > 0 and budget >= 0")
    if provider not in ("mock", "minimax", "wan", "flux"):
        raise ValueError("Unknown provider")
    pid = uuid.uuid4().hex[:12]
    with connect() as db:
        db.execute("INSERT INTO projects VALUES (?,?,?,?,?,?,?)",
                   (pid, prompt, duration, provider, budget, "CREATED", now()))
    return pid

def plan(pid, shot_length=10):
    if shot_length <= 0 or shot_length > 30:
        raise ValueError("shot_length must be between 1 and 30 seconds")
    with connect() as db:
        p = project(db, pid)
        if p["provider"] != "mock" and (not 5 <= shot_length <= 15 or p["duration"] % shot_length and p["duration"] % shot_length < 5):
            raise ValueError("Cloud shots must each be 5..15 seconds; choose another shot length")
        existing = db.execute("SELECT COUNT(*) FROM shots WHERE project_id=?", (pid,)).fetchone()[0]
        if existing:
            raise ValueError("Storyboard already exists; preserving existing shots and assets")
        remaining = p["duration"]
        seq = 0
        while remaining:
            seq += 1
            duration = min(shot_length, remaining)
            prompt = f"{p['prompt']}. Cinematic shot {seq}, natural motion, coherent visual style."
            db.execute("INSERT INTO shots(id,project_id,seq,prompt,duration,status) VALUES (?,?,?,?,?,?)",
                       (f"{pid}-s{seq:03d}", pid, seq, prompt, duration, "PLANNED"))
            remaining -= duration
        db.execute("UPDATE projects SET status='PLANNED' WHERE id=?", (pid,))
    return seq

def storyboard(pid):
    with connect() as db:
        p = dict(project(db, pid))
        row = db.execute("SELECT * FROM storyboards WHERE project_id=?", (pid,)).fetchone()
        shots = [dict(x) for x in db.execute("SELECT * FROM shots WHERE project_id=? ORDER BY seq", (pid,))]
    return {"project": p, "character_bible": json.loads(row["bible_json"]) if row else None,
            "planner": row["planner"] if row else "legacy", "shots": shots}


def _llm_json(system, user, model, base_url):
    load_dotenv()
    key = os.getenv("STUDIO_LLM_API_KEY")
    if not key:
        raise ValueError("Set STUDIO_LLM_API_KEY (no paid request made)")
    if not base_url.startswith("https://") and not os.getenv("STUDIO_ALLOW_HTTP_LLM"):
        raise ValueError("LLM endpoint must use HTTPS (or set STUDIO_ALLOW_HTTP_LLM=1 for local use)")
    with httpx.Client(timeout=90) as client:
        response = client.post(base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "messages": [{"role":"system","content":system},
                {"role":"user","content":user}], 
                "response_format":{"type":"json_object"}})
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    return json.loads(content)


def smart_plan(pid, shot_length=10, model=None, base_url=None):
    """Plan only: no video-generation API calls. Existing shots are never overwritten."""
    with connect() as db:
        p = dict(project(db, pid))
        if db.execute("SELECT 1 FROM shots WHERE project_id=?", (pid,)).fetchone():
            raise ValueError("Project already has shots; create a new project to preserve paid assets")
    if not model:
        raise ValueError("Provide --model for the LLM planner")
    if not base_url:
        base_url = os.getenv("STUDIO_LLM_BASE_URL", "https://api.openai.com/v1")
    lengths = []
    remaining = p["duration"]
    while remaining:
        length = min(shot_length, remaining)
        lengths.append(length)
        remaining -= length
    if shot_length < 5 or shot_length > 15 or (p["provider"] != "mock" and any(x < 5 for x in lengths)):
        raise ValueError("Use shot lengths 5..15s for cloud providers; total duration must fit")
    payload = _llm_json(
        "You are a film director. Return ONLY a JSON object with keys character_bible (object) and shots (array). "
        "Every shot must have prompt (English detailed visual prompt) and beat (short story action). "
        "Keep the same named characters' appearance, colors, costume and world across shots. "
        "Make each shot a distinct narrative beat. Do not include copyrighted character names or living artists' styles.",
        json.dumps({"brief":p["prompt"],"shot_durations_seconds":lengths,
                    "required_shot_count":len(lengths),"aspect_ratio":"16:9"},ensure_ascii=False),
        model, base_url)
    bible = payload.get("character_bible")
    items = payload.get("shots")
    if not isinstance(bible,dict) or not isinstance(items,list) or len(items)!=len(lengths):
        raise ValueError("LLM returned invalid storyboard shape; no shots saved")
    for item in items:
        if not isinstance(item,dict) or not isinstance(item.get("prompt"),str) or len(item["prompt"].strip()) < 15:
            raise ValueError("LLM returned an invalid shot prompt; no shots saved")
    with connect() as db:
        if db.execute("SELECT 1 FROM shots WHERE project_id=?",(pid,)).fetchone():
            raise ValueError("Storyboard was created concurrently; aborting")
        db.execute("INSERT INTO storyboards VALUES (?,?,?,?)",(pid,json.dumps(bible,ensure_ascii=False),model,now()))
        for i,(length,item) in enumerate(zip(lengths,items),1):
            db.execute("INSERT INTO shots(id,project_id,seq,prompt,duration,status) VALUES (?,?,?,?,?,?)",
                (f"{pid}-s{i:03d}",pid,i,item["prompt"].strip(),length,"PLANNED"))
        db.execute("UPDATE projects SET status='PLANNED' WHERE id=?",(pid,))
    return len(items)


def update_shot(pid, seq, prompt):
    if not prompt.strip():
        raise ValueError("Prompt must not be empty")
    with connect() as db:
        project(db,pid)
        shot=db.execute("SELECT * FROM shots WHERE project_id=? AND seq=?",(pid,seq)).fetchone()
        if not shot:
            raise ValueError("Shot not found")
        if shot["status"] not in ("PLANNED", "FAILED") or shot["remote_id"] or shot["file"] or shot["reserved_usd"]:
            raise ValueError("Cannot edit submitted/generated/charged shot; create a new project to preserve assets")
        db.execute("UPDATE shots SET prompt=?,status='PLANNED',error=NULL WHERE id=?",(prompt.strip(),shot["id"]))
    return shot["id"]


def save_storyboard(pid, target):
    data=storyboard(pid)
    target=Path(target)
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    return target


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(map(str,cmd))}\n{result.stderr[-1500:]}")

def require_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg and ffprobe must be installed and available in PATH")

def verify_video(path, expected):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                            capture_output=True, text=True, check=True)
    actual = float(result.stdout.strip())
    if abs(actual - expected) > 0.25:
        raise RuntimeError(f"Invalid clip duration: expected {expected}s, got {actual:.3f}s")

def mock_generate(shot, target):
    # Deterministic visual test media, not AI-generated footage.
    colors = ["0x1e3a5f", "0x315c72", "0x476d70", "0x625f72", "0x805f65"]
    color = colors[(shot["seq"]-1) % len(colors)]
    tmp = target.with_suffix(".part.mp4")
    tmp.unlink(missing_ok=True)
    try:
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"color=c={color}:s=640x360:r=24",
             "-t", str(shot["duration"]), "-an", "-c:v", "libx264",
             "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(tmp)])
        verify_video(tmp, shot["duration"])
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)

def generate(pid, live=False, budget=None, poll_interval=15, limit=0):
    load_dotenv()
    require_ffmpeg()
    with connect() as db:
        p = project(db, pid)
        if p["provider"] != "mock":
            return generate_cloud(pid, live=live, budget=budget, poll_interval=poll_interval, limit=limit)
        shots = db.execute("SELECT * FROM shots WHERE project_id=? ORDER BY seq", (pid,)).fetchall()
        if not shots:
            raise ValueError("Run 'studio plan' first")
    output = HOME / pid / "clips"
    output.mkdir(parents=True, exist_ok=True)
    for shot in shots:
        path = output / f"{shot['id']}.mp4"
        if path.exists():
            try:
                verify_video(path, shot["duration"])
                with connect() as db:
                    db.execute("UPDATE shots SET status='COMPLETED',file=?,error=NULL WHERE id=?",
                               (str(path), shot["id"]))
                continue
            except Exception:
                path.unlink(missing_ok=True)
        with connect() as db:
            db.execute("UPDATE shots SET status='GENERATING',attempts=attempts+1,error=NULL WHERE id=?", (shot["id"],))
        try:
            mock_generate(shot, path)
        except Exception as exc:
            with connect() as db:
                db.execute("UPDATE shots SET status='FAILED',error=? WHERE id=?", (str(exc), shot["id"]))
            raise
        with connect() as db:
            db.execute("UPDATE shots SET status='COMPLETED',file=? WHERE id=?", (str(path), shot["id"]))
    with connect() as db:
        db.execute("UPDATE projects SET status='GENERATED' WHERE id=?", (pid,))
    return len(shots)

def render(pid):
    require_ffmpeg()
    with connect() as db:
        p = project(db, pid)
        shots = db.execute("SELECT * FROM shots WHERE project_id=? ORDER BY seq", (pid,)).fetchall()
    if not shots or any(s["status"] != "COMPLETED" for s in shots):
        raise ValueError("All shots must be generated before rendering")
    directory = HOME / pid
    directory.mkdir(parents=True, exist_ok=True)
    normalized = directory / "normalized"
    normalized.mkdir(exist_ok=True)
    manifest = directory / "timeline.json"
    manifest.write_text(json.dumps({"project_id": pid, "fps": 24,
        "shots": [{"id": s["id"], "duration": s["duration"], "file": s["file"]} for s in shots]}, indent=2), encoding="utf-8")
    concat = directory / "concat.txt"
    paths=[]
    for shot in shots:
        if not shot["file"] or not Path(shot["file"]).exists():
            raise ValueError(f"Missing asset for {shot['id']}; run generate again")
        src=Path(shot["file"])
        dst=normalized / f"{shot['id']}.mp4"
        temp=normalized / f"{shot['id']}.part.mp4"
        # Always regenerate normalized media; never mutate expensive source assets.
        temp.unlink(missing_ok=True)
        try:
            run(["ffmpeg","-hide_banner","-loglevel","error","-y","-i",str(src),
                 "-vf",f"fps=24,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,tpad=stop_mode=clone:stop_duration={shot['duration']},trim=duration={shot['duration']},setpts=N/(24*TB)",
                 "-frames:v",str(shot["duration"]*24),"-an","-c:v","libx264","-preset","veryfast",
                 "-pix_fmt","yuv420p",str(temp)])
            verify_video(temp,shot["duration"])
            temp.replace(dst)
        finally:
            temp.unlink(missing_ok=True)
        paths.append(dst)
    concat.write_text("".join("file '" + str(x.resolve()).replace("'", "'\\''") + "'\n" for x in paths),encoding="utf-8")
    target=directory / "final.mp4"
    tmp=directory / "final.part.mp4"
    try:
        run(["ffmpeg","-hide_banner","-loglevel","error","-y","-f","concat","-safe","0","-i",str(concat),
             "-vf","fps=24,setpts=N/(24*TB)","-frames:v",str(p["duration"]*24),"-an",
             "-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p","-movflags","+faststart",str(tmp)])
        verify_video(tmp,p["duration"])
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    with connect() as db:
        db.execute("UPDATE projects SET status='RENDERED' WHERE id=?", (pid,))
    return target

def status(pid):
    with connect() as db:
        p = dict(project(db, pid))
        shots = [dict(s) for s in db.execute("SELECT * FROM shots WHERE project_id=? ORDER BY seq", (pid,))]
    return {"project": p, "shots": shots, "estimated_cost_usd": round(sum(s["reserved_usd"] for s in shots),3),
            "output": str(HOME / pid / "final.mp4") if (HOME / pid / "final.mp4").exists() else None}


def generate_cloud(pid, live=False, budget=None, poll_interval=15, limit=0):
    from video_provider_core.media import download
    if poll_interval < 1 or limit < 0:
        raise ValueError("Invalid poll interval or limit")
    with connect() as db:
        p = project(db, pid)
        shots = [dict(s) for s in db.execute("SELECT * FROM shots WHERE project_id=? ORDER BY seq", (pid,))]
        if not shots:
            raise ValueError("Run studio plan first")
        rate_env = {"minimax":"MINIMAX_USD_PER_SECOND", "wan":"WAN_USD_PER_SECOND", "flux":"FAL_USD_PER_SECOND"}[p["provider"]]
        raw_rate = os.getenv(rate_env)
        if not raw_rate:
            raise ValueError(f"Set {rate_env} to a verified USD/sec rate")
        rate = float(raw_rate)
        if not 0 < rate < 100:
            raise ValueError("Invalid rate")
        ceiling = p["budget"] if budget is None else min(p["budget"], budget)
        if ceiling < 0:
            raise ValueError("Budget must be nonnegative")
        pending = [s for s in shots if s["status"] != "COMPLETED"]
        if limit:
            pending = pending[:limit]
        reserved = sum(s["reserved_usd"] for s in shots)
        planned = sum(s["duration"] * rate for s in pending if s["status"] == "PLANNED")
        if not live:
            return {"mode":"DRY_RUN", "shots":len(pending), "new_estimate_usd":round(planned,3), "already_reserved_usd":round(reserved,3), "budget_usd":ceiling}
    output = HOME / pid / "clips"
    output.mkdir(parents=True, exist_ok=True)
    processed = 0
    with httpx.Client(timeout=httpx.Timeout(40,connect=15),follow_redirects=False) as client:
        provider = PROVIDERS[p["provider"]](client)
        for shot in pending:
            sid = shot["id"]
            state = shot["status"]
            remote = shot["remote_id"]
            if state in ("SUBMITTING", "GENERATING") and not remote:
                print(sid, "ambiguous submission: use studio attach after checking provider dashboard")
                continue
            if state in ("FAILED", "CANCELLED"):
                continue
            if state == "PLANNED":
                cost = shot["duration"] * rate
                with connect() as db:
                    reserved = db.execute("SELECT COALESCE(SUM(reserved_usd),0) FROM shots WHERE project_id=?", (pid,)).fetchone()[0]
                    if reserved + cost > ceiling + 1e-9:
                        print(sid, "SKIPPED: budget ceiling")
                        continue
                    db.execute("UPDATE shots SET status='SUBMITTING', attempts=attempts+1, reserved_usd=?, error=NULL WHERE id=?",(cost,sid))
                try:
                    remote = provider.submit(shot["prompt"],shot["duration"])
                except Exception as e:
                    with connect() as db:
                        db.execute("UPDATE shots SET error=? WHERE id=?",(str(e)[:500],sid))
                    print(sid, "submission uncertain:",str(e)[:180])
                    continue
                with connect() as db:
                    db.execute("UPDATE shots SET status='SUBMITTED',remote_id=? WHERE id=?",(remote,sid))
            url = shot["video_url"] if state == "DOWNLOAD_PENDING" else None
            if not url:
                while True:
                    try:
                        result = provider.poll(remote)
                    except Exception as e:
                        print(sid,"poll failed, resume next run:",str(e)[:180])
                        break
                    if result.status == "failed":
                        with connect() as db:
                            db.execute("UPDATE shots SET status='FAILED',error=? WHERE id=?",(result.error,sid))
                        break
                    if result.status == "succeeded":
                        url = result.url
                        with connect() as db:
                            db.execute("UPDATE shots SET status='DOWNLOAD_PENDING',video_url=? WHERE id=?",(url,sid))
                        break
                    with connect() as db:
                        db.execute("UPDATE shots SET status='RUNNING' WHERE id=?",(sid,))
                    time.sleep(poll_interval)
            if url:
                target=output / f"{sid}.mp4"
                try:
                    download(client,url,target)
                    verify_video(target,shot["duration"])
                except Exception as e:
                    with connect() as db:
                        db.execute("UPDATE shots SET error=? WHERE id=?",(str(e)[:500],sid))
                    print(sid,"download/verification pending:",str(e)[:180])
                    continue
                with connect() as db:
                    db.execute("UPDATE shots SET status='COMPLETED',file=?,error=NULL WHERE id=?",(str(target),sid))
                processed += 1
    with connect() as db:
        remaining = db.execute("SELECT COUNT(*) FROM shots WHERE project_id=? AND status!='COMPLETED'",(pid,)).fetchone()[0]
        if remaining == 0:
            db.execute("UPDATE projects SET status='GENERATED' WHERE id=?",(pid,))
    return {"completed_this_run":processed,"remaining":remaining}

def attach(pid, shot_id, remote_id):
    if not remote_id.strip():
        raise ValueError("Remote ID required")
    with connect() as db:
        project(db,pid)
        row=db.execute("SELECT * FROM shots WHERE project_id=? AND id=?",(pid,shot_id)).fetchone()
        if not row or row["status"]!="SUBMITTING" or row["remote_id"]:
            raise ValueError("Shot must be ambiguous SUBMITTING with no remote_id")
        db.execute("UPDATE shots SET status='SUBMITTED',remote_id=?,error=NULL WHERE id=?",(remote_id,shot_id))
