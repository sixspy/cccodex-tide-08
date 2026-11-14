"""坏行收口策略与批次归集的纯逻辑。

两种收口（也就是回滚设置里当日定死的那一个值）：

* ``rollback_all`` 整批收回：一批里只要冒出坏行，已经落库的行整批回滚；
* ``keep_good``   逐行留下：坏行单独进未接纳队列，其余行照常入库。

空行不算坏行，归集时直接跳过。
"""

import hashlib

POLICY_ROLLBACK_ALL = "rollback_all"
POLICY_KEEP_GOOD = "keep_good"

POLICY_LABELS = {
    POLICY_ROLLBACK_ALL: "整批收回",
    POLICY_KEEP_GOOD: "逐行留下",
}

# 批次状态
STATUS_CLEAN = "已归集"
STATUS_PARTIAL = "部分接纳"
STATUS_REJECTED = "未接纳"
STATUS_WITHDRAWN = "整批收回"
STATUS_CONTENTION = "回滚争用"

# 回滚事件类型
EVENT_ROLLBACK = "rollback"      # 整批收回
EVENT_RETAIN = "retain"          # 逐行留下
EVENT_CONTENTION = "contention"  # 回滚争用

EVENT_LABELS = {
    EVENT_ROLLBACK: "整批收回",
    EVENT_RETAIN: "逐行留下",
    EVENT_CONTENTION: "回滚争用",
}

MISMATCH_NOTE = "回滚收口不符"

# 全局咨询锁：回滚/归集事务持有时，回滚设置禁止改动。
CONTENTION_LOCK_KEY = -84011200001


def policy_label(policy):
    return POLICY_LABELS.get(policy, policy or "")


def content_fingerprint(raw_text):
    """同一批原文的指纹。

    返回 (指纹, 带原文行号的非空行列表)；空行不参与指纹，也不算坏行，
    但行号沿用它在原文里的位置。
    """
    numbered = [
        (line_no, line.strip())
        for line_no, line in enumerate(raw_text.splitlines(), start=1)
        if line.strip()
    ]
    digest = hashlib.sha256(
        "\n".join(text for _, text in numbered).encode("utf-8")
    ).hexdigest()
    return digest, numbered


def to_signed64(value):
    """把 16 进制指纹塞进 pg_advisory_xact_lock 的 bigint 入参。"""
    return value - (1 << 64) if value >= (1 << 63) else value


def fingerprint_lock_key(raw_text):
    digest, _ = content_fingerprint(raw_text)
    return to_signed64(int(digest[:16], 16))


def classify_lines(numbered_lines, existing_keys):
    """把带原文行号的非空行分成可落库行与坏行。

    ``existing_keys`` 是库里已有的 (站码, 时刻字符串) 集合，由调用方查好传入，
    这样这段逻辑不碰数据库，方便单测。
    """
    from parser import parse_telegram_line

    accepted = []
    rejected = []
    seen = set()
    for line_no, raw_line in numbered_lines:
        parsed, reason = parse_telegram_line(raw_line)
        if reason:
            rejected.append({"line_no": line_no, "raw_line": raw_line, "reason": reason})
            continue
        key = (parsed["station_code"], parsed["observed_at"].strftime("%Y%m%d%H%M"))
        if key in seen:
            reason = "同一批里观测时刻重复"
        elif key in existing_keys:
            reason = "这个站这个时刻已经落过库"
        if reason:
            rejected.append({"line_no": line_no, "raw_line": raw_line, "reason": reason})
            continue
        seen.add(key)
        accepted.append(
            {
                "line_no": line_no,
                "station_code": parsed["station_code"],
                "observed_at": parsed["observed_at"],
                "tide_cm": parsed["tide_cm"],
                "quality_flag": parsed["quality_flag"],
            }
        )
    return accepted, rejected


def final_status(accepted_count, rejected_count, policy, contention=False):
    """根据收口策略定批次最终状态。"""
    if contention:
        return STATUS_CONTENTION
    if not rejected_count:
        return STATUS_CLEAN
    if policy == POLICY_ROLLBACK_ALL:
        return STATUS_WITHDRAWN
    return STATUS_PARTIAL if accepted_count else STATUS_REJECTED


def is_mismatch(event_policy, current_policy):
    """事件盖的收口快照与当前生效设置对不上，即回滚收口不符。

    争用事件没有真正执行收口，不参与不符判定。
    """
    if not event_policy or not current_policy:
        return False
    return event_policy != current_policy
