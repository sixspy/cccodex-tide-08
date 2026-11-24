"""关窗：缓冲按运单物化为窗切片；缓冲为空时写空窗封印并推进水位。"""
from __future__ import annotations
import json
import uuid
from collections import defaultdict

from app.db import 连接
from app.seed import 此刻
from app.services import 目录, 时间
from app.services import 空封


def _推进窗规自(conn, 窗起: str, 窗止: str) -> tuple[str, str, str]:
    新起, 新止 = 时间.推进窗(窗起, 窗止)
    新号 = 时间.格式窗号("W", 新起)
    for k, v in {"当前窗号": 新号, "窗起": 新起, "窗止": 新止}.items():
        conn.execute(
            "INSERT INTO 窗规(键,值) VALUES (?,?) ON CONFLICT(键) DO UPDATE SET 值=excluded.值",
            (k, v),
        )
    return 新号, 新起, 新止


def _空封自(conn, 窗号: str, 窗起: str, 窗止: str, 批: str) -> dict:
    """双写两轨：空封占位 + 只读封印归档，并与窗号推进同事务。"""
    时刻 = 此刻()
    封键 = 空封.封印键(窗号)
    conn.execute(
        """INSERT INTO 窗切片账(切片编号,窗号,运单键,事件数,窗起,窗止,物化时,命运,形态)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (封键, 窗号, 空封.占位运单键, 0, 窗起, 窗止, 时刻, "已封印", 空封.占位形态),
    )
    conn.execute(
        """INSERT INTO 空窗封印(封印键,窗号,窗起,窗止,形态标记,封印时,批次编号,贯验票号,valid)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (封键, 窗号, 窗起, 窗止, 空封.形态标记, 时刻, 批, 批, 1),
    )
    报告 = {
        "批次编号": 批,
        "窗号": 窗号,
        "结果": "成功",
        "空封": True,
        "形态标记": 空封.形态标记,
        "封印键": 封键,
        "成片数": 0,
        "缓冲消耗": 0,
        "贯验票号": 批,
    }
    conn.execute(
        """INSERT INTO 关窗批次(编号,窗号,时刻,成片数,报告,形态) VALUES (?,?,?,?,?,?)""",
        (批, 窗号, 时刻, 0, json.dumps(报告, ensure_ascii=False), "空封"),
    )
    conn.execute(
        """INSERT INTO 窗贯验(时刻,缓冲笔数,切片笔数,迟到笔数,是否对齐,说明,票号,空封笔数)
           VALUES (?,?,?,?,?,?,?,?)""",
        (时刻, 0, 0, 0, 1, f"空封窗 {窗号}：占位 1 · 成片事件 0 · 在缓冲 0，封印可回读", 批, 1),
    )
    新号, 新起, 新止 = _推进窗规自(conn, 窗起, 窗止)
    报告["下一窗"] = {"窗号": 新号, "窗起": 新起, "窗止": 新止}
    return 报告


def 关当前窗(选项: dict | None = None) -> dict:
    选项 = 选项 or {}
    假定窗号 = str(选项.get("假定窗号") or "")
    规 = 目录.窗规()
    窗号 = 规.get("当前窗号", "W-demo")
    窗起 = 规.get("窗起", "")
    窗止 = 规.get("窗止", "")
    批 = f"关窗-{uuid.uuid4().hex[:8]}"

    conn = 连接(隔离=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 取得写锁后重读窗规，避免与并发关窗/装载交错
        今号 = conn.execute("SELECT 值 FROM 窗规 WHERE 键='当前窗号'").fetchone()
        今起 = conn.execute("SELECT 值 FROM 窗规 WHERE 键='窗起'").fetchone()
        今止 = conn.execute("SELECT 值 FROM 窗规 WHERE 键='窗止'").fetchone()
        窗号 = 今号["值"] if 今号 else 窗号
        窗起 = 今起["值"] if 今起 else 窗起
        窗止 = 今止["值"] if 今止 else 窗止

        if 假定窗号 and 假定窗号 != 窗号:
            conn.rollback()
            return {"结果": "冲突", "窗号": 假定窗号, "当前窗号": 窗号}

        rows = conn.execute(
            "SELECT * FROM 入窗缓冲 WHERE 窗号=? AND 命运='在缓冲'", (窗号,)
        ).fetchall()

        if not rows:
            if 空封.按窗自(conn, 窗号) is not None:
                conn.rollback()
                return {"结果": "冲突", "窗号": 窗号, "当前窗号": 窗号, "原因": "该窗已封印"}
            已有成片 = conn.execute(
                "SELECT COUNT(*) AS c FROM 窗切片账 WHERE 窗号=? AND 形态='成片'", (窗号,)
            ).fetchone()["c"]
            if 已有成片:
                conn.rollback()
                return {"结果": "冲突", "窗号": 窗号, "当前窗号": 窗号, "原因": "该窗已出片，不得补空封"}
            报告 = _空封自(conn, 窗号, 窗起, 窗止, 批)
            conn.commit()
            return 报告

        by_waybill: dict[str, list] = defaultdict(list)
        for r in rows:
            by_waybill[r["运单键"]].append(r)
        成片 = 0
        时刻 = 此刻()
        for 运单, evs in by_waybill.items():
            切片 = f"片-{窗号}-{运单}-{uuid.uuid4().hex[:6]}"
            conn.execute(
                """INSERT INTO 窗切片账(切片编号,窗号,运单键,事件数,窗起,窗止,物化时,命运,形态)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (切片, 窗号, 运单, len(evs), 窗起, 窗止, 时刻, "已成片", "成片"),
            )
            for e in evs:
                conn.execute(
                    "UPDATE 入窗缓冲 SET 命运=? WHERE 事件编号=?",
                    ("已物化", e["事件编号"]),
                )
                conn.execute(
                    "UPDATE 观测点 SET 命运=? WHERE 事件编号=?",
                    ("已成片", e["事件编号"]),
                )
            成片 += 1
        报告 = {
            "批次编号": 批,
            "窗号": 窗号,
            "结果": "成功",
            "空封": False,
            "成片数": 成片,
            "缓冲消耗": len(rows),
        }
        conn.execute(
            """INSERT INTO 关窗批次(编号,窗号,时刻,成片数,报告,形态) VALUES (?,?,?,?,?,?)""",
            (批, 窗号, 时刻, 成片, json.dumps(报告, ensure_ascii=False), "成片"),
        )
        conn.commit()
        return 报告
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
