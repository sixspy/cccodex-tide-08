import os
import time

import psycopg
from psycopg.rows import dict_row

from rollback import (
    CONTENTION_LOCK_KEY,
    EVENT_CONTENTION,
    EVENT_RETAIN,
    EVENT_ROLLBACK,
    POLICY_KEEP_GOOD,
    POLICY_ROLLBACK_ALL,
    classify_lines,
    content_fingerprint,
    final_status,
    fingerprint_lock_key,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_batches (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    status TEXT NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closing_policy TEXT,
    fingerprint TEXT,
    recovered_count INTEGER NOT NULL DEFAULT 0,
    kept_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS landed_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    station_code TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    tide_cm INTEGER NOT NULL,
    quality_flag TEXT NOT NULL,
    UNIQUE (station_code, observed_at)
);

CREATE TABLE IF NOT EXISTS reject_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    raw_line TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rollback_settings (
    setting_date DATE PRIMARY KEY DEFAULT CURRENT_DATE,
    closing_policy TEXT NOT NULL,
    locked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rollback_events (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    event_type TEXT NOT NULL,
    closing_policy TEXT,
    recovered_count INTEGER NOT NULL DEFAULT 0,
    kept_count INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS withdrawn_rows (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES rollback_events(id),
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    station_code TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    tide_cm INTEGER NOT NULL,
    quality_flag TEXT NOT NULL
);
"""

SEED_CLEAN = """吴淞口 11-12 早班
WG01|202611120830|186|A
WG01|202611120900|191|A
BG02|202611120830|142|M
"""

SEED_BROKEN = """外高桥 11-12 错报
LG04|202611121000|205|A
WG01|202611120830|186|A
WG0X|202611121000|10|A
"""


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                _ensure_columns(conn)
                count = conn.execute("SELECT COUNT(*) AS n FROM intake_batches").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _ensure_columns(conn):
    """给老库补列，重复启动不报错。"""
    existing = {
        row["column_name"]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'intake_batches'"
        ).fetchall()
    }
    additions = {
        "closing_policy": "TEXT",
        "fingerprint": "TEXT",
        "recovered_count": "INTEGER NOT NULL DEFAULT 0",
        "kept_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, ddl in additions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE intake_batches ADD COLUMN {name} {ddl}")


# ---------------------------------------------------------------------------
# 当日收口（回滚设置）
# ---------------------------------------------------------------------------

def get_today_setting(conn):
    return conn.execute(
        "SELECT setting_date, closing_policy, locked_at, locked_by "
        "FROM rollback_settings WHERE setting_date = CURRENT_DATE"
    ).fetchone()


def lock_settings(conn):
    """抢全局咨询锁。任何一批的回滚事务没落地前，这里都抢不到。"""
    return conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (CONTENTION_LOCK_KEY,)).fetchone()["pg_try_advisory_xact_lock"]


def lock_setting_for_today(conn, policy, username):
    """定死今日收口。调用方必须先 lock_settings 成功、再确认今日没有记录。"""
    return conn.execute(
        """
        INSERT INTO rollback_settings (closing_policy, locked_by)
        VALUES (%s, %s)
        RETURNING setting_date, closing_policy, locked_at, locked_by
        """,
        (policy, username),
    ).fetchone()


# ---------------------------------------------------------------------------
# 归集与回滚
# ---------------------------------------------------------------------------

class SettingMissing(Exception):
    """今日收口还没在策略页定死，不能归集。"""


class RollbackContention(Exception):
    """同一批原文的另一次提交正拿着锁，本次记回滚争用。"""

    def __init__(self, batch_id, event_id):
        super().__init__("回滚争用")
        self.batch_id = batch_id
        self.event_id = event_id


def insert_batch(conn, title, raw_text, created_by):
    """一个事务内完成：解析、去重、按当日收口落库或整批回滚。

    调用方不要自行 commit，本函数只管事务内动作，提交交给外层。
    """
    setting = get_today_setting(conn)
    if not setting:
        raise SettingMissing()
    policy = setting["closing_policy"]
    fingerprint, lines = content_fingerprint(raw_text)
    lock_key = fingerprint_lock_key(raw_text)

    # 加锁顺序固定为「指纹锁 → 全局锁」，避免死锁：
    # 1) 指纹锁用 try：同一批原文并发时，只有先拿到的那次做回滚，另一次记争用；
    # 2) 全局锁用阻塞等待：本事务（含可能的整批回滚）落地前一直持有，
    #    回滚设置端对同一把锁用 try，抢不到就拒绝改动。
    got_lock = conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s)", (lock_key,)
    ).fetchone()["pg_try_advisory_xact_lock"]

    batch = conn.execute(
        """
        INSERT INTO intake_batches
            (title, raw_text, status, accepted_count, rejected_count,
             created_by, closing_policy, fingerprint)
        VALUES (%s, %s, '处理中', 0, 0, %s, %s, %s)
        RETURNING id
        """,
        (title, raw_text, created_by, policy, fingerprint),
    ).fetchone()
    batch_id = batch["id"]

    if not got_lock:
        # 先拿到锁的那次才做回滚，这一次只记争用。
        conn.execute(
            "UPDATE intake_batches SET status = %s WHERE id = %s",
            (final_status(0, 0, policy, contention=True), batch_id),
        )
        event = conn.execute(
            """
            INSERT INTO rollback_events
                (batch_id, event_type, closing_policy, recovered_count, kept_count,
                 note, created_by)
            VALUES (%s, %s, %s, 0, 0, %s, %s)
            RETURNING id
            """,
            (batch_id, EVENT_CONTENTION, policy, "同一批原文已有一次提交持锁，本次不做回滚", created_by),
        ).fetchone()
        raise RollbackContention(batch_id, event["id"])

    # 持锁者占住全局锁直到事务提交：回滚没落地前，回滚设置不准再碰。
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (CONTENTION_LOCK_KEY,))

    existing = {
        (row["station_code"], row["observed_at"].strftime("%Y%m%d%H%M"))
        for row in conn.execute(
            "SELECT station_code, observed_at FROM landed_rows"
        ).fetchall()
    }
    accepted, rejected = classify_lines(lines, existing)

    for row in rejected:
        conn.execute(
            "INSERT INTO reject_rows (batch_id, line_no, raw_line, reason) "
            "VALUES (%s, %s, %s, %s)",
            (batch_id, row["line_no"], row["raw_line"], row["reason"]),
        )

    for row in accepted:
        conn.execute(
            """
            INSERT INTO landed_rows
                (batch_id, line_no, station_code, observed_at, tide_cm, quality_flag)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                batch_id,
                row["line_no"],
                row["station_code"],
                row["observed_at"],
                row["tide_cm"],
                row["quality_flag"],
            ),
        )

    has_bad = bool(rejected)
    if has_bad and policy == POLICY_ROLLBACK_ALL:
        return _rollback_all(conn, batch_id, accepted, rejected, policy, created_by)
    if has_bad and policy == POLICY_KEEP_GOOD:
        _record_retain(conn, batch_id, len(accepted), rejected, policy, created_by)
    status = final_status(len(accepted), len(rejected), policy)
    conn.execute(
        """
        UPDATE intake_batches
        SET status = %s, accepted_count = %s, rejected_count = %s,
            recovered_count = 0, kept_count = %s
        WHERE id = %s
        """,
        (status, len(accepted), len(rejected), len(accepted), batch_id),
    )
    return batch_id


def _rollback_all(conn, batch_id, accepted, rejected, policy, created_by):
    """整批收回：已落库的行快照后全部删除，坏行留在未接纳队列。"""
    landed_before = len(accepted)
    event = conn.execute(
        """
        INSERT INTO rollback_events
            (batch_id, event_type, closing_policy, recovered_count, kept_count,
             note, created_by)
        VALUES (%s, %s, %s, %s, 0, %s, %s)
        RETURNING id
        """,
        (
            batch_id,
            EVENT_ROLLBACK,
            policy,
            landed_before,
            "坏行出现后已落库行整批收回",
            created_by,
        ),
    ).fetchone()
    for row in accepted:
        conn.execute(
            """
            INSERT INTO withdrawn_rows
                (event_id, batch_id, line_no, station_code, observed_at,
                 tide_cm, quality_flag)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event["id"],
                batch_id,
                row["line_no"],
                row["station_code"],
                row["observed_at"],
                row["tide_cm"],
                row["quality_flag"],
            ),
        )
    conn.execute("DELETE FROM landed_rows WHERE batch_id = %s", (batch_id,))
    conn.execute(
        """
        UPDATE intake_batches
        SET status = %s, accepted_count = 0, rejected_count = %s,
            recovered_count = %s, kept_count = 0
        WHERE id = %s
        """,
        (final_status(len(accepted), len(rejected), policy), len(rejected), landed_before, batch_id),
    )
    return batch_id


def _record_retain(conn, batch_id, accepted_count, rejected, policy, created_by):
    """逐行留下：坏行进未接纳、其余照常入库，也留一条收口事件。"""
    conn.execute(
        """
        INSERT INTO rollback_events
            (batch_id, event_type, closing_policy, recovered_count, kept_count,
             note, created_by)
        VALUES (%s, %s, %s, 0, %s, %s, %s)
        """,
        (
            batch_id,
            EVENT_RETAIN,
            policy,
            accepted_count,
            "坏行单独进未接纳队列，其余行照常入库",
            created_by,
        ),
    )


def list_rollback_events(conn):
    return conn.execute(
        """
        SELECT e.id, e.batch_id, e.event_type, e.closing_policy,
               e.recovered_count, e.kept_count, e.note, e.created_by, e.created_at,
               b.title AS batch_title
        FROM rollback_events e
        JOIN intake_batches b ON b.id = e.batch_id
        ORDER BY e.id DESC
        """
    ).fetchall()


def get_event_withdrawn(conn, event_id):
    return conn.execute(
        """
        SELECT line_no, station_code, observed_at, tide_cm, quality_flag
        FROM withdrawn_rows WHERE event_id = %s ORDER BY line_no
        """,
        (event_id,),
    ).fetchall()


def get_event(conn, event_id):
    return conn.execute(
        """
        SELECT e.id, e.batch_id, e.event_type, e.closing_policy,
               e.recovered_count, e.kept_count, e.note, e.created_by, e.created_at,
               b.title AS batch_title
        FROM rollback_events e
        JOIN intake_batches b ON b.id = e.batch_id
        WHERE e.id = %s
        """,
        (event_id,),
    ).fetchone()


def list_reject_queue(conn):
    return conn.execute(
        """
        SELECT r.batch_id, b.title AS batch_title, r.line_no, r.raw_line, r.reason,
               b.closing_policy, b.created_at
        FROM reject_rows r
        JOIN intake_batches b ON b.id = r.batch_id
        ORDER BY r.id DESC
        """
    ).fetchall()


def _seed(conn):
    # 种子库沿用老的逐行留下语义，补一条当日设置让页面有默认收口。
    if not get_today_setting(conn):
        conn.execute(
            "INSERT INTO rollback_settings (closing_policy, locked_by) VALUES (%s, 'admin')",
            (POLICY_KEEP_GOOD,),
        )
    title, _, body = SEED_CLEAN.partition("\n")
    _insert_legacy_batch(conn, title, body.strip(), "admin")
    title, _, body = SEED_BROKEN.partition("\n")
    _insert_legacy_batch(conn, title, body.strip(), "admin")


def _insert_legacy_batch(conn, title, raw_text, created_by):
    """种子数据走老逻辑：坏行逐行留下，不产生回滚事件。"""
    from parser import parse_telegram_line

    batch = conn.execute(
        """
        INSERT INTO intake_batches
            (title, raw_text, status, accepted_count, rejected_count,
             created_by, closing_policy, fingerprint, kept_count)
        VALUES (%s, %s, '处理中', 0, 0, %s, %s, %s, 0)
        RETURNING id
        """,
        (title, raw_text, created_by, POLICY_KEEP_GOOD, content_fingerprint(raw_text)[0]),
    ).fetchone()
    batch_id = batch["id"]
    accepted = 0
    rejected = 0
    seen = set()
    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        parsed, reason = parse_telegram_line(raw_line)
        if reason:
            conn.execute(
                "INSERT INTO reject_rows (batch_id, line_no, raw_line, reason) "
                "VALUES (%s, %s, %s, %s)",
                (batch_id, line_no, raw_line.strip(), reason),
            )
            rejected += 1
            continue
        key = (parsed["station_code"], parsed["observed_at"])
        if key in seen:
            reason = "同一批里观测时刻重复"
        else:
            existing = conn.execute(
                "SELECT id FROM landed_rows WHERE station_code = %s AND observed_at = %s",
                key,
            ).fetchone()
            reason = "这个站这个时刻已经落过库" if existing else None
        if reason:
            conn.execute(
                "INSERT INTO reject_rows (batch_id, line_no, raw_line, reason) "
                "VALUES (%s, %s, %s, %s)",
                (batch_id, line_no, raw_line.strip(), reason),
            )
            rejected += 1
            continue
        seen.add(key)
        conn.execute(
            """
            INSERT INTO landed_rows
                (batch_id, line_no, station_code, observed_at, tide_cm, quality_flag)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                batch_id,
                line_no,
                parsed["station_code"],
                parsed["observed_at"],
                parsed["tide_cm"],
                parsed["quality_flag"],
            ),
        )
        accepted += 1
    status = final_status(accepted, rejected, POLICY_KEEP_GOOD)
    conn.execute(
        """
        UPDATE intake_batches
        SET status = %s, accepted_count = %s, rejected_count = %s, kept_count = %s
        WHERE id = %s
        """,
        (status, accepted, rejected, accepted, batch_id),
    )
