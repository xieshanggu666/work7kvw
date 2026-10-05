import time

from sqlalchemy import (Column, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint)

from app.core.database import Base


class LevelScore(Base):
    """最佳成绩汇总（旧存档表）。结构保持不变，历史存档可直接沿用。"""
    __tablename__ = "level_score"
    __table_args__ = (UniqueConstraint("level_id", name="uq_level"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    level_id = Column(Integer, nullable=False, index=True)
    stars = Column(Integer, nullable=False, default=0)
    fuel_used = Column(Float, nullable=False, default=0.0)
    elapsed_days = Column(Float, nullable=False, default=0.0)


class RunRecord(Base):
    """一次任务执行的完整档案：动作方案 + 结算结果 + 轨迹。

    /api/run 每次执行落库一条，成绩记录通过外键关联到它，
    使每条最佳成绩都能溯源到产生它的那次发射，并支持轨迹回放。
    """
    __tablename__ = "run_record"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_uid = Column(String(36), nullable=False, unique=True, index=True)
    level_id = Column(Integer, nullable=False, index=True)
    ok = Column(Integer, nullable=False, default=0)
    reason = Column(String(64), nullable=False, default="")
    stars = Column(Integer, nullable=False, default=0)
    fuel_used = Column(Float, nullable=False, default=0.0)
    elapsed_days = Column(Float, nullable=False, default=0.0)
    actions_json = Column(Text, nullable=False, default="[]")
    trajectory_json = Column(Text, nullable=False, default="[]")
    events_json = Column(Text, nullable=False, default="[]")
    milestones_json = Column(Text, nullable=False, default="[]")
    created_at = Column(Float, nullable=False, default=time.time)


class ScoreRecord(Base):
    """一条成绩提交记录。

    submission_id 是客户端生成的幂等键：同一提交（双击/重试/多标签页）
    重复到达时只落库一次，返回首个结果。
    """
    __tablename__ = "score_record"

    id = Column(Integer, primary_key=True, autoincrement=True)
    submission_id = Column(String(64), nullable=False, unique=True, index=True)
    run_id = Column(Integer, ForeignKey("run_record.id"), nullable=True, index=True)
    level_id = Column(Integer, nullable=False, index=True)
    stars = Column(Integer, nullable=False, default=0)
    fuel_used = Column(Float, nullable=False, default=0.0)
    elapsed_days = Column(Float, nullable=False, default=0.0)
    source = Column(String(16), nullable=False, default="run")  # run=关联执行 / legacy=旧版客户端
    created_at = Column(Float, nullable=False, default=time.time)


class Challenge(Base):
    """社区航线挑战：设计者发布的关卡系列（内容版本化，逐版本结算）。"""
    __tablename__ = "challenge"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(40), nullable=False)
    author = Column(String(24), nullable=False, default="匿名设计者")
    status = Column(String(16), nullable=False, default="published")  # published / archived
    current_version = Column(Integer, nullable=False, default=1)
    unlock_rule_json = Column(Text, nullable=False, default="")  # 空串 = 无解锁条件
    created_at = Column(Float, nullable=False, default=time.time)


class ChallengeVersion(Base):
    """挑战关卡的不可变版本：燃料预算 + 里程碑 + 时间限制。

    每次发布生成新版本；旧版本保留供成绩溯源与回放，不可修改。
    """
    __tablename__ = "challenge_version"
    __table_args__ = (UniqueConstraint("challenge_id", "version", name="uq_challenge_version"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    challenge_id = Column(Integer, ForeignKey("challenge.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    name = Column(String(40), nullable=False, default="")
    brief = Column(String(500), nullable=False, default="")
    hint = Column(String(500), nullable=False, default="")
    budget_dv = Column(Float, nullable=False)
    t_max = Column(Float, nullable=False)
    milestones_json = Column(Text, nullable=False, default="[]")
    created_at = Column(Float, nullable=False, default=time.time)


class ChallengeRun(Base):
    """一次挑战飞行的执行档案：动作方案 + 服务端结算 + 轨迹。

    与 RunRecord 同构，但锚定挑战版本（challenge_id + version），
    成绩提交与回放都通过它溯源。
    """
    __tablename__ = "challenge_run"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_uid = Column(String(36), nullable=False, unique=True, index=True)
    challenge_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    ok = Column(Integer, nullable=False, default=0)
    reason = Column(String(64), nullable=False, default="")
    stars = Column(Integer, nullable=False, default=0)
    fuel_used = Column(Float, nullable=False, default=0.0)
    elapsed_days = Column(Float, nullable=False, default=0.0)
    actions_json = Column(Text, nullable=False, default="[]")
    trajectory_json = Column(Text, nullable=False, default="[]")
    events_json = Column(Text, nullable=False, default="[]")
    milestones_json = Column(Text, nullable=False, default="[]")
    created_at = Column(Float, nullable=False, default=time.time)


class ChallengeSubmission(Base):
    """挑战成绩提交记录：幂等键去重 + 审核/申诉复核状态机。

    submission_id 是客户端生成的幂等键（双击/重试/多标签页只结算一次）。
    初审状态 pending → approved/rejected；只有 approved 的成绩才进入排行榜、
    开放轨迹回放，并计入关卡解锁条件。

    申诉与复核（可追溯链路）：
    - 玩家/审核席对终审成绩发起 appeal（appeal_id 为申诉幂等键），
      appeal_status: ""（未申诉）→ pending → upheld/denied。
    - 管理员复核（reviewer_role=admin，且不得与原审同人）：
      uphold 翻案 —— rejected→approved（恢复上榜/回放/解锁），
      approved→revoked（撤榜并回滚排行榜/解锁）；deny 维持原判。
    - reviewed_by/appeal_reviewed_by 等身份字段对历史成绩允许为空（旧存档兼容）；
      完整操作链路记录在 ChallengeReviewEvent（只追加，不可改）。
    """
    __tablename__ = "challenge_submission"

    id = Column(Integer, primary_key=True, autoincrement=True)
    submission_id = Column(String(64), nullable=False, unique=True, index=True)
    run_id = Column(Integer, ForeignKey("challenge_run.id"), nullable=False, index=True)
    challenge_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    player = Column(String(24), nullable=False, default="匿名飞行员")
    player_id = Column(String(64), nullable=True, index=True)  # 稳定身份（申诉归属/自审回避）；历史行可为空
    stars = Column(Integer, nullable=False, default=0)
    fuel_used = Column(Float, nullable=False, default=0.0)
    elapsed_days = Column(Float, nullable=False, default=0.0)
    review_status = Column(String(16), nullable=False, default="pending", index=True)
    # pending / approved / rejected / revoked（复核翻案撤榜）
    review_note = Column(String(200), nullable=False, default="")
    reviewed_by = Column(String(64), nullable=True)        # 原审审核员稳定 id
    reviewed_by_name = Column(String(24), nullable=True)   # 原审审核员署名
    reviewed_at = Column(Float, nullable=True)
    # ---- 申诉 / 复核 ----
    appeal_id = Column(String(64), nullable=True, unique=True, index=True)  # 申诉幂等键
    appeal_status = Column(String(16), nullable=False, default="", index=True)
    # "" / pending / upheld / denied
    appeal_reason = Column(Text, nullable=False, default="")
    appealed_by = Column(String(64), nullable=True)
    appealed_by_name = Column(String(24), nullable=True)
    appealed_at = Column(Float, nullable=True)
    appeal_note = Column(String(200), nullable=False, default="")  # 复核结论备注
    appeal_reviewed_by = Column(String(64), nullable=True)        # 复核管理员 id
    appeal_reviewed_by_name = Column(String(24), nullable=True)
    appeal_reviewed_at = Column(Float, nullable=True)
    created_at = Column(Float, nullable=False, default=time.time)


class ChallengeReviewEvent(Base):
    """审核/申诉链路事件流（只追加）：提交 → 初审 → 申诉 → 复核，全程可追溯。

    每条成绩的事件按 seq 递增；任何状态迁移都在此留痕，记录操作人身份、
    角色与动作明细，不随状态翻转而修改或删除。
    """
    __tablename__ = "challenge_review_event"
    __table_args__ = (UniqueConstraint("submission_id", "seq",
                                       name="uq_review_event_seq"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    submission_id = Column(Integer, ForeignKey("challenge_submission.id"),
                           nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    actor = Column(String(64), nullable=False, default="")        # 操作人稳定 id
    actor_name = Column(String(24), nullable=False, default="")   # 操作人署名
    actor_role = Column(String(16), nullable=False, default="")   # player/reviewer/admin
    # submit / review / appeal / appeal_rule
    action = Column(String(24), nullable=False)
    detail_json = Column(Text, nullable=False, default="{}")
    created_at = Column(Float, nullable=False, default=time.time)
