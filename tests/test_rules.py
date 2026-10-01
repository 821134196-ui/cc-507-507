"""核心规则测试：授权时段、设备型号匹配、排班冲突、职责分离、无证替岗。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

from app import db as dbmod
from app import rules, services, server


def d(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


class AppTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.attach = os.path.join(self.tmp.name, "attachments")
        self.conn = dbmod.connect(self.db_path)
        dbmod.init_schema(self.conn)
        dbmod.seed(self.conn, attachments_dir=self.attach)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # ---------- 取数助手 ----------
    def emp(self, name):
        return self.conn.execute("SELECT * FROM employees WHERE name=?", (name,)).fetchone()

    def eq(self, code):
        return self.conn.execute("SELECT * FROM equipment WHERE code=?", (code,)).fetchone()

    def model(self, code):
        return self.conn.execute("SELECT * FROM equipment_models WHERE code=?", (code,)).fetchone()

    def proc(self, code):
        return self.conn.execute("SELECT * FROM processes WHERE code=?", (code,)).fetchone()

    def shift(self, work_date, shift_no):
        return self.conn.execute("SELECT * FROM shifts WHERE work_date=? AND shift_no=?",
                                 (work_date, shift_no)).fetchone()

    def slot(self, shift_id, eq_code):
        return self.conn.execute(
            """SELECT sl.* FROM shift_slots sl JOIN equipment e ON e.id=sl.equipment_id
                WHERE sl.shift_id=? AND e.code=?""", (shift_id, eq_code)).fetchone()

    def seed_pending_request(self, shift_id):
        return self.conn.execute(
            "SELECT * FROM temp_requests WHERE shift_id=? AND status='pending'", (shift_id,)
        ).fetchone()


class TestAuthorizationWindow(AppTestBase):
    """授权时段：未生效 / 有效期内 / 到期。"""

    def test_valid_today(self):
        ev = rules.evaluate(self.conn, self.emp("王磊")["id"], self.eq("EQ-01")["id"])
        self.assertTrue(ev["eligible"], ev["reasons"])

    def test_future_not_effective(self):
        aid = self.conn.execute(
            "SELECT id FROM authorizations WHERE employee_id=(SELECT id FROM employees WHERE name='王磊') "
            "AND model_id=(SELECT id FROM equipment_models WHERE code='CNC-A100')").fetchone()["id"]
        self.conn.execute("UPDATE authorizations SET valid_from=? WHERE id=?", (d(3), aid))
        ev = rules.evaluate(self.conn, self.emp("王磊")["id"], self.eq("EQ-01")["id"])
        self.assertFalse(ev["eligible"])
        self.assertTrue(any("未生效" in r for r in ev["reasons"]))

    def test_expired_blocks_and_sweep_marks(self):
        ev = rules.evaluate(self.conn, self.emp("张晨")["id"], self.eq("EQ-03")["id"])
        self.assertFalse(ev["eligible"])
        self.assertTrue(any("已到期" in r for r in ev["reasons"]))

        res = services.sweep_authorizations(self.conn)
        self.assertGreaterEqual(res["expired"], 1)
        row = self.conn.execute(
            "SELECT status FROM authorizations WHERE employee_id="
            "(SELECT id FROM employees WHERE name='张晨') AND model_id="
            "(SELECT id FROM equipment_models WHERE code='WELD-MIG')").fetchone()
        self.assertEqual(row["status"], "expired")
        notes = services.list_notifications(self.conn)
        self.assertTrue(any(n["type"] == "auth_expired" for n in notes))
        self.assertTrue(all(n["receipt"].startswith("SIM-NTF-") for n in notes))

        # 重复扫描不重复通知
        services.sweep_authorizations(self.conn)
        n1 = self.conn.execute("SELECT COUNT(*) c FROM notifications WHERE type='auth_expired'").fetchone()["c"]
        services.sweep_authorizations(self.conn)
        n2 = self.conn.execute("SELECT COUNT(*) c FROM notifications WHERE type='auth_expired'").fetchone()["c"]
        self.assertEqual(n1, n2)

    def test_expiring_warning(self):
        # 构造 3 天后到期的授权
        self.conn.execute(
            """INSERT INTO authorizations(employee_id,process_id,model_id,level,status,valid_from,valid_until)
               VALUES((SELECT id FROM employees WHERE name='陈静'),
                      (SELECT id FROM processes WHERE code='P-QC'), NULL, 1,'active',?,?)""",
            (d(-1), d(3)))
        res = services.sweep_authorizations(self.conn)
        self.assertGreaterEqual(res["expiring"], 1)


class TestEquipmentModelMatch(AppTestBase):
    """设备型号匹配：专属型号不互通、全型号覆盖、型号变更后失配。"""

    def test_model_specific_does_not_cross(self):
        # 王磊仅 A100 → 不能上 B200 的 EQ-02
        ev = rules.evaluate(self.conn, self.emp("王磊")["id"], self.eq("EQ-02")["id"])
        self.assertFalse(ev["eligible"])
        self.assertTrue(any("CNC-B200" in r for r in ev["reasons"]))
        # 李娜/陈静 B200 → 可上 EQ-02，不能上 A100 的 EQ-01
        self.assertTrue(rules.evaluate(self.conn, self.emp("李娜")["id"], self.eq("EQ-02")["id"])["eligible"])
        self.assertFalse(rules.evaluate(self.conn, self.emp("李娜")["id"], self.eq("EQ-01")["id"])["eligible"])
        self.assertTrue(rules.evaluate(self.conn, self.emp("陈静")["id"], self.eq("EQ-02")["id"])["eligible"])

    def test_all_models_covers(self):
        # 李娜质检为全型号授权 → 覆盖 QC-STD 设备
        self.assertTrue(rules.evaluate(self.conn, self.emp("李娜")["id"], self.eq("EQ-06")["id"])["eligible"])
        # 全型号不跨工序：李娜不能去焊接
        self.assertFalse(rules.evaluate(self.conn, self.emp("李娜")["id"], self.eq("EQ-04")["id"])["eligible"])

    def test_model_change_invalidates_old_auth(self):
        before = rules.evaluate(self.conn, self.emp("王磊")["id"], self.eq("EQ-01")["id"])
        self.assertTrue(before["eligible"])
        r = services.change_equipment_model(
            self.conn, self.eq("EQ-01")["id"], self.model("CNC-B200")["id"],
            self.emp("马文涛")["id"], "设备升级为 B200")
        self.assertTrue(r["ok"])
        # 旧 A100 授权不再适用
        self.assertFalse(rules.evaluate(self.conn, self.emp("王磊")["id"], self.eq("EQ-01")["id"])["eligible"])
        # 持 B200 授权的人变为合格
        self.assertTrue(rules.evaluate(self.conn, self.emp("陈静")["id"], self.eq("EQ-01")["id"])["eligible"])
        chg = self.conn.execute("SELECT * FROM equipment_changes WHERE equipment_id=? ORDER BY id DESC LIMIT 1",
                                (self.eq("EQ-01")["id"],)).fetchone()
        self.assertEqual(chg["new_model_id"], self.model("CNC-B200")["id"])

    def test_level_required(self):
        # EQ-03 机器人焊站要求 L3：王磊 L3 可上
        self.assertTrue(rules.evaluate(self.conn, self.emp("王磊")["id"], self.eq("EQ-03")["id"])["eligible"])
        # 张晨原焊接 L2 授权已到期：续期后可上 EQ-04(L2)，仍不能上 EQ-03(L3)
        self.conn.execute(
            """UPDATE authorizations SET valid_until=?
               WHERE employee_id=(SELECT id FROM employees WHERE name='张晨')
                 AND process_id=(SELECT id FROM processes WHERE code='P-WELD')
                 AND model_id=(SELECT id FROM equipment_models WHERE code='WELD-MIG')""",
            (d(30),))
        self.assertTrue(rules.evaluate(self.conn, self.emp("张晨")["id"], self.eq("EQ-04")["id"])["eligible"])
        ev = rules.evaluate(self.conn, self.emp("张晨")["id"], self.eq("EQ-03")["id"])
        self.assertFalse(ev["eligible"])
        self.assertTrue(any("等级不足" in r for r in ev["reasons"]))


class TestStatusAndAssessment(AppTestBase):
    """暂停 / 撤销 / 考核未通过 / 申诉。"""

    def test_suspended_and_revoked_block(self):
        ev = rules.evaluate(self.conn, self.emp("赵强")["id"], self.eq("EQ-01")["id"])
        self.assertFalse(ev["eligible"])
        self.assertTrue(any("暂停" in r for r in ev["reasons"]))
        ev2 = rules.evaluate(self.conn, self.emp("刘洋")["id"], self.eq("EQ-04")["id"])
        self.assertFalse(ev2["eligible"])
        self.assertTrue(any("撤销" in r for r in ev2["reasons"]))

    def test_failed_assessment_blocks_with_appeal_pending(self):
        # 即使人为恢复 active 授权，未决失败考核仍不放行
        self.conn.execute(
            "UPDATE authorizations SET status='active' WHERE employee_id="
            "(SELECT id FROM employees WHERE name='赵强')")
        ev = rules.evaluate(self.conn, self.emp("赵强")["id"], self.eq("EQ-01")["id"])
        self.assertFalse(ev["eligible"])
        self.assertTrue(any("考核未通过" in r and "申诉处理中" in r for r in ev["reasons"]))


class TestAssignmentConflict(AppTestBase):
    """排班冲突：同一员工同一班次唯一占用；两名班长并发看到冲突。"""

    def test_same_employee_same_shift_conflict(self):
        day = self.shift(d(0), "day")
        s1 = self.slot(day["id"], "EQ-02")["id"]  # B200，陈静持证
        s2 = self.slot(day["id"], "EQ-01")["id"]  # 同班次另一工位
        r1 = services.assign_employee(self.conn, self.emp("陈静")["id"], s1,
                                      actor_id=self.emp("周建国")["id"])
        self.assertTrue(r1["ok"], r1.get("problems"))
        r2 = services.assign_employee(self.conn, self.emp("陈静")["id"], s2,
                                      actor_id=self.emp("孙立军")["id"])
        self.assertFalse(r2["ok"])
        self.assertTrue(any("时段冲突" in p for p in r2["problems"]))

    def test_seed_night_assignment_conflict(self):
        night = self.shift(d(0), "night")
        # 王磊夜班已在 EQ-01
        r = services.assign_employee(
            self.conn, self.emp("王磊")["id"], self.slot(night["id"], "EQ-03")["id"],
            actor_id=self.emp("周建国")["id"])
        self.assertFalse(r["ok"])
        self.assertTrue(any("时段冲突" in p for p in r["problems"]))

    def test_different_shifts_allowed(self):
        # 李娜白班在 EQ-06；她持 B200 授权，排入夜班 EQ-02 属不同时段，允许
        night = self.shift(d(0), "night")
        r = services.assign_employee(
            self.conn, self.emp("李娜")["id"], self.slot(night["id"], "EQ-02")["id"],
            actor_id=self.emp("周建国")["id"])
        self.assertTrue(r["ok"], r.get("problems"))

    def test_two_foremens_requests_for_same_employee_conflict(self):
        """周建国已替刘洋提交夜班申请；孙立军再为同一人同时段提交 → 看到冲突。"""
        night = self.shift(d(0), "night")
        self.assertIsNotNone(self.seed_pending_request(night["id"]))
        r = services.create_temp_request(
            self.conn, slot_id=self.slot(night["id"], "EQ-03")["id"],
            employee_id=self.emp("刘洋")["id"], requester_id=self.emp("孙立军")["id"],
            vacancy_reason="另一工位也缺人", basis="none")
        self.assertFalse(r["ok"])
        self.assertTrue(any("待批替岗" in p for p in r["problems"]))

    def test_pending_request_blocks_direct_assign(self):
        night = self.shift(d(0), "night")
        r = services.assign_employee(
            self.conn, self.emp("刘洋")["id"], self.slot(night["id"], "EQ-06")["id"],
            actor_id=self.emp("周建国")["id"])
        self.assertFalse(r["ok"])
        self.assertTrue(any("替岗待批" in p for p in r["problems"]))

    def test_unqualified_direct_assign_blocked(self):
        day = self.shift(d(0), "day")
        # 刘洋焊接授权撤销，不能直接排入 EQ-04
        r = services.assign_employee(
            self.conn, self.emp("刘洋")["id"], self.slot(day["id"], "EQ-04")["id"],
            actor_id=self.emp("周建国")["id"])
        self.assertFalse(r["ok"])
        self.assertTrue(any("无有效授权" in p for p in r["problems"]))


class TestSegregationOfDuties(AppTestBase):
    """职责分离：班长申请、安全复核高风险、不可自审、培训师不可审批自己登记。"""

    def test_only_foreman_can_request(self):
        day = self.shift(d(0), "day")
        r = services.create_temp_request(
            self.conn, slot_id=self.slot(day["id"], "EQ-06")["id"],
            employee_id=self.emp("王磊")["id"], requester_id=self.emp("何秀云")["id"],
            vacancy_reason="缺人", basis="none")
        self.assertFalse(r["ok"])
        self.assertIn("只有班长可以申请替岗", r["problems"])

    def test_vacancy_reason_required(self):
        day = self.shift(d(0), "day")
        r = services.create_temp_request(
            self.conn, slot_id=self.slot(day["id"], "EQ-06")["id"],
            employee_id=self.emp("王磊")["id"], requester_id=self.emp("周建国")["id"],
            vacancy_reason="  ", basis="none")
        self.assertFalse(r["ok"])
        self.assertIn("必须说明原岗位空缺原因", r["problems"])

    def test_high_risk_requires_safety_and_no_self_review(self):
        night = self.shift(d(0), "night")
        req = self.seed_pending_request(night["id"])
        zhou = self.emp("周建国")["id"]
        sun = self.emp("孙立军")["id"]
        ma = self.emp("马文涛")["id"]

        # 申请人自审 → 拒绝
        r = services.review_temp_request(self.conn, req["id"], zhou, True)
        self.assertFalse(r["ok"])
        self.assertTrue(any("不能复核自己" in p for p in r["problems"]))
        # 另一名班长复核高风险 → 拒绝
        r = services.review_temp_request(self.conn, req["id"], sun, True)
        self.assertFalse(r["ok"])
        self.assertTrue(any("安全负责人" in p for p in r["problems"]))
        # 安全负责人批准 → 通过并落班（无证替岗）
        r = services.review_temp_request(self.conn, req["id"], ma, True, "夜班应急，已加监护")
        self.assertTrue(r["ok"], r.get("problems"))
        sa = self.conn.execute(
            "SELECT * FROM shift_assignments WHERE request_id=?", (req["id"],)).fetchone()
        self.assertIsNotNone(sa)
        self.assertEqual(sa["is_temp"], 1)
        # 落班人员资格仍判定为无授权（留痕，不洗白）
        ev = rules.evaluate(self.conn, sa["employee_id"], self.eq("EQ-04")["id"])
        self.assertFalse(ev["eligible"])
        # 再次处理 → 已处理
        r = services.review_temp_request(self.conn, req["id"], ma, False)
        self.assertFalse(r["ok"])

    def test_normal_equipment_foreman_review(self):
        night = self.shift(d(0), "night")
        # 陈静夜班空闲、无质检授权 → 普通设备 EQ-06 的无证替岗，班长复核即可
        r = services.create_temp_request(
            self.conn, slot_id=self.slot(night["id"], "EQ-06")["id"],
            employee_id=self.emp("陈静")["id"], requester_id=self.emp("周建国")["id"],
            vacancy_reason="质检员李娜临时请假", basis="none")
        self.assertTrue(r["ok"], r.get("problems"))
        rid = r["request_id"]
        r = services.review_temp_request(self.conn, rid, self.emp("孙立军")["id"], True)
        self.assertTrue(r["ok"], r.get("problems"))

    def test_trainer_cannot_approve_own_assessment(self):
        trainer = self.emp("何秀云")["id"]
        safety = self.emp("马文涛")["id"]
        cur = self.conn.execute(
            """INSERT INTO assessments(employee_id,process_id,model_id,exam_date,score,pass_score,
               result,status,recorded_by,created_at)
               VALUES((SELECT id FROM employees WHERE name='王磊'),
                      (SELECT id FROM processes WHERE code='P-CNC'),
                      (SELECT id FROM equipment_models WHERE code='CNC-A100'),
                      ?,90,60,'pass','pending',?,?)""",
            (d(-1), trainer, rules.now_iso()))
        aid = cur.lastrowid
        r = services.approve_assessment(self.conn, aid, trainer)
        self.assertFalse(r["ok"])
        self.assertTrue(any("不能审批自己登记" in p for p in r["problems"]))
        r = services.approve_assessment(self.conn, aid, safety)
        self.assertTrue(r["ok"])

    def test_appeal_flow_and_recorder_conflict(self):
        zhao = self.emp("赵强")["id"]
        trainer = self.emp("何秀云")["id"]
        safety = self.emp("马文涛")["id"]
        aid = self.conn.execute(
            "SELECT id FROM assessments WHERE employee_id=? AND result='fail'", (zhao,)).fetchone()["id"]
        # 他人不能代申诉
        r = services.appeal_assessment(self.conn, aid, self.emp("王磊")["id"], "异议")
        self.assertFalse(r["ok"])
        self.assertTrue(any("本人" in p for p in r["problems"]))
        # 种子数据中赵强的申诉已是 pending；重复申诉被拒
        r = services.appeal_assessment(self.conn, aid, zhao, "实操第三项评分有误")
        self.assertFalse(r["ok"])
        self.assertIn("已在申诉处理中", r["problems"])
        # 登记培训师不能裁决
        r = services.decide_appeal(self.conn, aid, trainer, True)
        self.assertFalse(r["ok"])
        self.assertTrue(any("不能裁决自己登记" in p for p in r["problems"]))
        # 安全负责人裁决成立
        r = services.decide_appeal(self.conn, aid, safety, True, "复核录像后加分")
        self.assertTrue(r["ok"])
        self.assertEqual(r["result"], "upheld")


class TestUnlicensedSubstitute(AppTestBase):
    """无证替岗：审批可覆盖资格缺口，但不能覆盖排班冲突。"""

    def test_force_still_blocks_conflict(self):
        night = self.shift(d(0), "night")
        req = self.seed_pending_request(night["id"])
        # 先把刘洋排到夜班别处（绕过常规检查直接造数据：EQ-06）
        self.conn.execute(
            """INSERT INTO shift_assignments(slot_id,shift_id,employee_id,is_temp,created_by,created_at)
               VALUES(?,?,?,1,?,?)""",
            (self.slot(night["id"], "EQ-06")["id"], night["id"],
             self.emp("刘洋")["id"], self.emp("马文涛")["id"], rules.now_iso()))
        # 安全负责人批准原申请 → 因唯一索引冲突失败，而不是硬排
        r = services.review_temp_request(self.conn, req["id"], self.emp("马文涛")["id"], True)
        self.assertFalse(r["ok"])
        self.assertTrue(any("时段冲突" in p for p in r["problems"]))


class TestHTTPSmoke(unittest.TestCase):
    """起真实 HTTP 服务做端到端冒烟。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "http.db")
        self._old_attach = server.ATTACH_DIR
        server.ATTACH_DIR = os.path.join(self.tmp.name, "attachments")
        self.httpd = server.build("127.0.0.1", 0, db_path=self.db_path)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.conn = dbmod.connect(self.db_path)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.conn.close()
        server.ATTACH_DIR = self._old_attach
        self.tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())

    def post(self, path, payload):
        req = urllib.request.Request(
            self.url(path), data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_pages_and_rules_over_http(self):
        status, matrix = self.get("/api/matrix")
        self.assertEqual(status, 200)
        self.assertTrue(matrix["processes"] and matrix["employees"])
        self.assertIn("cells", matrix)

        # 首页可达
        with urllib.request.urlopen(self.url("/"), timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])

        shifts = self.get("/api/shifts")[1]
        night = next(s for s in shifts if s["shift_no"] == "night")
        sd = self.get(f"/api/shift?id={night['id']}")[1]
        self.assertTrue(any(r["unlicensed"] for r in sd["requests"]), "应包含无证替岗样例")

        emp_id = self.conn.execute("SELECT id FROM employees WHERE name='陈静'").fetchone()["id"]
        foreman = self.conn.execute("SELECT id FROM employees WHERE name='周建国'").fetchone()["id"]
        trainer = self.conn.execute("SELECT id FROM employees WHERE name='何秀云'").fetchone()["id"]
        safety = self.conn.execute("SELECT id FROM employees WHERE name='马文涛'").fetchone()["id"]

        # 合格排入成功
        slot_b200 = next(s for s in sd["slots"] if s["eq_code"] == "EQ-02")["id"]
        st, body = self.post("/api/assign", {"employee_id": emp_id, "slot_id": slot_b200,
                                             "actor_id": foreman})
        self.assertEqual(st, 200, body)

        # 再次排到同班次另一工位 → 409 冲突
        slot_other = next(s for s in sd["slots"] if s["eq_code"] == "EQ-06")["id"]
        st, body = self.post("/api/assign", {"employee_id": emp_id, "slot_id": slot_other,
                                             "actor_id": foreman})
        self.assertEqual(st, 409)
        self.assertTrue(any("时段冲突" in p for p in body["problems"]))

        # 培训师自批登记考核 → 409 职责分离
        st, asm = self.post("/api/assessments", {
            "employee_id": emp_id,
            "process_id": self.conn.execute("SELECT id FROM processes WHERE code='P-CNC'").fetchone()["id"],
            "model_id": None, "exam_date": d(-1), "score": 88, "recorded_by": trainer})
        self.assertEqual(st, 200, asm)
        st, body = self.post(f"/api/assessments/{asm['assessment_id']}/approve",
                             {"approver_id": trainer})
        self.assertEqual(st, 409)
        st, body = self.post(f"/api/assessments/{asm['assessment_id']}/approve",
                             {"approver_id": safety})
        self.assertEqual(st, 200)

        # 高风险无证替岗：班长自批 409，安全负责人批准 200
        req = next(r for r in sd["requests"] if r["unlicensed"])
        st, body = self.post(f"/api/temp-requests/{req['id']}/review",
                             {"reviewer_id": foreman, "approve": True})
        self.assertEqual(st, 409)
        st, body = self.post(f"/api/temp-requests/{req['id']}/review",
                             {"reviewer_id": safety, "approve": True, "note": "同意夜班应急"})
        self.assertEqual(st, 200, body)

        # 通知含模拟回执
        notes = self.get("/api/notifications")[1]
        self.assertTrue(notes)
        self.assertTrue(all(n["receipt"].startswith("SIM-NTF-") for n in notes))


if __name__ == "__main__":
    unittest.main()
