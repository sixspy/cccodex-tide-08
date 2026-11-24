"""空窗封印：双写两轨。

窗内无事件时关窗，仍须留下可回读的封印：
- 轨一：窗切片账写「空封占位」行（事件数 0），供既有列表与事件数互证。
- 轨二：空窗封印表写归档行（窗号、封印键、形态标记、valid），只读回读。

贯验独读空封集：空封窗必须恰有一行占位、零成片事件、零在缓冲。
封印写入与关窗水位推进同事务：写封印成功才推进窗号，任一失败整笔回滚。
"""
from __future__ import annotations

from app.db import 连接

策略名 = "双写两轨"
形态标记 = "双写两轨"
占位形态 = "空封占位"
占位运单键 = "空封"


def 封印键(窗号: str) -> str:
    return f"封-{窗号}"


def _出行(行) -> dict | None:
    if 行 is None:
        return None
    d = dict(行)
    d["valid"] = int(d.get("valid") or 0)
    d["可回读"] = bool(
        d["valid"] == 1 and d.get("封印键") and d.get("形态标记") in (策略名, "仅切片占位")
    )
    return d


def 按窗自(conn, 窗号: str) -> dict | None:
    """在调用方事务内按窗号取封印；无则 None。"""
    if not 窗号:
        return None
    行 = conn.execute("SELECT * FROM 空窗封印 WHERE 窗号=?", (窗号,)).fetchone()
    return _出行(行)


def 按窗(窗号: str) -> dict | None:
    conn = 连接()
    try:
        return 按窗自(conn, 窗号)
    finally:
        conn.close()


def 查看(窗号: str = "") -> dict:
    """只读接口：有封印时可回读窗号/封印键/形态标记/valid。"""
    conn = 连接()
    try:
        if 窗号:
            封 = 按窗自(conn, 窗号)
            return {
                "策略": 策略名,
                "窗号": 窗号,
                "有封印": 封 is not None,
                "可回读": bool(封 and 封["可回读"]),
                "封印": 封,
            }
        行 = conn.execute("SELECT * FROM 空窗封印 ORDER BY 封印时 DESC").fetchall()
        return {
            "策略": 策略名,
            "条数": len(行),
            "封印": [_出行(r) for r in 行],
        }
    finally:
        conn.close()


def 列表() -> list[dict]:
    conn = 连接()
    try:
        rows = conn.execute("SELECT * FROM 空窗封印 ORDER BY 封印时 DESC").fetchall()
        return [_出行(r) for r in rows]
    finally:
        conn.close()


def 计数() -> int:
    conn = 连接()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM 空窗封印 WHERE valid=1").fetchone()["c"]
    finally:
        conn.close()


def 描述() -> dict:
    return {
        "模块": "空窗封印",
        "策略": 策略名,
        "形态标记": 形态标记,
        "占位形态": 占位形态,
        "可区分": "有封印→可回读封印键与 valid；无封印→空窗未关或成片关窗",
        "下一步": "窗切片账看占位行；窗贯验看空封笔数与空封对齐；窗规看水位推进",
    }
