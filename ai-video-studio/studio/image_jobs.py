"""Enqueue and execute paid guided character/boundary image generation jobs.

One request maps to one ``image`` Job whose immutable payload carries everything
the worker needs after the API process exits. The provider request, PNG
validation, 16:9 crop, and domain-row writes all happen in the executor, never
in the enqueue path, and never in the legacy ``guided_image_attempts`` table.
"""
from __future__ import annotations

import base64
import hashlib
import os
import subprocess
from uuid import uuid4

import httpx
from dotenv import load_dotenv

from . import core, guided_boundaries, guided_characters, guided_compat, \
    guided_images, guided_workflow, request_jobs


class UnknownRemote(Exception):
    """The provider request may have been sent but its outcome is unknown."""


class PreRequestFailure(Exception):
    """A deterministic failure proven to occur before any provider request."""


class PostResponseFailure(Exception):
    """The provider returned a response but its content was rejected."""


_UNKNOWN_ERRORS = (httpx.HTTPError, OSError, ValueError, KeyError, IndexError, TypeError)


def _api_credentials() -> tuple[str, str]:
    """Resolve the image API key and base URL, or fail deterministically."""
    load_dotenv()
    key = os.getenv('STUDIO_IMAGE_API_KEY') or os.getenv('OPENAI_API_KEY')
    base = os.getenv('STUDIO_IMAGE_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
    if not key or not base.startswith('https://'):
        raise PreRequestFailure('Image API key and HTTPS endpoint required')
    return key, base


def _request_image(key: str, base: str, model: str, prompt: str, *, size: str | None = None) -> bytes:
    """POST an image generation and return the decoded PNG bytes."""
    body = {'model': model, 'prompt': prompt, 'output_format': 'png'}
    if size:
        body['size'] = size
    with httpx.Client(timeout=180, follow_redirects=False) as client:
        response = client.post(base + '/images/generations',
                               headers={'Authorization': 'Bearer ' + key}, json=body)
        response.raise_for_status()
        data = response.json()
    return base64.b64decode(data['data'][0]['b64_json'], validate=True)


def _request_edit(key: str, base: str, model: str, prompt: str, files: list) -> bytes:
    """POST an image edit with reference files and return the decoded PNG bytes."""
    with httpx.Client(timeout=180, follow_redirects=False) as client:
        response = client.post(base + '/images/edits',
                               headers={'Authorization': 'Bearer ' + key},
                               data={'model': model, 'prompt': prompt, 'size': '1536x1024',
                                     'output_format': 'png'},
                               files=files)
        response.raise_for_status()
        data = response.json()
    return base64.b64decode(data['data'][0]['b64_json'], validate=True)


def _request_unknown(fn, *args, **kwargs):
    """Run a provider request, mapping transport/parse failures to UnknownRemote."""
    try:
        return fn(*args, **kwargs)
    except _UNKNOWN_ERRORS as exc:
        raise UnknownRemote(str(exc)) from exc


def execute_character(payload: dict) -> dict:
    """Generate a character reference image and persist it; no legacy rows."""
    pid = payload['project_id']
    character_id = payload['target_id']
    model = payload['model']
    prompt = payload['prompt']
    key, base = _api_credentials()
    raw = _request_unknown(_request_image, key, base, model, prompt)
    if not raw.startswith(guided_images._FORMATS['.png']) or len(raw) > guided_images.MAX_IMAGE_BYTES:
        raise PostResponseFailure('Provider returned an invalid image')
    image_id = uuid4().hex
    target = guided_images._safe_path(pid, image_id, '.png')
    with target.open('xb') as f:
        f.write(raw)
    try:
        result = subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(target),
                                 '-frames:v', '1', '-f', 'null', '-'],
                                capture_output=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise PostResponseFailure('FFmpeg is required to validate image content') from exc
    if result.returncode:
        raise PostResponseFailure('Provider returned an invalid image')
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO guided_character_images VALUES (?,?,?,?,?,?,?)',
                   (image_id, pid, character_id, str(target),
                    hashlib.sha256(raw).hexdigest(), 'generated', core.now()))
        guided_characters._changed(db, pid)
    return {'image_id': image_id}


def execute_boundary(payload: dict) -> dict:
    """Generate a boundary frame, crop it to 16:9, and persist it; no legacy rows."""
    pid = payload['project_id']
    boundary_id = payload['target_id']
    model = payload['model']
    prompt = payload['prompt']
    references = []
    for ref in payload['references']:
        path = guided_images.image_path(pid, ref['image_id'])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref['sha256']:
            raise PreRequestFailure('Reference image changed')
        references.append((ref, path, raw))
    files = None
    if references:
        types = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
        files = [('image[]', (ref['image_id'] + path.suffix, raw, types[path.suffix.lower()]))
                 for ref, path, raw in references]
    key, base = _api_credentials()
    if files:
        data = _request_unknown(_request_edit, key, base, model, prompt, files)
    else:
        data = _request_unknown(_request_image, key, base, model, prompt, size='1536x1024')
    if not data.startswith(guided_images._FORMATS['.png']) or len(data) > guided_images.MAX_IMAGE_BYTES:
        raise PostResponseFailure('Invalid provider image')
    temp = guided_images._safe_path(pid, uuid4().hex + '-raw', '.png')
    with temp.open('xb') as f:
        f.write(data)
    image_id = uuid4().hex
    target = guided_images._safe_path(pid, image_id, '.png')
    try:
        guided_images.crop_to_16x9(temp, target)
    except Exception as exc:
        raise PostResponseFailure(f'Provider image could not be normalized: {str(exc)[:200]}') from exc
    finally:
        temp.unlink(missing_ok=True)
    with core.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        guided_workflow._row(db, pid)
        details = {'boundary_id': boundary_id, 'prompt': prompt, 'references': payload['references']}
        stale = int(not guided_boundaries._boundary_snapshot_current(db, pid, details))
        version_id = guided_boundaries.insert_version(
            db, pid, boundary_id, target, stale=stale, prompt=prompt, model=model,
            references=[(r['id'], r['image_id']) for r in payload['references']],
            version_id=image_id)
        guided_characters._changed(db, pid)
    return {'image_id': version_id}


def _reject_unresolved(projected: dict) -> dict:
    if projected['status'] == 'UNKNOWN':
        raise ValueError('Previous image submission is unresolved')
    return projected


def enqueue_character(pid: str, character_id: str, model: str, budget_usd: float, *,
                      confirm_paid: bool, request_id: str, revision: int) -> dict:
    details = guided_images.preview_character(pid, character_id, model)
    core.require_paid_budget(budget_usd)
    if confirm_paid is not True or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Explicit confirmation and request ID required')
    with core.connect() as db:
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
        role = db.execute('SELECT name,description FROM guided_characters WHERE project_id=? AND id=?',
                          (pid, character_id)).fetchone()
        if not role or (f"Character reference portrait for {role['name']}. "
                        f"{role['description']}. One character, no text.") != details['prompt']:
            raise ValueError('Character changed after preview')
    payload = {
        'target': 'character', 'project_id': pid, 'target_id': character_id,
        'model': model, 'budget_usd': budget_usd, 'request_id': request_id,
        'prompt': details['prompt'],
    }
    job = request_jobs.submit(pid, kind='image', request_id=request_id, payload=payload,
                              budget_usd=budget_usd, target_key=f'character:{character_id}')
    return _reject_unresolved(guided_compat.project_image_job(job))


def enqueue_boundary(pid: str, boundary_id: str, model: str, budget_usd: float, *,
                     confirm_paid: bool, request_id: str, revision: int) -> dict:
    details = guided_boundaries.preview(pid, boundary_id, model)
    core.require_paid_budget(budget_usd)
    if confirm_paid is not True or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError('Explicit confirmation and request ID required')
    with core.connect() as db:
        guided_workflow._revision(guided_workflow._row(db, pid), revision)
    payload = {
        'target': 'boundary', 'project_id': pid, 'target_id': boundary_id,
        'model': model, 'budget_usd': budget_usd, 'request_id': request_id,
        'prompt': details['prompt'], 'position': details['position'],
        'references': details['references'],
    }
    job = request_jobs.submit(pid, kind='image', request_id=request_id, payload=payload,
                              budget_usd=budget_usd, target_key=f'boundary:{boundary_id}')
    return _reject_unresolved(guided_compat.project_image_job(job))
