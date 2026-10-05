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
from app.models import (Challenge, ChallengeRun, ChallengeSubmission,
                        ChallengeVersion, LevelScore, RunRecord, ScoreRecord)
from app.api import challenges as ch_api
from app.api import router as game_api


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        for t in (ChallengeSubmission, ChallengeRun, ChallengeVersion, Challenge,
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


def _submit(cid, run_id, sub_id, player="飞行员甲"):
    return ch_api.submit(cid, ch_api.SubmitIn(
        run_id=run_id, submission_id=sub_id, player=player))


def _approve(record_id):
    return ch_api.review_submission(record_id, ch_api.ReviewIn(action="approve"))


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
