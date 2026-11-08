"""Non-destructive asset registry and timeline renderer for Studio v0.5."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from . import core


def migrate(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS shot_assets (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, shot_id TEXT NOT NULL,
      source TEXT NOT NULL, file TEXT NOT NULL, sha256 TEXT NOT NULL,
      created TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
      UNIQUE(shot_id, source, sha256),
      FOREIGN KEY(shot_id) REFERENCES shots(id));
    CREATE INDEX IF NOT EXISTS idx_assets_shot ON shot_assets(shot_id);
    CREATE TABLE IF NOT EXISTS timeline_items (
      project_id TEXT NOT NULL, shot_id TEXT NOT NULL, asset_id TEXT NOT NULL,
      position INTEGER NOT NULL, duration INTEGER NOT NULL,
      PRIMARY KEY(project_id, shot_id), FOREIGN KEY(asset_id) REFERENCES shot_assets(id));
    CREATE TABLE IF NOT EXISTS render_history (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, output TEXT NOT NULL,
      manifest_json TEXT NOT NULL, created TEXT NOT NULL);
    ''')


def _hash(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _shots(db, pid):
    core.project(db, pid)
    return db.execute('SELECT * FROM shots WHERE project_id=? ORDER BY seq', (pid,)).fetchall()


def _register(db, pid, shot, source, file):
    file = Path(file).expanduser().resolve()
    if not file.is_file() or file.stat().st_size == 0:
        raise ValueError(f'Missing/empty video: {file}')
    digest = _hash(file)
    found = db.execute('SELECT id FROM shot_assets WHERE shot_id=? AND source=? AND sha256=?',
                       (shot['id'], source, digest)).fetchone()
    if found:
        return found['id']
    aid = uuid.uuid4().hex[:16]
    db.execute('INSERT INTO shot_assets(id,project_id,shot_id,source,file,sha256,created) VALUES(?,?,?,?,?,?,?)',
               (aid,pid,shot['id'],source,str(file),digest,core.now()))
    return aid


def sync(pid):
    """Discover existing legacy and i2v assets without changing selected assets."""
    with core.connect() as db:
        migrate(db)
        shots = _shots(db,pid)
        counts = {'original':0,'i2v':0}
        for shot in shots:
            if shot['file'] and Path(shot['file']).is_file():
                _register(db,pid,shot,'original',shot['file'])
                counts['original'] += 1
            i2v = core.HOME / pid / 'experiments' / f"i2v-{shot['seq']:03d}" / 'clip.mp4'
            if i2v.is_file():
                _register(db,pid,shot,'i2v',i2v)
                counts['i2v'] += 1
        for shot in shots:
            if db.execute('SELECT 1 FROM timeline_items WHERE project_id=? AND shot_id=?', (pid,shot['id'])).fetchone():
                continue
            asset = db.execute('SELECT id FROM shot_assets WHERE shot_id=? ORDER BY CASE source WHEN \'i2v\' THEN 0 ELSE 1 END, created DESC LIMIT 1', (shot['id'],)).fetchone()
            if asset:
                db.execute('INSERT INTO timeline_items VALUES (?,?,?,?,?)',(pid,shot['id'],asset['id'],shot['seq'],shot['duration']))
                if db.execute('SELECT 1 FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone():
                    db.execute('INSERT INTO guided_stale_shots VALUES (?,?,NULL) ON CONFLICT(project_id,shot_id) DO UPDATE SET keep_asset_id=NULL', (pid,shot['id']))
    return counts


def import_asset(pid, seq, file, source='manual'):
    with core.connect() as db:
        migrate(db)
        shot = next((s for s in _shots(db,pid) if s['seq']==seq),None)
        if not shot: raise ValueError(f'Unknown shot: {seq}')
        return _register(db,pid,shot,source,file)


def assets(pid, seq=None):
    with core.connect() as db:
        migrate(db)
        _shots(db,pid)
        sql='SELECT a.*, s.seq, (t.asset_id=a.id) AS selected FROM shot_assets a JOIN shots s ON s.id=a.shot_id LEFT JOIN timeline_items t ON t.project_id=a.project_id AND t.shot_id=a.shot_id WHERE a.project_id=?'
        args=[pid]
        if seq is not None:
            sql+=' AND s.seq=?'; args.append(seq)
        sql+=' ORDER BY s.seq,a.created,a.id'
        return [dict(r) for r in db.execute(sql,args)]


def select(pid, seq, aid):
    with core.connect() as db:
        migrate(db)
        shot=next((s for s in _shots(db,pid) if s['seq']==seq),None)
        if not shot: raise ValueError('Unknown shot')
        a=db.execute('SELECT * FROM shot_assets WHERE id=? AND project_id=? AND shot_id=?',(aid,pid,shot['id'])).fetchone()
        if not a: raise ValueError('Asset does not belong to this shot')
        if not Path(a['file']).is_file(): raise ValueError('Asset file is missing')
        old=db.execute('SELECT asset_id FROM timeline_items WHERE project_id=? AND shot_id=?',(pid,shot['id'])).fetchone()
        if old is None or old['asset_id'] != aid:
            db.execute('DELETE FROM guided_confirmations WHERE project_id=? AND shot_id=?',(pid,shot['id']))
            db.execute('UPDATE guided_projects SET revision=revision+1 WHERE project_id=?',(pid,))
            current = db.execute('''SELECT 1 FROM guided_attempts a JOIN guided_video_provenance p ON p.attempt_id=a.id
                JOIN guided_frame_selection f ON f.project_id=a.project_id AND f.shot_id=a.shot_id AND f.confirmed_frame_id=p.frame_id
                WHERE a.project_id=? AND a.shot_id=? AND a.asset_id=?''', (pid,shot['id'],aid)).fetchone()
            if current:
                db.execute('DELETE FROM guided_stale_shots WHERE project_id=? AND shot_id=?', (pid,shot['id']))
            elif db.execute('SELECT 1 FROM guided_character_settings WHERE project_id=?', (pid,)).fetchone():
                db.execute('INSERT INTO guided_stale_shots VALUES (?,?,NULL) ON CONFLICT(project_id,shot_id) DO UPDATE SET keep_asset_id=NULL', (pid,shot['id']))
        db.execute('INSERT INTO timeline_items VALUES(?,?,?,?,?) ON CONFLICT(project_id,shot_id) DO UPDATE SET asset_id=excluded.asset_id, position=excluded.position, duration=excluded.duration',
                   (pid,shot['id'],aid,shot['seq'],shot['duration']))
    return aid


def timeline(pid):
    with core.connect() as db:
        migrate(db)
        shots=_shots(db,pid)
        items=[]
        for s in shots:
            r=db.execute('SELECT a.id,a.source,a.file,a.sha256,t.duration FROM timeline_items t JOIN shot_assets a ON a.id=t.asset_id WHERE t.project_id=? AND t.shot_id=?',(pid,s['id'])).fetchone()
            items.append({'seq':s['seq'],'shot_id':s['id'],'planned_duration':s['duration'], 'selected':dict(r) if r else None})
    return items


def render(pid, output=None, source='selected'):
    """Render a snapshot; never overwrite existing output or mutate original shots."""
    if source not in ('selected','original','i2v','mixed'):
        raise ValueError('source must be selected, original, i2v or mixed')
    core.require_ffmpeg()
    with core.connect() as db:
        migrate(db)
        db.execute('BEGIN')
        p=core.project(db,pid)
        shots=_shots(db,pid)
        if not shots: raise ValueError('No shots')
        guided=db.execute('SELECT * FROM guided_projects WHERE project_id=?',(pid,)).fetchone()
        if guided:
            if not guided['storyboard_confirmed'] or source != 'selected':
                raise ValueError('Guided render requires confirmed storyboard and selected assets')
            from . import guided_characters
            guided_characters.require_render_ready(db, pid)
            for s in shots:
                approval=db.execute('''SELECT 1 FROM guided_confirmations c
                    JOIN timeline_items t ON t.project_id=c.project_id AND t.shot_id=c.shot_id
                    WHERE c.project_id=? AND c.shot_id=? AND c.asset_id=t.asset_id AND c.prompt=?''',
                    (pid,s['id'],s['prompt'])).fetchone()
                if not approval: raise ValueError('All guided shots must be confirmed before render')
        selected=[]
        for s in shots:
            if source in ('selected','mixed'):
                a=db.execute('SELECT a.* FROM timeline_items t JOIN shot_assets a ON a.id=t.asset_id WHERE t.project_id=? AND t.shot_id=?',(pid,s['id'])).fetchone()
            else:
                a=db.execute('SELECT * FROM shot_assets WHERE shot_id=? AND source=? ORDER BY created DESC LIMIT 1',(s['id'],source)).fetchone()
            if a is None: raise ValueError(f'No {source} asset for shot {s["seq"]}; run sync-assets or select-asset')
            path=Path(a['file'])
            if not path.is_file() or _hash(path)!=a['sha256']:
                raise ValueError(f'Asset missing or changed for shot {s["seq"]}: {path}')
            selected.append((s,dict(a)))
    root=core.HOME/pid
    if output is None:
        output=root/f'final-{source}-{uuid.uuid4().hex[:8]}.mp4'
    else:
        output=Path(output).expanduser().resolve()
    if output.exists(): raise ValueError(f'Output exists; refusing overwrite: {output}')
    output.parent.mkdir(parents=True,exist_ok=True)
    run_id=uuid.uuid4().hex
    work=root/'renders'/run_id
    work.mkdir(parents=True,exist_ok=False)
    paths=[]
    manifest={'project_id':pid,'source':source,'fps':24,'output':str(output),'shots':[]}
    try:
        for s,a in selected:
            dst=work/f"shot-{s['seq']:03d}.mp4"
            duration=s['duration']
            vf=(f'fps=24,scale=1280:720:force_original_aspect_ratio=decrease,'
                f'pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,'
                f'tpad=stop_mode=clone:stop_duration={duration},trim=duration={duration},setpts=N/(24*TB)')
            core.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',a['file'],'-vf',vf,
                      '-frames:v',str(duration*24),'-an','-c:v','libx264','-preset','veryfast','-pix_fmt','yuv420p',str(dst)])
            core.verify_video(dst,duration)
            paths.append(dst)
            manifest['shots'].append({'seq':s['seq'],'asset_id':a['id'],'source':a['source'],'file':a['file'],'sha256':a['sha256'],'duration':duration})
        concat=work/'concat.txt'
        concat.write_text(''.join("file '"+str(x.resolve()).replace("'", "'\\''")+"'\n" for x in paths),encoding='utf-8')
        temp=work/'final.mp4'
        core.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(concat),
                  '-vf','fps=24,setpts=N/(24*TB)','-frames:v',str(p['duration']*24),'-an',
                  '-c:v','libx264','-preset','veryfast','-pix_fmt','yuv420p','-movflags','+faststart',str(temp)])
        core.verify_video(temp,p['duration'])
        temp.replace(output)
        manifest_path=output.with_suffix('.json')
        if manifest_path.exists(): raise ValueError('Manifest already exists; choose another output')
        manifest_path.write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
        with core.connect() as db:
            migrate(db)
            db.execute('INSERT INTO render_history VALUES (?,?,?,?,?)',(run_id,pid,str(output),json.dumps(manifest),core.now()))
        return output
    finally:
        import shutil
        shutil.rmtree(work,ignore_errors=True)
