import tempfile
import unittest
from pathlib import Path

from src import batch_release as br
from src.heritage_workshop_safety import validate_event


def build_service(log_path=None):
    """标准环境：一个工艺版本、两个批号、一件工具、一名教师。"""
    svc = br.BatchReleaseService(log_path=log_path)
    svc.register_craft_version("tie-dye", "v3", min_age=4, max_age=12,
                               required_qualification="DYE-LEAD",
                               guardian_required_below=8)
    svc.receive_material("B-001", "植物染料", allergens=("茜草",), quantity=20)
    svc.receive_material("B-002", "棉布", quantity=20)
    svc.set_tool_status("T-roller", br.TOOL_OPERATIONAL, kind="轧染机")
    svc.set_teacher_qualifications("TCH-1", ("DYE-LEAD", "FIRST-AID"))
    return svc


def plan_session(svc, session_id="S-1", batches=("B-001",), capacity=2):
    return svc.plan_session(session_id, "tie-dye", "v3", list(batches),
                            ["T-roller"], "TCH-1", capacity)


def clear_session(svc, session_id="S-1"):
    svc.submit_review(session_id, "SAFETY_OFFICER", "rev-safe")
    svc.submit_review(session_id, "CRAFT_LEAD", "rev-craft")
    svc.submit_review(session_id, "OPS_MANAGER", "rev-ops")


def event_count(svc, kind):
    return sum(1 for e in svc.events if e["kind"] == kind)


class PlanAndRiskAdviceTest(unittest.TestCase):
    def test_plan_locks_session_scope_and_advises(self):
        svc = build_service()
        session = plan_session(svc, "S-1")
        # 排场即锁定工艺、批号、工具、教师、年龄限制与名额
        self.assertEqual(session.status, br.SESSION_UNDER_REVIEW)
        self.assertEqual((session.min_age, session.max_age, session.guardian_required_below),
                         (4, 12, 8))
        self.assertEqual(session.required_qualification, "DYE-LEAD")
        self.assertEqual(session.material_batch_ids, ["B-001"])
        self.assertEqual(session.tool_ids, ["T-roller"])
        self.assertEqual(session.teacher_id, "TCH-1")
        # 系统先给出风险建议：过敏提示与监护确认为提醒项，无阻断项
        findings = svc.risk_advice("S-1")
        codes = {f.code for f in findings}
        self.assertIn("ALLERGEN_NOTICE", codes)
        self.assertIn("GUARDIAN_CONSENT_REQUIRED", codes)
        self.assertFalse([f for f in findings if f.severity == br.SEVERITY_BLOCKER])

    def test_plan_rejects_unknown_references(self):
        svc = build_service()
        with self.assertRaises(ValueError):
            svc.plan_session("S-9", "tie-dye", "v9", [], [], "TCH-1", 2)
        with self.assertRaises(ValueError):
            svc.plan_session("S-9", "tie-dye", "v3", ["B-404"], [], "TCH-1", 2)
        with self.assertRaises(ValueError):
            svc.plan_session("S-9", "tie-dye", "v3", [], [], "TCH-404", 2)


class ReviewAndReleaseTest(unittest.TestCase):
    def test_release_requires_all_roles(self):
        svc = build_service()
        plan_session(svc, "S-1")
        svc.submit_review("S-1", "SAFETY_OFFICER", "rev-safe")
        svc.submit_review("S-1", "CRAFT_LEAD", "rev-craft")
        self.assertEqual(svc.sessions["S-1"].status, br.SESSION_UNDER_REVIEW)
        svc.submit_review("S-1", "OPS_MANAGER", "rev-ops")
        self.assertEqual(svc.sessions["S-1"].status, br.SESSION_CLEARED)
        self.assertEqual(event_count(svc, "BATCH_RELEASED"), 1)
        self.assertIsNotNone(svc.sessions["S-1"].release_event_id)
        with self.assertRaises(ValueError):
            svc.submit_review("S-1", "INTERN", "rev-x")

    def test_blocker_prevents_release_until_resolved(self):
        svc = build_service()
        svc.set_tool_status("T-roller", br.TOOL_NEEDS_CALIBRATION)
        plan_session(svc, "S-1")
        blockers = {f.code for f in svc.risk_advice("S-1")
                    if f.severity == br.SEVERITY_BLOCKER}
        self.assertIn("TOOL_NOT_OPERATIONAL", blockers)
        # 复核集齐但阻断项未解除，仍不放行
        clear_session(svc, "S-1")
        self.assertEqual(svc.sessions["S-1"].status, br.SESSION_UNDER_REVIEW)
        # 工具恢复后自动补做放行
        svc.set_tool_status("T-roller", br.TOOL_OPERATIONAL)
        self.assertEqual(svc.sessions["S-1"].status, br.SESSION_CLEARED)

    def test_teacher_qualification_missing_blocks_release(self):
        svc = build_service()
        svc.set_teacher_qualifications("TCH-2", ("FIRST-AID",))
        svc.plan_session("S-1", "tie-dye", "v3", ["B-001"], ["T-roller"], "TCH-2", 2)
        blockers = {f.code for f in svc.risk_advice("S-1")
                    if f.severity == br.SEVERITY_BLOCKER}
        self.assertIn("TEACHER_QUALIFICATION_MISSING", blockers)


class GuardianConfirmationTest(unittest.TestCase):
    def test_duplicate_confirmation_yields_single_eligibility(self):
        svc = build_service()
        plan_session(svc, "S-1", capacity=2)
        first = svc.confirm_guardian("S-1", "fam-A", "P-1", 6, "家长甲")
        second = svc.confirm_guardian("S-1", "fam-A", "P-1", 6, "家长甲")
        self.assertEqual(first.eligibility_id, second.eligibility_id)
        self.assertEqual(len(svc.eligibilities), 1)
        self.assertEqual(event_count(svc, "GUARDIAN_CONFIRMED"), 1)

    def test_conflicting_confirmation_rejected(self):
        svc = build_service()
        plan_session(svc, "S-1")
        svc.confirm_guardian("S-1", "fam-A", "P-1", 6, "家长甲")
        with self.assertRaises(ValueError):
            svc.confirm_guardian("S-1", "fam-A", "P-9", 6, "家长甲")

    def test_capacity_not_exceeded(self):
        svc = build_service()
        plan_session(svc, "S-1", capacity=2)
        svc.confirm_guardian("S-1", "fam-A", "P-1", 6, "家长甲")
        svc.confirm_guardian("S-1", "fam-B", "P-2", 7, "家长乙")
        with self.assertRaises(ValueError):
            svc.confirm_guardian("S-1", "fam-C", "P-3", 8, "家长丙")

    def test_age_limits_enforced_at_confirmation(self):
        svc = build_service()
        plan_session(svc, "S-1")
        with self.assertRaises(ValueError):
            svc.confirm_guardian("S-1", "fam-A", "P-1", 3, "家长甲")
        with self.assertRaises(ValueError):
            svc.confirm_guardian("S-1", "fam-A", "P-1", 13, "家长甲")


class CheckInTest(unittest.TestCase):
    def test_checkin_requires_clearance_and_confirmation(self):
        svc = build_service()
        plan_session(svc, "S-1")
        svc.confirm_guardian("S-1", "fam-A", "P-1", 6, "家长甲")
        with self.assertRaises(ValueError):
            svc.check_in("S-1", "fam-A")  # 未放行不能签到
        clear_session(svc, "S-1")
        with self.assertRaises(ValueError):
            svc.check_in("S-1", "fam-B")  # 未确认不能签到
        participation = svc.check_in("S-1", "fam-A")
        self.assertEqual(participation.participation_id, "prt:S-1:fam-A")

    def test_duplicate_checkin_returns_same_record(self):
        svc = build_service()
        plan_session(svc, "S-1")
        clear_session(svc, "S-1")
        svc.confirm_guardian("S-1", "fam-A", "P-1", 6, "家长甲")
        first = svc.check_in("S-1", "fam-A")
        second = svc.check_in("S-1", "fam-A")
        self.assertEqual(first.participation_id, second.participation_id)
        self.assertEqual(event_count(svc, "PARTICIPANT_CHECKED_IN"), 1)


class FreezeScopeTest(unittest.TestCase):
    def test_recall_freezes_only_affected_and_keeps_completed_basis(self):
        svc = build_service()
        plan_session(svc, "S-1", ("B-001",))
        clear_session(svc, "S-1")
        plan_session(svc, "S-2", ("B-002",))
        clear_session(svc, "S-2")
        plan_session(svc, "S-3", ("B-001",))
        clear_session(svc, "S-3")
        svc.confirm_guardian("S-3", "fam-X", "P-X", 6, "家长")
        participation = svc.check_in("S-3", "fam-X")
        svc.complete_session("S-3")

        svc.recall_material("B-001", "抽检不合格")

        # 只冻结受影响的未完结场次
        self.assertEqual(svc.sessions["S-1"].status, br.SESSION_FROZEN)
        self.assertEqual(svc.sessions["S-2"].status, br.SESSION_CLEARED)
        self.assertEqual(svc.sessions["S-3"].status, br.SESSION_COMPLETED)
        # 已完成的体验保留当时依据
        trace = svc.trace_participation(participation.participation_id)
        self.assertEqual(trace["basis"]["materials"][0]["status"], br.BATCH_RECEIVED)
        self.assertEqual(svc.batches["B-001"].status, br.BATCH_RECALLED)
        # 停用通知只发给受影响场次
        notes = svc.pending_notifications()
        self.assertEqual([n.session_id for n in notes], ["S-1"])
        with self.assertRaises(ValueError):
            svc.check_in("S-1", "fam-A")

    def test_tool_failure_freezes_and_recovery_restores(self):
        svc = build_service()
        plan_session(svc, "S-1")
        clear_session(svc, "S-1")
        svc.set_tool_status("T-roller", br.TOOL_OUT_OF_SERVICE)
        self.assertEqual(svc.sessions["S-1"].status, br.SESSION_FROZEN)
        svc.set_tool_status("T-roller", br.TOOL_OPERATIONAL)
        self.assertEqual(svc.sessions["S-1"].status, br.SESSION_CLEARED)

    def test_teacher_qualification_change_freezes_and_restores(self):
        svc = build_service()
        plan_session(svc, "S-1")
        clear_session(svc, "S-1")
        svc.set_teacher_qualifications("TCH-1", ("FIRST-AID",))
        self.assertEqual(svc.sessions["S-1"].status, br.SESSION_FROZEN)
        svc.set_teacher_qualifications("TCH-1", ("FIRST-AID", "DYE-LEAD"))
        self.assertEqual(svc.sessions["S-1"].status, br.SESSION_CLEARED)

    def test_incident_freezes_only_related_sessions(self):
        svc = build_service()
        plan_session(svc, "S-1", ("B-001",))
        clear_session(svc, "S-1")
        plan_session(svc, "S-2", ("B-002",))
        clear_session(svc, "S-2")
        svc.report_incident("染料泼洒", batch_id="B-001")
        self.assertEqual(svc.sessions["S-1"].status, br.SESSION_FROZEN)
        self.assertEqual(svc.sessions["S-2"].status, br.SESSION_CLEARED)


class MaterialReplacementTest(unittest.TestCase):
    def test_replacement_invalidates_reviews_and_recomputes_limits(self):
        svc = build_service()
        plan_session(svc, "S-1", ("B-002",), capacity=3)
        clear_session(svc, "S-1")
        svc.confirm_guardian("S-1", "fam-A", "P-1", 5, "家长甲")
        svc.recall_material("B-002", "供应商召回")
        self.assertEqual(svc.sessions["S-1"].status, br.SESSION_FROZEN)

        svc.receive_material("B-009", "植物染料", allergens=("茜草",), min_age=6, quantity=20)
        svc.replace_material("S-1", "B-002", "B-009")
        session = svc.sessions["S-1"]
        # 旧签署作废，回到待复核
        self.assertEqual(session.status, br.SESSION_UNDER_REVIEW)
        self.assertEqual(session.reviews, {})
        self.assertEqual(session.material_batch_ids, ["B-009"])
        replaced = [e for e in svc.events if e["kind"] == "MATERIAL_REPLACED"][0]
        self.assertEqual(replaced["payload"]["invalidated_roles"],
                         ["CRAFT_LEAD", "OPS_MANAGER", "SAFETY_OFFICER"])
        # 限制已重算：最低年龄随新批号提高到 6 岁
        with self.assertRaises(ValueError):
            svc.confirm_guardian("S-1", "fam-C", "P-3", 5, "家长丙")
        # 需重新复核才可放行
        clear_session(svc, "S-1")
        self.assertEqual(svc.sessions["S-1"].status, br.SESSION_CLEARED)
        # 旧确认按新限制校验：5 岁不能签到
        with self.assertRaises(ValueError):
            svc.check_in("S-1", "fam-A")
        svc.confirm_guardian("S-1", "fam-B", "P-2", 7, "家长乙")
        svc.check_in("S-1", "fam-B")

    def test_replacement_rejects_unusable_batch_and_completed_session(self):
        svc = build_service()
        svc.receive_material("B-100", "矿物颜料", allergens=("石青",), quantity=10)
        svc.receive_material("B-100", "矿物颜料", allergens=("朱砂",), quantity=10)
        plan_session(svc, "S-1", ("B-001",))
        clear_session(svc, "S-1")
        with self.assertRaises(ValueError):
            svc.replace_material("S-1", "B-001", "B-100")  # 隔离批号不可用于替换
        svc.complete_session("S-1")
        with self.assertRaises(ValueError):
            svc.replace_material("S-1", "B-001", "B-002")  # 已完成场次保留当时依据


class BatchConflictTest(unittest.TestCase):
    def test_conflicting_batch_quarantined(self):
        svc = build_service()
        svc.receive_material("B-100", "矿物颜料", allergens=("石青",), quantity=10)
        plan_session(svc, "S-1", ("B-100",))
        clear_session(svc, "S-1")

        svc.receive_material("B-100", "矿物颜料", allergens=("朱砂",), quantity=10)
        batch = svc.batches["B-100"]
        self.assertEqual(batch.status, br.BATCH_QUARANTINED)
        self.assertEqual(batch.allergens, ("石青",))  # 原始内容不被覆盖
        self.assertEqual(len(batch.conflicts), 1)
        # 使用中的未完结场次被冻结
        self.assertEqual(svc.sessions["S-1"].status, br.SESSION_FROZEN)
        # 隔离批号无法放行新场次
        plan_session(svc, "S-2", ("B-100",))
        codes = {f.code for f in svc.risk_advice("S-2")}
        self.assertIn("BATCH_QUARANTINED", codes)
        clear_session(svc, "S-2")
        self.assertEqual(svc.sessions["S-2"].status, br.SESSION_UNDER_REVIEW)
        # 相同内容重复接收是幂等操作，不再产生隔离事件
        svc.receive_material("B-100", "矿物颜料", allergens=("石青",), quantity=10)
        self.assertEqual(event_count(svc, "MATERIAL_BATCH_QUARANTINED"), 1)


class RecoveryTest(unittest.TestCase):
    def test_recovery_resumes_reviews_and_notifications_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "events.jsonl"
            svc = build_service(log_path=log)
            plan_session(svc, "S-1", ("B-001",))
            clear_session(svc, "S-1")
            svc.confirm_guardian("S-1", "fam-A", "P-1", 6, "家长甲")
            svc.check_in("S-1", "fam-A")
            plan_session(svc, "S-2", ("B-002",))
            svc.submit_review("S-2", "SAFETY_OFFICER", "rev-safe")
            svc.recall_material("B-001", "抽检不合格")
            sent = svc.flush_notifications()
            self.assertEqual(len(sent), 1)

            # 进程中断后从日志恢复
            svc2 = br.BatchReleaseService.recover(log)
            # 停用消息不重复下发
            self.assertEqual(svc2.pending_notifications(), [])
            self.assertEqual(svc2.flush_notifications(), [])
            # 重复确认与签到不产生第二份资格、不重复占用名额
            again = svc2.confirm_guardian("S-1", "fam-A", "P-1", 6, "家长甲")
            self.assertEqual(again.eligibility_id, "elg:S-1:fam-A")
            self.assertEqual(len(svc2.eligibilities), 1)
            svc2.check_in("S-1", "fam-A")
            self.assertEqual(len(svc2.participations), 1)
            # 继续未完成的复核
            svc2.submit_review("S-2", "CRAFT_LEAD", "rev-craft")
            svc2.submit_review("S-2", "OPS_MANAGER", "rev-ops")
            self.assertEqual(svc2.sessions["S-2"].status, br.SESSION_CLEARED)

            # 再次恢复仍然一致
            svc3 = br.BatchReleaseService.recover(log)
            self.assertEqual(svc3.sessions["S-2"].status, br.SESSION_CLEARED)
            self.assertEqual(len(svc3.participations), 1)
            self.assertEqual(svc3.pending_notifications(), [])
            self.assertEqual(event_count(svc3, "GUARDIAN_CONFIRMED"), 1)
            self.assertEqual(event_count(svc3, "PARTICIPANT_CHECKED_IN"), 1)
            self.assertEqual(event_count(svc3, "NOTIFICATION_DELIVERED"), 1)


class TraceTest(unittest.TestCase):
    def test_trace_participation_shows_craft_materials_and_releasers(self):
        svc = build_service()
        plan_session(svc, "S-1", ("B-001",))
        clear_session(svc, "S-1")
        svc.confirm_guardian("S-1", "fam-A", "P-1", 6, "家长甲")
        participation = svc.check_in("S-1", "fam-A")

        trace = svc.trace_participation(participation.participation_id)
        self.assertEqual(trace["basis"]["craft"]["version"], "v3")
        self.assertEqual([m["batch_id"] for m in trace["basis"]["materials"]], ["B-001"])
        self.assertEqual(trace["basis"]["tools"][0]["tool_id"], "T-roller")
        self.assertEqual(trace["basis"]["teacher"]["teacher_id"], "TCH-1")
        self.assertEqual(set(trace["released_by"]), {"rev-safe", "rev-craft", "rev-ops"})
        self.assertEqual(trace["guardian_name"], "家长甲")
        self.assertEqual(trace["release_event_id"], svc.sessions["S-1"].release_event_id)
        with self.assertRaises(ValueError):
            svc.trace_participation("prt:S-1:fam-404")


class ContractTest(unittest.TestCase):
    def test_emitted_events_match_domain_contract(self):
        svc = build_service()
        plan_session(svc, "S-1")
        clear_session(svc, "S-1")
        svc.confirm_guardian("S-1", "fam-A", "P-1", 6, "家长甲")
        svc.check_in("S-1", "fam-A")
        svc.complete_session("S-1")
        svc.recall_material("B-001", "抽检不合格")
        svc.flush_notifications()
        self.assertGreater(len(svc.events), 0)
        for event in svc.events:
            self.assertEqual(validate_event(event), [], event)


if __name__ == "__main__":
    unittest.main()
