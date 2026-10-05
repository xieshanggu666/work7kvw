"""社区航线挑战服务：版本化发布 / 幂等提交 / 审核 / 申诉复核 / 排行榜 / 回放 / 解锁联动。

设计要点：
- 版本化：每次发布生成不可变的 ChallengeVersion（预算 + 里程碑 + 时间限制），
  飞行记录与成绩都锚定具体版本；排行榜按版本结算，旧版本仍可查看与回放。
- 幂等结算：/run 落库执行档案（ChallengeRun），/submit 以 submission_id 为幂等键
  关联档案落库成绩；重复提交（双击/重试/多标签页）返回首个结果，不重复计数。
- 审核联动：成绩默认 pending；审核通过（approved）后才进入排行榜、开放轨迹回放，
  并计入解锁条件。解锁状态按规则实时求值，审核通过即自动联动，无需额外迁移。
- 申诉与复核：玩家/审核席可对终审成绩发起幂等申诉（appeal_id）；管理员复核
  （与原审同人回避）后 uphold 翻案 —— 驳回翻通过则恢复上榜/回放/解锁，
  通过翻 revoked 则撤榜并回滚排行榜/解锁；deny 维持原判。排行榜、回放与解锁
  全部派生自 review_status，翻案即联动，无需级联修补。全部动作写入只追加的
  ChallengeReviewEvent，形成「提交 → 初审 → 申诉 → 复核」可追溯链路。
- 历史兼容：旧成绩无审核员身份/申诉字段时按终审状态正常参与排行榜与解锁，
  事件流从迁移后新动作开始追加；复核同人回避对无原审身份的行自动豁免。
- 并发安全：模块级锁串行化提交/审核/申诉临界区（SQLite 单写者），唯一约束兜底。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import List, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from app.models import (Challenge, ChallengeReviewEvent, ChallengeRun,
                        ChallengeSubmission, ChallengeVersion, LevelScore)
from app.services import physics
from app.services.levels import LEVEL_BY_ID

# SQLite 单写者：序列化"查重-插入 / 审核/申诉状态迁移"临界区。
_LOCK = threading.Lock()

_KEEP_RULE = object()  # publish_version 的"保留原解锁条件"哨兵

_PLANET_IDS = {b["id"] for b in physics.BODIES if b["id"] != "sun"}
_MILESTONE_KINDS = {"proximity", "radius", "assist_capture", "assist_then_radius", "escape"}
_REVIEW_ACTIONS = {"approve": "approved", "reject": "rejected"}
_REVIEW_STATUSES = {"pending", "approved", "rejected", "revoked"}
_REVIEWER_ROLES = {"reviewer", "admin"}
_APPEAL_STATUSES = {"pending", "upheld", "denied"}
# 允许发起申诉的角色：player 仅可申诉本人被驳回的成绩
DEFAULT_PLAYER = "匿名飞行员"


# ---------- 异常 ----------

class ChallengeNotFound(KeyError):
    """挑战不存在。"""


class VersionNotFound(KeyError):
    """挑战版本不存在。"""


class SubmissionNotFound(KeyError):
    """成绩提交记录不存在。"""


class ValidationError(ValueError):
    """关卡定义 / 解锁条件 / 请求参数校验失败。"""


class RunNotFound(KeyError):
    """run_id 对应的挑战执行档案不存在。"""


class RunChallengeMismatch(ValueError):
    """执行档案与提交的挑战不一致。"""


class ChallengeLocked(PermissionError):
    """挑战未解锁，不能结算飞行记录。"""


class ReviewConflict(RuntimeError):
    """成绩已终审 / 已申诉，当前状态不允许该操作。"""


class ReviewerPermission(PermissionError):
    """审核权限不足或身份校验失败（自审回避 / 角色不符 / 申诉非本人）。"""


class AppealNotAllowed(RuntimeError):
    """当前状态不允许发起申诉（如成绩仍在待审核）。"""


class AppealConflict(RuntimeError):
    """申诉已存在或已复核，不能重复申诉/复核。"""


# ---------- 定义校验 ----------

def _clean_text(s, max_len: int, field: str, allow_empty: bool = False) -> str:
    s = (s or "").strip()
    if not s and not allow_empty:
        raise ValidationError(f"{field}不能为空")
    if len(s) > max_len:
        raise ValidationError(f"{field}过长（≤{max_len} 字）")
    return s


def _bounded(v, lo: float, hi: float, field: str) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}必须是数字")
    if not lo <= v <= hi:
        raise ValidationError(f"{field}需在 {lo}~{hi} 之间")
    return v


def _planet(pid) -> str:
    if pid not in _PLANET_IDS:
        raise ValidationError(f"未知行星: {pid}")
    return pid


def validate_milestones(ms_list) -> List[dict]:
    """校验并规范化里程碑定义（与物理引擎支持的判定类型对齐）。"""
    if not isinstance(ms_list, list) or not 1 <= len(ms_list) <= 5:
        raise ValidationError("里程碑需 1~5 个")
    out, seen = [], set()
    for i, m in enumerate(ms_list, 1):
        kind = (m or {}).get("kind")
        if kind not in _MILESTONE_KINDS:
            raise ValidationError(f"未知里程碑类型: {kind}")
        mid = str(m.get("id") or f"m{i}")
        if mid in seen:
            raise ValidationError(f"里程碑 id 重复: {mid}")
        seen.add(mid)
        item = {"id": mid, "kind": kind,
                "name": _clean_text(m.get("name"), 40, "里程碑名称")}
        if kind == "proximity":
            item["planet_id"] = _planet(m.get("planet_id"))
            item["dist"] = _bounded(m.get("dist"), 0.001, 5.0, "飞掠距离")
        elif kind == "radius":
            item["r"] = _bounded(m.get("r"), 0.2, 50.0, "目标半径")
        elif kind == "assist_capture":
            item["planet_id"] = _planet(m.get("planet_id"))
        elif kind == "assist_then_radius":
            item["planet_id"] = _planet(m.get("planet_id"))
            item["r"] = _bounded(m.get("r"), 0.2, 50.0, "目标半径")
        elif kind == "escape":
            item["r"] = _bounded(m.get("r"), 1.0, 100.0, "逃逸边界")
        out.append(item)
    return out


def validate_definition(d: dict) -> dict:
    """校验一版关卡定义：预算 / 时间限制 / 里程碑。"""
    d = d or {}
    return {
        "name": _clean_text(d.get("name"), 40, "版本名", allow_empty=True),
        "brief": _clean_text(d.get("brief"), 500, "简介", allow_empty=True),
        "hint": _clean_text(d.get("hint"), 500, "提示", allow_empty=True),
        "budget_dv": _bounded(d.get("budget_dv"), 0.0002, 0.05, "燃料预算"),
        "t_max": _bounded(d.get("t_max"), 30, 20000, "时间限制"),
        "milestones": validate_milestones(d.get("milestones") or []),
    }


def validate_unlock_rule(db, rule, self_id: Optional[int] = None) -> Optional[dict]:
    """校验解锁条件；{"type":"none"} 与 None 都表示无条件（返回 None）。"""
    if not rule or rule.get("type") in (None, "none"):
        return None
    t = rule.get("type")
    if t == "builtin_level":
        lid = rule.get("level_id")
        if lid not in LEVEL_BY_ID:
            raise ValidationError(f"未知内置关卡: {lid}")
        return {"type": t, "level_id": lid}
    if t == "challenge":
        cid = rule.get("challenge_id")
        if self_id is not None and cid == self_id:
            raise ValidationError("解锁条件不能引用挑战自身")
        if db.query(Challenge).filter(Challenge.id == cid).first() is None:
            raise ValidationError(f"前置挑战不存在: {cid}")
        return {"type": t, "challenge_id": cid}
    if t == "stars_total":
        return {"type": t, "value": int(_bounded(rule.get("value"), 1, 99, "所需星数"))}
    raise ValidationError(f"未知解锁条件类型: {t}")


# ---------- 发布（版本化） ----------

def _add_version(db, challenge_id: int, version: int, defn: dict) -> ChallengeVersion:
    ver = ChallengeVersion(
        challenge_id=challenge_id, version=version,
        name=defn["name"], brief=defn["brief"], hint=defn["hint"],
        budget_dv=defn["budget_dv"], t_max=defn["t_max"],
        milestones_json=json.dumps(defn["milestones"], ensure_ascii=False),
        created_at=time.time(),
    )
    db.add(ver)
    return ver


def create_challenge(db, *, title: str, author: str, definition: dict,
                     unlock_rule=None) -> dict:
    """发布新挑战：创建挑战主体 + 不可变的 v1 版本。"""
    title = _clean_text(title, 40, "挑战标题")
    author = _clean_text(author, 24, "设计者署名", allow_empty=True) or "匿名设计者"
    defn = validate_definition(definition)
    rule = validate_unlock_rule(db, unlock_rule)
    ch = Challenge(title=title, author=author, status="published", current_version=1,
                   unlock_rule_json=json.dumps(rule) if rule else "",
                   created_at=time.time())
    db.add(ch)
    db.flush()  # 取 challenge.id
    _add_version(db, ch.id, 1, defn)
    db.commit()
    return challenge_detail(db, ch.id)


def publish_version(db, challenge_id: int, definition: dict,
                    unlock_rule=_KEEP_RULE) -> dict:
    """发布新版本：定义校验后落库为不可变版本并设为当前版本。

    unlock_rule 缺省（未传）时保留原解锁条件；显式传 {"type":"none"} 可清除。
    """
    ch = _get_challenge(db, challenge_id)
    defn = validate_definition(definition)
    _add_version(db, ch.id, ch.current_version + 1, defn)
    ch.current_version += 1
    if unlock_rule is not _KEEP_RULE:
        rule = validate_unlock_rule(db, unlock_rule, self_id=ch.id)
        ch.unlock_rule_json = json.dumps(rule) if rule else ""
    db.commit()
    return challenge_detail(db, ch.id)


# ---------- 查询与解锁 ----------

def _get_challenge(db, challenge_id: int) -> Challenge:
    ch = db.query(Challenge).filter(Challenge.id == challenge_id).first()
    if ch is None or ch.status == "archived":
        raise ChallengeNotFound(f"挑战不存在: {challenge_id}")
    return ch


def _get_version(db, challenge_id: int, version: int) -> ChallengeVersion:
    ver = (db.query(ChallengeVersion)
             .filter(ChallengeVersion.challenge_id == challenge_id,
                     ChallengeVersion.version == version)
             .first())
    if ver is None:
        raise VersionNotFound(f"挑战 #{challenge_id} 没有版本 v{version}")
    return ver


def level_def(ver: ChallengeVersion) -> dict:
    """把版本行转成物理引擎可用的关卡定义。"""
    return {
        "id": ver.challenge_id,
        "name": ver.name,
        "milestones": json.loads(ver.milestones_json),
        "budget_dv": ver.budget_dv,
        "t_max": ver.t_max,
    }


def _builtin_stars(db) -> dict:
    return {s.level_id: s.stars for s in db.query(LevelScore).all()}


def _challenge_stars(db) -> dict:
    """每个挑战已审核通过的最佳星数（跨版本）。"""
    out = {}
    rows = (db.query(ChallengeSubmission)
              .filter(ChallengeSubmission.review_status == "approved")
              .all())
    for r in rows:
        out[r.challenge_id] = max(out.get(r.challenge_id, 0), r.stars)
    return out


def _unlock_state(rule: Optional[dict], builtin: dict, ch_stars: dict,
                  total_stars: int, titles: dict) -> Tuple[bool, Optional[str]]:
    """求值解锁条件，返回 (是否解锁, 未解锁时的说明)。"""
    if not rule:
        return True, None
    t = rule["type"]
    if t == "builtin_level":
        ok = builtin.get(rule["level_id"], 0) >= 1
        return ok, None if ok else f"通关内置第 {rule['level_id']} 关后解锁"
    if t == "challenge":
        ok = ch_stars.get(rule["challenge_id"], 0) >= 1
        dep = titles.get(rule["challenge_id"], f"#{rule['challenge_id']}")
        return ok, None if ok else f"通关挑战「{dep}」后解锁"
    if t == "stars_total":
        ok = total_stars >= rule["value"]
        return ok, None if ok else f"累计星数 ≥ {rule['value']} 后解锁"
    return True, None


def _unlock_context(db):
    builtin = _builtin_stars(db)
    ch_stars = _challenge_stars(db)
    total = sum(builtin.values()) + sum(ch_stars.values())
    titles = {c.id: c.title for c in db.query(Challenge).all()}
    return builtin, ch_stars, total, titles


def _rule_of(ch: Challenge) -> Optional[dict]:
    return json.loads(ch.unlock_rule_json) if ch.unlock_rule_json else None


def _best_approved(db) -> dict:
    """每个挑战本机最佳已审核成绩（星数 → 燃料 → 先到优先）。"""
    rows = (db.query(ChallengeSubmission)
              .filter(ChallengeSubmission.review_status == "approved")
              .order_by(ChallengeSubmission.stars.desc(),
                        ChallengeSubmission.fuel_used.asc(),
                        ChallengeSubmission.id.asc())
              .all())
    best = {}
    for r in rows:
        if r.challenge_id not in best:
            best[r.challenge_id] = {
                "stars": r.stars, "fuel_used": r.fuel_used,
                "elapsed_days": r.elapsed_days, "record_id": r.id,
            }
    return best


def list_challenges(db) -> List[dict]:
    """挑战列表：当前版本摘要 + 解锁状态 + 本机最佳（已审核）+ 待审数。"""
    rows = (db.query(Challenge)
              .filter(Challenge.status != "archived")
              .order_by(Challenge.id)
              .all())
    vers = {}
    for v in db.query(ChallengeVersion).all():
        vers.setdefault(v.challenge_id, {})[v.version] = v
    pend = {}
    for r in (db.query(ChallengeSubmission)
                .filter(ChallengeSubmission.review_status == "pending").all()):
        pend[r.challenge_id] = pend.get(r.challenge_id, 0) + 1
    builtin, ch_stars, total, titles = _unlock_context(db)
    best = _best_approved(db)
    out = []
    for ch in rows:
        v = vers.get(ch.id, {}).get(ch.current_version)
        unlocked, desc = _unlock_state(_rule_of(ch), builtin, ch_stars, total, titles)
        out.append({
            "id": ch.id, "title": ch.title, "author": ch.author,
            "current_version": ch.current_version,
            "version_count": len(vers.get(ch.id, {})),
            "created_at": ch.created_at,
            "brief": v.brief if v else "",
            "budget_dv": v.budget_dv if v else 0.0,
            "t_max": v.t_max if v else 0.0,
            "milestone_count": len(json.loads(v.milestones_json)) if v else 0,
            "unlocked": unlocked, "unlock_desc": desc,
            "best": best.get(ch.id),
            "pending_count": pend.get(ch.id, 0),
        })
    return out


def challenge_detail(db, challenge_id: int) -> dict:
    """挑战详情：全部版本列表 + 当前版本完整定义 + 解锁状态。"""
    ch = _get_challenge(db, challenge_id)
    vers = (db.query(ChallengeVersion)
              .filter(ChallengeVersion.challenge_id == ch.id)
              .order_by(ChallengeVersion.version)
              .all())
    cur = next((v for v in vers if v.version == ch.current_version), vers[-1])
    builtin, ch_stars, total, titles = _unlock_context(db)
    rule = _rule_of(ch)
    unlocked, desc = _unlock_state(rule, builtin, ch_stars, total, titles)
    return {
        "id": ch.id, "title": ch.title, "author": ch.author,
        "status": ch.status, "current_version": ch.current_version,
        "created_at": ch.created_at,
        "unlock_rule": rule, "unlocked": unlocked, "unlock_desc": desc,
        "versions": [{
            "version": v.version, "name": v.name,
            "budget_dv": v.budget_dv, "t_max": v.t_max,
            "milestone_count": len(json.loads(v.milestones_json)),
            "created_at": v.created_at,
        } for v in vers],
        "current": {
            "version": cur.version, "name": cur.name, "brief": cur.brief,
            "hint": cur.hint, "budget_dv": cur.budget_dv, "t_max": cur.t_max,
            "milestones": json.loads(cur.milestones_json),
        },
        "best": _best_approved(db).get(ch.id),
    }


def version_detail(db, challenge_id: int, version: int) -> dict:
    """指定版本的完整定义（查看旧版本 / 按旧版本游玩）。"""
    ch = _get_challenge(db, challenge_id)
    ver = _get_version(db, challenge_id, version)
    return {
        "challenge_id": ch.id, "title": ch.title, "author": ch.author,
        "version": ver.version, "name": ver.name, "brief": ver.brief,
        "hint": ver.hint, "budget_dv": ver.budget_dv, "t_max": ver.t_max,
        "milestones": json.loads(ver.milestones_json),
        "is_current": ver.version == ch.current_version,
        "created_at": ver.created_at,
    }


def get_playable(db, challenge_id: int, version: Optional[int] = None,
                 enforce_unlock: bool = False) -> Tuple[Challenge, ChallengeVersion]:
    """取挑战 + 版本行；enforce_unlock 时未解锁抛 ChallengeLocked（结算类接口用）。"""
    ch = _get_challenge(db, challenge_id)
    ver = _get_version(db, challenge_id, version or ch.current_version)
    if enforce_unlock:
        builtin, ch_stars, total, titles = _unlock_context(db)
        unlocked, desc = _unlock_state(_rule_of(ch), builtin, ch_stars, total, titles)
        if not unlocked:
            raise ChallengeLocked(desc or "挑战未解锁")
    return ch, ver


# ---------- 飞行记录：执行档案 + 幂等提交 ----------

def _clean_actor_id(v) -> str:
    return _clean_text(v, 64, "操作人身份", allow_empty=True)


def _actor(actor_id=None, actor_name=None, role="reviewer") -> Tuple[str, str, str]:
    """规范化操作人三元组 (id, 署名, 角色)；本地单机允许匿名审核席。"""
    aid = _clean_actor_id(actor_id) or "local-reviewer"
    name = _clean_text(actor_name, 24, "操作人署名", allow_empty=True) or "本地审核席"
    if role not in _REVIEWER_ROLES:
        raise ValidationError(f"未知审核角色: {role}")
    return aid, name, role


def _append_event(db, rec: ChallengeSubmission, *, action: str,
                  actor: str, actor_name: str, actor_role: str,
                  detail: dict) -> ChallengeReviewEvent:
    """向只追加的事件流追加一条（seq 取该成绩现有最大值 + 1）。"""
    nxt = (db.query(ChallengeReviewEvent)
             .filter(ChallengeReviewEvent.submission_id == rec.id)
             .count()) + 1
    ev = ChallengeReviewEvent(
        submission_id=rec.id, seq=nxt,
        actor=actor, actor_name=actor_name, actor_role=actor_role,
        action=action, detail_json=json.dumps(detail, ensure_ascii=False),
        created_at=time.time())
    db.add(ev)
    return ev


def _appeal_block(rec: ChallengeSubmission) -> Optional[dict]:
    """申诉信息块（无申诉时为 None）。"""
    if not rec.appeal_status:
        return None
    return {
        "appeal_id": rec.appeal_id,
        "status": rec.appeal_status,
        "reason": rec.appeal_reason,
        "appealed_by": rec.appealed_by,
        "appealed_by_name": rec.appealed_by_name,
        "appealed_at": rec.appealed_at,
        "note": rec.appeal_note,
        "reviewed_by": rec.appeal_reviewed_by,
        "reviewed_by_name": rec.appeal_reviewed_by_name,
        "reviewed_at": rec.appeal_reviewed_at,
    }


def record_run(db, ch: Challenge, ver: ChallengeVersion, actions: List[dict],
               result: dict, stars: int) -> ChallengeRun:
    """把一次挑战飞行的完整结算落库，返回执行档案。"""
    run = ChallengeRun(
        run_uid=uuid.uuid4().hex,
        challenge_id=ch.id, version=ver.version,
        ok=1 if result["ok"] else 0,
        reason=result["reason"], stars=stars,
        fuel_used=result["fuel_used"], elapsed_days=result["elapsed_days"],
        actions_json=json.dumps(actions, ensure_ascii=False),
        trajectory_json=json.dumps(result["trajectory"]),
        events_json=json.dumps(result["events"], ensure_ascii=False),
        milestones_json=json.dumps(result["milestones"], ensure_ascii=False),
        created_at=time.time(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _submitted_response(rec: ChallengeSubmission, duplicated: bool) -> dict:
    return {
        "saved": True,
        "duplicated": duplicated,
        "record_id": rec.id,
        "challenge_id": rec.challenge_id,
        "version": rec.version,
        "review_status": rec.review_status,
        "appeal_status": rec.appeal_status or None,
        "stars": rec.stars,
    }


def submit(db, *, challenge_id: int, run_id: str, submission_id: Optional[str],
           player: Optional[str], player_id: Optional[str] = None) -> dict:
    """落库一条挑战成绩（默认待审核）。

    幂等：submission_id 已存在时直接返回首个提交的结果（duplicated=True），
    不重复落库、不重复进入审核队列。player_id 为玩家稳定身份（申诉归属/自审回避），
    缺省时仅用署名字符串（历史行为不变）。
    """
    with _LOCK:
        # 1) 幂等重放：同一提交已受理过 → 返回首个结果
        if submission_id:
            dup = (db.query(ChallengeSubmission)
                     .filter(ChallengeSubmission.submission_id == submission_id)
                     .first())
            if dup is not None:
                return _submitted_response(dup, duplicated=True)

        # 2) 关联执行档案：成绩以服务端结算为准，且必须属于本挑战
        run = (db.query(ChallengeRun)
                 .filter(ChallengeRun.run_uid == run_id)
                 .first())
        if run is None:
            raise RunNotFound(f"执行记录不存在: {run_id}")
        if run.challenge_id != challenge_id:
            raise RunChallengeMismatch("执行记录与挑战不匹配")

        # 3) 落库成绩记录（唯一约束兜底并发重复）
        player_name = (player or "").strip()[:24] or DEFAULT_PLAYER
        pid = _clean_text(player_id, 64, "玩家身份", allow_empty=True) or None
        rec = ChallengeSubmission(
            submission_id=submission_id or uuid.uuid4().hex,
            run_id=run.id,
            challenge_id=run.challenge_id,
            version=run.version,
            player=player_name,
            player_id=pid,
            stars=run.stars,
            fuel_used=run.fuel_used,
            elapsed_days=run.elapsed_days,
            review_status="pending",
            created_at=time.time(),
        )
        db.add(rec)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            dup = (db.query(ChallengeSubmission)
                     .filter(ChallengeSubmission.submission_id == rec.submission_id)
                     .first())
            return _submitted_response(dup, duplicated=True)
        _append_event(db, rec, action="submit",
                      actor=pid or "", actor_name=player_name,
                      actor_role="player",
                      detail={"run_id": run.id, "version": run.version,
                              "submission_id": rec.submission_id})
        db.commit()
        return _submitted_response(rec, duplicated=False)


# ---------- 审核 / 申诉 / 复核 ----------

def _review_payload(rec: ChallengeSubmission, duplicated: bool) -> dict:
    return {
        "record_id": rec.id,
        "challenge_id": rec.challenge_id,
        "version": rec.version,
        "review_status": rec.review_status,
        "appeal_status": rec.appeal_status or None,
        "duplicated": duplicated,
    }


def review(db, *, record_id: int, action: str, note: str = "",
           reviewer_id: Optional[str] = None,
           reviewer_name: Optional[str] = None,
           reviewer_role: str = "reviewer") -> dict:
    """初审状态机：pending → approved / rejected（审核席权限 + 自审回避）。

    幂等：重复同一终审动作返回当前状态（duplicated=True）；
    已终审（含复核撤榜 revoked）的成绩再作相反动作 → ReviewConflict(409)。
    历史成绩无 player_id 时不做自审校验（兼容旧审核流程）。
    """
    if action not in _REVIEW_ACTIONS:
        raise ValidationError(f"未知审核动作: {action}")
    aid, name, role = _actor(reviewer_id, reviewer_name, reviewer_role)
    target = _REVIEW_ACTIONS[action]
    with _LOCK:
        rec = (db.query(ChallengeSubmission)
                 .filter(ChallengeSubmission.id == record_id)
                 .first())
        if rec is None:
            raise SubmissionNotFound(f"成绩记录不存在: {record_id}")
        if rec.review_status == target:
            return _review_payload(rec, duplicated=True)
        if rec.review_status != "pending":
            raise ReviewConflict(
                f"成绩已审核（{rec.review_status}），不能重复审核")
        # 自审回避：提交者本人不能审核自己的成绩（历史行无 player_id 则豁免）
        if rec.player_id and rec.player_id == aid:
            raise ReviewerPermission("不能审核自己提交的成绩，请换审核员处理")
        note = (note or "").strip()[:200]
        rec.review_status = target
        rec.review_note = note
        rec.reviewed_by = aid
        rec.reviewed_by_name = name
        rec.reviewed_at = time.time()
        _append_event(db, rec, action="review",
                      actor=aid, actor_name=name, actor_role=role,
                      detail={"action": action, "result": target, "note": note})
        db.commit()
        return _review_payload(rec, duplicated=False)


def appeal(db, *, record_id: int, reason: str,
           appeal_id: Optional[str] = None,
           player_id: Optional[str] = None,
           player_name: Optional[str] = None,
           role: str = "player") -> dict:
    """对终审成绩发起申诉：approved/rejected → appeal pending。

    权限：player 仅可申诉本人（player_id 一致）被驳回（rejected/revoked）的成绩；
    reviewer/admin 可代为申诉任意终审成绩（含对上榜成绩提撤榜复核）。
    幂等：同一 appeal_id 重复到达返回首个申诉（duplicated=True），不重复建单；
    成绩仍待审 → AppealNotAllowed；已有/已决申诉 → AppealConflict。
    """
    reason = _clean_text(reason, 500, "申诉理由")
    aid = _clean_text(player_id, 64, "操作人身份", allow_empty=True)
    raw_name = (player_name or "").strip()
    name = raw_name[:24] or "匿名飞行员"
    is_staff = role in _REVIEWER_ROLES
    if not is_staff and role != "player":
        raise ValidationError(f"未知申诉角色: {role}")
    if not is_staff and not aid:
        raise ReviewerPermission("玩家申诉需要提供稳定身份（player_id）")

    with _LOCK:
        # 1) 幂等重放
        if appeal_id:
            exist = (db.query(ChallengeSubmission)
                       .filter(ChallengeSubmission.appeal_id == appeal_id)
                       .first())
            if exist is not None:
                return _appeal_payload(exist, duplicated=True)

        rec = (db.query(ChallengeSubmission)
                 .filter(ChallengeSubmission.id == record_id)
                 .first())
        if rec is None:
            raise SubmissionNotFound(f"成绩记录不存在: {record_id}")

        # 2) 状态门禁（成绩状态先于归属判断）
        if rec.review_status == "pending":
            raise AppealNotAllowed("成绩仍在初审中，不能申诉")
        if rec.appeal_status == "pending":
            raise AppealConflict("该成绩已有待复核的申诉，请勿重复发起")
        if rec.appeal_status in _APPEAL_STATUSES:
            raise AppealConflict(
                f"该成绩已申诉并复核（{rec.appeal_status}），不能再次申诉")

        # 3) 权限与归属
        if not is_staff:
            if not rec.player_id or rec.player_id != aid:
                raise ReviewerPermission("只能对自己提交的成绩发起申诉")
            if rec.review_status not in ("rejected", "revoked"):
                raise ReviewerPermission(
                    "玩家仅可对被驳回的成绩申诉；上榜成绩异议请由审核席发起")
        else:
            aid = aid or "local-reviewer"
            name = name if raw_name else "本地审核席"

        key = _clean_text(appeal_id, 64, "申诉幂等键", allow_empty=True) or uuid.uuid4().hex
        rec.appeal_id = key
        rec.appeal_status = "pending"
        rec.appeal_reason = reason
        rec.appealed_by = aid
        rec.appealed_by_name = name
        rec.appealed_at = time.time()
        rec.appeal_note = ""
        rec.appeal_reviewed_by = None
        rec.appeal_reviewed_by_name = None
        rec.appeal_reviewed_at = None
        _append_event(db, rec, action="appeal",
                      actor=aid, actor_name=name, actor_role=role,
                      detail={"appeal_id": key, "reason": reason,
                              "from_status": rec.review_status})
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            dup = (db.query(ChallengeSubmission)
                     .filter(ChallengeSubmission.appeal_id == key)
                     .first())
            return _appeal_payload(dup, duplicated=True)
        db.commit()
        return _appeal_payload(rec, duplicated=False)


def _appeal_payload(rec: ChallengeSubmission, duplicated: bool) -> dict:
    return {
        "record_id": rec.id,
        "challenge_id": rec.challenge_id,
        "version": rec.version,
        "review_status": rec.review_status,
        "appeal_id": rec.appeal_id,
        "appeal_status": rec.appeal_status,
        "duplicated": duplicated,
    }


def appeal_rule(db, *, record_id: int, decision: str, note: str = "",
                reviewer_id: Optional[str] = None,
                reviewer_name: Optional[str] = None,
                reviewer_role: str = "admin") -> dict:
    """管理员复核申诉：upheld（翻案）/ denied（维持原判）。

    权限：仅 reviewer_role=admin；且复核人不得与原审审核员同人（历史成绩无
    原审身份时豁免）。翻案直接翻转 review_status —— rejected→approved 恢复
    上榜/回放/解锁；approved→revoked 撤榜并回滚排行榜/解锁。
    幂等：对同一结论重复裁决返回当前状态（duplicated=True）；
    申诉未决以外的状态 → AppealConflict(409)。
    """
    if decision not in ("upheld", "denied"):
        raise ValidationError(f"未知复核结论: {decision}")
    if reviewer_role != "admin":
        raise ReviewerPermission("申诉复核仅管理员（admin）可操作")
    aid, name, role = _actor(reviewer_id, reviewer_name, reviewer_role)
    note = (note or "").strip()[:200]
    with _LOCK:
        rec = (db.query(ChallengeSubmission)
                 .filter(ChallengeSubmission.id == record_id)
                 .first())
        if rec is None:
            raise SubmissionNotFound(f"成绩记录不存在: {record_id}")
        if rec.appeal_status == decision:
            return _appeal_payload(rec, duplicated=True)
        if rec.appeal_status != "pending":
            raise AppealConflict("该成绩没有待复核的申诉（或已复核）")
        # 同人回避：复核管理员不能就是原审审核员（历史行 reviewed_by 为空则豁免）
        if rec.reviewed_by and rec.reviewed_by == aid:
            raise ReviewerPermission("复核管理员不能与原审审核员为同一人")

        before = rec.review_status
        if decision == "upheld":
            # 翻案：驳回 → 通过（恢复）；通过/撤销 → revoked（撤榜回滚）
            after = "approved" if before == "rejected" else "revoked"
            rec.review_status = after
        rec.appeal_status = "upheld" if decision == "upheld" else "denied"
        rec.appeal_note = note
        rec.appeal_reviewed_by = aid
        rec.appeal_reviewed_by_name = name
        rec.appeal_reviewed_at = time.time()
        _append_event(db, rec, action="appeal_rule",
                      actor=aid, actor_name=name, actor_role="admin",
                      detail={"decision": decision, "note": note,
                              "status_before": before,
                              "status_after": rec.review_status})
        db.commit()
        return _appeal_payload(rec, duplicated=False)


def appeal_timeline(db, record_id: int) -> Optional[List[dict]]:
    """某条成绩的完整追溯链路：提交 → 初审 → 申诉 → 复核（按序）。"""
    rec = (db.query(ChallengeSubmission)
             .filter(ChallengeSubmission.id == record_id)
             .first())
    if rec is None:
        return None
    rows = (db.query(ChallengeReviewEvent)
              .filter(ChallengeReviewEvent.submission_id == rec.id)
              .order_by(ChallengeReviewEvent.seq)
              .all())
    return [{
        "seq": e.seq,
        "action": e.action,
        "actor": e.actor,
        "actor_name": e.actor_name,
        "actor_role": e.actor_role,
        "detail": json.loads(e.detail_json),
        "created_at": e.created_at,
    } for e in rows]


def list_submissions(db, *, challenge_id: Optional[int] = None,
                     status: str = "pending", limit: int = 50) -> List[dict]:
    """成绩提交列表：默认待初审队列（审核工作流），可按挑战/状态过滤。

    status 支持 pending/approved/rejected/revoked/all；申诉中的记录仍保留在其
    终审状态列表里，另用 appeal_status 字段标注，供界面提示。
    """
    q = db.query(ChallengeSubmission)
    if challenge_id is not None:
        q = q.filter(ChallengeSubmission.challenge_id == challenge_id)
    if status in _REVIEW_STATUSES:
        q = q.filter(ChallengeSubmission.review_status == status)
    rows = (q.order_by(ChallengeSubmission.id.desc())
             .limit(max(1, min(200, limit)))
             .all())
    titles = {c.id: c.title for c in db.query(Challenge).all()}
    return [{
        "record_id": r.id,
        "challenge_id": r.challenge_id,
        "challenge_title": titles.get(r.challenge_id, f"#{r.challenge_id}"),
        "version": r.version,
        "player": r.player,
        "stars": r.stars,
        "fuel_used": r.fuel_used,
        "elapsed_days": r.elapsed_days,
        "review_status": r.review_status,
        "review_note": r.review_note,
        "reviewed_by_name": r.reviewed_by_name,
        "appeal": _appeal_block(r),
        "created_at": r.created_at,
    } for r in rows]


def list_appeal_queue(db, *, status: str = "pending",
                      limit: int = 50) -> List[dict]:
    """申诉复核队列：默认待管理员复核（appeal_status=pending），可查已决。"""
    q = db.query(ChallengeSubmission)
    if status in _APPEAL_STATUSES:
        q = q.filter(ChallengeSubmission.appeal_status == status)
    else:
        q = q.filter(ChallengeSubmission.appeal_status != "")
    rows = (q.order_by(ChallengeSubmission.appealed_at.is_(None),
                       ChallengeSubmission.appealed_at.asc(),
                       ChallengeSubmission.id.asc())
             .limit(max(1, min(200, limit)))
             .all())
    titles = {c.id: c.title for c in db.query(Challenge).all()}
    return [{
        "record_id": r.id,
        "challenge_id": r.challenge_id,
        "challenge_title": titles.get(r.challenge_id, f"#{r.challenge_id}"),
        "version": r.version,
        "player": r.player,
        "stars": r.stars,
        "fuel_used": r.fuel_used,
        "elapsed_days": r.elapsed_days,
        "review_status": r.review_status,
        "review_note": r.review_note,
        "reviewed_by_name": r.reviewed_by_name,
        "appeal": _appeal_block(r),
        "created_at": r.created_at,
    } for r in rows]


# ---------- 排行榜与回放 ----------

def leaderboard(db, challenge_id: int, version: Optional[int] = None,
                limit: int = 50) -> dict:
    """排行榜：仅审核通过的成绩，按版本结算，每名玩家取最佳一条。

    排序：星数 → 燃料 → 用时 → 先到优先。
    """
    ch = _get_challenge(db, challenge_id)
    v_no = version or ch.current_version
    _get_version(db, challenge_id, v_no)  # 版本必须存在
    rows = (db.query(ChallengeSubmission)
              .filter(ChallengeSubmission.challenge_id == challenge_id,
                      ChallengeSubmission.version == v_no,
                      ChallengeSubmission.review_status == "approved")
              .order_by(ChallengeSubmission.stars.desc(),
                        ChallengeSubmission.fuel_used.asc(),
                        ChallengeSubmission.elapsed_days.asc(),
                        ChallengeSubmission.id.asc())
              .all())
    entries, seen = [], set()
    for r in rows:
        if r.player in seen:
            continue
        seen.add(r.player)
        entries.append({
            "rank": len(entries) + 1,
            "record_id": r.id,
            "player": r.player,
            "stars": r.stars,
            "fuel_used": r.fuel_used,
            "elapsed_days": r.elapsed_days,
            "replayable": True,
            # 经申诉复核翻案恢复的上榜成绩（原驳回 → 管理员 uphold）
            "restored": r.appeal_status == "upheld",
            "created_at": r.created_at,
        })
        if len(entries) >= max(1, min(100, limit)):
            break
    return {"challenge_id": challenge_id, "version": v_no, "entries": entries}


def submission_detail(db, record_id: int) -> Optional[dict]:
    """成绩详情：审核通过后附带动作方案与轨迹（供回放）；否则只回元信息。"""
    rec = (db.query(ChallengeSubmission)
             .filter(ChallengeSubmission.id == record_id)
             .first())
    if rec is None:
        return None
    detail = {
        "record_id": rec.id,
        "challenge_id": rec.challenge_id,
        "version": rec.version,
        "player": rec.player,
        "player_id": rec.player_id,
        "stars": rec.stars,
        "fuel_used": rec.fuel_used,
        "elapsed_days": rec.elapsed_days,
        "review_status": rec.review_status,
        "review_note": rec.review_note,
        "reviewed_by_name": rec.reviewed_by_name,
        "appeal": _appeal_block(rec),
        "replayable": rec.review_status == "approved",
        "created_at": rec.created_at,
    }
    if detail["replayable"]:
        run = (db.query(ChallengeRun)
                 .filter(ChallengeRun.id == rec.run_id)
                 .first())
        if run is not None:
            detail.update({
                "run_uid": run.run_uid,
                "ok": bool(run.ok),
                "reason": run.reason,
                "actions": json.loads(run.actions_json),
                "trajectory": json.loads(run.trajectory_json),
                "events": json.loads(run.events_json),
                "milestones": json.loads(run.milestones_json),
            })
    return detail
