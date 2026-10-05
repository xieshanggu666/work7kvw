import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

from app.core.config import DB_PATH

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# 申诉/复核链路给 challenge_submission 增补的列（旧存档已审核成绩兼容）。
# ALTER TABLE ADD COLUMN 对旧库补齐列；新库由 create_all 直接建全。
_SUBMISSION_ADDED_COLUMNS = {
    "player_id": "VARCHAR(64)",
    "reviewed_by": "VARCHAR(64)",
    "reviewed_by_name": "VARCHAR(24)",
    "appeal_id": "VARCHAR(64)",
    "appeal_status": "VARCHAR(16) NOT NULL DEFAULT ''",
    "appeal_reason": "TEXT NOT NULL DEFAULT ''",
    "appealed_by": "VARCHAR(64)",
    "appealed_by_name": "VARCHAR(24)",
    "appealed_at": "FLOAT",
    "appeal_note": "VARCHAR(200) NOT NULL DEFAULT ''",
    "appeal_reviewed_by": "VARCHAR(64)",
    "appeal_reviewed_by_name": "VARCHAR(24)",
    "appeal_reviewed_at": "FLOAT",
}


def migrate(engine) -> None:
    """轻量迁移：对既有 challenge_submission 表追加新列（幂等，重复执行无副作用）。

    历史成绩的新列为空/默认值：review_status 维持 approved/rejected，
    appeal_status 为空表示从未申诉，继续正常上榜/回放/计入解锁。
    """
    inspector = inspect(engine)
    if "challenge_submission" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("challenge_submission")}
    with engine.begin() as conn:
        for name, ddl_type in _SUBMISSION_ADDED_COLUMNS.items():
            if name not in existing:
                conn.execute(text(
                    f"ALTER TABLE challenge_submission ADD COLUMN {name} {ddl_type}"))
        # 申诉幂等键唯一索引（SQLite：历史行 NULL 不冲突）
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_challenge_submission_appeal_id "
            "ON challenge_submission (appeal_id)"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
