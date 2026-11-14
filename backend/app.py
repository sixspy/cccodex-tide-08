from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from auth import authenticate, issue_token, read_user
from db import (
    RollbackContention,
    SettingMissing,
    connect,
    get_event,
    get_event_withdrawn,
    get_today_setting,
    init_db,
    insert_batch,
    list_reject_queue,
    list_rollback_events,
    lock_setting_for_today,
    lock_settings,
)
from parser import FLAGS
from rollback import (
    EVENT_LABELS,
    MISMATCH_NOTE,
    POLICY_KEEP_GOOD,
    POLICY_LABELS,
    POLICY_ROLLBACK_ALL,
    is_mismatch,
    policy_label,
)


class LoginBody(BaseModel):
    username: str
    password: str


class BatchBody(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    raw_text: str = Field(min_length=1)


class SettingBody(BaseModel):
    closing_policy: str = Field(pattern=f"^({POLICY_ROLLBACK_ALL}|{POLICY_KEEP_GOOD})$")


def _stamp(value):
    return value.isoformat(sep=" ", timespec="minutes") if value else None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)


def _user_or_401(authorization: str | None):
    user = read_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _writer_or_403(user):
    if user["role"] != "writer":
        raise HTTPException(status_code=403, detail="档案员不能改归集与回滚设置")


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "service": "tide-telegram-intake"}


@app.post("/api/login")
def login(body: LoginBody):
    user = authenticate(body.username.strip(), body.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {
        "access_token": issue_token(user),
        "username": user["username"],
        "role": user["role"],
        "title": user["title"],
    }


# ---------------------------------------------------------------------------
# 回滚设置（策略页）：收口写进策略页后，本日不得更换；回滚没落地前不准碰
# ---------------------------------------------------------------------------

@app.get("/api/rollback/settings")
def rollback_settings_get(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        today = get_today_setting(conn)
        latest = conn.execute(
            "SELECT setting_date, closing_policy, locked_at, locked_by "
            "FROM rollback_settings ORDER BY setting_date DESC LIMIT 1"
        ).fetchone()
    payload = {
        "options": [
            {"value": POLICY_ROLLBACK_ALL, "label": POLICY_LABELS[POLICY_ROLLBACK_ALL]},
            {"value": POLICY_KEEP_GOOD, "label": POLICY_LABELS[POLICY_KEEP_GOOD]},
        ],
        "today": None,
        "effective": None,
    }
    if today:
        payload["today"] = {
            "setting_date": str(today["setting_date"]),
            "closing_policy": today["closing_policy"],
            "closing_label": policy_label(today["closing_policy"]),
            "locked_at": _stamp(today["locked_at"]),
            "locked_by": today["locked_by"],
        }
    if latest:
        payload["effective"] = {
            "setting_date": str(latest["setting_date"]),
            "closing_policy": latest["closing_policy"],
            "closing_label": policy_label(latest["closing_policy"]),
        }
    return payload


@app.put("/api/rollback/settings")
def rollback_settings_put(body: SettingBody, authorization: str | None = Header(default=None)):
    user = _user_or_401(authorization)
    _writer_or_403(user)
    with connect() as conn:
        try:
            got_lock = lock_settings(conn)
            if not got_lock:
                raise HTTPException(status_code=409, detail="回滚还没落地，不准再碰回滚设置")
            existing = get_today_setting(conn)
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"今日收口已定为{policy_label(existing['closing_policy'])}，本日不得更换",
                )
            setting = lock_setting_for_today(conn, body.closing_policy, user["username"])
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
    return {
        "setting_date": str(setting["setting_date"]),
        "closing_policy": setting["closing_policy"],
        "closing_label": policy_label(setting["closing_policy"]),
        "locked_at": _stamp(setting["locked_at"]),
        "locked_by": setting["locked_by"],
    }


# ---------------------------------------------------------------------------
# 批次
# ---------------------------------------------------------------------------

@app.get("/api/batches")
def list_batches(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, status, accepted_count, rejected_count,
                   closing_policy, recovered_count, kept_count, created_by, created_at
            FROM intake_batches
            ORDER BY id DESC
            """
        ).fetchall()
    for row in rows:
        row["created_at"] = _stamp(row["created_at"])
        row["closing_label"] = policy_label(row["closing_policy"])
    return rows


@app.get("/api/batches/{batch_id}")
def batch_detail(batch_id: int, authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        batch = conn.execute(
            """
            SELECT id, title, raw_text, status, accepted_count, rejected_count,
                   closing_policy, recovered_count, kept_count, fingerprint,
                   created_by, created_at
            FROM intake_batches WHERE id = %s
            """,
            (batch_id,),
        ).fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="没有这一批报文")
        landed = conn.execute(
            """
            SELECT line_no, station_code, observed_at, tide_cm, quality_flag
            FROM landed_rows WHERE batch_id = %s ORDER BY line_no
            """,
            (batch_id,),
        ).fetchall()
        rejected = conn.execute(
            """
            SELECT line_no, raw_line, reason
            FROM reject_rows WHERE batch_id = %s ORDER BY line_no
            """,
            (batch_id,),
        ).fetchall()
        events = conn.execute(
            """
            SELECT id, event_type, closing_policy, recovered_count, kept_count,
                   note, created_by, created_at
            FROM rollback_events WHERE batch_id = %s ORDER BY id
            """,
            (batch_id,),
        ).fetchall()
        withdrawn = []
        if events:
            event_ids = [event["id"] for event in events]
            withdrawn = conn.execute(
                f"""
                SELECT event_id, line_no, station_code, observed_at, tide_cm, quality_flag
                FROM withdrawn_rows
                WHERE event_id = ANY(%s)
                ORDER BY line_no
                """,
                (event_ids,),
            ).fetchall()
        effective = get_today_setting(conn)
    batch["created_at"] = _stamp(batch["created_at"])
    batch["closing_label"] = policy_label(batch["closing_policy"])
    effective_policy = effective["closing_policy"] if effective else None
    for row in landed:
        row["observed_at"] = _stamp(row["observed_at"])
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
    for row in withdrawn:
        row["observed_at"] = _stamp(row["observed_at"])
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
    for event in events:
        event["created_at"] = _stamp(event["created_at"])
        event["event_label"] = EVENT_LABELS.get(event["event_type"], event["event_type"])
        event["closing_label"] = policy_label(event["closing_policy"])
        event["mismatch"] = is_mismatch(event["closing_policy"], effective_policy)
        if event["mismatch"]:
            event["mismatch_note"] = MISMATCH_NOTE
    return {
        "batch": batch,
        "landed": landed,
        "rejected": rejected,
        "events": events,
        "withdrawn": withdrawn,
        "effective_policy": effective_policy,
        "effective_label": policy_label(effective_policy),
    }


@app.post("/api/batches")
def create_batch(body: BatchBody, authorization: str | None = Header(default=None)):
    user = _user_or_401(authorization)
    _writer_or_403(user)

    title = body.title.strip()
    # 只做空值校验，不剥原文首尾：空行要保留在原行号位置（空行不算坏行）。
    raw_text = body.raw_text
    if not title or not raw_text.strip():
        raise HTTPException(status_code=400, detail="标题和报文都不能空")
    with connect() as conn:
        try:
            batch_id = insert_batch(conn, title, raw_text, user["username"])
            conn.commit()
        except SettingMissing:
            conn.rollback()
            raise HTTPException(
                status_code=400,
                detail="今日收口还没在策略页定死，先定回滚设置再归集",
            )
        except RollbackContention as exc:
            conn.commit()
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "同一批原文已有一次提交持锁，本次记回滚争用，未做回滚",
                    "batch_id": exc.batch_id,
                    "event_id": exc.event_id,
                },
            )
    return batch_detail(batch_id, authorization)


# ---------------------------------------------------------------------------
# 回滚列表与详情
# ---------------------------------------------------------------------------

@app.get("/api/rollback/events")
def rollback_events(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = list_rollback_events(conn)
        effective = get_today_setting(conn)
    effective_policy = effective["closing_policy"] if effective else None
    for row in rows:
        row["created_at"] = _stamp(row["created_at"])
        row["event_label"] = EVENT_LABELS.get(row["event_type"], row["event_type"])
        row["closing_label"] = policy_label(row["closing_policy"])
        row["mismatch"] = is_mismatch(row["closing_policy"], effective_policy)
        if row["mismatch"]:
            row["mismatch_note"] = MISMATCH_NOTE
    return rows


@app.get("/api/rollback/events/{event_id}")
def rollback_event_detail(event_id: int, authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        event = get_event(conn, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="没有这条回滚记录")
        withdrawn = get_event_withdrawn(conn, event_id)
        effective = get_today_setting(conn)
    event["created_at"] = _stamp(event["created_at"])
    event["event_label"] = EVENT_LABELS.get(event["event_type"], event["event_type"])
    event["closing_label"] = policy_label(event["closing_policy"])
    event["mismatch"] = is_mismatch(event["closing_policy"], effective["closing_policy"] if effective else None)
    if event["mismatch"]:
        event["mismatch_note"] = MISMATCH_NOTE
    for row in withdrawn:
        row["observed_at"] = _stamp(row["observed_at"])
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
    # 回滚前后的落库差：收回前 = recovered + kept，收回后留下 kept。
    landed_before = event["recovered_count"] + event["kept_count"]
    return {
        "event": event,
        "withdrawn": withdrawn,
        "landed_before": landed_before,
        "landed_after": event["kept_count"],
        "landed_diff": event["recovered_count"],
    }


# ---------------------------------------------------------------------------
# 未接纳队列
# ---------------------------------------------------------------------------

@app.get("/api/rejects")
def reject_queue(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = list_reject_queue(conn)
    for row in rows:
        row["created_at"] = _stamp(row["created_at"])
        row["closing_label"] = policy_label(row["closing_policy"])
    return rows


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
