"""空窗封印：双写两轨（空封占位 + 只读封印归档）与关窗水位编排。"""
from pathlib import Path
import os
import tempfile
import threading

import pytest

_tmp = tempfile.mkdtemp()
os.environ["数据目录"] = _tmp
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

from app import seed
from app.db import 连接
from app.services import 装载, 关窗, 贯验, 目录, 空封


@pytest.fixture(autouse=True)
def _db():
    seed.重置库()
    yield


def _设窗(窗号="W1", 起="2026-11-24T00:00:00Z", 止="2026-11-24T01:00:00Z"):
    目录.改窗规({"当前窗号": 窗号, "窗起": 起, "窗止": 止, "窗长秒": "3600"})


def _事件(编号="c-e1", 时="2026-11-24T00:10:00Z", 运单="运单封"):
    return {"事件编号": 编号, "运单键": 运单, "网点键": "网点-东站", "事件时": 时}


def _计数(sql, 参数=()):
    conn = 连接()
    try:
        return conn.execute(sql, 参数).fetchone()[0]
    finally:
        conn.close()


def test_空缓冲关窗写封印可回读():
    _设窗()
    报 = 关窗.关当前窗()
    assert 报["空封"] is True
    assert 报["成片数"] == 0 and 报["缓冲消耗"] == 0
    assert 报["形态标记"] == "双写两轨"

    封 = 空封.按窗("W1")
    assert 封 is not None
    assert 封["窗号"] == "W1"
    assert 封["封印键"] == 报["封印键"]
    assert 封["形态标记"] == "双写两轨"
    assert 封["valid"] == 1
    assert 封["可回读"] is True

    占位 = [r for r in 目录.切片列表() if r["形态"] == "空封占位"]
    assert len(占位) == 1
    assert 占位[0]["事件数"] == 0
    assert 占位[0]["切片编号"] == 报["封印键"]

    规 = 目录.窗规()
    assert 规["当前窗号"].startswith("W-")
    assert 规["窗起"] == "2026-11-24T01:00:00Z"  # 写封印与推进窗号同成败
    assert 规["窗止"] == "2026-11-24T02:00:00Z"

    验 = 贯验.跑一次()
    assert 验["空封笔数"] == 1
    assert 验["空封对齐"] is True
    assert 验["是否对齐"] is True


def test_有缓冲成片且无封印可区分():
    _设窗()
    装 = 装载.装一批({"事件": [_事件()]})
    assert 装["入缓冲"] == 1
    报 = 关窗.关当前窗()
    assert 报["空封"] is False
    assert 报["成片数"] == 1

    assert 空封.按窗("W1") is None
    查 = 空封.查看("W1")
    assert 查["有封印"] is False
    assert 查["可回读"] is False

    片 = [r for r in 目录.切片列表() if r["窗号"] == "W1"]
    assert len(片) == 1
    assert 片[0]["形态"] == "成片"
    assert 片[0]["事件数"] == 1
    assert 目录.窗规()["当前窗号"] == "W1"  # 有缓冲走基线，不推进

    验 = 贯验.跑一次()
    assert 验["空封笔数"] == 0
    assert 验["是否对齐"] is True


def test_关窗批次与贯验同票():
    _设窗()
    报 = 关窗.关当前窗()
    assert 报["贯验票号"] == 报["批次编号"]
    conn = 连接()
    try:
        批 = conn.execute("SELECT * FROM 关窗批次 WHERE 编号=?", (报["批次编号"],)).fetchone()
        票 = conn.execute("SELECT * FROM 窗贯验 WHERE 票号=?", (报["批次编号"],)).fetchone()
    finally:
        conn.close()
    assert 批 is not None and 批["形态"] == "空封" and 批["成片数"] == 0
    assert 票 is not None
    assert 票["空封笔数"] == 1
    assert "空封" in 票["说明"]


def test_写封印与推进窗号同败():
    _设窗()
    # 在贯验写入处制造失败：占位与封印已写，后续写票抛错，整笔须回滚
    conn = 连接()
    try:
        conn.execute(
            "CREATE TRIGGER 挡贯验 BEFORE INSERT ON 窗贯验 "
            "BEGIN SELECT RAISE(ABORT, '模拟写闸'); END"
        )
        conn.commit()
    finally:
        conn.close()

    try:
        with pytest.raises(Exception):
            关窗.关当前窗()
    finally:
        conn = 连接()
        try:
            conn.execute("DROP TRIGGER IF EXISTS 挡贯验")
        finally:
            conn.close()

    assert 空封.按窗("W1") is None
    assert _计数("SELECT COUNT(*) FROM 窗切片账 WHERE 窗号='W1' AND 形态='空封占位'") == 0
    assert 目录.窗规()["当前窗号"] == "W1"
    assert 目录.窗规()["窗起"] == "2026-11-24T00:00:00Z"
    assert _计数("SELECT COUNT(*) FROM 关窗批次 WHERE 窗号='W1'") == 0


def test_并发双关只成一次():
    for i in range(6):
        seed.重置库()
        _设窗()
        barrier = threading.Barrier(2)
        结果 = []
        错误 = []

        def 关():
            try:
                barrier.wait(timeout=5)
                结果.append(关窗.关当前窗({"假定窗号": "W1"}))
            except Exception as exc:  # noqa: BLE001
                错误.append(exc)

        ts = [threading.Thread(target=关) for _ in range(2)]
        [t.start() for t in ts]
        [t.join(timeout=15) for t in ts]

        assert not 错误, 错误
        assert len(结果) == 2
        成功 = [r for r in 结果 if r.get("空封")]
        冲突 = [r for r in 结果 if r.get("结果") == "冲突"]
        assert len(成功) == 1
        assert len(冲突) == 1
        assert _计数("SELECT COUNT(*) FROM 空窗封印 WHERE 窗号='W1'") == 1
        assert 目录.窗规()["窗起"] == "2026-11-24T01:00:00Z"


def test_空关与灌入并发不漏封又成片():
    for i in range(12):
        seed.重置库()
        _设窗()
        barrier = threading.Barrier(2)
        结果 = {}
        错误 = []

        def 关():
            try:
                barrier.wait(timeout=5)
                结果["关"] = 关窗.关当前窗({"假定窗号": "W1"})
            except Exception as exc:  # noqa: BLE001
                错误.append(exc)

        def 装():
            try:
                barrier.wait(timeout=5)
                结果["装"] = 装载.装一批({"事件": [_事件(编号=f"c-e{i}")]})
            except Exception as exc:  # noqa: BLE001
                错误.append(exc)

        ts = [threading.Thread(target=关), threading.Thread(target=装)]
        [t.start() for t in ts]
        [t.join(timeout=15) for t in ts]

        assert not 错误, 错误
        封 = 空封.按窗("W1")
        在缓冲 = _计数("SELECT COUNT(*) FROM 入窗缓冲 WHERE 窗号='W1' AND 命运='在缓冲'")
        真片 = _计数("SELECT COALESCE(SUM(事件数),0) FROM 窗切片账 WHERE 窗号='W1' AND 形态='成片'")
        迟到 = _计数("SELECT COUNT(*) FROM 迟到匣 WHERE 事件编号=?", (f"c-e{i}",))
        assert 在缓冲 == 0  # 不得把事件悬在已关窗的缓冲里
        if 封 is not None:
            assert 真片 == 0 and 迟到 == 1
            assert 结果["关"]["空封"] is True
        else:
            assert 真片 == 1 and 迟到 == 0
            assert 结果["关"]["空封"] is False
            assert 结果["装"]["入缓冲"] == 1


def test_已出片窗不补空封():
    _设窗()
    装载.装一批({"事件": [_事件()]})
    首 = 关窗.关当前窗()
    assert 首["成片数"] == 1 and 首["空封"] is False

    # 有缓冲关窗不推进水位，同窗再关不得把已成片的窗补成空封
    再 = 关窗.关当前窗()
    assert 再["结果"] == "冲突"
    assert 再["原因"] == "该窗已出片，不得补空封"
    assert 空封.按窗("W1") is None
    assert _计数("SELECT COUNT(*) FROM 窗切片账 WHERE 窗号='W1' AND 形态='空封占位'") == 0
    assert 贯验.跑一次()["空封对齐"] is True


def test_空封只读接口():
    from starlette.testclient import TestClient
    from app.main import app

    _设窗()
    with TestClient(app) as c:
        关 = c.post("/api/关窗/跑", json={})
        assert 关.status_code == 200
        assert 关.json()["空封"] is True

        d = c.get("/api/空窗封印", params={"窗号": "W1"}).json()
        assert d["有封印"] is True
        assert d["策略"] == "双写两轨"
        封 = d["封印"]
        for 键 in ("窗号", "封印键", "形态标记", "valid"):
            assert 键 in 封
        assert 封["valid"] == 1 and 封["可回读"] is True

        全 = c.get("/api/空窗封印").json()
        assert 全["条数"] == 1
        assert 全["封印"][0]["窗号"] == "W1"
        assert c.get("/api/窗贯验").status_code == 200


def test_空桩目录剔出已接线():
    from starlette.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        d = c.get("/api/空桩").json()
    assert "空窗封印" in d["已接线"]
    assert "空窗封印" not in d["空桩"]
    assert d["条数"] == len(d["空桩"])


def test_旧库补列():
    seed.重置库()
    conn = 连接()
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS 窗切片账;
            DROP TABLE IF EXISTS 窗贯验;
            DROP TABLE IF EXISTS 关窗批次;
            DROP TABLE IF EXISTS 空窗封印;
            CREATE TABLE 窗切片账 (
              切片编号 TEXT PRIMARY KEY, 窗号 TEXT NOT NULL, 运单键 TEXT NOT NULL,
              事件数 INTEGER NOT NULL, 窗起 TEXT NOT NULL, 窗止 TEXT NOT NULL,
              物化时 TEXT NOT NULL, 命运 TEXT NOT NULL DEFAULT '已成片'
            );
            CREATE TABLE 窗贯验 (
              编号 INTEGER PRIMARY KEY AUTOINCREMENT, 时刻 TEXT NOT NULL,
              缓冲笔数 INTEGER NOT NULL, 切片笔数 INTEGER NOT NULL,
              迟到笔数 INTEGER NOT NULL, 是否对齐 INTEGER NOT NULL, 说明 TEXT NOT NULL
            );
            CREATE TABLE 关窗批次 (
              编号 TEXT PRIMARY KEY, 窗号 TEXT NOT NULL, 时刻 TEXT NOT NULL,
              成片数 INTEGER NOT NULL DEFAULT 0, 报告 TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
    finally:
        conn.close()

    seed.初始化库()

    conn = 连接()
    try:
        片列 = {r["name"] for r in conn.execute("PRAGMA table_info(窗切片账)")}
        验列 = {r["name"] for r in conn.execute("PRAGMA table_info(窗贯验)")}
        批列 = {r["name"] for r in conn.execute("PRAGMA table_info(关窗批次)")}
        封表 = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='空窗封印'"
        ).fetchone()
    finally:
        conn.close()
    assert "形态" in 片列
    assert {"票号", "空封笔数"} <= 验列
    assert "形态" in 批列
    assert 封表 is not None
