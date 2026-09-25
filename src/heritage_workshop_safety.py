"""heritage_workshop_safety 领域资料的基础结构（兼容入口）。

批次放行流程落地后，事件种类与字段校验的实现迁移到 :mod:`src.events`，
领域流程见 :mod:`src.release`；本模块保留原有名称的再导出，
既有资料与脚本无需改动。
"""

from __future__ import annotations

from .events import EVENT_KINDS, REQUIRED_FIELDS, validate_event

__all__ = ["EVENT_KINDS", "REQUIRED_FIELDS", "validate_event"]
