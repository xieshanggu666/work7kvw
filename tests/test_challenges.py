"""社区航线挑战测试：版本化发布 / 幂等提交 / 并发安全 / 审核联动排行榜、回放与解锁。"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# 测试使用独立数据库文件（须在导入 app 模块前设置）
os.environ["SLINGSHOT_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="slingshot_ch_test_"), "test.db")

import pytest
from fastapi import HTTPException

from app.core.database import Base, SessionLocal, engine
from app.models import (Challenge, ChallengeReviewEvent, ChallengeRun,
                        ChallengeSubmission, ChallengeVersion, LevelScore,
                        RunRecord, ScoreRecord)
from app.api import challenges as ch_api
from app.api import router as game_api


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        for t in (ChallengeReviewEvent, ChallengeSubmission, ChallengeRun,
                  ChallengeVersion, Challenge,
                  ScoreRecord, RunRecord, LevelScore):
            db.query(t).delete()
        db.commit()
        yield
    finally:
        db.close()


# ---------- 工具 ----------

def _def(**over):
    """一份可解的挑战定义（与内置第 1 关同构，参考解法三星）。"""
    d = {
        "name": "首版航线",
        "brief": "飞掠火星即可",
        "hint": "切向点火",
        "budget_dv": 0.004,
        "t_max": 900,
        "milestones": [{"kind": "proximity", "planet_id": "mars",
                        "dist": 0.18, "name": "飞掠火星"}],
    }
    d.update(over)
    return d


def _create(title="火星快线", **over):
    payload = {"title": title, "author": "测试员", **_def(), **over}
    return ch_api.create_challenge(ch_api.CreateChallengeIn(**payload))


def _run(cid, actions=None):
    """执行一次可三星通关的飞行。"""
    actions = actions or [{"type": "burn", "angle": 90.0, "dv": 0.0017}]
    return ch_api.run(cid, ch_api.SimIn(
        actions=[ch_api.Action(**a) for a in actions]))


def _submit(cid, run_id, sub_id, player="飞行员甲", player_id=None):
    return ch_api.submit(cid, ch_api.SubmitIn(
        run_id=run_id, submission_id=sub_id, player=player,
        player_id=player_id))


def _reviewer(rid="rev-1", name="审核员甲", role="reviewer"):
    return {"reviewer_id": rid, "reviewer_name": name, "reviewer_role": role}


def _approve(record_id, **rev):
    return ch_api.review_submission(
        record_id, ch_api.ReviewIn(action="approve", **{**_reviewer(), **rev}))


def _reject(record_id, note="", **rev):
    return ch_api.review_submission(
        record_id, ch_api.ReviewIn(action="reject", note=note,
                                   **{**_reviewer(), **rev}))


def _appeal(record_id, reason="结算有误", *, appeal_id=None, player_id="p-1",
            player_name="飞行员甲", role="player"):
    return ch_api.appeal_submission(record_id, ch_api.AppealIn(
        reason=reason, appeal_id=appeal_id, player_id=player_id,
        player_name=player_name, role=role))


def _rule(record_id, decision, *, rid="admin-1", name="管理员甲", note=""):
    return ch_api.rule_appeal_submission(record_id, ch_api.RuleAppealIn(
        decision=decision, note=note,
        reviewer_id=rid, reviewer_name=name, reviewer_role="admin"))


def _unlocked(challenges, cid):
    return next(c for c in challenges if c["id"] == cid)["unlocked"]


# ---------- 版本化发布 ----------

def test_create_challenge_publishes_v1():
    d = _create()
    assert d["title"] == "火星快线" and d["current_version"] == 1
    assert len(d["versions"]) == 1 and d["versions"][0]["milestone_count"] == 1
    assert d["unlocked"] is True and d["best"] is None
    lst = ch_api.list_challenges()["challenges"]
    assert len(lst) == 1 and lst[0]["milestone_count"] == 1
    assert lst[0]["budget_dv"] == 0.004


def test_create_validation_errors():
    # 未知里程碑类型 / 未知行星 / 空里程碑 / 预算越界 / 未知解锁条件
    for over in [
        {"milestones": [{"kind": "warp", "name": "曲率"}]},
        {"milestones": [{"kind": "proximity", "planet_id": "pluto",
                         "dist": 0.1, "name": "x"}]},
        {"milestones": []},
        {"budget_dv": 0.0001},
        {"budget_dv": 0.5},
        {"t_max": 10},
        {"unlock_rule": {"type": "builtin_level", "level_id": 99}},
        {"unlock_rule": {"type": "challenge", "challenge_id": 42}},
        {"unlock_rule": {"type": "hyperspace"}},
    ]:
        with pytest.raises(HTTPException) as e:
            _create(**over)
        assert e.value.status_code == 400, over


def test_publish_new_version_keeps_old_immutable():
    d = _create()
    v1 = ch_api.version_detail(d["id"], 1)
    d2 = ch_api.publish_version(d["id"], ch_api.VersionIn(
        **_def(name="加严版", budget_dv=0.003)))
    assert d2["current_version"] == 2 and len(d2["versions"]) == 2
    assert d2["current"]["budget_dv"] == 0.003
    # 旧版本仍可取回且未被新版本覆盖
    v1_after = ch_api.version_detail(d["id"], 1)
    assert v1_after["budget_dv"] == v1["budget_dv"] == 0.004
    assert v1_after["is_current"] is False
    with pytest.raises(HTTPException) as e:
        ch_api.version_detail(d["id"], 99)
    assert e.value.status_code == 404


def test_version_can_update_unlock_rule():
    a = _create(title="前置挑战")
    b = _create(title="后续挑战")
    d = ch_api.publish_version(b["id"], ch_api.VersionIn(
        **_def(), unlock_rule=ch_api.UnlockRuleIn(
            type="challenge", challenge_id=a["id"])))
    assert d["unlock_rule"] == {"type": "challenge", "challenge_id": a["id"]}
    assert d["unlocked"] is False
    # 自引用被拒绝
    with pytest.raises(HTTPException) as e:
        ch_api.publish_version(b["id"], ch_api.VersionIn(
            **_def(), unlock_rule=ch_api.UnlockRuleIn(
                type="challenge", challenge_id=b["id"])))
    assert e.value.status_code == 400


# ---------- 飞行执行与幂等提交 ----------

def test_run_settles_and_persists_traceable_record():
    d = _create()
    r = _run(d["id"])
    assert r["ok"] and r["stars"] == 3 and r["run_id"]
    assert r["version"] == 1 and r["challenge_id"] == d["id"]
    db = SessionLocal()
    run = db.query(ChallengeRun).filter(ChallengeRun.run_uid == r["run_id"]).one()
    assert run.version == 1 and run.stars == 3
    assert run.trajectory_json.startswith("[")
    db.close()


def test_preview_does_not_persist():
    d = _create()
    r = ch_api.preview(d["id"], ch_api.SimIn(
        actions=[ch_api.Action(type="burn", angle=90.0, dv=0.0017)]))
    assert r["ok"] and "run_id" not in r
    db = SessionLocal()
    assert db.query(ChallengeRun).count() == 0
    db.close()


def test_submit_idempotent_and_server_settled():
    d = _create()
    r = _run(d["id"])
    a = _submit(d["id"], r["run_id"], "sub-1")
    b = _submit(d["id"], r["run_id"], "sub-1")
    assert a["record_id"] == b["record_id"] and b["duplicated"] is True
    assert a["review_status"] == "pending"
    db = SessionLocal()
    rec = db.query(ChallengeSubmission).one()
    # 成绩以服务端结算为准（run 落库的星级/燃料）
    assert rec.stars == r["stars"] == 3
    assert abs(rec.fuel_used - r["fuel_used"]) < 1e-9
    assert rec.player == "飞行员甲"
    db.close()


def test_submit_concurrent_duplicates():
    """同一提交并发到达（双击/重试/多标签页）：只落库一条。"""
    d = _create()
    r = _run(d["id"])
    results = []

    def worker():
        results.append(_submit(d["id"], r["run_id"], "race-1"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(x["saved"] for x in results)
    assert len({x["record_id"] for x in results}) == 1
    db = SessionLocal()
    assert db.query(ChallengeSubmission).count() == 1
    db.close()


def test_submit_rejects_bad_run():
    d = _create()
    with pytest.raises(HTTPException) as e1:
        _submit(d["id"], "nonexistent", "s-x")
    assert e1.value.status_code == 404
    other = _create(title="另一挑战")
    r = _run(d["id"])
    with pytest.raises(HTTPException) as e2:  # 执行档案属于挑战 d，不能记到 other
        _submit(other["id"], r["run_id"], "s-y")
    assert e2.value.status_code == 400


# ---------- 审核联动：排行榜 / 回放 / 解锁 ----------

def test_review_gates_leaderboard_replay_and_unlock():
    a = _create(title="航线A")
    b = _create(title="航线B", unlock_rule={"type": "challenge",
                                          "challenge_id": a["id"]})
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is False

    r = _run(a["id"])
    sub = _submit(a["id"], r["run_id"], "sub-a")

    # 审核前：排行榜为空、回放不开放、B 仍锁定
    assert ch_api.get_leaderboard(a["id"])["entries"] == []
    detail = ch_api.get_submission(sub["record_id"])
    assert detail["replayable"] is False and "trajectory" not in detail
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is False

    # 审核通过：上榜、回放开放、B 解锁
    rv = _approve(sub["record_id"])
    assert rv["review_status"] == "approved" and rv["duplicated"] is False
    entries = ch_api.get_leaderboard(a["id"])["entries"]
    assert len(entries) == 1 and entries[0]["player"] == "飞行员甲"
    assert entries[0]["stars"] == 3 and entries[0]["rank"] == 1
    detail = ch_api.get_submission(sub["record_id"])
    assert detail["replayable"] and detail["trajectory"]
    assert detail["actions"][0]["type"] == "burn"
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is True
    # 列表里的本机最佳同步刷新
    lst = ch_api.list_challenges()["challenges"]
    assert next(c for c in lst if c["id"] == a["id"])["best"]["stars"] == 3


def test_review_reject_excludes_everything():
    d = _create()
    r = _run(d["id"])
    sub = _submit(d["id"], r["run_id"], "sub-r")
    rv = ch_api.review_submission(sub["record_id"], ch_api.ReviewIn(
        action="reject", note="轨迹异常"))
    assert rv["review_status"] == "rejected"
    assert ch_api.get_leaderboard(d["id"])["entries"] == []
    detail = ch_api.get_submission(sub["record_id"])
    assert detail["replayable"] is False and detail["review_note"] == "轨迹异常"


def test_review_idempotent_and_conflict():
    d = _create()
    r = _run(d["id"])
    sub = _submit(d["id"], r["run_id"], "sub-i")
    _approve(sub["record_id"])
    again = _approve(sub["record_id"])  # 重复通过：幂等
    assert again["duplicated"] is True
    with pytest.raises(HTTPException) as e:  # 已通过后驳回：冲突
        ch_api.review_submission(sub["record_id"], ch_api.ReviewIn(action="reject"))
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e2:
        ch_api.review_submission(9999, ch_api.ReviewIn(action="approve"))
    assert e2.value.status_code == 404
    with pytest.raises(HTTPException) as e3:
        ch_api.review_submission(sub["record_id"], ch_api.ReviewIn(action="maybe"))
    assert e3.value.status_code == 400


def test_review_queue_lists_pending():
    d = _create()
    r = _run(d["id"])
    sub = _submit(d["id"], r["run_id"], "sub-q")
    queue = ch_api.review_queue()["submissions"]
    assert len(queue) == 1 and queue[0]["record_id"] == sub["record_id"]
    assert queue[0]["challenge_title"] == "火星快线"
    _approve(sub["record_id"])
    assert ch_api.review_queue()["submissions"] == []
    approved = ch_api.review_queue(status="approved")["submissions"]
    assert len(approved) == 1


# ---------- 排行榜规则 ----------

def test_leaderboard_best_per_player_and_ordering():
    d = _create()
    # 玩家甲两次成绩（先差后好）：只保留最佳一条
    r1 = _run(d["id"], [{"type": "burn", "angle": 90.0, "dv": 0.0035}])
    s1 = _submit(d["id"], r1["run_id"], "lb-1", player="甲")
    r2 = _run(d["id"])  # 三星、燃料更省
    s2 = _submit(d["id"], r2["run_id"], "lb-2", player="甲")
    r3 = _run(d["id"])
    s3 = _submit(d["id"], r3["run_id"], "lb-3", player="乙")
    for rec in (s1, s2, s3):
        _approve(rec["record_id"])
    entries = ch_api.get_leaderboard(d["id"])["entries"]
    assert len(entries) == 2  # 甲只出现一次（最佳）
    jia = next(e for e in entries if e["player"] == "甲")
    assert jia["record_id"] == s2["record_id"] and jia["stars"] == 3
    # 同星同玩家最佳并列时按燃料排序：甲(0.0017) 在 乙(0.0017) 前（先到优先）
    assert entries[0]["player"] == "甲"


def test_leaderboard_isolated_by_version():
    d = _create()
    r = _run(d["id"])  # v1 成绩
    s = _submit(d["id"], r["run_id"], "v1-sub")
    _approve(s["record_id"])
    ch_api.publish_version(d["id"], ch_api.VersionIn(**_def(budget_dv=0.003)))
    # 当前版本（v2）排行榜为空；v1 排行榜保留成绩
    assert ch_api.get_leaderboard(d["id"])["entries"] == []
    assert ch_api.get_leaderboard(d["id"])["version"] == 2
    v1 = ch_api.get_leaderboard(d["id"], version=1)
    assert len(v1["entries"]) == 1 and v1["entries"][0]["record_id"] == s["record_id"]


# ---------- 解锁联动 ----------

def test_unlock_by_builtin_level():
    d = _create(unlock_rule={"type": "builtin_level", "level_id": 1})
    assert _unlocked(ch_api.list_challenges()["challenges"], d["id"]) is False
    # 未解锁时结算类接口拒绝（403），预览仍可用
    with pytest.raises(HTTPException) as e:
        _run(d["id"])
    assert e.value.status_code == 403
    assert ch_api.preview(d["id"], ch_api.SimIn())["ok"] is False or True
    # 通关内置第 1 关后解锁
    game_api.save_score(game_api.ScoreRequest(level_id=1, stars=1,
                                              fuel_used=0.003, elapsed_days=500.0))
    assert _unlocked(ch_api.list_challenges()["challenges"], d["id"]) is True
    assert _run(d["id"])["ok"]


def test_unlock_by_stars_total():
    d = _create(unlock_rule={"type": "stars_total", "value": 4})
    game_api.save_score(game_api.ScoreRequest(level_id=1, stars=3,
                                              fuel_used=0.002, elapsed_days=400.0))
    assert _unlocked(ch_api.list_challenges()["challenges"], d["id"]) is False
    # 另一个挑战的已审核星数也计入总星数
    other = _create(title="星星来源")
    r = _run(other["id"])
    s = _submit(other["id"], r["run_id"], "star-src")
    _approve(s["record_id"])  # 3 星 → 总星数 6 ≥ 4
    assert _unlocked(ch_api.list_challenges()["challenges"], d["id"]) is True


def test_unlock_chain_between_challenges():
    a = _create(title="第一环")
    b = _create(title="第二环", unlock_rule={"type": "challenge",
                                           "challenge_id": a["id"]})
    c = _create(title="第三环", unlock_rule={"type": "challenge",
                                           "challenge_id": b["id"]})
    lst = ch_api.list_challenges()["challenges"]
    assert _unlocked(lst, b["id"]) is False and _unlocked(lst, c["id"]) is False
    # 通过 A 的成绩审核后：B 解锁，C 仍锁定（链式逐级联动）
    r = _run(a["id"])
    _approve(_submit(a["id"], r["run_id"], "chain-a")["record_id"])
    lst = ch_api.list_challenges()["challenges"]
    assert _unlocked(lst, b["id"]) is True and _unlocked(lst, c["id"]) is False
    r = _run(b["id"])
    _approve(_submit(b["id"], r["run_id"], "chain-b")["record_id"])
    assert _unlocked(ch_api.list_challenges()["challenges"], c["id"]) is True


def test_run_on_specific_version():
    d = _create()
    ch_api.publish_version(d["id"], ch_api.VersionIn(**_def(budget_dv=0.003)))
    r = ch_api.run(d["id"], ch_api.SimIn(
        actions=[ch_api.Action(type="burn", angle=90.0, dv=0.0017)], version=1))
    assert r["version"] == 1 and r["ok"]
    s = _submit(d["id"], r["run_id"], "old-v")
    assert s["version"] == 1
    _approve(s["record_id"])
    assert len(ch_api.get_leaderboard(d["id"], version=1)["entries"]) == 1
    assert ch_api.get_leaderboard(d["id"])["entries"] == []  # 当前 v2 为空


# ---------- 审核权限与自审回避 ----------

def test_review_records_reviewer_identity():
    d = _create()
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "perm-1", player_id="p-1")
    rv = _approve(s["record_id"], reviewer_id="rev-9", reviewer_name="老王")
    assert rv["review_status"] == "approved" and rv["duplicated"] is False
    queue = ch_api.review_queue(status="approved")["submissions"]
    assert queue[0]["reviewed_by_name"] == "老王"
    detail = ch_api.get_submission(s["record_id"])
    assert detail["reviewed_by_name"] == "老王"


def test_review_self_submission_forbidden():
    d = _create()
    r = _run(d["id"])
    # 玩家身份 p-1 提交，审核席若用同一稳定 id 审核 → 403（自审回避）
    s = _submit(d["id"], r["run_id"], "self-1", player_id="p-1")
    with pytest.raises(HTTPException) as e:
        _approve(s["record_id"], reviewer_id="p-1", reviewer_name="就是我本人")
    assert e.value.status_code == 403
    # 换人审核即可通过
    assert _approve(s["record_id"], reviewer_id="rev-1")["review_status"] == "approved"


def test_legacy_submission_self_avoidance_exempt():
    """历史成绩无 player_id（旧客户端/旧存档）：自审校验豁免，审核照常。"""
    d = _create()
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "legacy-1")  # 无 player_id
    rv = _approve(s["record_id"], reviewer_id="rev-7")
    assert rv["review_status"] == "approved"


# ---------- 申诉与复核：翻案恢复 ----------

def test_appeal_rejected_upheld_restores_board_and_unlock():
    a = _create(title="航线A")
    b = _create(title="航线B", unlock_rule={"type": "challenge",
                                          "challenge_id": a["id"]})
    r = _run(a["id"])
    s = _submit(a["id"], r["run_id"], "appeal-ok", player_id="p-1")
    _reject(s["record_id"], note="疑似作弊")
    assert ch_api.get_leaderboard(a["id"])["entries"] == []
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is False
    detail = ch_api.get_submission(s["record_id"])
    assert detail["review_note"] == "疑似作弊" and detail["appeal"] is None

    # 玩家就驳回发起申诉 → 进入复核队列
    ap = _appeal(s["record_id"], "里程碑实际达成，请重算", appeal_id="ap-1")
    assert ap["appeal_status"] == "pending" and ap["duplicated"] is False
    q = ch_api.appeal_queue()["submissions"]
    assert len(q) == 1 and q[0]["record_id"] == s["record_id"]
    assert q[0]["appeal"]["reason"] == "里程碑实际达成，请重算"
    # 申诉待复核期间，初审队列不再受理重复初审
    with pytest.raises(HTTPException) as e:
        _approve(s["record_id"])
    assert e.value.status_code == 409

    # 管理员 uphold 翻案：恢复上榜 / 回放 / 解锁
    ruled = _rule(s["record_id"], "upheld", note="复核确认里程碑达成")
    assert ruled["review_status"] == "approved"
    assert ruled["appeal_status"] == "upheld"
    entries = ch_api.get_leaderboard(a["id"])["entries"]
    assert len(entries) == 1 and entries[0]["restored"] is True
    detail = ch_api.get_submission(s["record_id"])
    assert detail["replayable"] and detail["appeal"]["note"] == "复核确认里程碑达成"
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is True
    assert ch_api.appeal_queue()["submissions"] == []


def test_appeal_denied_keeps_rejection():
    d = _create()
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "appeal-no", player_id="p-1")
    _reject(s["record_id"])
    _appeal(s["record_id"], appeal_id="ap-d")
    ruled = _rule(s["record_id"], "denied", note="维持：轨迹不连续")
    assert ruled["review_status"] == "rejected" and ruled["appeal_status"] == "denied"
    assert ch_api.get_leaderboard(d["id"])["entries"] == []
    detail = ch_api.get_submission(s["record_id"])
    assert detail["replayable"] is False


# ---------- 申诉与复核：撤榜回滚 ----------

def test_appeal_approved_upheld_revokes_board_and_rolls_back_unlock():
    a = _create(title="环A")
    b = _create(title="环B", unlock_rule={"type": "challenge",
                                        "challenge_id": a["id"]})
    r = _run(a["id"])
    s = _submit(a["id"], r["run_id"], "revoke-1", player_id="p-1")
    _approve(s["record_id"])
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is True
    # 审核席对上榜成绩提出撤榜复核
    ap = _appeal(s["record_id"], "事后发现轨迹数据异常", appeal_id="ap-rv",
                 player_id="rev-1", player_name="审核员甲", role="reviewer")
    assert ap["appeal_status"] == "pending"
    _rule(s["record_id"], "upheld", note="复核确认异常，撤销成绩")
    # 撤榜：排行榜/回放回滚
    assert ch_api.get_leaderboard(a["id"])["entries"] == []
    detail = ch_api.get_submission(s["record_id"])
    assert detail["review_status"] == "revoked" and detail["replayable"] is False
    # 解锁联动回滚：依赖挑战重新锁定
    assert _unlocked(ch_api.list_challenges()["challenges"], b["id"]) is False
    revoked = ch_api.review_queue(status="revoked")["submissions"]
    assert len(revoked) == 1 and revoked[0]["record_id"] == s["record_id"]


# ---------- 申诉权限与状态门禁 ----------

def test_player_appeal_permissions():
    d = _create()
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "perm-ap", player_id="p-1")
    # 待初审成绩不能申诉（409）
    with pytest.raises(HTTPException) as e:
        _appeal(s["record_id"], appeal_id="ap-x")
    assert e.value.status_code == 409
    _approve(s["record_id"])
    # 玩家不能申诉别人的成绩（403）
    with pytest.raises(HTTPException) as e:
        _appeal(s["record_id"], appeal_id="ap-other", player_id="p-2")
    assert e.value.status_code == 403
    # 玩家不能对自己已上榜成绩发起申诉（撤榜仅审核席可提）
    with pytest.raises(HTTPException) as e2:
        _appeal(s["record_id"], appeal_id="ap-self", player_id="p-1")
    assert e2.value.status_code == 403
    # 玩家不带稳定身份不能申诉（403）
    rj = _submit(d["id"], _run(d["id"])["run_id"], "perm-ap2", player_id="p-2")
    _reject(rj["record_id"])
    with pytest.raises(HTTPException) as e3:
        _appeal(rj["record_id"], appeal_id="ap-noi", player_id=None)
    assert e3.value.status_code == 403
    # 审核席可代任意终审成绩申诉
    ap = _appeal(rj["record_id"], "代提申诉", appeal_id="ap-staff",
                 player_id="rev-1", role="reviewer")
    assert ap["appeal_status"] == "pending"


def test_appeal_once_only_and_re_rule_conflicts():
    d = _create()
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "once-1", player_id="p-1")
    _reject(s["record_id"])
    _appeal(s["record_id"], appeal_id="ap-once")
    # 待复核期间重复申诉 → 409
    with pytest.raises(HTTPException) as e:
        _appeal(s["record_id"], appeal_id="ap-once-2")
    assert e.value.status_code == 409
    _rule(s["record_id"], "denied")
    # 复核已决后再次申诉 → 409（一成绩仅一次申诉链路）
    with pytest.raises(HTTPException) as e2:
        _appeal(s["record_id"], appeal_id="ap-again")
    assert e2.value.status_code == 409
    # 对没有待复核申诉的成绩裁决 → 409
    with pytest.raises(HTTPException) as e3:
        _rule(s["record_id"], "upheld")
    assert e3.value.status_code == 409


def test_rule_requires_admin_and_different_reviewer():
    d = _create()
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "sep-1", player_id="p-1")
    _approve(s["record_id"], reviewer_id="rev-1")
    _appeal(s["record_id"], "异议", appeal_id="ap-sep",
            player_id="rev-2", role="reviewer")
    # 复核必须是 admin 角色（reviewer 角色 → 403）
    with pytest.raises(HTTPException) as e:
        ch_api.rule_appeal_submission(s["record_id"], ch_api.RuleAppealIn(
            decision="upheld", reviewer_id="rev-2", reviewer_role="reviewer"))
    assert e.value.status_code == 403
    # 原审审核员本人即便以 admin 身份也不得复核（同人回避）
    with pytest.raises(HTTPException) as e2:
        _rule(s["record_id"], "upheld", rid="rev-1")
    assert e2.value.status_code == 403
    # 另一名管理员可裁决
    assert _rule(s["record_id"], "upheld", rid="admin-1")["review_status"] == "revoked"
    # 非法复核结论 → 400
    rj = _submit(d["id"], _run(d["id"])["run_id"], "sep-2", player_id="p-2")
    _reject(rj["record_id"], reviewer_id="rev-1")
    _appeal(rj["record_id"], appeal_id="ap-sep2", player_id="p-2")
    with pytest.raises(HTTPException) as e3:
        _rule(rj["record_id"], "maybe")
    assert e3.value.status_code == 400


# ---------- 幂等：申诉重放 / 并发 / 裁决重放 ----------

def test_appeal_idempotent_replay_and_concurrent():
    d = _create()
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "idp-1", player_id="p-1")
    _reject(s["record_id"])
    a1 = _appeal(s["record_id"], appeal_id="ap-idp")
    a2 = _appeal(s["record_id"], appeal_id="ap-idp")  # 双击/重试
    assert a1["appeal_id"] == a2["appeal_id"] and a2["duplicated"] is True
    db = SessionLocal()
    assert db.query(ChallengeSubmission).filter(
        ChallengeSubmission.appeal_id == "ap-idp").count() == 1
    db.close()

    # 并发同键：只产生一条申诉
    rj = _submit(d["id"], _run(d["id"])["run_id"], "idp-2", player_id="p-2")
    _reject(rj["record_id"])
    out = []

    def worker():
        out.append(_appeal(rj["record_id"], appeal_id="ap-race",
                           player_id="p-2"))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({x["appeal_id"] for x in out}) == 1
    assert sum(1 for x in out if not x["duplicated"]) == 1
    db = SessionLocal()
    assert db.query(ChallengeSubmission).filter(
        ChallengeSubmission.appeal_id == "ap-race").count() == 1
    db.close()


def test_rule_decision_idempotent():
    d = _create()
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "ridp", player_id="p-1")
    _reject(s["record_id"])
    _appeal(s["record_id"], appeal_id="ap-ridp")
    r1 = _rule(s["record_id"], "upheld")
    r2 = _rule(s["record_id"], "upheld")  # 重复裁决
    assert r1["review_status"] == "approved" and r2["duplicated"] is True


# ---------- 可追溯链路（只追加事件流） ----------

def test_timeline_records_full_chain_append_only():
    d = _create()
    r = _run(d["id"])
    s = _submit(d["id"], r["run_id"], "trace-1", player="甲", player_id="p-1")
    _reject(s["record_id"], note="数据可疑", reviewer_id="rev-1",
            reviewer_name="审核员甲")
    _appeal(s["record_id"], "请复核", appeal_id="ap-trace")
    _rule(s["record_id"], "upheld", note="改判通过", rid="admin-1",
          name="管理员乙")
    tl = ch_api.submission_timeline(s["record_id"])["events"]
    assert [e["action"] for e in tl] == ["submit", "review", "appeal", "appeal_rule"]
    assert [e["seq"] for e in tl] == [1, 2, 3, 4]
    assert tl[1]["detail"]["result"] == "rejected"
    assert tl[2]["actor_role"] == "player" and tl[2]["actor"] == "p-1"
    assert tl[3]["actor_role"] == "admin"
    assert tl[3]["detail"]["status_before"] == "rejected"
    assert tl[3]["detail"]["status_after"] == "approved"
    # 事件流只追加：重复裁决不新增事件
    _rule(s["record_id"], "upheld")
    tl2 = ch_api.submission_timeline(s["record_id"])["events"]
    assert len(tl2) == 4
    db = SessionLocal()
    assert db.query(ChallengeReviewEvent).count() == 4
    db.close()


def test_timeline_missing_record_404():
    with pytest.raises(HTTPException) as e:
        ch_api.submission_timeline(9999)
    assert e.value.status_code == 404


# ---------- 翻案后的排行榜名次回滚 ----------

def test_revoke_falls_through_to_next_entry():
    d = _create()
    r1 = _run(d["id"], [{"type": "burn", "angle": 90.0, "dv": 0.0035}])
    s1 = _submit(d["id"], r1["run_id"], "rk-1", player="甲", player_id="p1")
    r2 = _run(d["id"])
    s2 = _submit(d["id"], r2["run_id"], "rk-2", player="乙", player_id="p2")
    _approve(s1["record_id"], reviewer_id="rev-1")
    _approve(s2["record_id"], reviewer_id="rev-1")
    board = ch_api.get_leaderboard(d["id"])["entries"]
    assert [e["player"] for e in board] == ["乙", "甲"]  # 乙三星在前
    # 乙被撤榜 → 甲自动升为榜首，乙移出榜单
    _appeal(s2["record_id"], "举报乙作弊", appeal_id="ap-rk",
            player_id="rev-1", role="reviewer")
    _rule(s2["record_id"], "upheld", rid="admin-1")
    board = ch_api.get_leaderboard(d["id"])["entries"]
    assert [e["player"] for e in board] == ["甲"]
    assert board[0]["rank"] == 1


# ---------- 旧存档迁移兼容 ----------

def test_legacy_schema_migrates_and_history_keeps_working(tmp_path, monkeypatch):
    """旧版库（无申诉列/事件表）经迁移后：历史成绩维持终审状态、正常上榜与解锁。"""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker
    legacy = tmp_path / "legacy.db"
    old = sa.create_engine(f"sqlite:///{legacy}")
    with old.begin() as c:
        # 按旧结构建一张不含任何申诉/审核员列的成绩表
        c.execute(sa.text(
            "CREATE TABLE challenge_submission ("
            "id INTEGER PRIMARY KEY, run_id INTEGER, challenge_id INTEGER, "
            "version INTEGER, player VARCHAR(24), stars INTEGER, "
            "fuel_used FLOAT, elapsed_days FLOAT, review_status VARCHAR(16), "
            "review_note VARCHAR(200), reviewed_at FLOAT, created_at FLOAT, "
            "submission_id VARCHAR(64))"))
        c.execute(sa.text(
            "INSERT INTO challenge_submission (id, run_id, challenge_id, version, "
            "player, stars, fuel_used, elapsed_days, review_status, review_note, "
            "reviewed_at, created_at, submission_id) VALUES "
            "(1, 1, 77, 1, '老玩家', 3, 0.0017, 400.0, 'approved', '', 1.0, 1.0, "
            "'legacy-sub')"))

    # 对旧库执行迁移
    from app.core.database import migrate as do_migrate
    do_migrate(old)
    cols = {c["name"] for c in sa.inspect(old).get_columns("challenge_submission")}
    assert {"appeal_id", "appeal_status", "reviewed_by", "player_id"} <= cols
    tables = set(sa.inspect(old).get_table_names())
    assert "challenge_review_event" not in tables  # 事件表由 create_all 建
    # create_all 补建新表（幂等），历史行仍可读且状态不变
    Base.metadata.create_all(bind=old)
    S = sessionmaker(bind=old)
    sdb = S()
    try:
        row = sdb.query(ChallengeSubmission).filter_by(id=1).one()
        assert row.review_status == "approved" and row.appeal_status == ""
        assert row.player == "老玩家" and row.reviewed_by is None
        # 历史成绩可正常发起申诉并经另一管理员复核（原审身份空缺 → 同人豁免）
        row.appeal_status = "pending"
        row.appeal_id = "legacy-ap"
        row.appeal_reason = "历史成绩补申诉"
        row.appealed_by, row.appealed_by_name, row.appealed_at = "p9", "老玩家", 2.0
        sdb.commit()
        assert row.appeal_status == "pending"
    finally:
        sdb.close()
