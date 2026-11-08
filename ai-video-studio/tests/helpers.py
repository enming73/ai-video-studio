import hashlib
import subprocess
from pathlib import Path

from studio import core, guided_boundaries as boundaries, timeline


def make_png(path, size='1536x1024', color='red'):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c={color}:s={size}',
                    '-frames:v', '1', str(path)], check=True)
    return path.read_bytes()


def _color_for(name):
    digest = hashlib.md5(name.encode()).digest()
    return ('red', 'green', 'blue')[digest[0] % 3]


def attach_video(pid, shot, first_id, last_id):
    path = Path(core.HOME) / f'video-{shot["seq"]}.mp4'
    path.write_bytes(b'old-video')
    asset = timeline.import_asset(pid, shot['seq'], str(path))
    timeline.select(pid, shot['seq'], asset)
    with core.connect() as db:
        db.execute("INSERT INTO guided_attempts(id,project_id,shot_id,request_id,prompt,duration,provider,reserved_usd,status,asset_id) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ('a-' + shot['id'], pid, shot['id'], 'r-' + shot['id'], shot['prompt'], shot['duration'],
                    'mock', 0, 'COMPLETED', asset))
        db.execute("INSERT INTO guided_video_provenance(attempt_id,frame_id,frame_sha256,last_frame_id,last_frame_sha256,model) "
                   "VALUES (?,?,?,?,?,?)",
                   ('a-' + shot['id'], first_id, 'f', last_id, 'l', 'mock'))
    return path


def add_version(pid, boundary_id, name, size='1536x1024', stale=False, references=None):
    directory = Path(core.HOME) / pid / 'guided-images'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{name}.png'
    make_png(path, size, color=_color_for(name))
    with core.connect() as db:
        return boundaries.insert_version(db, pid, boundary_id, path, stale=stale,
                                         prompt='p', model='test', references=references or [])
