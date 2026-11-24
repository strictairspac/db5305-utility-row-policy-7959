from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download, page
from src.models.tables import audit, cases, runs
from src.schemas.workflow import CaseForm, LoginForm, ReviewForm
from src.services import domain, workflow
from src.services.security import current_user, login, operator, reviewer

router = APIRouter()


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return workflow.recent(runs)


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return workflow.enqueue(case_id, actor)


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.evidence_summaries()


@router.get('/evidence/{identity}')
def evidence_detail(identity: int, _=Depends(current_user)):
    return workflow.evidence_index(identity)


@router.get('/evidence/{identity}/page')
def evidence_page(identity: int, section: str = Query('逐工单访问矩阵'),
                  size: int | None = Query(None, ge=1, le=1000),
                  offset: int | None = Query(None, ge=0),
                  _=Depends(current_user)):
    return page(identity, section, size, offset)


@router.get('/evidence/{identity}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.post('/evidence/{identity}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)
