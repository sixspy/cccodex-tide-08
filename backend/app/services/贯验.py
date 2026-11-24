"""窗贯验：缓冲/切片/空封/迟到互证。"""
from __future__ import annotations
from app.db import 连接
from app.seed import 此刻
from app.services import 空封


def 跑一次() -> dict:
    conn = 连接()
    try:
        缓冲 = conn.execute(
            "SELECT COUNT(*) AS c FROM 入窗缓冲 WHERE 命运='在缓冲'"
        ).fetchone()["c"]
        已物化缓冲 = conn.execute(
            "SELECT COUNT(*) AS c FROM 入窗缓冲 WHERE 命运='已物化'"
        ).fetchone()["c"]
        切片事件 = conn.execute(
            "SELECT COALESCE(SUM(事件数),0) AS c FROM 窗切片账 WHERE 形态='成片'"
        ).fetchone()["c"]
        占位 = conn.execute(
            "SELECT COUNT(*) AS c FROM 窗切片账 WHERE 形态='空封占位'"
        ).fetchone()["c"]
        封印 = conn.execute(
            "SELECT COUNT(*) AS c FROM 空窗封印 WHERE valid=1"
        ).fetchone()["c"]
        迟到 = conn.execute("SELECT COUNT(*) AS c FROM 迟到匣").fetchone()["c"]

        # 空封集互证：每个 valid 封印须恰有一行占位，且占位号=封印键
        撕裂空封 = conn.execute(
            """SELECT 封.窗号 FROM 空窗封印 封
               LEFT JOIN 窗切片账 片 ON 片.切片编号=封.封印键 AND 片.形态='空封占位' AND 片.事件数=0
               WHERE 封.valid=1 AND 片.切片编号 IS NULL"""
        ).fetchall()
        野占位 = conn.execute(
            """SELECT 片.切片编号 FROM 窗切片账 片
               LEFT JOIN 空窗封印 封 ON 封.封印键=片.切片编号 AND 封.valid=1
               WHERE 片.形态='空封占位' AND 封.封印键 IS NULL"""
        ).fetchall()
        空封对齐 = int(not 撕裂空封 and not 野占位 and int(占位) == int(封印))
        # 基线口径：已物化缓冲事件数应等于成片切片事件数之和
        对齐 = 1 if int(已物化缓冲) == int(切片事件) else 0
        全对齐 = int(bool(对齐) and bool(空封对齐))

        if 撕裂空封:
            说明 = f"空封撕裂：封印无占位 {[r['窗号'] for r in 撕裂空封]}"
        elif 野占位:
            说明 = f"空封撕裂：占位无封印 {[r['切片编号'] for r in 野占位]}"
        elif 占位 != 封印:
            说明 = f"空封撕裂：占位 {占位} 与封印 {封印} 不等"
        elif 对齐:
            说明 = f"缓冲物化与切片事件数一致；空封 {封印} 笔可回读"
        else:
            说明 = f"撕裂：已物化缓冲={已物化缓冲} 切片事件={切片事件}"

        时刻 = 此刻()
        游 = conn.execute(
            """INSERT INTO 窗贯验(时刻,缓冲笔数,切片笔数,迟到笔数,是否对齐,说明,票号,空封笔数)
               VALUES (?,?,?,?,?,?,?,?)""",
            (时刻, 缓冲, int(切片事件), 迟到, 全对齐, 说明, "", 封印),
        )
        conn.commit()
        return {
            "票号": 游.lastrowid,
            "时刻": 时刻,
            "在缓冲": 缓冲,
            "已物化缓冲": int(已物化缓冲),
            "切片事件": int(切片事件),
            "迟到": 迟到,
            "空封笔数": int(封印),
            "空封对齐": bool(空封对齐),
            "是否对齐": bool(全对齐),
            "说明": 说明,
        }
    finally:
        conn.close()
