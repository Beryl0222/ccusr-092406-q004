"""事件日志：批次放行流程的唯一事实来源（append-only）。

所有状态变更都以事件形式追加；进程中断后重放日志即可恢复，
同一 ``event_id`` 的重复提交不会产生第二份事实（幂等），
同一键的内容冲突会被拒绝并隔离，绝不静默覆盖。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

# 事件种类
EVENT_KINDS = [
    "CRAFT_VERSIONED",        # 工艺版本登记（含年龄/资质/材料/过敏限制）
    "MATERIAL_RECEIVED",      # 材料批号接收（成分与过敏原）
    "TOOL_STATUS_REPORTED",   # 工具校准/失效状态上报
    "TEACHER_QUALIFIED",      # 教师资质登记/失效
    "SESSION_SCHEDULED",      # 场次排期（容量）
    "SESSION_LOCKED",         # 场次锁定六要素 + 系统风险建议
    "REVIEW_RECORDED",        # 角色复核（同意/拒绝，含理由）
    "SESSION_RELEASED",       # 复核齐备，放行（记录放行人）
    "PARTICIPANT_REGISTERED", # 参与者报名（占名额）
    "GUARDIAN_CONFIRMED",     # 监护确认（含当时限制版本，幂等）
    "PARTICIPANT_CHECKED_IN", # 签到（仅放行后）
    "SESSION_COMPLETED",      # 场次完成（此后依据快照永久保留）
    "BATCH_RECALLED",         # 材料召回
    "TOOL_DECOMMISSIONED",    # 设备失效
    "INCIDENT_REPORTED",      # 现场事件
    "INCIDENT_RESOLVED",      # 现场事件解除（场次自动恢复，历史冻结留痕）
    "SESSION_FROZEN",         # 系统派生：冻结受影响场次
    "MATERIAL_SWAPPED",       # 替换材料（限制重算，旧签署作废）
    "LOCK_SUPERSEDED",        # 系统派生：旧锁作废，等待按新依据锁定
    "REVIEWS_VOIDED",         # 系统派生：旧复核/放行因依据变化作废
    "BATCH_QUARANTINED",      # 同批号内容冲突，整批隔离
    "NOTIFICATION_REQUESTED", # 请求下发停用/冻结通知（发件箱）
    "NOTIFICATION_DELIVERED", # 通知已下发（幂等键保证只一次）
]

# 兼容基线名称：既有资料仍称 SESSION_CLEARED / BATCH_RELEASED
EVENT_KINDS_ALIASES = {
    "SESSION_CLEARED": "SESSION_RELEASED",
    "BATCH_RELEASED": "SESSION_RELEASED",
}

REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")


class EventConflictError(Exception):
    """同一 event_id 以不同内容重复提交——必须隔离，不能覆盖。"""


class EventStore:
    """JSONL 事件日志；写入走同目录临时文件 + rename，保证原子落盘。"""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        records: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        return records

    # -- 写入 ---------------------------------------------------------------

    def append(self, event: dict) -> bool:
        """追加一个事件。

        幂等语义：``event_id`` 已存在且内容一致 -> 忽略，返回 False；
        内容不一致 -> 抛 :class:`EventConflictError`，由上层隔离。
        """
        problems = validate_event(event)
        if problems:
            raise ValueError(f"事件缺少必要字段或类型非法: {problems}")
        return self._append_lines([event])

    def append_many(self, events: Iterable[dict]) -> int:
        """原子批量追加；任一新事件与已存内容冲突则整批不落盘。"""
        events = list(events)
        for event in events:
            problems = validate_event(event)
            if problems:
                raise ValueError(f"事件缺少必要字段或类型非法: {problems}")
        return self._append_lines(events)

    def _append_lines(self, new_events: list[dict]) -> bool:
        existing = self.load()
        by_id: dict[str, dict] = {}
        for event in existing:
            # 同批内自身重复也算冲突输入
            if event["event_id"] in by_id and event != by_id[event["event_id"]]:
                raise EventConflictError(event["event_id"])
            by_id[event["event_id"]] = event

        to_write: list[dict] = []
        for event in new_events:
            old = by_id.get(event["event_id"])
            if old is not None:
                if _canonical(old) != _canonical(event):
                    raise EventConflictError(event["event_id"])
                continue  # 完全相同的重复提交，忽略
            if event["event_id"] in {e["event_id"] for e in to_write}:
                raise EventConflictError(event["event_id"])
            to_write.append(event)

        if not to_write:
            return False

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for event in existing + to_write:
                    handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return True


def _canonical(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, sort_keys=True)


def validate_event(record: dict) -> list[str]:
    """检查事件是否具备可交换的最小字段，且类型在约定范围内。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    kind = record.get("kind")
    if kind not in EVENT_KINDS and kind not in EVENT_KINDS_ALIASES:
        problems.append("kind")
    return problems
