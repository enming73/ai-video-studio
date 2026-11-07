import sqlite3, json, os, time, hashlib, subprocess, html, pathlib, ipaddress, socket
from urllib.parse import urlparse
import httpx
from .providers import PROVIDERS, ProviderError

PROMPTS = {
 'T01':'A continuous cinematic shot of sunrise over an ocean. Waves move naturally, clouds drift slowly, golden reflections shimmer on the water. Natural ocean sound.',
 'T02':'Close-up of an adult woman speaking one short sentence to camera in a quiet studio. Natural lip synchronization, stable face, continuous shot.',
 'T03':'A professional road cyclist rides a blue road bicycle through a mountain hairpin turn. Side tracking camera, both wheels rotate naturally, realistic balance, golden hour.',
 'T04':'A busy pedestrian crossing in a city. Several people walk past each other without collisions. Fixed wide camera, realistic movement and proportions.',
 'T05':'A hand slowly pours water from a glass jug into a clear glass on a wooden table. Accurate fluid motion, reflections and refraction, macro shot.',
 'T06':'A silver sports car drives along a coastal road at sunrise. Smooth tracking shot, realistic reflections, stable bodywork and wheels.',
 'T07':'An industrial robot arm picks up a small metal gear and inserts it into a matching assembly on a workbench. Precise tool contact and continuous motion.',
 'T08':'A golden retriever runs across a green meadow, changing direction once. Stable anatomy and realistic four-legged motion, natural light.',
 'T09':'Two adults sit opposite each other in a cafe and exchange one short line each. Correct speaker turn-taking, stable faces and hands.',
 'T10':'One continuous forward camera move through a furnished living room, past a sofa and into a bright kitchen. Consistent room geometry, no cuts.'
}

def connect(path):
    p=pathlib.Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p); db.row_factory=sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY, provider TEXT NOT NULL, scene TEXT NOT NULL, repeat INTEGER NOT NULL,
      prompt TEXT NOT NULL, duration INTEGER NOT NULL, rate REAL NOT NULL,
      status TEXT NOT NULL, remote_id TEXT, created REAL NOT NULL, submitted REAL,
      finished REAL, url TEXT, file TEXT, error TEXT, metadata TEXT,
      score REAL, UNIQUE(provider,scene,repeat))''')
    db.commit(); return db

def rate(provider):
    env={'minimax':'MINIMAX_USD_PER_SECOND','flux':'FAL_USD_PER_SECOND','wan':'WAN_USD_PER_SECOND'}[provider]
    default={'minimax':'0.08','flux':'','wan':''}[provider]
    value=os.getenv(env,default)
    if not value: raise ValueError(f'{env} must be set to a verified rate before planning/submission')
    result=float(value)
    if result <= 0: raise ValueError('Rate must be positive')
    return result

def plan(db,models,scenes,repeats,duration):
    if not 5<=duration<=15: raise ValueError('MVP common duration must be 5..15 seconds')
    added=0
    for model in models:
        r=rate(model)
        for scene in scenes:
            for n in range(1,repeats+1):
                key=f'{model}:{scene}:{n}'
                cur=db.execute('''INSERT OR IGNORE INTO jobs(id,provider,scene,repeat,prompt,duration,rate,status,created)
                VALUES (?,?,?,?,?,?,?,?,?)''',(key,model,scene,n,PROMPTS[scene],duration,r,'planned',time.time()))
                added+=cur.rowcount
    db.commit(); return added

def total_reserved(db):
    return db.execute("SELECT COALESCE(SUM(duration*rate),0) FROM jobs WHERE status NOT IN ('planned','dry_run')").fetchone()[0]

def summary(db):
    return [dict(r) for r in db.execute('SELECT provider,status,COUNT(*) count,ROUND(SUM(duration*rate),3) estimated_usd FROM jobs GROUP BY provider,status ORDER BY provider,status')]

def probe(path):
    try:
        data=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],timeout=15))
        streams=data.get('streams',[])
        video=next((s for s in streams if s.get('codec_type')=='video'),{})
        return {'duration':float(data.get('format',{}).get('duration') or 0), 'width':video.get('width'),
                'height':video.get('height'), 'fps':video.get('avg_frame_rate'),
                'audio':any(s.get('codec_type')=='audio' for s in streams),
                'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    except (FileNotFoundError,subprocess.CalledProcessError,subprocess.TimeoutExpired,ValueError):
        return {'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'ffprobe':'unavailable'}

def safe_url(url):
    p=urlparse(url)
    if p.scheme!='https' or not p.hostname or p.username or p.password: raise ValueError('Only public HTTPS video URLs allowed')
    # Restrict to known vendor media domains to prevent SSRF and malicious redirects.
    allowed=('fal.media','fal.ai','minimax.io','minimax.chat','aliyuncs.com','aliyun.com')
    if not any(p.hostname==s or p.hostname.endswith('.'+s) for s in allowed):
        raise ValueError('Untrusted video host: '+p.hostname)
    return url

def download(client,url,path):
    safe_url(url)
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.part')
    try:
        with client.stream('GET',url,follow_redirects=False) as r:
            r.raise_for_status()
            if r.status_code in (301,302,303,307,308): raise ValueError('Redirect not permitted')
            length=0
            with open(temp,'wb') as f:
                for chunk in r.iter_bytes():
                    length+=len(chunk)
                    if length>250*1024*1024: raise ValueError('Video exceeds 250MB limit')
                    f.write(chunk)
        if length<128: raise ValueError('Suspiciously small video')
        with open(temp,'rb') as f:
            head=f.read(32)
        if b'ftyp' not in head[:16]: raise ValueError('Downloaded data does not look like MP4')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return probe(path)

def run(db,live,budget,limit=0,poll_interval=15,output='results/videos',job_id=None):
    if job_id is not None:
        selected=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if selected is None:
            raise ValueError(f'Unknown job ID: {job_id}')
        if selected['status'] not in ('planned','submitting','submitted','running','download_pending'):
            raise ValueError(f'Job {job_id} is not runnable (status={selected["status"]})')
        jobs=[selected]
    else:
        jobs=list(db.execute("SELECT * FROM jobs WHERE status IN ('planned','submitting','submitted','running','download_pending') ORDER BY created,id"))
    if limit: jobs=jobs[:limit]
    if not live:
        print(json.dumps({'mode':'DRY_RUN','jobs':[{'id':r['id'],'cost_usd':round(r['duration']*r['rate'],3)} for r in jobs],
                          'planned_total_usd':round(sum(r['duration']*r['rate'] for r in jobs),3),
                          'already_reserved_usd':round(total_reserved(db),3)},indent=2))
        return
    with httpx.Client(timeout=httpx.Timeout(40,connect=15),follow_redirects=False) as client:
        for job in jobs:
            jid=job['id']; state=job['status']; remote=job['remote_id']
            provider=PROVIDERS[job['provider']](client)
            if state=='submitting' and not remote:
                print(jid,'AMBIGUOUS SUBMISSION: do not resubmit automatically. Check provider dashboard; use attach command.')
                continue
            if state=='planned':
                cost=job['duration']*job['rate']
                if total_reserved(db)+cost > budget+1e-9:
                    print(jid,'SKIPPED: budget ceiling'); continue
                db.execute("UPDATE jobs SET status='submitting' WHERE id=?",(jid,));db.commit()
                try:
                    remote=provider.submit(job['prompt'],job['duration'])
                    db.execute("UPDATE jobs SET status='submitted',remote_id=?,submitted=? WHERE id=?",(remote,time.time(),jid));db.commit()
                except Exception as e:
                    # Network failures may occur after acceptance: keep ambiguous state, never auto-retry.
                    db.execute('UPDATE jobs SET error=? WHERE id=?',(str(e)[:500],jid));db.commit()
                    print(jid,'submission uncertain:',str(e)[:200]);continue
            if state=='download_pending':
                url=job['url']
            else:
                url=None
                while True:
                    try: result=provider.poll(remote)
                    except Exception as e:
                        print(jid,'poll error; recover with next run:',str(e)[:200]);break
                    if result.status=='failed':
                        db.execute("UPDATE jobs SET status='failed',error=?,finished=? WHERE id=?",(result.error,time.time(),jid));db.commit();break
                    if result.status=='succeeded':
                        url=result.url
                        db.execute("UPDATE jobs SET status='download_pending',url=?,finished=? WHERE id=?",(url,time.time(),jid));db.commit();break
                    db.execute("UPDATE jobs SET status='running' WHERE id=?",(jid,));db.commit()
                    time.sleep(poll_interval)
            if url:
                path=pathlib.Path(output)/(jid.replace(':','_')+'.mp4')
                try:
                    meta=download(client,url,path)
                    db.execute("UPDATE jobs SET status='completed',file=?,metadata=?,error=NULL WHERE id=?",(str(path),json.dumps(meta),jid));db.commit()
                    print(jid,'saved',path)
                except Exception as e:
                    db.execute('UPDATE jobs SET error=? WHERE id=?',(str(e)[:500],jid));db.commit()
                    print(jid,'download pending:',str(e)[:200])

def report(db,dest):
    rows=list(db.execute('SELECT * FROM jobs ORDER BY scene,provider,repeat'))
    cards=[]
    for r in rows:
        title=html.escape(r['id'])
        video=''
        if r['file'] and pathlib.Path(r['file']).exists():
            rel=os.path.relpath(pathlib.Path(r['file']).resolve(),pathlib.Path(dest).resolve().parent)
            video=f'<video controls preload="metadata" src="{html.escape(rel,quote=True)}"></video>'
        meta=json.loads(r['metadata'] or '{}')
        elapsed=round(r['finished']-r['submitted'],1) if r['finished'] and r['submitted'] else '—'
        cards.append(f'<article><h3>{title}</h3>{video}<p>Status: {html.escape(r["status"])} · estimated ${r["duration"]*r["rate"]:.2f} · latency {elapsed}s</p><p>Quality score: {r["score"] if r["score"] is not None else "not rated"}</p><small>{html.escape(json.dumps(meta))}</small></article>')
    stats=html.escape(json.dumps(summary(db),indent=2))
    page='''<!doctype html><html lang="en"><meta charset="utf-8"><title>Video Benchmark</title><style>
    body{font:15px system-ui;background:#f5f7fb;color:#172033;margin:3vw}h1{font-size:30px}pre{background:white;padding:18px;overflow:auto;border-radius:12px}
    main{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}article{background:white;border-radius:14px;padding:18px;box-shadow:0 2px 12px #0001;overflow-wrap:anywhere}video{width:100%;border-radius:8px}small{color:#666}</style>
    <h1>Video Benchmark</h1><p>Estimates are not provider invoices. Quality ratings are entered manually.</p><pre>'''+stats+'</pre><main>'+''.join(cards)+'</main></html>'
    p=pathlib.Path(dest);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(page,encoding='utf-8');return p
