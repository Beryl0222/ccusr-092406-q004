"""非遗工坊亲子体验批次安全放行服务。

在基线事件契约（src/heritage_workshop_safety.py）之上，把材料接收与工艺版本
扩展为完整的批次放行流程：

- 每场体验锁定工艺版本、材料批号、工具状态、教师资质、年龄限制与监护确认；
- 系统先给出风险建议，安全、工艺、运营三类角色复核通过后才放行签到；
- 材料召回、设备失效、教师资质变化或现场事件只冻结受影响的未完结场次，
  已经完成的体验保留当时依据；
- 替换材料会作废旧复核、重新计算年龄与过敏限制，不允许沿用旧签署；
- 家庭重复提交确认不会产生第二份资格，同一批号内容冲突会被隔离；
- 全部状态由事件日志投影而来，进程中断后重放日志即可继续未完成的复核与
  通知，既不重复占用名额，也不重复下发停用消息；
- 任一参与记录都可追溯到实际工艺版本、材料批号与放行复核人。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.heritage_workshop_safety import EVENT_KINDS

# ---- 材料批号状态 ----
BATCH_RECEIVED = "RECEIVED"
BATCH_QUARANTINED = "QUARANTINED"
BATCH_RECALLED = "RECALLED"

# ---- 工具状态 ----
TOOL_OPERATIONAL = "OPERATIONAL"
TOOL_NEEDS_CALIBRATION = "NEEDS_CALIBRATION"
TOOL_OUT_OF_SERVICE = "OUT_OF_SERVICE"
TOOL_STATUSES = (TOOL_OPERATIONAL, TOOL_NEEDS_CALIBRATION, TOOL_OUT_OF_SERVICE)

# ---- 场次状态 ----
SESSION_UNDER_REVIEW = "UNDER_REVIEW"
SESSION_CLEARED = "CLEARED"
SESSION_FROZEN = "FROZEN"
SESSION_COMPLETED = "COMPLETED"

# ---- 风险建议严重度 ----
SEVERITY_WARNING = "WARNING"
SEVERITY_BLOCKER = "BLOCKER"

# ---- 复核决定 ----
DECISION_APPROVE = "APPROVE"
DECISION_REJECT = "REJECT"

#: 放行前必须全部复核通过的角色
REVIEW_ROLES = ("SAFETY_OFFICER", "CRAFT_LEAD", "OPS_MANAGER")

# ---- 冻结原因 ----
CAUSE_MATERIAL = "MATERIAL"
CAUSE_TOOL = "TOOL"
CAUSE_TEACHER = "TEACHER"
CAUSE_INCIDENT = "INCIDENT"

# ---- 通知种类 ----
NOTICE_SESSION_FROZEN = "SESSION_FROZEN_NOTICE"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CraftVersion:
    """工艺版本：年龄限制、所需教师资质与监护确认门槛。"""

    craft_id: str
    version: str
    min_age: int
    max_age: int
    required_qualification: str
    guardian_required_below: int

    @property
    def version_id(self) -> str:
        return f"{self.craft_id}@{self.version}"


@dataclass
class MaterialBatch:
    """材料批号：过敏提示与最低年龄随批号锁定，冲突内容记入 conflicts。"""

    batch_id: str
    material: str
    allergens: tuple[str, ...]
    min_age: int | None
    quantity: int
    status: str = BATCH_RECEIVED
    conflicts: list[dict] = field(default_factory=list)


@dataclass
class Tool:
    tool_id: str
    kind: str
    status: str = TOOL_OPERATIONAL


@dataclass
class Teacher:
    teacher_id: str
    qualifications: tuple[str, ...]


@dataclass
class RiskFinding:
    """一条风险建议：code 便于程序判断，message 面向复核人。"""

    code: str
    severity: str
    message: str
    subject_id: str


@dataclass
class Review:
    role: str
    reviewer_id: str
    decision: str
    note: str
    event_id: str


@dataclass
class Session:
    """场次：排场时锁定的全部依据与当前状态。"""

    session_id: str
    craft_id: str
    craft_version: str
    required_qualification: str
    min_age: int
    max_age: int
    guardian_required_below: int
    material_batch_ids: list[str]
    tool_ids: list[str]
    teacher_id: str
    capacity: int
    status: str = SESSION_UNDER_REVIEW
    reviews: dict[str, Review] = field(default_factory=dict)
    freeze_reason: str | None = None
    freeze_cause_kind: str | None = None
    freeze_cause_subject_id: str | None = None
    release_event_id: str | None = None
    completion_snapshot: dict | None = None


@dataclass
class Eligibility:
    """家庭监护确认产生的参与资格，占用一个名额。"""

    eligibility_id: str
    session_id: str
    family_id: str
    participant_id: str
    participant_age: int
    guardian_name: str


@dataclass
class Participation:
    participation_id: str
    session_id: str
    family_id: str
    eligibility_id: str
    checked_in_at: str


@dataclass
class Notification:
    """待下发的通知（停用消息），按确定性 ID 去重，投递后标记。"""

    notification_id: str
    kind: str
    session_id: str
    message: str
    delivered: bool = False


class BatchReleaseService:
    """批次放行服务。所有状态都是事件日志的投影，可重放恢复。"""

    def __init__(self, log_path: str | Path | None = None):
        self._log_path = Path(log_path) if log_path else None
        self._events: list[dict] = []
        self._seq = 0
        self.craft_versions: dict[str, CraftVersion] = {}
        self.batches: dict[str, MaterialBatch] = {}
        self.tools: dict[str, Tool] = {}
        self.teachers: dict[str, Teacher] = {}
        self.sessions: dict[str, Session] = {}
        self.eligibilities: dict[tuple[str, str], Eligibility] = {}
        self.participations: dict[str, Participation] = {}
        self.notifications: dict[str, Notification] = {}
        if self._log_path and self._log_path.exists():
            self._replay()

    @classmethod
    def recover(cls, log_path: str | Path) -> "BatchReleaseService":
        """从事件日志恢复服务，继续未完成的复核与通知。"""
        return cls(log_path=log_path)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    # ------------------------------------------------------------------
    # 事件日志
    # ------------------------------------------------------------------

    def _append(self, kind: str, subject_id: str, payload: dict, occurred_at: str | None = None) -> dict:
        if kind not in EVENT_KINDS:
            raise ValueError(f"事件种类不在领域契约中: {kind}")
        self._seq += 1
        event = {
            "event_id": f"evt-{self._seq:06d}",
            "kind": kind,
            "occurred_at": occurred_at or _utcnow(),
            "subject_id": subject_id,
            "payload": payload,
        }
        if self._log_path:
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._events.append(event)
        self._apply(event)
        return event

    def _replay(self) -> None:
        for line in self._log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            self._events.append(event)
            self._seq = max(self._seq, int(str(event["event_id"]).rsplit("-", 1)[1]))
            self._apply(event)

    def _apply(self, event: dict) -> None:
        kind = event["kind"]
        p = event["payload"]
        if kind == "CRAFT_VERSIONED":
            cv = CraftVersion(p["craft_id"], p["version"], p["min_age"], p["max_age"],
                              p["required_qualification"], p["guardian_required_below"])
            self.craft_versions[cv.version_id] = cv
        elif kind == "MATERIAL_RECEIVED":
            # 冲突内容不覆盖原批号，由 MATERIAL_BATCH_QUARANTINED 隔离
            if p["batch_id"] not in self.batches:
                self.batches[p["batch_id"]] = MaterialBatch(
                    p["batch_id"], p["material"], tuple(p["allergens"]), p.get("min_age"), p["quantity"])
        elif kind == "MATERIAL_BATCH_QUARANTINED":
            batch = self.batches[p["batch_id"]]
            batch.status = BATCH_QUARANTINED
            batch.conflicts.append({"expected": p["expected"], "received": p["received"]})
        elif kind == "MATERIAL_RECALLED":
            self.batches[p["batch_id"]].status = BATCH_RECALLED
        elif kind == "TOOL_STATUS_CHANGED":
            tool = self.tools.get(p["tool_id"])
            if tool is None:
                self.tools[p["tool_id"]] = Tool(p["tool_id"], p.get("kind") or "", p["status"])
            else:
                tool.status = p["status"]
                if p.get("kind"):
                    tool.kind = p["kind"]
        elif kind == "TEACHER_QUALIFICATION_CHANGED":
            self.teachers[p["teacher_id"]] = Teacher(p["teacher_id"], tuple(p["qualifications"]))
        elif kind == "SESSION_PLANNED":
            self.sessions[p["session_id"]] = Session(
                session_id=p["session_id"], craft_id=p["craft_id"], craft_version=p["craft_version"],
                required_qualification=p["required_qualification"],
                min_age=p["min_age"], max_age=p["max_age"],
                guardian_required_below=p["guardian_required_below"],
                material_batch_ids=list(p["material_batch_ids"]), tool_ids=list(p["tool_ids"]),
                teacher_id=p["teacher_id"], capacity=p["capacity"])
        elif kind == "REVIEW_SUBMITTED":
            session = self.sessions[p["session_id"]]
            session.reviews[p["role"]] = Review(p["role"], p["reviewer_id"], p["decision"],
                                                p.get("note", ""), event["event_id"])
        elif kind == "BATCH_RELEASED":
            session = self.sessions[p["session_id"]]
            session.status = SESSION_CLEARED
            session.release_event_id = event["event_id"]
        elif kind == "SESSION_FROZEN":
            session = self.sessions[p["session_id"]]
            session.status = SESSION_FROZEN
            session.freeze_reason = p["reason"]
            session.freeze_cause_kind = p["cause_kind"]
            session.freeze_cause_subject_id = p["cause_subject_id"]
            # 通知 ID 由冻结事件派生，重放与重试都不会产生重复停用消息
            ntf_id = f"ntf:freeze:{event['event_id']}"
            self.notifications[ntf_id] = Notification(ntf_id, NOTICE_SESSION_FROZEN,
                                                      session.session_id, p["reason"])
        elif kind == "SESSION_RESTORED":
            session = self.sessions[p["session_id"]]
            session.status = SESSION_UNDER_REVIEW
            session.freeze_reason = None
            session.freeze_cause_kind = None
            session.freeze_cause_subject_id = None
        elif kind == "SESSION_COMPLETED":
            session = self.sessions[p["session_id"]]
            session.status = SESSION_COMPLETED
            session.completion_snapshot = p["snapshot"]
        elif kind == "MATERIAL_REPLACED":
            session = self.sessions[p["session_id"]]
            session.material_batch_ids = [p["new_batch_id"] if b == p["old_batch_id"] else b
                                          for b in session.material_batch_ids]
            session.reviews.clear()
            session.status = SESSION_UNDER_REVIEW
            session.freeze_reason = None
            session.freeze_cause_kind = None
            session.freeze_cause_subject_id = None
            session.release_event_id = None
        elif kind == "GUARDIAN_CONFIRMED":
            self.eligibilities[(p["session_id"], p["family_id"])] = Eligibility(
                p["eligibility_id"], p["session_id"], p["family_id"],
                p["participant_id"], p["participant_age"], p["guardian_name"])
        elif kind == "PARTICIPANT_CHECKED_IN":
            self.participations[p["participation_id"]] = Participation(
                p["participation_id"], p["session_id"], p["family_id"],
                p["eligibility_id"], event["occurred_at"])
        elif kind == "NOTIFICATION_DELIVERED":
            ntf = self.notifications.get(p["notification_id"])
            if ntf:
                ntf.delivered = True
        elif kind == "INCIDENT_REPORTED":
            pass  # 冻结范围由独立的 SESSION_FROZEN 事件表达
        else:
            raise ValueError(f"未知事件种类: {kind}")

    # ------------------------------------------------------------------
    # 登记：工艺版本、材料批号、工具、教师
    # ------------------------------------------------------------------

    def register_craft_version(self, craft_id: str, version: str, min_age: int, max_age: int,
                               required_qualification: str, guardian_required_below: int = 18,
                               occurred_at: str | None = None) -> CraftVersion:
        if min_age < 0 or max_age < min_age:
            raise ValueError("年龄区间不合法")
        self._append("CRAFT_VERSIONED", f"{craft_id}@{version}", {
            "craft_id": craft_id, "version": version, "min_age": min_age, "max_age": max_age,
            "required_qualification": required_qualification,
            "guardian_required_below": guardian_required_below,
        }, occurred_at)
        return self.craft_versions[f"{craft_id}@{version}"]

    def receive_material(self, batch_id: str, material: str, allergens: tuple[str, ...] = (),
                         min_age: int | None = None, quantity: int = 0,
                         occurred_at: str | None = None) -> MaterialBatch:
        """接收材料批号。同一批号内容冲突时隔离批号，不覆盖原始记录。"""
        allergens = tuple(sorted(allergens))
        payload = {"batch_id": batch_id, "material": material, "allergens": list(allergens),
                   "min_age": min_age, "quantity": quantity}
        existing = self.batches.get(batch_id)
        if existing is not None:
            same = (existing.material == material and existing.allergens == allergens
                    and existing.min_age == min_age)
            if same:
                return existing  # 重复接收，幂等
            self._append("MATERIAL_RECEIVED", batch_id, payload, occurred_at)
            self._append("MATERIAL_BATCH_QUARANTINED", batch_id, {
                "batch_id": batch_id,
                "expected": {"material": existing.material, "allergens": list(existing.allergens),
                             "min_age": existing.min_age},
                "received": payload,
            }, occurred_at)
            self._freeze_sessions(lambda s: batch_id in s.material_batch_ids,
                                  CAUSE_MATERIAL, batch_id,
                                  f"材料批号 {batch_id} 内容冲突，已隔离", occurred_at)
            return self.batches[batch_id]
        self._append("MATERIAL_RECEIVED", batch_id, payload, occurred_at)
        return self.batches[batch_id]

    def set_tool_status(self, tool_id: str, status: str, kind: str = "",
                        occurred_at: str | None = None) -> Tool:
        """登记或变更工具状态；失效时冻结受影响场次，恢复时解除对应冻结。"""
        if status not in TOOL_STATUSES:
            raise ValueError(f"未知工具状态: {status}")
        self._append("TOOL_STATUS_CHANGED", tool_id,
                     {"tool_id": tool_id, "status": status, "kind": kind}, occurred_at)
        if status == TOOL_OPERATIONAL:
            self._restore_sessions(
                lambda s: s.freeze_cause_kind == CAUSE_TOOL and s.freeze_cause_subject_id == tool_id,
                occurred_at)
            self._retry_release(lambda s: tool_id in s.tool_ids, occurred_at)
        else:
            self._freeze_sessions(lambda s: tool_id in s.tool_ids,
                                  CAUSE_TOOL, tool_id,
                                  f"工具 {tool_id} 状态变更为 {status}", occurred_at)
        return self.tools[tool_id]

    def set_teacher_qualifications(self, teacher_id: str, qualifications: tuple[str, ...],
                                   occurred_at: str | None = None) -> Teacher:
        """登记或变更教师资质；不再满足要求的场次被冻结，恢复资质后解除。"""
        quals = tuple(sorted(qualifications))
        self._append("TEACHER_QUALIFICATION_CHANGED", teacher_id,
                     {"teacher_id": teacher_id, "qualifications": list(quals)}, occurred_at)
        teacher = self.teachers[teacher_id]
        self._freeze_sessions(
            lambda s: s.teacher_id == teacher_id and s.required_qualification not in teacher.qualifications,
            CAUSE_TEACHER, teacher_id, f"教师 {teacher_id} 资质变化，不再满足要求", occurred_at)
        self._restore_sessions(
            lambda s: s.freeze_cause_kind == CAUSE_TEACHER
            and s.freeze_cause_subject_id == teacher_id
            and s.required_qualification in teacher.qualifications,
            occurred_at)
        self._retry_release(lambda s: s.teacher_id == teacher_id, occurred_at)
        return teacher

    # ------------------------------------------------------------------
    # 排场与风险建议
    # ------------------------------------------------------------------

    def plan_session(self, session_id: str, craft_id: str, version: str,
                     material_batch_ids: list[str], tool_ids: list[str], teacher_id: str,
                     capacity: int, occurred_at: str | None = None) -> Session:
        """排场：锁定工艺版本、材料批号、工具、教师、年龄限制与名额。"""
        if session_id in self.sessions:
            raise ValueError(f"场次已存在: {session_id}")
        cv = self.craft_versions.get(f"{craft_id}@{version}")
        if cv is None:
            raise ValueError(f"未知工艺版本: {craft_id}@{version}")
        for batch_id in material_batch_ids:
            if batch_id not in self.batches:
                raise ValueError(f"未知材料批号: {batch_id}")
        for tool_id in tool_ids:
            if tool_id not in self.tools:
                raise ValueError(f"未知工具: {tool_id}")
        if teacher_id not in self.teachers:
            raise ValueError(f"未知教师: {teacher_id}")
        if capacity <= 0:
            raise ValueError("名额必须为正数")
        self._append("SESSION_PLANNED", session_id, {
            "session_id": session_id, "craft_id": craft_id, "craft_version": version,
            "required_qualification": cv.required_qualification,
            "min_age": cv.min_age, "max_age": cv.max_age,
            "guardian_required_below": cv.guardian_required_below,
            "material_batch_ids": list(material_batch_ids), "tool_ids": list(tool_ids),
            "teacher_id": teacher_id, "capacity": capacity,
        }, occurred_at)
        return self.sessions[session_id]

    def risk_advice(self, session_id: str) -> list[RiskFinding]:
        """系统给出的风险建议：阻断项必须解除，提醒项供复核人参考。"""
        return self._risk_findings(self._session(session_id))

    def _risk_findings(self, session: Session) -> list[RiskFinding]:
        findings: list[RiskFinding] = []
        for batch_id in session.material_batch_ids:
            batch = self.batches.get(batch_id)
            if batch is None:
                findings.append(RiskFinding("BATCH_UNKNOWN", SEVERITY_BLOCKER,
                                            f"材料批号 {batch_id} 无接收记录", batch_id))
                continue
            if batch.status == BATCH_QUARANTINED:
                findings.append(RiskFinding("BATCH_QUARANTINED", SEVERITY_BLOCKER,
                                            f"材料批号 {batch_id} 内容冲突，已隔离", batch_id))
            elif batch.status == BATCH_RECALLED:
                findings.append(RiskFinding("BATCH_RECALLED", SEVERITY_BLOCKER,
                                            f"材料批号 {batch_id} 已召回", batch_id))
            if batch.allergens:
                findings.append(RiskFinding("ALLERGEN_NOTICE", SEVERITY_WARNING,
                                            f"材料批号 {batch_id} 含过敏提示：{'、'.join(batch.allergens)}",
                                            batch_id))
        for tool_id in session.tool_ids:
            tool = self.tools.get(tool_id)
            if tool is None:
                findings.append(RiskFinding("TOOL_UNKNOWN", SEVERITY_BLOCKER,
                                            f"工具 {tool_id} 无状态记录", tool_id))
            elif tool.status != TOOL_OPERATIONAL:
                findings.append(RiskFinding("TOOL_NOT_OPERATIONAL", SEVERITY_BLOCKER,
                                            f"工具 {tool_id} 状态为 {tool.status}，不可用", tool_id))
        teacher = self.teachers.get(session.teacher_id)
        if teacher is None or session.required_qualification not in teacher.qualifications:
            findings.append(RiskFinding("TEACHER_QUALIFICATION_MISSING", SEVERITY_BLOCKER,
                                        f"教师 {session.teacher_id} 缺少资质 {session.required_qualification}",
                                        session.teacher_id))
        min_age = self._effective_min_age(session)
        if min_age > session.max_age:
            findings.append(RiskFinding("AGE_WINDOW_EMPTY", SEVERITY_BLOCKER,
                                        f"年龄限制冲突：最低 {min_age} 岁高于最高 {session.max_age} 岁",
                                        session.session_id))
        elif session.guardian_required_below > min_age:
            findings.append(RiskFinding("GUARDIAN_CONSENT_REQUIRED", SEVERITY_WARNING,
                                        f"{session.guardian_required_below} 岁以下参与者需监护确认",
                                        session.session_id))
        return findings

    # ------------------------------------------------------------------
    # 复核与放行
    # ------------------------------------------------------------------

    def submit_review(self, session_id: str, role: str, reviewer_id: str, approve: bool = True,
                      note: str = "", occurred_at: str | None = None) -> Session:
        """提交角色复核；全部角色通过且无阻断项时自动放行。"""
        session = self._session(session_id)
        if role not in REVIEW_ROLES:
            raise ValueError(f"未知复核角色: {role}")
        if session.status != SESSION_UNDER_REVIEW:
            raise ValueError(f"场次状态为 {session.status}，不能复核")
        self._append("REVIEW_SUBMITTED", session_id, {
            "session_id": session_id, "role": role, "reviewer_id": reviewer_id,
            "decision": DECISION_APPROVE if approve else DECISION_REJECT, "note": note,
        }, occurred_at)
        self._maybe_release(session_id, occurred_at)
        return self.sessions[session_id]

    def _maybe_release(self, session_id: str, occurred_at: str | None = None) -> None:
        session = self.sessions[session_id]
        if session.status != SESSION_UNDER_REVIEW:
            return
        approved = {role for role, r in session.reviews.items() if r.decision == DECISION_APPROVE}
        if not set(REVIEW_ROLES) <= approved:
            return
        findings = self._risk_findings(session)
        if any(f.severity == SEVERITY_BLOCKER for f in findings):
            return
        self._append("BATCH_RELEASED", session_id, {
            "session_id": session_id,
            "reviews": [asdict(r) for r in session.reviews.values()],
            "risk": [asdict(f) for f in findings],
        }, occurred_at)

    def _retry_release(self, predicate, occurred_at: str | None = None) -> None:
        """阻塞因素解除后，为已集齐复核的场次补做放行尝试。"""
        for session in list(self.sessions.values()):
            if session.status == SESSION_UNDER_REVIEW and predicate(session):
                self._maybe_release(session.session_id, occurred_at)

    # ------------------------------------------------------------------
    # 监护确认与签到
    # ------------------------------------------------------------------

    def confirm_guardian(self, session_id: str, family_id: str, participant_id: str,
                         participant_age: int, guardian_name: str,
                         occurred_at: str | None = None) -> Eligibility:
        """家庭监护确认。重复提交返回既有资格，不产生第二份，也不重复占用名额。"""
        session = self._session(session_id)
        key = (session_id, family_id)
        existing = self.eligibilities.get(key)
        if existing is not None:
            if existing.participant_id != participant_id:
                raise ValueError(
                    f"家庭 {family_id} 已确认参与者 {existing.participant_id}，与本次提交冲突")
            return existing
        if session.status not in (SESSION_UNDER_REVIEW, SESSION_CLEARED):
            raise ValueError(f"场次状态为 {session.status}，不能确认")
        min_age = self._effective_min_age(session)
        if not min_age <= participant_age <= session.max_age:
            raise ValueError(f"参与者年龄 {participant_age} 超出限制 {min_age}-{session.max_age} 岁")
        if len(self._session_eligibilities(session_id)) >= session.capacity:
            raise ValueError("场次名额已满")
        eligibility_id = f"elg:{session_id}:{family_id}"
        self._append("GUARDIAN_CONFIRMED", session_id, {
            "session_id": session_id, "family_id": family_id, "eligibility_id": eligibility_id,
            "participant_id": participant_id, "participant_age": participant_age,
            "guardian_name": guardian_name,
        }, occurred_at)
        return self.eligibilities[key]

    def check_in(self, session_id: str, family_id: str,
                 occurred_at: str | None = None) -> Participation:
        """签到。需要场次已放行且已有监护确认；重复签到返回同一记录。"""
        session = self._session(session_id)
        eligibility = self.eligibilities.get((session_id, family_id))
        if eligibility is None:
            raise ValueError("缺少监护确认，不能签到")
        participation_id = f"prt:{session_id}:{family_id}"
        existing = self.participations.get(participation_id)
        if existing is not None:
            return existing
        if session.status != SESSION_CLEARED:
            raise ValueError(f"场次状态为 {session.status}，未放行签到")
        min_age = self._effective_min_age(session)
        if not min_age <= eligibility.participant_age <= session.max_age:
            raise ValueError("参与者年龄已超出场次当前限制")
        self._append("PARTICIPANT_CHECKED_IN", session_id, {
            "session_id": session_id, "family_id": family_id,
            "eligibility_id": eligibility.eligibility_id, "participation_id": participation_id,
        }, occurred_at)
        return self.participations[participation_id]

    def complete_session(self, session_id: str, occurred_at: str | None = None) -> Session:
        """完结场次：把当时依据（工艺、材料、工具、教师、复核）固化进快照。"""
        session = self._session(session_id)
        if session.status != SESSION_CLEARED:
            raise ValueError("仅已放行场次可标记完成")
        self._append("SESSION_COMPLETED", session_id,
                     {"session_id": session_id, "snapshot": self._session_snapshot(session)},
                     occurred_at)
        return self.sessions[session_id]

    # ------------------------------------------------------------------
    # 召回、设备失效、资质变化与现场事件：只冻结受影响的未完结场次
    # ------------------------------------------------------------------

    def recall_material(self, batch_id: str, reason: str, occurred_at: str | None = None) -> None:
        batch = self._batch(batch_id)
        if batch.status != BATCH_RECALLED:
            self._append("MATERIAL_RECALLED", batch_id,
                         {"batch_id": batch_id, "reason": reason}, occurred_at)
        # 冻结扫描幂等：中断后重发召回可继续冻结剩余场次
        self._freeze_sessions(lambda s: batch_id in s.material_batch_ids,
                              CAUSE_MATERIAL, batch_id, f"材料召回：{reason}", occurred_at)

    def report_incident(self, description: str, session_id: str | None = None,
                        batch_id: str | None = None, tool_id: str | None = None,
                        occurred_at: str | None = None) -> None:
        """现场事件：按场次、批号或工具冻结受影响的未完结场次。"""
        if not (session_id or batch_id or tool_id):
            raise ValueError("现场事件需至少关联场次、批号或工具之一")
        if session_id and session_id not in self.sessions:
            raise ValueError(f"未知场次: {session_id}")
        if batch_id and batch_id not in self.batches:
            raise ValueError(f"未知材料批号: {batch_id}")
        if tool_id and tool_id not in self.tools:
            raise ValueError(f"未知工具: {tool_id}")
        subject_id = session_id or batch_id or tool_id
        self._append("INCIDENT_REPORTED", subject_id, {
            "description": description, "session_id": session_id,
            "batch_id": batch_id, "tool_id": tool_id,
        }, occurred_at)

        def affected(session: Session) -> bool:
            return bool(
                (session_id and session.session_id == session_id)
                or (batch_id and batch_id in session.material_batch_ids)
                or (tool_id and tool_id in session.tool_ids))

        self._freeze_sessions(affected, CAUSE_INCIDENT, subject_id,
                              f"现场事件：{description}", occurred_at)

    def _freeze_sessions(self, predicate, cause_kind: str, cause_subject_id: str,
                         reason: str, occurred_at: str | None = None) -> None:
        for session in list(self.sessions.values()):
            if session.status in (SESSION_UNDER_REVIEW, SESSION_CLEARED) and predicate(session):
                self._append("SESSION_FROZEN", session.session_id, {
                    "session_id": session.session_id, "reason": reason,
                    "cause_kind": cause_kind, "cause_subject_id": cause_subject_id,
                }, occurred_at)

    def _restore_sessions(self, predicate, occurred_at: str | None = None) -> None:
        for session in list(self.sessions.values()):
            if session.status == SESSION_FROZEN and predicate(session):
                self._append("SESSION_RESTORED", session.session_id,
                             {"session_id": session.session_id}, occurred_at)
                self._maybe_release(session.session_id, occurred_at)

    # ------------------------------------------------------------------
    # 材料替换：作废旧复核，重新计算限制
    # ------------------------------------------------------------------

    def replace_material(self, session_id: str, old_batch_id: str, new_batch_id: str,
                         occurred_at: str | None = None) -> Session:
        """替换场次材料批号。旧复核全部作废，年龄与过敏限制按新批号重算。"""
        session = self._session(session_id)
        if session.status == SESSION_COMPLETED:
            raise ValueError("已完成场次保留当时依据，不可替换材料")
        if old_batch_id not in session.material_batch_ids:
            raise ValueError(f"场次未使用批号 {old_batch_id}")
        new_batch = self.batches.get(new_batch_id)
        if new_batch is None:
            raise ValueError(f"未知材料批号: {new_batch_id}")
        if new_batch.status != BATCH_RECEIVED:
            raise ValueError(f"批号 {new_batch_id} 已隔离或召回，不能用于替换")
        self._append("MATERIAL_REPLACED", session_id, {
            "session_id": session_id, "old_batch_id": old_batch_id, "new_batch_id": new_batch_id,
            "invalidated_roles": sorted(session.reviews),
        }, occurred_at)
        return self.sessions[session_id]

    # ------------------------------------------------------------------
    # 通知：中断恢复后不重复下发
    # ------------------------------------------------------------------

    def pending_notifications(self) -> list[Notification]:
        return sorted((n for n in self.notifications.values() if not n.delivered),
                      key=lambda n: n.notification_id)

    def flush_notifications(self, occurred_at: str | None = None) -> list[Notification]:
        """下发全部未投递通知并标记；已投递的不会重复下发。"""
        pending = self.pending_notifications()
        for ntf in pending:
            self._append("NOTIFICATION_DELIVERED", ntf.notification_id, {
                "notification_id": ntf.notification_id, "session_id": ntf.session_id,
                "kind": ntf.kind,
            }, occurred_at)
        return pending

    # ------------------------------------------------------------------
    # 追溯
    # ------------------------------------------------------------------

    def trace_participation(self, participation_id: str) -> dict:
        """从任一参与记录追到实际工艺版本、材料批号、工具、教师与放行人。"""
        participation = self.participations.get(participation_id)
        if participation is None:
            raise ValueError(f"未知参与记录: {participation_id}")
        session = self.sessions[participation.session_id]
        eligibility = self.eligibilities[(participation.session_id, participation.family_id)]
        # 已完成场次以完成时快照为准，保留当时依据
        basis = session.completion_snapshot or self._session_snapshot(session)
        return {
            "participation_id": participation.participation_id,
            "session_id": session.session_id,
            "session_status": session.status,
            "family_id": participation.family_id,
            "participant_id": eligibility.participant_id,
            "guardian_name": eligibility.guardian_name,
            "basis": basis,
            "released_by": sorted({r["reviewer_id"] for r in basis["reviews"]
                                   if r["decision"] == DECISION_APPROVE}),
            "release_event_id": basis["release_event_id"],
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _session(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"未知场次: {session_id}")
        return session

    def _batch(self, batch_id: str) -> MaterialBatch:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise ValueError(f"未知材料批号: {batch_id}")
        return batch

    def _effective_min_age(self, session: Session) -> int:
        """场次当前生效的最低年龄：工艺版本与材料批号取最严。"""
        floors = [session.min_age]
        for batch_id in session.material_batch_ids:
            batch = self.batches.get(batch_id)
            if batch and batch.min_age is not None:
                floors.append(batch.min_age)
        return max(floors)

    def _session_eligibilities(self, session_id: str) -> list[Eligibility]:
        return [e for (sid, _), e in self.eligibilities.items() if sid == session_id]

    def _session_snapshot(self, session: Session) -> dict:
        teacher = self.teachers.get(session.teacher_id)
        return {
            "craft": {"craft_id": session.craft_id, "version": session.craft_version,
                      "required_qualification": session.required_qualification},
            "age_limits": {"min_age": self._effective_min_age(session),
                           "max_age": session.max_age,
                           "guardian_required_below": session.guardian_required_below},
            "materials": [{"batch_id": b.batch_id, "material": b.material,
                           "allergens": sorted(b.allergens), "status": b.status}
                          for b in (self.batches.get(bid) for bid in session.material_batch_ids)
                          if b],
            "tools": [{"tool_id": t.tool_id, "kind": t.kind, "status": t.status}
                      for t in (self.tools.get(tid) for tid in session.tool_ids) if t],
            "teacher": {"teacher_id": session.teacher_id,
                        "qualifications": sorted(teacher.qualifications) if teacher else []},
            "reviews": [asdict(r) for r in session.reviews.values()],
            "release_event_id": session.release_event_id,
        }
