"""批次放行流程场景测试。

覆盖运营负责人提出的全部约束：
六要素锁定与风险建议、多角色复核门禁、召回/失效/事件的精准冻结、
已完成场次依据封存、换料重算与旧签署作废、确认幂等与名额、
同批号冲突隔离、中断后续做不重复通知、参与记录全链路追溯。

门禁顺序：排期 → 锁定（工艺/批号/工具/教师/年龄）→ 报名与监护确认
→ 多角色复核 → 放行 → 签到 → 完成。
"""

import tempfile
import unittest
from pathlib import Path

from src.events import EventConflictError, EventStore
from src.notifications import FlakySink, OutboxDispatcher, RecordingSink
from src.release import (
    GateBlocked,
    ReleaseError,
    ReleaseService,
    SessionCompleted,
    SessionFrozen,
    UnknownReference,
)

SINCE = "2026-09-25T08:00:00+08:00"


class FlowTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "log.jsonl"
        self.sink = RecordingSink()
        self.service, self.dispatcher = self._service(self.path, self.sink)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _service(path, sink=None):
        store = EventStore(path)
        service = ReleaseService(store, clock=lambda: SINCE)
        dispatcher = OutboxDispatcher(store, sink, lambda: SINCE) if sink is not None else None
        return service, dispatcher

    def seed(self, *, session="S1", capacity=2, tools=("T1",)):
        """登记一条可正常锁定的基线（工艺、材料、工具、教师、场次）。"""
        svc = self.service
        svc.register_craft("craft-paper-1", "PAPER_CUT", "v3", min_age=6, max_age=12,
                           required_teacher_level="INSTRUCTOR",
                           required_materials=["paper"], name="剪纸基础")
        svc.receive_material("mat-paper-a", "BATCH-A", "paper", allergens=["latex"])
        svc.receive_material("mat-paper-a2", "BATCH-A2", "paper", allergens=[])
        svc.report_tool("tool-t1-ok", "T1", "CALIBRATED", checked_at=SINCE)
        svc.qualify_teacher("teacher-li", "LI", "INSTRUCTOR", valid_to="2027-01-01T00:00:00+08:00")
        svc.schedule_session(f"sched-{session.lower()}", session, capacity=capacity,
                             tools=list(tools), scheduled_at=SINCE)
        return svc

    def lock(self, session="S1", batch="BATCH-A2"):
        self.service.lock_session(f"lock-{session}", session, "PAPER_CUT", "v3",
                                  {"paper": batch}, "LI")

    def register_confirm(self, pid, age, *, allergens=None, session="S1", suffix=""):
        self.service.register_participant(f"reg-{pid}{suffix}", pid, session, age)
        self.service.confirm_guardian(f"guard-{pid}{suffix}", pid, session,
                                      declared_allergens=allergens or [])

    def review_release(self, session="S1"):
        self.service.record_review(f"rev-safety-{session}", session,
                                   "SAFETY_OFFICER", "U1", "APPROVED")
        self.service.record_review(f"rev-ops-{session}", session,
                                   "OPERATIONS_MANAGER", "U2", "APPROVED")
        self.service.release_session(f"release-{session}", session, "U2")

    def happy_path(self, pid="KID-1", age=7, *, allergens=None, session="S1",
                   batch="BATCH-A2", check_in=False):
        """锁定 → 报名确认 → 复核放行（可选签到）。"""
        self.lock(session, batch)
        self.register_confirm(pid, age, allergens=allergens, session=session)
        self.review_release(session)
        if check_in:
            self.service.check_in(f"in-{pid}", pid, session)


class HappyPathTests(FlowTestBase):
    def test_lock_advice_review_release_checkin(self):
        self.seed()
        # 孩子对乳胶过敏，但锁定的 BATCH-A2 不含 latex，风险建议为空
        self.lock()
        view = self.service.session_view("S1")
        self.assertEqual(view["status"], "LOCKED_PENDING_REVIEW")
        self.assertEqual(view["lock"]["batches"], {"paper": "BATCH-A2"})
        self.assertEqual(view["lock"]["teacher"]["teacher_id"], "LI")
        self.assertEqual(view["freeze_causes"], [])

        self.register_confirm("KID-1", 7, allergens=["latex"])

        # 未完成两个角色复核前不能放行
        self.service.record_review("rev-safety", "S1", "SAFETY_OFFICER", "U1", "APPROVED")
        with self.assertRaises(GateBlocked):
            self.service.release_session("release-S1", "S1", "U2")

        self.service.record_review("rev-ops", "S1", "OPERATIONS_MANAGER", "U2", "APPROVED")
        released = self.service.release_session("release-S1", "S1", "U2")
        self.assertEqual(released["payload"]["released_by"], "U2")
        self.assertEqual(self.service.session_view("S1")["status"], "RELEASED")

        # 放行后签到；重复签到幂等
        checkin = self.service.check_in("in-KID-1", "KID-1", "S1")
        self.assertEqual(checkin["kind"], "PARTICIPANT_CHECKED_IN")
        again = self.service.check_in("in-KID-1", "KID-1", "S1")
        self.assertTrue(again["deduped"])
        self.assertEqual(self.service.session_view("S1")["checked_in"], ["KID-1"])

    def test_cannot_check_in_before_release(self):
        self.seed()
        self.lock()
        self.register_confirm("KID-1", 7)
        with self.assertRaises(GateBlocked):
            self.service.check_in("in-KID-1", "KID-1", "S1")

    def test_confirmation_requires_existing_lock(self):
        self.seed()
        self.service.register_participant("reg-1", "KID-1", "S1", 7)
        with self.assertRaises(ReleaseError):
            self.service.confirm_guardian("guard-1", "KID-1", "S1")


class RiskAdviceTests(FlowTestBase):
    def test_allergen_and_age_block_release(self):
        self.seed()
        self.lock(batch="BATCH-A")  # 含 latex
        # 年龄超限 + 命中乳胶：双阻断
        self.register_confirm("KID-YOUNG", 5)
        self.register_confirm("KID-ALLERGIC", 8, allergens=["latex"])
        self.service.record_review("rev-safety", "S1", "SAFETY_OFFICER", "U1", "APPROVED")
        self.service.record_review("rev-ops", "S1", "OPERATIONS_MANAGER", "U2", "APPROVED")
        with self.assertRaises(GateBlocked) as ctx:
            self.service.release_session("release-S1", "S1", "U2")
        codes = {f["code"] for f in ctx.exception.findings}
        self.assertIn("AGE_OUT_OF_RANGE", codes)
        self.assertIn("ALLERGEN_MATCH", codes)

    def test_missing_guardian_confirmation_blocks(self):
        self.seed()
        self.lock()
        self.service.register_participant("reg-1", "KID-1", "S1", 7)
        # 已报名但未做监护确认
        self.service.record_review("rev-safety", "S1", "SAFETY_OFFICER", "U1", "APPROVED")
        self.service.record_review("rev-ops", "S1", "OPERATIONS_MANAGER", "U2", "APPROVED")
        with self.assertRaises(GateBlocked) as ctx:
            self.service.release_session("release-S1", "S1", "U2")
        self.assertIn("GUARDIAN_NOT_CONFIRMED",
                      {f["code"] for f in ctx.exception.findings})

    def test_rejected_review_blocks(self):
        self.seed()
        self.lock()
        self.register_confirm("KID-1", 7)
        self.service.record_review("rev-safety", "S1", "SAFETY_OFFICER", "U1", "REJECTED",
                                   reason="护具未到位")
        self.service.record_review("rev-ops", "S1", "OPERATIONS_MANAGER", "U2", "APPROVED")
        with self.assertRaises(GateBlocked):
            self.service.release_session("release-S1", "S1", "U2")

    def test_teacher_level_drop_blocks(self):
        self.seed()
        # 锁后教师降为助教（级别不足），放行时即时拦住
        self.lock()
        self.service.qualify_teacher("teacher-li-demote", "LI", "ASSISTANT")
        self.register_confirm("KID-1", 7)
        self.service.record_review("rev-safety", "S1", "SAFETY_OFFICER", "U1", "APPROVED")
        self.service.record_review("rev-ops", "S1", "OPERATIONS_MANAGER", "U2", "APPROVED")
        with self.assertRaises(GateBlocked) as ctx:
            self.service.release_session("release-S1", "S1", "U2")
        self.assertIn("TEACHER_LEVEL_LOW", {f["code"] for f in ctx.exception.findings})


class FreezeTests(FlowTestBase):
    def test_recall_freezes_only_unfinished_session_and_completed_is_immune(self):
        self.seed(session="S1")
        self.seed(session="S2")
        # S1 完成
        self.happy_path(session="S1", check_in=True)
        self.service.complete_session("done-S1", "S1")
        # S2 已放行未完成
        self.happy_path(pid="KID-2", age=8, session="S2")

        self.service.recall_batch("recall-a2", "BATCH-A2", reason="供应商通报")
        self.assertEqual(self.service.session_view("S1")["status"], "COMPLETED")
        self.assertEqual(self.service.session_view("S2")["status"], "FROZEN")
        with self.assertRaises(SessionFrozen):
            self.service.check_in("in-2", "KID-2", "S2")

        # 完成场次的追溯仍指向当时批号 A2 与放行人
        trace = self.service.trace("KID-1", "S1")
        self.assertEqual(trace["batches"], {"paper": "BATCH-A2"})
        self.assertEqual(trace["release"]["released_by"], "U2")

        # 停用通知：召回一条；冻结通知只给未完成的 S2
        self.dispatcher.dispatch_pending()
        self.assertEqual(list(self.sink.sent).count("recall:BATCH-A2"), 1)
        freeze_keys = [k for k in self.sink.sent if k.startswith("freeze:")]
        self.assertTrue(any("S2" in k for k in freeze_keys))
        self.assertFalse(any("S1" in k for k in freeze_keys))

    def test_recall_of_unused_batch_freezes_nothing(self):
        self.seed(session="S1")
        self.seed(session="S2")
        self.happy_path(session="S1")
        self.happy_path(pid="KID-2", age=8, session="S2")
        # 召回的 A 从未被任何场次锁定——只发召回通知，不冻结场次
        self.service.recall_batch("recall-a", "BATCH-A")
        self.assertEqual(self.service.session_view("S1")["status"], "RELEASED")
        self.assertEqual(self.service.session_view("S2")["status"], "RELEASED")

    def test_tool_decommission_freezes_only_session_using_it(self):
        self.seed(session="S1", tools=("T1",))
        self.seed(session="S2", tools=("T2",))
        self.service.report_tool("tool-t2-ok", "T2", "CALIBRATED", checked_at=SINCE)
        self.happy_path(session="S1")
        self.happy_path(pid="KID-2", age=8, session="S2")

        self.service.decommission_tool("tool-t1-dead", "T1", reason="刀头崩裂")
        self.assertEqual(self.service.session_view("S1")["status"], "FROZEN")
        self.assertEqual(self.service.session_view("S2")["status"], "RELEASED")

    def test_incident_freezes_and_resolve_unfreezes(self):
        self.seed()
        self.happy_path()
        self.service.report_incident("inc-1", "S1", "现场护目镜损坏", severity="MINOR")
        self.assertEqual(self.service.session_view("S1")["status"], "FROZEN")
        self.service.resolve_incident("inc-1-ok", "S1", incident_event_id="inc-1")
        self.assertEqual(self.service.session_view("S1")["status"], "RELEASED")
        self.service.check_in("in-1", "KID-1", "S1")

    def test_teacher_revocation_after_release_blocks_checkin(self):
        self.seed()
        self.happy_path()
        self.service.qualify_teacher("teacher-li-revoked", "LI", "INSTRUCTOR", status="REVOKED")
        self.assertEqual(self.service.session_view("S1")["status"], "FROZEN")
        with self.assertRaises(SessionFrozen):
            self.service.check_in("in-1", "KID-1", "S1")

    def test_frozen_session_recovers_after_swapping_recalled_batch(self):
        self.seed()
        self.happy_path()
        self.service.recall_batch("recall-a2", "BATCH-A2")
        self.assertEqual(self.service.session_view("S1")["status"], "FROZEN")

        # 替换召回批号：重锁、重新签署与复核后恢复放行（历史冻结事件仍留痕）
        self.service.swap_material("swap-1", "S1", "paper", "BATCH-A")
        self.service.lock_session("lock-S1-2", "S1", "PAPER_CUT", "v3",
                                  {"paper": "BATCH-A"}, "LI")
        self.service.confirm_guardian("guard-1-new", "KID-1", "S1", declared_allergens=[])
        self.service.record_review("rev2-safety", "S1", "SAFETY_OFFICER", "U1", "APPROVED")
        self.service.record_review("rev2-ops", "S1", "OPERATIONS_MANAGER", "U2", "APPROVED")
        self.service.release_session("release-S1-2", "S1", "U2")
        self.assertEqual(self.service.session_view("S1")["status"], "RELEASED")
        self.service.check_in("in-1", "KID-1", "S1")


class SwapMaterialTests(FlowTestBase):
    def test_swap_recomputes_allergen_restriction_against_new_batch(self):
        self.seed()
        # 孩子对乳胶过敏：A2 不含 latex，可以放行
        self.happy_path(allergens=["latex"])
        self.assertEqual(self.service.session_view("S1")["status"], "RELEASED")

        # 替换为含 latex 的 A：旧锁/复核/签署全部作废，风险按新批号重算
        appended = self.service.swap_material("swap-1", "S1", "paper", "BATCH-A")
        kinds = {e["kind"] for e in appended}
        self.assertIn("LOCK_SUPERSEDED", kinds)
        self.assertIn("REVIEWS_VOIDED", kinds)
        view = self.service.session_view("S1")
        self.assertIsNone(view["lock"])
        self.assertIsNone(view["release"])

        self.service.lock_session("lock-S1-2", "S1", "PAPER_CUT", "v3",
                                  {"paper": "BATCH-A"}, "LI")
        self.service.confirm_guardian("guard-1-latex", "KID-1", "S1",
                                      declared_allergens=["latex"])
        self.service.record_review("rev2-safety", "S1", "SAFETY_OFFICER", "U1", "APPROVED")
        self.service.record_review("rev2-ops", "S1", "OPERATIONS_MANAGER", "U2", "APPROVED")
        with self.assertRaises(GateBlocked) as ctx:
            self.service.release_session("release-S1-2", "S1", "U2")
        self.assertIn("ALLERGEN_MATCH", {f["code"] for f in ctx.exception.findings})

    def test_swap_requires_new_confirmation_and_new_reviews_then_releases(self):
        self.seed()
        self.happy_path()
        self.service.swap_material("swap-1", "S1", "paper", "BATCH-A")

        # 旧监护签署绑定旧锁，对新依据无效；trace 可见
        trace = self.service.trace("KID-1", "S1")
        self.assertFalse(trace["guardian_confirmation"]["valid_for_current_basis"])

        self.service.lock_session("lock-S1-2", "S1", "PAPER_CUT", "v3",
                                  {"paper": "BATCH-A"}, "LI")
        with self.assertRaises(GateBlocked) as ctx:
            self.service.release_session("release-S1-2", "S1", "U2")
        codes = {f["code"] for f in ctx.exception.findings}
        self.assertIn("GUARDIAN_NOT_CONFIRMED", codes)
        self.assertIn("REVIEW_MISSING", codes)

        # 名额与报名沿用（未重复占用），重新签署与复核后放行签到
        self.assertEqual(self.service.session_view("S1")["registered"], ["KID-1"])
        self.service.confirm_guardian("guard-1-new", "KID-1", "S1", declared_allergens=[])
        self.service.record_review("rev2-safety", "S1", "SAFETY_OFFICER", "U1", "APPROVED")
        self.service.record_review("rev2-ops", "S1", "OPERATIONS_MANAGER", "U2", "APPROVED")
        self.service.release_session("release-S1-2", "S1", "U2")
        self.assertEqual(self.service.session_view("S1")["status"], "RELEASED")
        self.service.check_in("in-1", "KID-1", "S1")

    def test_swap_rejected_after_completion(self):
        self.seed()
        self.happy_path(check_in=True)
        self.service.complete_session("done-S1", "S1")
        with self.assertRaises(SessionCompleted):
            self.service.swap_material("swap-late", "S1", "paper", "BATCH-A")


class IdempotencyAndConflictTests(FlowTestBase):
    def test_duplicate_guardian_confirmation_is_idempotent(self):
        self.seed(capacity=2)
        self.lock()
        self.service.register_participant("reg-1", "KID-1", "S1", 7)
        first = self.service.confirm_guardian("guard-1", "KID-1", "S1",
                                              declared_allergens=["latex"])
        dup = self.service.confirm_guardian("guard-1", "KID-1", "S1",
                                            declared_allergens=["latex"])
        self.assertTrue(dup["deduped"])
        self.assertEqual(dup["event_id"], first["event_id"])
        confirm_events = [e for e in EventStore(self.path).load()
                          if e["kind"] == "GUARDIAN_CONFIRMED"]
        self.assertEqual(len(confirm_events), 1)

    def test_duplicate_registration_does_not_take_second_seat(self):
        self.seed(capacity=1)
        self.service.register_participant("reg-1", "KID-1", "S1", 7)
        dup = self.service.register_participant("reg-1", "KID-1", "S1", 7)
        self.assertTrue(dup["deduped"])
        self.assertEqual(self.service.session_view("S1")["registered"], ["KID-1"])

    def test_capacity_enforced(self):
        self.seed(capacity=1)
        self.service.register_participant("reg-1", "KID-1", "S1", 7)
        with self.assertRaises(ReleaseError):
            self.service.register_participant("reg-2", "KID-2", "S1", 8)

    def test_same_batch_conflicting_contents_is_quarantined(self):
        self.seed()
        # BATCH-A2 已登记为无过敏原 paper；再以冲突内容接收同批号
        self.service.receive_material("mat-a2-conflict", "BATCH-A2", "paper",
                                      allergens=["peanut"])
        kinds = {e["kind"] for e in EventStore(self.path).load()}
        self.assertIn("BATCH_QUARANTINED", kinds)

        # 锁定冲突批号即触发冻结，无法放行
        self.lock()
        self.register_confirm("KID-1", 7)
        view = self.service.session_view("S1")
        self.assertEqual(view["status"], "FROZEN")
        self.assertEqual(view["freeze_causes"][0]["type"], "BATCH_QUARANTINED")
        # 锁定时的风险建议同样标出隔离批号
        self.assertIn("BATCH_QUARANTINED",
                      {f["code"] for f in view["lock"]["advice"]["findings"]})
        self.service.record_review("rev-safety", "S1", "SAFETY_OFFICER", "U1", "APPROVED")
        self.service.record_review("rev-ops", "S1", "OPERATIONS_MANAGER", "U2", "APPROVED")
        with self.assertRaises(SessionFrozen):
            self.service.release_session("release-S1", "S1", "U2")

    def test_same_event_id_with_different_payload_is_rejected(self):
        store = EventStore(self.path)
        store.append({"event_id": "x1", "kind": "MATERIAL_RECEIVED", "occurred_at": SINCE,
                      "subject_id": "B9", "payload": {"material_type": "paper"}})
        with self.assertRaises(EventConflictError):
            store.append({"event_id": "x1", "kind": "MATERIAL_RECEIVED", "occurred_at": SINCE,
                          "subject_id": "B9", "payload": {"material_type": "CLAY"}})

    def test_unknown_references_rejected(self):
        self.seed()
        with self.assertRaises(UnknownReference):
            self.service.lock_session("lock-x", "S1", "PAPER_CUT", "v9",
                                      {"paper": "BATCH-A2"}, "LI")
        with self.assertRaises(UnknownReference):
            self.service.recall_batch("recall-x", "BATCH-NOPE")


class ResumeAndNotificationTests(FlowTestBase):
    def test_resume_after_crash_does_not_duplicate_freeze_or_message(self):
        self.seed(session="S1")
        self.seed(session="S2")
        self.happy_path(session="S1")
        self.happy_path(pid="KID-2", age=8, session="S2")

        # 召回落日志、冻结与通知请求已派生（此进程不触碰下发通道）
        self.service.recall_batch("recall-a2", "BATCH-A2", reason="供应商通报")
        self.assertEqual(self.service.session_view("S1")["status"], "FROZEN")
        self.assertEqual(self.service.session_view("S2")["status"], "FROZEN")

        # 中继进程在“第一条消息已发送、DELIVERED 未确认”时崩溃
        flaky = FlakySink(crash_first=True)
        crashed_dispatcher = OutboxDispatcher(EventStore(self.path), flaky, lambda: SINCE)
        with self.assertRaises(RuntimeError):
            crashed_dispatcher.dispatch_pending()
        crashed_key = list(flaky.sent)[0]
        self.assertEqual(len(flaky.sent), 1)

        # 新进程继续：已发消息按幂等键取原回执，其余补齐，无任何重复
        store = EventStore(self.path)
        svc3 = ReleaseService(store, clock=lambda: SINCE)
        svc3.pump()
        dispatcher3 = OutboxDispatcher(store, flaky, lambda: SINCE)
        delivered = dispatcher3.dispatch_pending()
        keys = [e["payload"]["key"] for e in delivered]
        self.assertEqual(keys.count(crashed_key), 1)
        self.assertEqual(list(flaky.sent).count(crashed_key), 1)

        # 再跑一轮无事可做；冻结事件各场次仅一条
        self.assertEqual(dispatcher3.dispatch_pending(), [])
        store_events = store.load()
        freeze = [e for e in store_events if e["kind"] == "SESSION_FROZEN"]
        self.assertEqual({e["payload"]["session_id"] for e in freeze}, {"S1", "S2"})
        notifications = [e for e in store_events if e["kind"] == "NOTIFICATION_DELIVERED"]
        delivered_keys = [e["payload"]["key"] for e in notifications]
        self.assertEqual(len(delivered_keys), len(set(delivered_keys)))

    def test_pump_is_convergent_and_idempotent(self):
        self.seed()
        self.lock()
        # 无风险时 pump 不产生派生事件；重复执行同样为空
        self.assertEqual(self.service.pump(), [])
        self.assertEqual(self.service.pump(), [])
        self.service.recall_batch("recall-a2", "BATCH-A2")
        freeze_count = len([e for e in EventStore(self.path).load()
                            if e["kind"] == "SESSION_FROZEN"])
        self.assertEqual(self.service.pump(), [])
        self.assertEqual(len([e for e in EventStore(self.path).load()
                              if e["kind"] == "SESSION_FROZEN"]), freeze_count)


class TraceTests(FlowTestBase):
    def test_trace_participant_to_craft_material_and_releaser(self):
        self.seed()
        self.happy_path(check_in=True)
        self.service.complete_session("done-S1", "S1")
        trace = self.service.trace("KID-1", "S1")
        self.assertEqual(trace["craft"], {"craft_id": "PAPER_CUT", "version": "v3"})
        self.assertEqual(trace["batches"], {"paper": "BATCH-A2"})
        self.assertEqual(trace["teacher"]["teacher_id"], "LI")
        self.assertEqual(trace["release"]["released_by"], "U2")
        roles = {r["role"] for r in trace["release"]["reviewers"]}
        self.assertEqual(roles, {"SAFETY_OFFICER", "OPERATIONS_MANAGER"})
        self.assertTrue(trace["checked_in"])
        self.assertEqual(trace["completion"]["released_by"], "U2")

    def test_trace_shows_confirmation_invalid_after_swap(self):
        self.seed()
        self.happy_path()
        self.service.swap_material("swap-1", "S1", "paper", "BATCH-A")
        trace = self.service.trace("KID-1", "S1")
        self.assertFalse(trace["guardian_confirmation"]["valid_for_current_basis"])
        self.assertIsNone(trace["release"])


if __name__ == "__main__":
    unittest.main()
