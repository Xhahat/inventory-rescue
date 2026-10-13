"""Evaluation endpoints: extraction quality on the labelled gold set (background job, latest result persisted)."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Job
from ..services import extraction_eval
from ..services import plans as plan_svc

router = APIRouter()

JOB_KIND = "extraction_eval"


def _job(j: Job | None) -> dict | None:
    if j is None:
        return None
    return {"id": j.id, "kind": j.kind, "status": j.status, "error": j.error, "created_at": j.created_at.isoformat(),
            "finished_at": j.finished_at.isoformat() if j.finished_at else None}


@router.get("/api/eval/extraction")
def get_extraction_eval(db: Session = Depends(get_db)):
    """Latest persisted result plus the most recent job (to show queued / running / failed)."""
    job = db.scalars(select(Job).where(Job.kind == JOB_KIND).order_by(Job.id.desc()).limit(1)).first()
    return {"result": extraction_eval.latest(db), "job": _job(job)}


@router.post("/api/eval/extraction")
def start_extraction_eval(bg: BackgroundTasks, db: Session = Depends(get_db)):
    job = plan_svc.enqueue(db, JOB_KIND, {})
    bg.add_task(plan_svc.run_job, job.id)
    return _job(job)
