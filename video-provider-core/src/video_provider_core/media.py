import json, subprocess, hashlib, pathlib
from urllib.parse import urlparse
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

