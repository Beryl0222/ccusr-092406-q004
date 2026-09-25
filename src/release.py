"""批次放行领域服务。

每场体验在开课前锁定六要素（工艺版本、材料批号、工具状态、教师资质、
年龄限制、监护确认），系统先给风险建议，不同角色复核齐备后放行，
放行后参与者方可签到。事实全部来自事件日志重放：

* 材料召回 / 设备失效 / 教师资质变化 / 现场事件只冻结**当前依据命中**的场次；
* 已完成场次的依据以事件内快照永久保留，不受事后变化影响；
* 替换材料会作废旧锁、旧复核与旧监护签署，必须按新依据重走一遍；
* 监护确认按（参与者, 场次, 锁）幂等，重复提交不产生第二份资格、不重复占名额；
* 同批号内容冲突自动隔离（BATCH_QUARANTINED）；
* ``pump()`` 是纯派生过程：重放日志补齐缺失的冻结/作废/通知请求，
  进程中断后重跑即可继续，幂等键保证效果只发生一次。
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone

from .events import EVENT_KINDS, EventStore  # noqa: F401  (对外再导出)

# 教师资质级别（数值越高资质越深）
TEACHER_LEVELS = {
    "ASSISTANT": 1,
    "INSTRUCTOR": 2,
    "SENIOR_INSTRUCTOR": 3,
    "MASTER": 4,
}

# 放行前必须完成复核的角色；全部 APPROVED 且无人 REJECTED 才可放行
DEFAULT_REVIEW_ROLES = ("SAFETY_OFFICER", "OPERATIONS_MANAGER")

BLOCK = "BLOCK"
WARN = "WARN"
INFO = "INFO"


class ReleaseError(Exception):
    """领域规则拒绝。"""


class UnknownReference(ReleaseError):
    """引用了不存在的工艺/批号/工具/教师/场次。"""


class SessionFrozen(ReleaseError):
    """场次处于冻结状态，当前操作不允许。"""


class SessionCompleted(ReleaseError):
    """场次已完成，依据已封存，不允许再变更。"""


class GateBlocked(ReleaseError):
    """放行/签到门禁未通过，findings 给出具体阻断项。"""

    def __init__(self, message: str, findings: list[dict]):
        super().__init__(message)
        self.findings = findings


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts)


# ---------------------------------------------------------------------------
# 投影：把事件日志折叠成当前状态
# ---------------------------------------------------------------------------


class Projection:
    def __init__(self, events: list[dict] | None = None):
        # craft_id -> version -> 工艺版本快照（不可变）
        self.crafts: dict[str, dict[str, dict]] = {}
        # batch_no -> 批号状态
        self.batches: dict[str, dict] = {}
        # tool_id -> 最新工具状态
        self.tools: dict[str, dict] = {}
        # teacher_id -> 最新资质状态
        self.teachers: dict[str, dict] = {}
        # session_id -> 场次状态
        self.sessions: dict[str, dict] = OrderedDict()
        # 通知请求 key -> 事件（去重视图）
        self.notifications: dict[str, dict] = {}
        self.delivered_keys: set[str] = set()
        if events:
            for event in events:
                self.apply(event)

    def apply(self, event: dict) -> None:
        kind = event["kind"]
        p = event.get("payload", {})
        if kind == "CRAFT_VERSIONED":
            self.crafts.setdefault(event["subject_id"], {})[p["version"]] = {
                "craft_id": event["subject_id"],
                "version": p["version"],
                "name": p.get("name", ""),
                "min_age": p["min_age"],
                "max_age": p.get("max_age"),
                "required_teacher_level": p["required_teacher_level"],
                "required_materials": list(p.get("required_materials", [])),
            }
        elif kind == "MATERIAL_RECEIVED":
            batch = self.batches.setdefault(
                event["subject_id"],
                {"batch_no": event["subject_id"], "contents": [], "recalled": False,
                 "quarantined": False, "conflict_with": []},
            )
            content = {
                "material_type": p["material_type"],
                "allergens": tuple(sorted(p.get("allergens", []))),
                "event_id": event["event_id"],
            }
            if batch["contents"] and (
                content["material_type"], content["allergens"]
            ) != (batch["contents"][0]["material_type"], batch["contents"][0]["allergens"]):
                # 同批号前后内容不一致：标记冲突，派生事件负责隔离
                batch["conflict_with"] = [batch["contents"][0]["event_id"], event["event_id"]]
            batch["contents"].append(content)
        elif kind == "BATCH_RECALLED":
            self.batches.setdefault(event["subject_id"], {"contents": []})["recalled"] = True
        elif kind == "BATCH_QUARANTINED":
            self.batches.setdefault(event["subject_id"], {"contents": []})["quarantined"] = True
        elif kind == "TOOL_STATUS_REPORTED":
            self.tools[event["subject_id"]] = {
                "tool_id": event["subject_id"],
                "status": p.get("status", "CALIBRATED"),
                "checked_at": p.get("checked_at"),
                "decommissioned": self.tools.get(event["subject_id"], {}).get("decommissioned", False),
            }
        elif kind == "TOOL_DECOMMISSIONED":
            tool = self.tools.setdefault(event["subject_id"], {"tool_id": event["subject_id"]})
            tool["decommissioned"] = True
            tool["status"] = "OUT_OF_SERVICE"
        elif kind == "TEACHER_QUALIFIED":
            self.teachers[event["subject_id"]] = {
                "teacher_id": event["subject_id"],
                "level": p.get("level"),
                "status": p.get("status", "ACTIVE"),
                "valid_to": p.get("valid_to"),
            }
        elif kind == "SESSION_SCHEDULED":
            self.sessions[event["subject_id"]] = {
                "session_id": event["subject_id"],
                "scheduled_at": p.get("scheduled_at"),
                "capacity": p["capacity"],
                "tool_ids": list(p.get("tools", [])),
                "locks": [],
                "voided_locks": set(),
                "reviews": {},          # lock_id -> role -> 决定
                "release": None,
                "registrations": OrderedDict(),
                # lock_id -> participant_id -> 确认（旧锁确认保留留痕，但对新锁无效）
                "confirmations": {},
                "checked_in": OrderedDict(),
                "completed_at": None,
                "completion": None,
                "incidents": [],
                "swaps": [],
            }
        elif kind == "SESSION_LOCKED":
            session = self.sessions[p["session_id"]]
            session["locks"].append({**p, "active": True})
        elif kind == "LOCK_SUPERSEDED":
            for lock in self.sessions[p["session_id"]]["locks"]:
                if lock["lock_id"] == p["lock_id"]:
                    lock["active"] = False
        elif kind == "REVIEWS_VOIDED":
            self.sessions[p["session_id"]]["voided_locks"].add(p["lock_id"])
        elif kind == "REVIEW_RECORDED":
            session = self.sessions[p["session_id"]]
            session["reviews"].setdefault(p["lock_id"], {})[p["role"]] = p
        elif kind == "SESSION_RELEASED":
            self.sessions[p["session_id"]]["release"] = {**p, "event_id": event["event_id"]}
        elif kind == "PARTICIPANT_REGISTERED":
            session = self.sessions[p["session_id"]]
            session["registrations"].setdefault(
                event["subject_id"],
                {"participant_id": event["subject_id"], "event_id": event["event_id"], **p}
            )
        elif kind == "GUARDIAN_CONFIRMED":
            session = self.sessions[p["session_id"]]
            session["confirmations"].setdefault(p["lock_id"], {})[event["subject_id"]] = {
                "participant_id": event["subject_id"], **p, "event_id": event["event_id"]
            }
        elif kind == "PARTICIPANT_CHECKED_IN":
            session = self.sessions[p["session_id"]]
            session["checked_in"].setdefault(
                event["subject_id"], {**p, "event_id": event["event_id"]})
        elif kind == "SESSION_COMPLETED":
            session = self.sessions[p["session_id"]]
            session["completed_at"] = event["occurred_at"]
            session["completion"] = p
        elif kind == "INCIDENT_REPORTED":
            self.sessions[p["session_id"]]["incidents"].append(
                {"event_id": event["event_id"], "status": p.get("status", "OPEN"), **p}
            )
        elif kind == "INCIDENT_RESOLVED":
            session = self.sessions[p["session_id"]]
            for incident in session["incidents"]:
                if incident["status"] != "RESOLVED" and (
                        p.get("incident_event_id") is None
                        or p["incident_event_id"] == incident["event_id"]):
                    incident["status"] = "RESOLVED"
                    incident["resolved_by"] = event["event_id"]
        elif kind == "MATERIAL_SWAPPED":
            self.sessions[p["session_id"]]["swaps"].append({"event_id": event["event_id"], **p})
        elif kind == "NOTIFICATION_REQUESTED":
            self.notifications.setdefault(p["key"], event)
        elif kind == "NOTIFICATION_DELIVERED":
            self.delivered_keys.add(p["key"])

    # -- 读取辅助 -----------------------------------------------------------

    def current_lock(self, session_id: str) -> dict | None:
        for lock in reversed(self.sessions[session_id]["locks"]):
            if lock["active"] and lock["lock_id"] not in self.sessions[session_id]["voided_locks"]:
                return lock
        return None

    def open_incidents(self, session_id: str) -> list[dict]:
        return [i for i in self.sessions[session_id]["incidents"] if i["status"] != "RESOLVED"]


# ---------------------------------------------------------------------------
# 风险评估
# ---------------------------------------------------------------------------


def _finding(code: str, level: str, message: str, subject: str | None = None) -> dict:
    return {"code": code, "level": level, "message": message, "subject": subject}


def evaluate_basis(lock: dict, proj: Projection, session: dict) -> list[dict]:
    """依据锁定快照 + 当前事实，评估场次级/参与者级风险。

    锁定时把结论写入 SESSION_LOCKED.advice；放行/签到时以当前事实再算一遍，
    因此锁后发生的召回、失效、资质变化也能即时拦住。
    """
    findings: list[dict] = []
    craft = lock["craft"]

    # 材料批号
    for material_type in craft["required_materials"]:
        batch_no = lock["batches"].get(material_type)
        if not batch_no:
            findings.append(_finding("MATERIAL_MISSING", BLOCK,
                                     f"必需材料 {material_type} 未指定批号", material_type))
            continue
        batch = proj.batches.get(batch_no)
        if batch is None or not batch.get("contents"):
            findings.append(_finding("BATCH_UNKNOWN", BLOCK,
                                     f"批号 {batch_no} 未接收", batch_no))
        else:
            if batch.get("quarantined"):
                findings.append(_finding("BATCH_QUARANTINED", BLOCK,
                                         f"批号 {batch_no} 内容冲突已隔离，禁止使用", batch_no))
            if batch.get("recalled"):
                findings.append(_finding("BATCH_RECALLED", BLOCK,
                                         f"批号 {batch_no} 已召回", batch_no))

    # 工具状态
    for tool_snap in lock["tools"]:
        tool = proj.tools.get(tool_snap["tool_id"], {})
        if tool.get("decommissioned"):
            findings.append(_finding("TOOL_DECOMMISSIONED", BLOCK,
                                     f"工具 {tool_snap['tool_id']} 已停用", tool_snap["tool_id"]))
        elif tool.get("status") == "OUT_OF_SERVICE":
            findings.append(_finding("TOOL_OUT_OF_SERVICE", BLOCK,
                                     f"工具 {tool_snap['tool_id']} 失效未校准", tool_snap["tool_id"]))

    # 教师资质
    teacher = lock["teacher"]
    current = proj.teachers.get(teacher["teacher_id"], {})
    if current.get("status") == "REVOKED":
        findings.append(_finding("TEACHER_REVOKED", BLOCK,
                                 f"教师 {teacher['teacher_id']} 资质已撤销", teacher["teacher_id"]))
    else:
        required_rank = TEACHER_LEVELS.get(craft["required_teacher_level"], 0)
        actual_rank = TEACHER_LEVELS.get(current.get("level") or teacher.get("level"), 0)
        if actual_rank < required_rank:
            findings.append(_finding("TEACHER_LEVEL_LOW", BLOCK,
                                     f"教师资质 {current.get('level')} 低于要求 "
                                     f"{craft['required_teacher_level']}", teacher["teacher_id"]))
        valid_to = _parse(current.get("valid_to"))
        scheduled_at = _parse(session.get("scheduled_at"))
        if valid_to and scheduled_at and valid_to < scheduled_at:
            findings.append(_finding("TEACHER_EXPIRED", BLOCK,
                                     f"教师资质在场次时间前到期（{current['valid_to']}）",
                                     teacher["teacher_id"]))

    # 参与者级：年龄、过敏、监护
    min_age, max_age = craft["min_age"], craft.get("max_age")
    for pid, reg in session["registrations"].items():
        age = reg["age"]
        if age < min_age or (max_age is not None and age > max_age):
            findings.append(_finding(
                "AGE_OUT_OF_RANGE", BLOCK,
                f"参与者 {pid} 年龄 {age} 不在限制 {min_age}-{max_age or '∞'} 内", pid))
        used_allergens: set[str] = set()
        for batch_no in lock["batches"].values():
            batch = proj.batches.get(batch_no)
            if batch and batch.get("contents"):
                used_allergens.update(batch["contents"][0]["allergens"])
        declared = set(
            session["confirmations"].get(lock["lock_id"], {})
            .get(pid, {}).get("declared_allergens", []))
        hit = used_allergens & declared
        if hit:
            findings.append(_finding("ALLERGEN_MATCH", BLOCK,
                                     f"参与者 {pid} 声明过敏原命中材料成分: {sorted(hit)}", pid))
        confirmation = session["confirmations"].get(lock["lock_id"], {}).get(pid)
        if not confirmation or confirmation.get("lock_id") != lock["lock_id"]:
            findings.append(_finding("GUARDIAN_NOT_CONFIRMED", BLOCK,
                                     f"参与者 {pid} 缺少对当前依据的监护确认", pid))
    return findings


def active_freeze_causes(session: dict, lock: dict | None, proj: Projection) -> list[dict]:
    """当前仍生效的冻结原因（用于门禁；派生事件负责留痕与通知）。"""
    causes: list[dict] = []
    for incident in proj.open_incidents(session["session_id"]):
        causes.append({"type": "INCIDENT", "subject": session["session_id"],
                       "cause_event_id": incident["event_id"], "reason": incident.get("description", "")})
    if lock is not None:
        for material_type, batch_no in lock["batches"].items():
            batch = proj.batches.get(batch_no, {})
            if batch.get("recalled"):
                causes.append({"type": "BATCH_RECALLED", "subject": batch_no,
                               "cause_event_id": _trigger_id(proj, batch, "recall"),
                               "reason": f"材料 {material_type} 批号 {batch_no} 召回"})
            if batch.get("quarantined"):
                causes.append({"type": "BATCH_QUARANTINED", "subject": batch_no,
                               "cause_event_id": "quarantine:" + batch_no,
                               "reason": f"材料 {material_type} 批号 {batch_no} 内容冲突隔离"})
        for tool_snap in lock["tools"]:
            tool = proj.tools.get(tool_snap["tool_id"], {})
            if tool.get("decommissioned"):
                causes.append({"type": "TOOL_DECOMMISSIONED", "subject": tool_snap["tool_id"],
                               "cause_event_id": "tool:" + tool_snap["tool_id"],
                               "reason": "工具已停用"})
        teacher_id = lock["teacher"]["teacher_id"]
        teacher = proj.teachers.get(teacher_id, {})
        if teacher.get("status") == "REVOKED":
            causes.append({"type": "TEACHER_REVOKED", "subject": teacher_id,
                           "cause_event_id": "teacher:" + teacher_id, "reason": "教师资质撤销"})
    return causes


def _trigger_id(proj: Projection, batch: dict, flag: str) -> str:
    # 召回/隔离触发事件的稳定标识（用于派生事件幂等 id）
    return f"{flag}:{batch['batch_no']}"


# ---------------------------------------------------------------------------
# 领域服务
# ---------------------------------------------------------------------------


class ReleaseService:
    def __init__(self, store: EventStore, review_roles=DEFAULT_REVIEW_ROLES, clock=_now_iso):
        self.store = store
        self.review_roles = tuple(review_roles)
        self.clock = clock

    # -- 基础事实登记 -------------------------------------------------------

    def register_craft(self, event_id: str, craft_id: str, version: str, *,
                       min_age: int, required_teacher_level: str,
                       required_materials: list[str], max_age: int | None = None,
                       name: str = "", occurred_at: str | None = None) -> dict:
        return self._append({
            "event_id": event_id, "kind": "CRAFT_VERSIONED",
            "occurred_at": occurred_at or self.clock(), "subject_id": craft_id,
            "payload": {"version": version, "name": name, "min_age": min_age, "max_age": max_age,
                        "required_teacher_level": required_teacher_level,
                        "required_materials": required_materials},
        })

    def receive_material(self, event_id: str, batch_no: str, material_type: str,
                         allergens: list[str] | None = None,
                         occurred_at: str | None = None) -> list[dict]:
        """接收材料批号；若同批号内容冲突，pump 会自动追加隔离事件。"""
        appended = [self._append({
            "event_id": event_id, "kind": "MATERIAL_RECEIVED",
            "occurred_at": occurred_at or self.clock(), "subject_id": batch_no,
            "payload": {"material_type": material_type, "allergens": list(allergens or [])},
        })]
        appended += self.pump()
        return appended

    def report_tool(self, event_id: str, tool_id: str, status: str = "CALIBRATED",
                    checked_at: str | None = None, occurred_at: str | None = None) -> list[dict]:
        appended = [self._append({
            "event_id": event_id, "kind": "TOOL_STATUS_REPORTED",
            "occurred_at": occurred_at or self.clock(), "subject_id": tool_id,
            "payload": {"status": status, "checked_at": checked_at or self.clock()},
        })]
        appended += self.pump()
        return appended

    def decommission_tool(self, event_id: str, tool_id: str, reason: str = "") -> list[dict]:
        appended = [self._append({
            "event_id": event_id, "kind": "TOOL_DECOMMISSIONED",
            "occurred_at": self.clock(), "subject_id": tool_id,
            "payload": {"reason": reason},
        })]
        appended += self.pump()
        return appended

    def qualify_teacher(self, event_id: str, teacher_id: str, level: str,
                        status: str = "ACTIVE", valid_to: str | None = None,
                        occurred_at: str | None = None) -> list[dict]:
        appended = [self._append({
            "event_id": event_id, "kind": "TEACHER_QUALIFIED",
            "occurred_at": occurred_at or self.clock(), "subject_id": teacher_id,
            "payload": {"level": level, "status": status, "valid_to": valid_to},
        })]
        appended += self.pump()
        return appended

    def schedule_session(self, event_id: str, session_id: str, capacity: int,
                         tools: list[str] | None = None, scheduled_at: str | None = None) -> dict:
        return self._append({
            "event_id": event_id, "kind": "SESSION_SCHEDULED",
            "occurred_at": self.clock(), "subject_id": session_id,
            "payload": {"capacity": capacity, "tools": list(tools or []),
                        "scheduled_at": scheduled_at or self.clock()},
        })

    def recall_batch(self, event_id: str, batch_no: str, reason: str = "") -> list[dict]:
        if batch_no not in self._proj().batches:
            raise UnknownReference(f"批号 {batch_no} 从未接收，不能召回")
        appended = [self._append({
            "event_id": event_id, "kind": "BATCH_RECALLED",
            "occurred_at": self.clock(), "subject_id": batch_no,
            "payload": {"reason": reason},
        })]
        appended += self.pump()
        return appended

    def resolve_incident(self, event_id: str, session_id: str,
                         incident_event_id: str | None = None, note: str = "") -> list[dict]:
        """解除现场事件；只影响该场次，当前冻结原因消失即恢复可复核/签到。"""
        self._require_session(session_id)
        appended = [self._append({
            "event_id": event_id, "kind": "INCIDENT_RESOLVED",
            "occurred_at": self.clock(), "subject_id": session_id,
            "payload": {"session_id": session_id,
                        "incident_event_id": incident_event_id, "note": note},
        })]
        appended += self.pump()
        return appended

    def report_incident(self, event_id: str, session_id: str, description: str,
                        severity: str = "MINOR", status: str = "OPEN") -> list[dict]:
        self._require_session(session_id)
        appended = [self._append({
            "event_id": event_id, "kind": "INCIDENT_REPORTED",
            "occurred_at": self.clock(), "subject_id": session_id,
            "payload": {"session_id": session_id, "description": description,
                        "severity": severity, "status": status},
        })]
        appended += self.pump()
        return appended

    # -- 参与者 -------------------------------------------------------------

    def register_participant(self, event_id: str, participant_id: str, session_id: str,
                             age: int, occurred_at: str | None = None) -> dict:
        session = self._require_session(session_id)
        if participant_id in session["registrations"]:
            # 重复报名：幂等，不再占名额；返回原始报名事件 id
            return {"event_id": session["registrations"][participant_id]["event_id"],
                    "deduped": True, "kind": "PARTICIPANT_REGISTERED"}
        if len(session["registrations"]) >= session["capacity"]:
            raise ReleaseError(f"场次 {session_id} 名额已满（{session['capacity']}）")
        return self._append({
            "event_id": event_id, "kind": "PARTICIPANT_REGISTERED",
            "occurred_at": occurred_at or self.clock(), "subject_id": participant_id,
            "payload": {"session_id": session_id, "age": age},
        })

    def confirm_guardian(self, event_id: str, participant_id: str, session_id: str,
                         declared_allergens: list[str] | None = None,
                         occurred_at: str | None = None) -> dict:
        session = self._require_session(session_id)
        self._require_registered(participant_id, session)
        self._require_open(session_id)
        lock = self._require_lock(session_id)
        existing = session["confirmations"].get(lock["lock_id"], {}).get(participant_id)
        if existing:
            # 同一份依据上的重复确认：幂等，不产生第二份资格
            return {"event_id": existing["event_id"], "deduped": True, "kind": "GUARDIAN_CONFIRMED"}
        return self._append({
            "event_id": event_id, "kind": "GUARDIAN_CONFIRMED",
            "occurred_at": occurred_at or self.clock(), "subject_id": participant_id,
            "payload": {"session_id": session_id, "lock_id": lock["lock_id"],
                        "declared_allergens": list(declared_allergens or [])},
        })

    # -- 锁定 → 复核 → 放行 → 签到 ------------------------------------------

    def lock_session(self, event_id: str, session_id: str, craft_id: str, version: str,
                     batches: dict[str, str], teacher_id: str,
                     occurred_at: str | None = None) -> list[dict]:
        session = self._require_session(session_id)
        self._require_open(session_id)
        craft = self._proj().crafts.get(craft_id, {}).get(version)
        if craft is None:
            raise UnknownReference(f"工艺版本 {craft_id}@{version} 不存在")
        if teacher_id not in self._proj().teachers:
            raise UnknownReference(f"教师 {teacher_id} 未登记资质")
        missing_tools = [tid for tid in session["tool_ids"] if tid not in self._proj().tools]
        if missing_tools:
            raise UnknownReference(f"工具未上报校准状态: {missing_tools}")
        missing_materials = [m for m in craft["required_materials"] if m not in batches]
        extra_materials = [m for m in batches if m not in craft["required_materials"]]
        if missing_materials or extra_materials:
            raise ReleaseError(
                f"批号映射必须精确覆盖工艺所需材料，缺少 {missing_materials}，多余 {extra_materials}")
        for material_type, batch_no in batches.items():
            if batch_no not in self._proj().batches:
                raise UnknownReference(f"批号 {batch_no} 未接收")

        seq = len(session["locks"]) + 1
        lock_id = f"lock-{session_id}-{seq}"
        tools = [{
            "tool_id": tid,
            "status": self._proj().tools.get(tid, {}).get("status", "UNKNOWN"),
            "checked_at": self._proj().tools.get(tid, {}).get("checked_at"),
        } for tid in session["tool_ids"]]
        teacher = dict(self._proj().teachers[teacher_id])
        lock_payload = {
            "session_id": session_id,
            "lock_id": lock_id,
            "seq": seq,
            "craft": {
                "craft_id": craft["craft_id"], "version": craft["version"],
                "name": craft.get("name", ""), "min_age": craft["min_age"],
                "max_age": craft.get("max_age"),
                "required_teacher_level": craft["required_teacher_level"],
                "required_materials": craft["required_materials"],
            },
            "batches": dict(batches),
            "tools": tools,
            "teacher": {"teacher_id": teacher_id, "level": teacher.get("level"),
                        "status": teacher.get("status", "ACTIVE"),
                        "valid_to": teacher.get("valid_to")},
            "age_rule": {"min_age": craft["min_age"], "max_age": craft.get("max_age")},
            "guardian_required": True,
        }
        advice = evaluate_basis(lock_payload, self._proj(), session)
        lock_payload["advice"] = {"findings": advice,
                                  "evaluated_at": occurred_at or self.clock()}
        appended = [self._append({
            "event_id": event_id, "kind": "SESSION_LOCKED",
            "occurred_at": occurred_at or self.clock(), "subject_id": session_id,
            "payload": lock_payload,
        })]
        appended += self.pump()
        return appended

    def record_review(self, event_id: str, session_id: str, role: str, reviewer_id: str,
                      decision: str, reason: str = "", occurred_at: str | None = None) -> dict:
        if role not in self.review_roles:
            raise ReleaseError(f"角色 {role} 不在放行复核分工内: {self.review_roles}")
        if decision not in ("APPROVED", "REJECTED"):
            raise ReleaseError("decision 必须是 APPROVED 或 REJECTED")
        session = self._require_session(session_id)
        self._require_open(session_id)
        lock = self._require_lock(session_id)
        return self._append({
            "event_id": event_id, "kind": "REVIEW_RECORDED",
            "occurred_at": occurred_at or self.clock(), "subject_id": reviewer_id,
            "payload": {"session_id": session_id, "lock_id": lock["lock_id"], "role": role,
                        "reviewer_id": reviewer_id, "decision": decision, "reason": reason},
        })

    def release_session(self, event_id: str, session_id: str, released_by: str) -> dict:
        session = self._require_session(session_id)
        self._require_open(session_id)
        lock = self._require_lock(session_id)
        self._assert_not_frozen(session, lock)

        findings = evaluate_basis(lock, self._proj(), session)
        blockers = [f for f in findings if f["level"] == BLOCK]

        reviews = session["reviews"].get(lock["lock_id"], {})
        missing = [r for r in self.review_roles if reviews.get(r, {}).get("decision") != "APPROVED"]
        rejected = [r for r, v in reviews.items() if v.get("decision") == "REJECTED"]
        if rejected:
            blockers += [_finding("REVIEW_REJECTED", BLOCK, f"角色 {r} 拒绝放行")
                         for r in rejected]
        if missing:
            blockers += [_finding("REVIEW_MISSING", BLOCK, f"缺少角色 {r} 的 APPROVED")
                         for r in missing]
        if blockers:
            raise GateBlocked(f"场次 {session_id} 放行门禁未通过", blockers)

        payload = {
            "session_id": session_id,
            "lock_id": lock["lock_id"],
            "released_by": released_by,
            "reviewers": [{"role": r, "reviewer_id": reviews[r]["reviewer_id"]}
                          for r in self.review_roles],
            "basis": {
                "craft": {"craft_id": lock["craft"]["craft_id"], "version": lock["craft"]["version"]},
                "batches": dict(lock["batches"]),
                "tool_ids": [t["tool_id"] for t in lock["tools"]],
                "teacher_id": lock["teacher"]["teacher_id"],
                "age_rule": lock["age_rule"],
                "guardian_required": lock["guardian_required"],
                "lock_event_advice": lock["advice"]["findings"],
            },
        }
        return self._append({
            "event_id": event_id, "kind": "SESSION_RELEASED",
            "occurred_at": self.clock(), "subject_id": session_id, "payload": payload,
        })

    def check_in(self, event_id: str, participant_id: str, session_id: str) -> dict:
        session = self._require_session(session_id)
        self._require_registered(participant_id, session)
        self._require_open(session_id)
        lock = self._require_lock(session_id)
        if participant_id in session["checked_in"]:
            return {"event_id": session["checked_in"][participant_id].get("event_id", event_id),
                    "deduped": True, "kind": "PARTICIPANT_CHECKED_IN"}
        release = session["release"]
        if not release or release.get("lock_id") != lock["lock_id"]:
            raise GateBlocked("场次尚未按当前依据放行", [
                _finding("NOT_RELEASED", BLOCK, "锁定依据未放行或已变更")])
        self._assert_not_frozen(session, lock)
        blockers = [f for f in evaluate_basis(lock, self._proj(), session)
                    if f["level"] == BLOCK and f.get("subject") == participant_id]
        if blockers:
            raise GateBlocked(f"参与者 {participant_id} 不满足签到条件", blockers)
        return self._append({
            "event_id": event_id, "kind": "PARTICIPANT_CHECKED_IN",
            "occurred_at": self.clock(), "subject_id": participant_id,
            "payload": {"session_id": session_id, "lock_id": lock["lock_id"]},
        })

    def complete_session(self, event_id: str, session_id: str) -> dict:
        session = self._require_session(session_id)
        if not session["release"]:
            raise ReleaseError(f"场次 {session_id} 未放行，不能完成")
        lock = self.current_basis(session_id)
        payload = {
            "session_id": session_id,
            "lock_id": lock["lock_id"],
            "release_event_ref": session["release"]["event_id"],
            "craft": {"craft_id": lock["craft"]["craft_id"], "version": lock["craft"]["version"]},
            "batches": dict(lock["batches"]),
            "teacher_id": lock["teacher"]["teacher_id"],
            "released_by": session["release"]["released_by"],
            "checked_in": list(session["checked_in"]),
        }
        return self._append({
            "event_id": event_id, "kind": "SESSION_COMPLETED",
            "occurred_at": self.clock(), "subject_id": session_id, "payload": payload,
        })

    def swap_material(self, event_id: str, session_id: str, material_type: str,
                      new_batch_no: str) -> list[dict]:
        """替换材料：旧锁/复核/监护签署全部作废，必须按新批号重新锁定。

        名额与报名保留；已完成场次不允许换料（当时依据封存）。
        """
        session = self._require_session(session_id)
        if session["completed_at"]:
            raise SessionCompleted(f"场次 {session_id} 已完成，依据封存不可替换材料")
        lock = self.current_basis(session_id)
        if lock is None:
            raise ReleaseError(f"场次 {session_id} 尚未锁定，无需替换；可直接按新批号锁定")
        old_batch_no = lock["batches"].get(material_type)
        if not old_batch_no:
            raise ReleaseError(f"当前锁未使用材料类型 {material_type}")
        new_batch = self._proj().batches.get(new_batch_no)
        if new_batch is None or not new_batch.get("contents"):
            raise UnknownReference(f"新批号 {new_batch_no} 未接收")
        if new_batch.get("recalled") or new_batch.get("quarantined"):
            raise GateBlocked(f"新批号 {new_batch_no} 不可用", [
                _finding("BATCH_UNUSABLE", BLOCK, "批号已召回或隔离")])
        appended = [self._append({
            "event_id": event_id, "kind": "MATERIAL_SWAPPED",
            "occurred_at": self.clock(), "subject_id": session_id,
            "payload": {"session_id": session_id, "lock_id": lock["lock_id"],
                        "material_type": material_type,
                        "old_batch": old_batch_no, "new_batch": new_batch_no},
        })]
        appended += self.pump()
        return appended

    # -- 派生：冻结 / 作废 / 通知请求（中断后重跑补齐） ----------------------

    def pump(self) -> list[dict]:
        """重放日志，补齐所有缺失的派生事件，直到状态收敛。

        派生事件 id 由原因确定性生成，重复执行不会重复冻结、重复停用通知。
        """
        all_appended: list[dict] = []
        while True:
            proj = self._proj()
            derived = self._derive(proj)
            if not derived:
                return all_appended
            self.store.append_many(derived)
            all_appended.extend(derived)

    def _derive(self, proj: Projection) -> list[dict]:
        expected: "OrderedDict[str, dict]" = OrderedDict()
        ts = self.clock()

        # 1) 同批号内容冲突 → 隔离
        for batch_no, batch in proj.batches.items():
            if batch.get("conflict_with") and not batch.get("quarantined"):
                eid = f"derived-quarantine-{_sanitize(batch_no)}"
                expected[eid] = {
                    "event_id": eid, "kind": "BATCH_QUARANTINED", "occurred_at": ts,
                    "subject_id": batch_no,
                    "payload": {"batch_no": batch_no,
                                "conflicting_event_ids": batch["conflict_with"],
                                "reason": "同一批号接收到冲突内容，整批隔离待查"},
                }

        # 2) 当前依据命中风险 → 冻结未完成场次（已完成场次豁免）
        for session_id, session in proj.sessions.items():
            if session["completed_at"]:
                continue  # 已完成体验保留当时依据
            lock = proj.current_lock(session_id)
            if lock is None:
                continue
            for cause in active_freeze_causes(session, lock, proj):
                eid = ("derived-freeze-" + _sanitize(session_id) + "-"
                       + _sanitize(lock["lock_id"]) + "-" + _sanitize(cause["cause_event_id"]))
                if not any(e["event_id"] == eid for e in self.store.load()):
                    payload = {"session_id": session_id, "lock_id": lock["lock_id"],
                               "cause": cause}
                    expected[eid] = {
                        "event_id": eid, "kind": "SESSION_FROZEN", "occurred_at": ts,
                        "subject_id": session_id, "payload": payload}

        # 3) 换料 → 旧锁作废、旧复核作废（旧监护签署因绑定 lock_id 自动失效）
        for session_id, session in proj.sessions.items():
            for swap in session["swaps"]:
                sup_id = (f"derived-supersede-{_sanitize(session_id)}-"
                          f"{_sanitize(swap['lock_id'])}-{_sanitize(swap['event_id'])}")
                void_id = (f"derived-void-{_sanitize(session_id)}-"
                           f"{_sanitize(swap['lock_id'])}-{_sanitize(swap['event_id'])}")
                expected.setdefault(sup_id, {
                    "event_id": sup_id, "kind": "LOCK_SUPERSEDED", "occurred_at": ts,
                    "subject_id": session_id,
                    "payload": {"session_id": session_id, "lock_id": swap["lock_id"],
                                "swap_event_id": swap["event_id"],
                                "material_type": swap["material_type"],
                                "old_batch": swap["old_batch"], "new_batch": swap["new_batch"]}})
                expected.setdefault(void_id, {
                    "event_id": void_id, "kind": "REVIEWS_VOIDED", "occurred_at": ts,
                    "subject_id": session_id,
                    "payload": {"session_id": session_id, "lock_id": swap["lock_id"],
                                "swap_event_id": swap["event_id"],
                                "reason": "材料替换，旧复核依据失效，须重新复核"}})

        # 4) 通知请求（停用/冻结/召回/隔离），每个原因一把幂等键
        existing_ids = {e["event_id"] for e in self.store.load()}
        for eid, event in list(expected.items()):
            if event["kind"] == "SESSION_FROZEN":
                self._request_notification(
                    expected, existing_ids,
                    key=f"freeze:{eid}", template="SESSION_SUSPENDED",
                    occurred_at=ts, subject=event["subject_id"],
                    extra={"session_id": event["subject_id"], "cause": event["payload"]["cause"]})
            elif event["kind"] == "BATCH_QUARANTINED":
                self._request_notification(
                    expected, existing_ids,
                    key=f"quarantine:{event['subject_id']}", template="BATCH_QUARANTINE",
                    occurred_at=ts, subject=event["subject_id"],
                    extra={"batch_no": event["subject_id"]})
        for event in self.store.load():
            if event["kind"] == "BATCH_RECALLED":
                self._request_notification(
                    expected, existing_ids,
                    key=f"recall:{event['subject_id']}", template="BATCH_WITHDRAWAL",
                    occurred_at=ts, subject=event["subject_id"],
                    extra={"batch_no": event["subject_id"], "reason": event["payload"].get("reason", "")})
            elif event["kind"] == "TOOL_DECOMMISSIONED":
                self._request_notification(
                    expected, existing_ids,
                    key=f"tool-decommission:{event['subject_id']}", template="TOOL_OUT_OF_USE",
                    occurred_at=ts, subject=event["subject_id"],
                    extra={"tool_id": event["subject_id"]})

        return [e for eid, e in expected.items() if eid not in existing_ids]

    @staticmethod
    def _request_notification(expected, existing_ids, *, key: str, template: str,
                              occurred_at: str, subject: str, extra: dict) -> None:
        eid = "derived-notif-" + _sanitize(key)
        if eid in expected or eid in existing_ids:
            return
        expected[eid] = {
            "event_id": eid, "kind": "NOTIFICATION_REQUESTED", "occurred_at": occurred_at,
            "subject_id": subject,
            "payload": {"key": key, "template": template, **extra},
        }

    # -- 查询 / 追溯 --------------------------------------------------------

    def current_basis(self, session_id: str) -> dict | None:
        proj = self._proj()
        return proj.current_lock(session_id)

    def session_view(self, session_id: str) -> dict:
        """场次当前视图：状态、依据、风险建议与冻结原因。"""
        proj = self._proj()
        session = proj.sessions[session_id]
        lock = proj.current_lock(session_id)
        view = {
            "session_id": session_id,
            "status": self._status(session, lock, proj),
            "capacity": session["capacity"],
            "registered": list(session["registrations"]),
            "checked_in": list(session["checked_in"]),
            "lock": _public_lock(lock) if lock else None,
            "reviews": ({role: {"reviewer_id": p["reviewer_id"], "decision": p["decision"]}
                         for role, p in session["reviews"].get(lock["lock_id"], {}).items()}
                        if lock else {}),
            "release": (session["release"] if lock and session["release"]
                        and session["release"].get("lock_id") == lock["lock_id"] else None),
            "freeze_causes": active_freeze_causes(session, lock, proj) if lock else [],
            "completed_at": session["completed_at"],
        }
        return view

    def trace(self, participant_id: str, session_id: str) -> dict:
        """从任一参与记录追到实际工艺、材料、放行人与全过程依据。"""
        proj = self._proj()
        session = proj.sessions[session_id]
        reg = session["registrations"].get(participant_id)
        if reg is None:
            raise UnknownReference(f"参与者 {participant_id} 未报名场次 {session_id}")
        checkin = session["checked_in"].get(participant_id)
        current_lock = proj.current_lock(session_id)
        # 依据优先级：签到当时的锁（事后可追溯）> 当前有效锁 > 最近一次已作废锁
        effective_lock_id = (checkin or {}).get("lock_id")
        if effective_lock_id is None and current_lock is not None:
            effective_lock_id = current_lock["lock_id"]
        if effective_lock_id is None and session["locks"]:
            effective_lock_id = session["locks"][-1]["lock_id"]
        lock = next((c for c in session["locks"] if c["lock_id"] == effective_lock_id), None)
        if lock is None:
            raise UnknownReference(f"场次 {session_id} 从未锁定，无法追溯依据")
        confirmation = session["confirmations"].get(lock["lock_id"], {}).get(participant_id)
        release = (session["release"] if current_lock is not None and session["release"]
                   and session["release"].get("lock_id") == current_lock["lock_id"]
                   and current_lock["lock_id"] == lock["lock_id"] else None)
        return {
            "participant_id": participant_id,
            "session_id": session_id,
            "registration": {"age": reg["age"]},
            "guardian_confirmation": (
                {"lock_id": confirmation["lock_id"],
                 "declared_allergens": confirmation.get("declared_allergens", []),
                 "valid_for_current_basis": bool(
                     current_lock and confirmation.get("lock_id") == current_lock["lock_id"])}
                if confirmation else None),
            "checked_in": bool(checkin),
            "craft": {"craft_id": lock["craft"]["craft_id"], "version": lock["craft"]["version"]},
            "batches": dict(lock["batches"]),
            "tools": list(lock["tools"]),
            "teacher": dict(lock["teacher"]),
            "age_rule": lock["age_rule"],
            "risk_advice_at_lock": lock["advice"]["findings"],
            "release": ({"released_by": release["released_by"],
                         "reviewers": release.get("reviewers", [])} if release else None),
            "completion": session["completion"],
        }

    # -- 内部辅助 -----------------------------------------------------------

    def _proj(self) -> Projection:
        return Projection(self.store.load())

    def _append(self, event: dict) -> dict:
        self.store.append(event)
        return event

    def _require_session(self, session_id: str) -> dict:
        proj = self._proj()
        if session_id not in proj.sessions:
            raise UnknownReference(f"场次 {session_id} 不存在")
        return proj.sessions[session_id]

    def _require_lock(self, session_id: str) -> dict:
        lock = self._proj().current_lock(session_id)
        if lock is None:
            raise ReleaseError(f"场次 {session_id} 当前没有有效锁定")
        return lock

    @staticmethod
    def _require_registered(participant_id: str, session: dict) -> None:
        if participant_id not in session["registrations"]:
            raise UnknownReference(f"参与者 {participant_id} 未报名该场次")

    def _require_open(self, session_id: str) -> None:
        session = self._require_session(session_id)
        if session["completed_at"]:
            raise SessionCompleted(f"场次 {session_id} 已完成并封存依据")

    def _assert_not_frozen(self, session: dict, lock: dict) -> None:
        causes = active_freeze_causes(session, lock, self._proj())
        if causes:
            raise SessionFrozen(f"场次 {session['session_id']} 已冻结: "
                                + "; ".join(c["reason"] for c in causes))

    def _status(self, session: dict, lock: dict | None, proj: Projection) -> str:
        if session["completed_at"]:
            return "COMPLETED"
        if lock is None:
            return "AWAITING_LOCK" if session["locks"] else "SCHEDULED"
        if active_freeze_causes(session, lock, proj):
            return "FROZEN"
        if session["release"] and session["release"].get("lock_id") == lock["lock_id"]:
            return "RELEASED"
        return "LOCKED_PENDING_REVIEW"


def _public_lock(lock: dict) -> dict:
    return {k: v for k, v in lock.items() if k != "active"}


def _sanitize(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value))
