"""空窗封印（已接线）

契约：
- 缓冲为空时关窗仍写空窗封印记录，避免误关漏记。
- 不得改坏基线默认关窗 happy path（事件时入窗 → 关窗物化 → 贯验）。

实现选型「双写两轨」：
- 轨一：窗切片账写「空封占位」行（事件数 0），现行账本列表可直接看到关过。
- 轨二：空窗封印表写详细封印（窗号、封印键、形态标记、valid），只读归档。
- 贯验独读空封集：占位↔封印一对一互证。
- 关窗批次与贯验同票：批次编号即贯验票号。
- 写封印与推进窗号同一事务，任一失败整笔回滚。

可区分：有封印→能回读封印键与 valid；无封印→空窗未关或成片关窗。
"""
from __future__ import annotations

from app.services import 空封

NAME = "空窗封印"
CONTRACT = "缓冲为空时关窗仍写空窗封印记录，避免误关漏记"

HOOKS = (
    "before_load",
    "after_load",
    "decide_window",
    "before_close",
    "after_close",
    "extra_verify_checks",
)


def describe() -> dict:
    return {
        "空桩": NAME,
        "契约": CONTRACT,
        "策略": 空封.策略名,
        "形态标记": 空封.形态标记,
        "占位形态": 空封.占位形态,
        "钩子": list(HOOKS),
        "默认启用": True,
        "说明": "关窗服务写空封占位与封印归档；贯验独读空封集；接口 GET /api/空窗封印",
        "接口": "/api/空窗封印?窗号=…",
    }


def before_load(batch: dict) -> dict:
    """装载前：默认透传。"""
    return batch


def after_load(report: dict) -> dict:
    """装载后：默认透传。"""
    return report


def decide_window(event: dict, rules: dict) -> str:
    """返回 入缓冲 / 迟到 / 驳回。基线默认由装载服务按事件时判定。"""
    return "入缓冲"


def before_close(window_id: str) -> None:
    """关窗前：若该窗已封印则阻断重复关窗。"""
    if window_id and 空封.按窗(window_id) is not None:
        raise ValueError(f"窗 {window_id} 已封印，拒绝重复关窗")
    return None


def after_close(report: dict) -> dict:
    """关窗后：空封回执已带下一窗水位，默认透传。"""
    return report


def extra_verify_checks(snapshot: dict) -> list[str]:
    """空封集与成片互证；返回错误文案列表，空列表表示通过。"""
    错: list[str] = []
    if (snapshot or {}).get("空封笔数", 0) != (snapshot or {}).get("占位笔数", 0):
        错.append("空封封印数与切片占位数不等")
    return 错
