import os
import sqlite3
from pathlib import Path

数据目录 = Path(os.environ.get("数据目录", Path(__file__).resolve().parent.parent / "data"))
夹具目录 = Path(os.environ.get("夹具目录", Path(__file__).resolve().parent.parent.parent / "fixtures"))
库路径 = 数据目录 / "潮切.db"


def 连接(隔离: bool = False) -> sqlite3.Connection:
    数据目录.mkdir(parents=True, exist_ok=True)
    if 隔离:
        c = sqlite3.connect(库路径, timeout=15.0, isolation_level=None)
    else:
        c = sqlite3.connect(库路径)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    if 隔离:
        c.execute("PRAGMA busy_timeout = 15000")
    return c
