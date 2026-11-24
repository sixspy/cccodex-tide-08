from datetime import datetime, timezone
from app.db import 连接

建表 = """
CREATE TABLE IF NOT EXISTS 窗规 (
  键 TEXT PRIMARY KEY,
  值 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 网点源 (
  源键 TEXT PRIMARY KEY,
  名称 TEXT NOT NULL,
  区域 TEXT NOT NULL DEFAULT '',
  说明 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 观测点 (
  事件编号 TEXT PRIMARY KEY,
  运单键 TEXT NOT NULL,
  网点键 TEXT NOT NULL,
  事件时 TEXT NOT NULL,
  处理时 TEXT NOT NULL,
  命运 TEXT NOT NULL DEFAULT '已装载',
  批次编号 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 入窗缓冲 (
  事件编号 TEXT PRIMARY KEY,
  运单键 TEXT NOT NULL,
  网点键 TEXT NOT NULL,
  事件时 TEXT NOT NULL,
  窗号 TEXT NOT NULL,
  命运 TEXT NOT NULL DEFAULT '在缓冲',
  入缓冲时 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 窗切片账 (
  切片编号 TEXT PRIMARY KEY,
  窗号 TEXT NOT NULL,
  运单键 TEXT NOT NULL,
  事件数 INTEGER NOT NULL,
  窗起 TEXT NOT NULL,
  窗止 TEXT NOT NULL,
  物化时 TEXT NOT NULL,
  命运 TEXT NOT NULL DEFAULT '已成片',
  形态 TEXT NOT NULL DEFAULT '成片'
);
CREATE TABLE IF NOT EXISTS 迟到匣 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  事件编号 TEXT NOT NULL,
  运单键 TEXT NOT NULL,
  原因 TEXT NOT NULL,
  事件时 TEXT NOT NULL,
  入匣时 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 窗贯验 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  时刻 TEXT NOT NULL,
  缓冲笔数 INTEGER NOT NULL,
  切片笔数 INTEGER NOT NULL,
  迟到笔数 INTEGER NOT NULL,
  是否对齐 INTEGER NOT NULL,
  说明 TEXT NOT NULL,
  票号 TEXT NOT NULL DEFAULT '',
  空封笔数 INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS 装载批次 (
  编号 TEXT PRIMARY KEY,
  时刻 TEXT NOT NULL,
  入流 INTEGER NOT NULL DEFAULT 0,
  入缓冲 INTEGER NOT NULL DEFAULT 0,
  迟到 INTEGER NOT NULL DEFAULT 0,
  报告 TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS 关窗批次 (
  编号 TEXT PRIMARY KEY,
  窗号 TEXT NOT NULL,
  时刻 TEXT NOT NULL,
  成片数 INTEGER NOT NULL DEFAULT 0,
  报告 TEXT NOT NULL DEFAULT '{}',
  形态 TEXT NOT NULL DEFAULT '成片'
);
CREATE TABLE IF NOT EXISTS 空窗封印 (
  封印键 TEXT PRIMARY KEY,
  窗号 TEXT NOT NULL UNIQUE,
  窗起 TEXT NOT NULL,
  窗止 TEXT NOT NULL,
  形态标记 TEXT NOT NULL,
  封印时 TEXT NOT NULL,
  批次编号 TEXT NOT NULL,
  贯验票号 TEXT NOT NULL,
  valid INTEGER NOT NULL DEFAULT 1,
  CHECK (形态标记 IN ('双写两轨', '仅切片占位'))
);
"""


def 此刻() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _补列(conn, 表: str, 列定义: dict[str, str]) -> None:
    """旧库升级：只补缺失列，不动已有数据。"""
    现有 = {r["name"] for r in conn.execute(f"PRAGMA table_info({表})")}
    for 列, 类型 in 列定义.items():
        if 列 not in 现有:
            conn.execute(f"ALTER TABLE {表} ADD COLUMN {列} {类型}")


def 迁移(conn) -> None:
    _补列(conn, "窗切片账", {"形态": "TEXT NOT NULL DEFAULT '成片'"})
    _补列(conn, "窗贯验", {"票号": "TEXT NOT NULL DEFAULT ''", "空封笔数": "INTEGER NOT NULL DEFAULT 0"})
    _补列(conn, "关窗批次", {"形态": "TEXT NOT NULL DEFAULT '成片'"})


def 初始化库() -> None:
    conn = 连接()
    conn.executescript(建表)
    迁移(conn)
    if conn.execute("SELECT COUNT(*) AS c FROM 窗规").fetchone()["c"] == 0:
        for k, v in {
            "窗长秒": "3600",
            "允许迟到秒": "0",
            "关窗策略": "按事件时整点",
            "当前窗号": "W-demo",
            "窗起": "2026-11-24T00:00:00Z",
            "窗止": "2026-11-24T01:00:00Z",
        }.items():
            conn.execute("INSERT INTO 窗规(键,值) VALUES (?,?)", (k, v))
    if conn.execute("SELECT COUNT(*) AS c FROM 网点源").fetchone()["c"] == 0:
        for row in [
            ("网点-东站", "东站分拨", "华东", "示范网点"),
            ("网点-西港", "西港分拨", "华东", "示范网点"),
            ("网点-北仓", "北仓分拨", "华北", "示范网点"),
        ]:
            conn.execute(
                "INSERT INTO 网点源(源键,名称,区域,说明) VALUES (?,?,?,?)", row
            )
    conn.commit()
    conn.close()


def 重置库() -> None:
    """测试用：清空全部行后重建种子，表结构保持不变。"""
    conn = 连接()
    try:
        conn.executescript(建表)
        迁移(conn)
        for 表 in (
            "空窗封印", "关窗批次", "窗贯验", "装载批次", "迟到匣",
            "窗切片账", "入窗缓冲", "观测点", "网点源", "窗规",
        ):
            conn.execute(f"DELETE FROM {表}")
        conn.commit()
    finally:
        conn.close()
    初始化库()
