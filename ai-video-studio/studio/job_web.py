"""Persistent job and cost API."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import attempts, core, jobs, ledger

router = APIRouter(prefix='/api')


def _checked(pid: str):
    try:
        with core.connect() as db:
            core.project(db, pid)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get('/projects/{pid}/jobs')
def list_jobs(pid: str, status: str | None = None):
    _checked(pid)
    from . import job_runner
    job_runner.reconcile(pid)
    try:
        return jobs.list_for_project(pid, status=status)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/jobs/{jid}')
def get_job(jid: str):
    try:
        data = jobs.get(jid)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    from . import job_runner
    job_runner.reconcile(data['project_id'])
    data = jobs.get(jid)
    data['attempts'] = attempts.list_for_job(jid)
    return data


@router.post('/jobs/{jid}/pause')
def pause_job(jid: str):
    try:
        return jobs.pause(jid)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post('/jobs/{jid}/resume')
def resume_job(jid: str):
    try:
        return jobs.resume(jid)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post('/jobs/{jid}/cancel')
def cancel_job(jid: str):
    try:
        return jobs.cancel(jid)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get('/projects/{pid}/costs')
def costs(pid: str, run_id: str | None = None):
    _checked(pid)
    summary = ledger.summary(pid, run_id)
    summary['note'] = 'Local reservation/estimate; not a provider invoice.'
    return summary
