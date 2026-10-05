"""社区航线挑战 API：版本化发布 / 飞行记录幂等提交 / 审核 / 排行榜与回放 / 解锁联动。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional

from app.core.database import SessionLocal
from app.schemas import Action, validate_actions
from app.services import physics
from app.services import challenges as ch_svc

router = APIRouter(prefix="/api/challenges")


class MilestoneIn(BaseModel):
    id: Optional[str] = None
    kind: str
    name: str = ""
    planet_id: Optional[str] = None
    dist: Optional[float] = None
    r: Optional[float] = None


class UnlockRuleIn(BaseModel):
    type: str = "none"          # none / builtin_level / challenge / stars_total
    level_id: Optional[int] = None
    challenge_id: Optional[int] = None
    value: Optional[int] = None


class DefinitionIn(BaseModel):
    """一版关卡定义：燃料预算 + 时间限制 + 里程碑。"""
    name: str = ""
    brief: str = ""
    hint: str = ""
    budget_dv: float
    t_max: float
    milestones: List[MilestoneIn] = Field(default_factory=list)


class CreateChallengeIn(DefinitionIn):
    title: str
    author: str = ""
    unlock_rule: Optional[UnlockRuleIn] = None


class VersionIn(DefinitionIn):
    unlock_rule: Optional[UnlockRuleIn] = None  # 缺省保留原条件；{"type":"none"} 清除


class SimIn(BaseModel):
    actions: List[Action] = Field(default_factory=list)
    version: Optional[int] = None  # 缺省游玩当前版本


class SubmitIn(BaseModel):
    """成绩提交：run_id 关联执行档案（服务端结算），submission_id 为幂等键。"""
    run_id: str
    submission_id: Optional[str] = None
    player: Optional[str] = None


class ReviewIn(BaseModel):
    action: str                 # approve / reject
    note: Optional[str] = ""


def _dump(model) -> dict:
    """pydantic v1/v2 兼容的 dict 导出（剔除未提供的字段）。"""
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


def _definition(req: DefinitionIn) -> dict:
    d = _dump(req)
    d["milestones"] = [_dump(m) for m in req.milestones]
    return d


def _sim_dict(r: dict, challenge_id: int, version: int) -> dict:
    return {
        "ok": r["ok"],
        "reason": r["reason"],
        "elapsed_days": r["elapsed_days"],
        "fuel_used": r["fuel_used"],
        "budget_dv": r["budget_dv"],
        "milestones": r["milestones"],
        "events": r["events"],
        "trajectory": r["trajectory"],
        "challenge_id": challenge_id,
        "version": version,
    }


def _error(e: Exception) -> HTTPException:
    if isinstance(e, (ch_svc.ChallengeNotFound, ch_svc.VersionNotFound,
                      ch_svc.RunNotFound, ch_svc.SubmissionNotFound)):
        return HTTPException(404, str(e))
    if isinstance(e, ch_svc.ChallengeLocked):
        return HTTPException(403, str(e))
    if isinstance(e, ch_svc.ReviewConflict):
        return HTTPException(409, str(e))
    return HTTPException(400, str(e))


# ---------- 发布（版本化） ----------

@router.get("")
def list_challenges():
    """挑战列表：当前版本摘要 + 解锁状态 + 本机最佳（已审核）+ 待审数。"""
    with SessionLocal() as db:
        return {"challenges": ch_svc.list_challenges(db)}


@router.post("", status_code=201)
def create_challenge(req: CreateChallengeIn):
    """发布新挑战：校验定义后创建挑战主体与不可变的 v1 版本。"""
    try:
        with SessionLocal() as db:
            return ch_svc.create_challenge(
                db, title=req.title, author=req.author,
                definition=_definition(req),
                unlock_rule=_dump(req.unlock_rule) if req.unlock_rule else None)
    except (ch_svc.ValidationError,) as e:
        raise _error(e)


@router.get("/review_queue")
def review_queue(status: str = "pending", limit: int = 100):
    """全站成绩提交队列（默认待审核），供审核工作流使用。"""
    with SessionLocal() as db:
        return {"submissions": ch_svc.list_submissions(db, status=status, limit=limit)}


@router.get("/submissions/{record_id}")
def get_submission(record_id: int):
    """成绩详情：审核通过后附带动作方案与轨迹（供回放）。"""
    with SessionLocal() as db:
        detail = ch_svc.submission_detail(db, record_id)
    if detail is None:
        raise HTTPException(404, "成绩记录不存在")
    return detail


@router.post("/submissions/{record_id}/review")
def review_submission(record_id: int, req: ReviewIn):
    """审核成绩：通过后进入排行榜、开放回放并联动解锁；驳回则排除。"""
    try:
        with SessionLocal() as db:
            return ch_svc.review(db, record_id=record_id,
                                 action=req.action, note=req.note or "")
    except (ch_svc.ValidationError, ch_svc.SubmissionNotFound,
            ch_svc.ReviewConflict) as e:
        raise _error(e)


@router.get("/{challenge_id}")
def challenge_detail(challenge_id: int):
    """挑战详情：版本列表 + 当前版本完整定义 + 解锁状态。"""
    try:
        with SessionLocal() as db:
            return ch_svc.challenge_detail(db, challenge_id)
    except ch_svc.ChallengeNotFound as e:
        raise _error(e)


@router.post("/{challenge_id}/versions", status_code=201)
def publish_version(challenge_id: int, req: VersionIn):
    """发布新版本：定义落库为不可变版本并设为当前版本（旧版本保留）。"""
    try:
        with SessionLocal() as db:
            kwargs = {}
            if req.unlock_rule is not None:
                kwargs["unlock_rule"] = _dump(req.unlock_rule)
            return ch_svc.publish_version(db, challenge_id,
                                          definition=_definition(req), **kwargs)
    except (ch_svc.ValidationError, ch_svc.ChallengeNotFound) as e:
        raise _error(e)


@router.get("/{challenge_id}/versions/{version}")
def version_detail(challenge_id: int, version: int):
    """指定版本的完整定义（旧版本仍可查看）。"""
    try:
        with SessionLocal() as db:
            return ch_svc.version_detail(db, challenge_id, version)
    except (ch_svc.ChallengeNotFound, ch_svc.VersionNotFound) as e:
        raise _error(e)


# ---------- 飞行记录：预览 / 执行 / 幂等提交 ----------

@router.post("/{challenge_id}/preview")
def preview(challenge_id: int, req: SimIn):
    """实时预览轨迹（不评分、不落库、不校验解锁）。"""
    try:
        with SessionLocal() as db:
            ch, ver = ch_svc.get_playable(db, challenge_id, req.version)
            lv = ch_svc.level_def(ver)
            cid, v_no = ch.id, ver.version
    except (ch_svc.ChallengeNotFound, ch_svc.VersionNotFound) as e:
        raise _error(e)
    r = physics.integrate(lv, validate_actions(req.actions))
    return _sim_dict(r, cid, v_no)


@router.post("/{challenge_id}/run")
def run(challenge_id: int, req: SimIn):
    """执行挑战飞行：服务端结算（含星级），执行档案落库并返回 run_id。

    挑战未解锁时拒绝结算（403）。
    """
    try:
        with SessionLocal() as db:
            ch, ver = ch_svc.get_playable(db, challenge_id, req.version,
                                          enforce_unlock=True)
            lv = ch_svc.level_def(ver)
            actions = validate_actions(req.actions)
            r = physics.integrate(lv, actions)
            stars = physics.stars_for(lv, r["fuel_used"], r["ok"])
            rec = ch_svc.record_run(db, ch, ver, actions, r, stars)
            resp = _sim_dict(r, ch.id, ver.version)
            resp["stars"] = stars
            resp["run_id"] = rec.run_uid
            return resp
    except (ch_svc.ChallengeNotFound, ch_svc.VersionNotFound,
            ch_svc.ChallengeLocked) as e:
        raise _error(e)


@router.post("/{challenge_id}/submit")
def submit(challenge_id: int, req: SubmitIn):
    """提交成绩：幂等键去重，落库为待审核；审核通过后才上榜/回放/联动解锁。"""
    try:
        with SessionLocal() as db:
            return ch_svc.submit(db, challenge_id=challenge_id,
                                 run_id=req.run_id,
                                 submission_id=req.submission_id,
                                 player=req.player)
    except (ch_svc.RunNotFound, ch_svc.RunChallengeMismatch) as e:
        raise _error(e)


@router.get("/{challenge_id}/submissions")
def challenge_submissions(challenge_id: int, status: str = "pending",
                          limit: int = 50):
    """某挑战的成绩提交列表（默认待审核队列）。"""
    try:
        with SessionLocal() as db:
            ch_svc.get_playable(db, challenge_id)  # 存在性校验
            return {"submissions": ch_svc.list_submissions(
                db, challenge_id=challenge_id, status=status, limit=limit)}
    except ch_svc.ChallengeNotFound as e:
        raise _error(e)


@router.get("/{challenge_id}/leaderboard")
def get_leaderboard(challenge_id: int, version: Optional[int] = None,
                    limit: int = 50):
    """排行榜：仅审核通过的成绩，按版本结算，每名玩家取最佳一条。"""
    try:
        with SessionLocal() as db:
            return ch_svc.leaderboard(db, challenge_id, version=version,
                                      limit=limit)
    except (ch_svc.ChallengeNotFound, ch_svc.VersionNotFound) as e:
        raise _error(e)
