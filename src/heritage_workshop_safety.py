"""heritage_workshop_safety 领域资料的基础结构。"""

from __future__ import annotations

# 领域事件种类。
# 第一行是基线已有的事件；其余为批次放行流程扩展的事件，
# 由 src/batch_release.py 产生与消费。
EVENT_KINDS = [
    'CRAFT_VERSIONED', 'MATERIAL_RECEIVED', 'SESSION_CLEARED', 'INCIDENT_REPORTED', 'BATCH_RELEASED',
    'MATERIAL_BATCH_QUARANTINED', 'MATERIAL_RECALLED',
    'TOOL_STATUS_CHANGED', 'TEACHER_QUALIFICATION_CHANGED',
    'SESSION_PLANNED', 'REVIEW_SUBMITTED', 'MATERIAL_REPLACED',
    'SESSION_FROZEN', 'SESSION_RESTORED', 'SESSION_COMPLETED',
    'GUARDIAN_CONFIRMED', 'PARTICIPANT_CHECKED_IN', 'NOTIFICATION_DELIVERED',
]

REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")

def validate_event(record: dict) -> list[str]:
    """检查样例事件是否具备可交换的最小字段。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    return problems
