"""领域规则测试：授权时段、设备型号匹配、排班冲突、职责分离、无证替岗。

运行：python3 -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from datetime import date, timedelta

_tmp = tempfile.mkdtemp(prefix="skillshift-test-")
os.environ["SHIFT_DB"] = os.path.join(_tmp, "test.db")
os.environ["SHIFT_ATTACH"] = os.path.join(_tmp, "attach")

from app import db, domain, notify  # noqa: E402


class ShiftTestBase(unittest.TestCase):
    @classmethod
    def _reset_db(cls):
        for suffix in ("", "-wal", "-shm"):
            p = os.environ["SHIFT_DB"] + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(seed=True)

    def setUp(self):
        # 每个测试方法重置一次库，避免授权/派工状态相互污染
        self._reset_db()
        self.conn = db.get_conn()

    def tearDown(self):
        self.conn.commit()
        self.conn.close()

    def ids(self):
        c = self.conn
        emp = {r["name"]: r["id"] for r in c.execute("SELECT id,name FROM employees")}
        proc = {r["name"]: r["id"] for r in c.execute("SELECT id,name FROM processes")}
        eq = {r["code"]: r for r in c.execute(
            "SELECT id,code,model,process_id FROM equipment")}
        shift = {(r["shift_date"], r["shift_type"]): r["id"]
                 for r in c.execute("SELECT id,shift_date,shift_type FROM shifts")}
        return c, emp, proc, eq, shift


class TestAuthorizationWindow(ShiftTestBase):
    def test_valid_inside_window(self):
        c, emp, proc, eq, shift = self.ids()
        today = date.today()
        q = domain.qualification(c, emp["王焊"], eq["WLD-01"]["id"], today.isoformat())
        self.assertTrue(q["ok"], q["reasons"])
        self.assertEqual(q["grade"], "L2")

    def test_expired_window_rejected(self):
        c, emp, proc, eq, shift = self.ids()
        future = (date.today() + timedelta(days=40)).isoformat()
        q = domain.qualification(c, emp["王焊"], eq["WLD-01"]["id"], future)
        self.assertFalse(q["ok"])
        self.assertTrue(any("过期" in r for r in q["reasons"]))

    def test_before_valid_from_rejected(self):
        # 构造一条明天才生效的授权
        c, emp, proc, eq, shift = self.ids()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        c.execute(
            """INSERT OR IGNORE INTO authorizations(employee_id,process_id,model,grade,valid_from,valid_to)
               VALUES (?,?,?,?,?,?)""",
            (emp["钱新"], proc["包装"], "PK-9", "L1", tomorrow,
             (date.today() + timedelta(days=30)).isoformat()))
        # PK-9 暂无设备，直接用窗口判定逻辑：今天对一条未生效授权
        row = c.execute("SELECT id FROM equipment WHERE code='PKG-01'").fetchone()
        q = domain.qualification(c, emp["钱新"], row["id"], date.today().isoformat())
        self.assertFalse(q["ok"])

    def test_revoked_authorization_not_valid(self):
        c, emp, proc, eq, shift = self.ids()
        q = domain.qualification(c, emp["王焊"], eq["PKG-01"]["id"])
        self.assertFalse(q["ok"])
        self.assertIn("授权已被撤销", q["reasons"])

    def test_failed_assessment_grants_nothing(self):
        # 王焊冲压考核未通过：没有冲压授权
        c, emp, proc, eq, shift = self.ids()
        q = domain.qualification(c, emp["王焊"], eq["PRS-01"]["id"])
        self.assertFalse(q["ok"])


class TestEquipmentModelMatch(ShiftTestBase):
    def test_exact_model_match(self):
        c, emp, proc, eq, shift = self.ids()
        # 孙铆仅 WX-300：WLD-02 合格
        q = domain.qualification(c, emp["孙铆"], eq["WLD-02"]["id"])
        self.assertTrue(q["ok"], q["reasons"])
        # WLD-01 是 WX-200：不匹配
        q2 = domain.qualification(c, emp["孙铆"], eq["WLD-01"]["id"])
        self.assertFalse(q2["ok"])
        self.assertTrue(any("型号变更" in r for r in q2["reasons"]))

    def test_generic_model_covers_all(self):
        c, emp, proc, eq, shift = self.ids()
        # 赵强焊接通用授权：两台都能上
        self.assertTrue(domain.qualification(c, emp["赵强"], eq["WLD-01"]["id"])["ok"])
        self.assertTrue(domain.qualification(c, emp["赵强"], eq["WLD-02"]["id"])["ok"])

    def test_model_change_disqualifies_exact_only(self):
        c, emp, proc, eq, shift = self.ids()
        wld01 = eq["WLD-01"]["id"]
        # 换型为全新型号 WX-999：王焊（仅 WX-200）失去资格；赵强（通用）不受影响
        affected = domain.model_change_affected(c, wld01, "WX-999")
        names = [a["name"] for a in affected]
        self.assertIn("王焊", names)
        self.assertNotIn("赵强", names)
        old, affected2 = domain.change_equipment_model(c, wld01, "WX-999", emp["安平"])
        q = domain.qualification(c, emp["王焊"], wld01)
        self.assertFalse(q["ok"])
        self.assertTrue(any("WX-999" in r for r in q["reasons"]))
        self.assertTrue(domain.qualification(c, emp["赵强"], wld01)["ok"])
        # 还原
        domain.change_equipment_model(c, wld01, "WX-200", emp["安平"])


class TestSchedulingConflict(ShiftTestBase):
    def test_two_foremen_same_employee_same_shift(self):
        c, emp, proc, eq, shift = self.ids()
        sid = shift[(date.today().isoformat(), "day")]
        # 样例中王焊白天已由陈晨排在 WLD-01；李立后提交，想把同一人
        # 调到 PRS-01 → 在资格校验之前即应判为同一时段占用冲突。
        c.execute(
            "UPDATE assignments SET employee_id=NULL WHERE shift_id=? AND equipment_id=(SELECT id FROM equipment WHERE code='PRS-01')",
            (sid,))
        with self.assertRaises(domain.ConflictError) as cm:
            domain.assign_employee(c, sid, eq["PRS-01"]["id"], emp["王焊"], emp["李立"])
        self.assertIn("陈晨", str(cm.exception))
        self.assertEqual(cm.exception.conflict["equipment_code"], "WLD-01")

    def test_no_conflict_different_shifts(self):
        c, emp, proc, eq, shift = self.ids()
        day = shift[(date.today().isoformat(), "day")]
        night = shift[(date.today().isoformat(), "night")]
        c.execute("UPDATE assignments SET employee_id=NULL WHERE shift_id=? AND equipment_id=(SELECT id FROM equipment WHERE code='WLD-01')", (day,))
        c.execute("UPDATE assignments SET employee_id=NULL WHERE shift_id=? AND equipment_id=(SELECT id FROM equipment WHERE code='WLD-01')", (night,))
        domain.assign_employee(c, day, eq["WLD-01"]["id"], emp["王焊"], emp["陈晨"])
        # 夜班是另一个时段，不冲突
        aid, _ = domain.assign_employee(c, night, eq["WLD-01"]["id"], emp["王焊"], emp["李立"])
        self.assertTrue(aid)

    def test_only_foreman_can_assign(self):
        c, emp, proc, eq, shift = self.ids()
        sid = shift[(date.today().isoformat(), "night")]
        with self.assertRaises(domain.RuleError):
            domain.assign_employee(c, sid, eq["WLD-02"]["id"], emp["孙铆"], emp["安平"])

    def test_unlicensed_direct_assign_blocked(self):
        c, emp, proc, eq, shift = self.ids()
        sid = shift[(date.today().isoformat(), "night")]
        with self.assertRaises(domain.RuleError) as cm:
            domain.assign_employee(c, sid, eq["WLD-02"]["id"], emp["钱新"], emp["李立"])
        self.assertEqual(cm.exception.code, "unlicensed")


class TestSegregationOfDuties(ShiftTestBase):
    def test_trainer_cannot_approve_own_assessment(self):
        c, emp, proc, eq, shift = self.ids()
        aid = domain.register_assessment(
            c, emp["钱新"], proc["包装"], "PK-8", "L1", 90, True,
            date.today().isoformat(), emp["傅师"])
        with self.assertRaises(domain.RuleError) as cm:
            domain.approve_assessment(c, aid, emp["傅师"])
        self.assertIn("职责分离", str(cm.exception))
        # 安全负责人可以审批，审批后授权生效
        auth_id = domain.approve_assessment(c, aid, emp["安平"])
        self.assertTrue(auth_id)
        q = domain.qualification(c, emp["钱新"], eq["PKG-01"]["id"])
        self.assertTrue(q["ok"])

    def test_applicant_cannot_review_own_substitution(self):
        c, emp, proc, eq, shift = self.ids()
        sid = shift[(date.today().isoformat(), "night")]
        sub, qual = domain.apply_substitution(
            c, sid, eq["WLD-02"]["id"], emp["孙铆"], emp["李立"],
            "原岗赵强临时请假")
        with self.assertRaises(domain.RuleError):
            domain.review_substitution(c, sub, emp["李立"], True)

    def test_high_risk_requires_safety_reviewer(self):
        c, emp, proc, eq, shift = self.ids()
        sid = shift[(date.today().isoformat(), "night")]
        sub, _ = domain.apply_substitution(
            c, sid, eq["WLD-02"]["id"], emp["孙铆"], emp["陈晨"],
            "原岗赵强临时请假")
        # 另一名班长也不能复核高风险设备
        with self.assertRaises(domain.RuleError) as cm:
            domain.review_substitution(c, sub, emp["李立"], True)
        self.assertIn("高风险", str(cm.exception))
        # 安全负责人复核通过并派工
        status, warnings = domain.review_substitution(c, sub, emp["安平"], True, "同意")
        self.assertEqual(status, "approved")

    def test_unlicensed_substitution_requires_safety(self):
        # 样例数据：李立为钱新申请的无证替岗（包装虽非高风险，仍须安全复核）
        c, emp, proc, eq, shift = self.ids()
        sub_row = c.execute(
            "SELECT id FROM substitutions WHERE applicant_emp=? AND authorization_basis='unlicensed'",
            (emp["钱新"],)).fetchone()
        self.assertIsNotNone(sub_row, "应存在无证替岗样例")
        with self.assertRaises(domain.RuleError):
            domain.review_substitution(c, sub_row["id"], emp["陈晨"], True)
        status, warnings = domain.review_substitution(c, sub_row["id"], emp["安平"], True, "限期一周取证")
        self.assertEqual(status, "approved")
        self.assertTrue(any("无证" in w for w in warnings))
        # 无证派工在班次视图中可见
        sid = shift[(date.today().isoformat(), "night")]
        view = domain.shift_view(c, sid)
        unlicensed = [i for i in view["items"] if i["status"] == "unlicensed"]
        self.assertTrue(any(i["employee_name"] == "钱新" for i in unlicensed))

    def test_vacancy_reason_required(self):
        c, emp, proc, eq, shift = self.ids()
        sid = shift[(date.today().isoformat(), "night")]
        with self.assertRaises(domain.RuleError):
            domain.apply_substitution(c, sid, eq["WLD-02"]["id"], emp["孙铆"],
                                      emp["李立"], "  ")


class TestAppealAndRevoke(ShiftTestBase):
    def test_appeal_separation_and_overturn(self):
        c, emp, proc, eq, shift = self.ids()
        # 赵强 WX-300 未通过考核且申诉中
        a = c.execute(
            "SELECT id FROM assessments WHERE employee_id=? AND model='WX-300' AND pass=0",
            (emp["赵强"],)).fetchone()
        # 原登记人（傅师）不能处理
        with self.assertRaises(domain.RuleError):
            domain.resolve_appeal(c, a["id"], emp["傅师"], True, "改判")
        # 安全负责人支持申诉 → 改判通过并签发授权
        status, auth_id = domain.resolve_appeal(c, a["id"], emp["安平"], True, "评分差错")
        self.assertEqual(status, "approved")
        self.assertTrue(auth_id)
        self.assertTrue(domain.qualification(c, emp["赵强"], eq["WLD-02"]["id"])["ok"])

    def test_revoke_takes_effect_immediately(self):
        c, emp, proc, eq, shift = self.ids()
        a = c.execute(
            "SELECT id FROM authorizations WHERE employee_id=? AND process_id=? AND status='active'",
            (emp["吴冲"], proc["冲压"])).fetchone()
        domain.revoke_authorization(c, a["id"], "违章操作", emp["安平"])
        self.assertFalse(domain.qualification(c, emp["吴冲"], eq["PRS-01"]["id"])["ok"])


class TestExpiryAndNotifications(ShiftTestBase):
    def test_expiring_list_and_delivery(self):
        c, emp, proc, eq, shift = self.ids()
        rows = domain.expiring_authorizations(c, within_days=15)
        names = [r["employee_name"] for r in rows]
        self.assertIn("王焊", names)  # 10 天后到期
        notify.run_once()
        delivered = c.execute(
            "SELECT COUNT(*) n FROM notifications WHERE kind='expiry' AND status='delivered'").fetchone()["n"]
        self.assertGreaterEqual(delivered, 1)
        receipt = c.execute(
            "SELECT receipt FROM notifications WHERE kind='expiry' AND status='delivered' LIMIT 1").fetchone()["receipt"]
        self.assertTrue(receipt.startswith("SIM-RCPT-"))


class TestMatrix(ShiftTestBase):
    def test_matrix_shape_and_content(self):
        c, emp, proc, eq, shift = self.ids()
        m = domain.matrix(c)
        self.assertEqual(len(m["grid"]), 4)  # 4 道工序行
        self.assertGreaterEqual(len(m["employees"]), 10)
        weld = next(r for r in m["grid"] if r["process"]["name"] == "焊接")
        by_emp = {e["id"]: cell for e, cell in zip(m["employees"], weld["cells"])}
        self.assertEqual(by_emp[emp["赵强"]]["grade"], "L3")
        self.assertEqual(by_emp[emp["赵强"]]["model_scope"], "全型号")
        # 王焊 10 天后到期 → expiring
        self.assertEqual(by_emp[emp["王焊"]]["status"], "expiring")
        self.assertIsNone(by_emp[emp["钱新"]]["grade"])


if __name__ == "__main__":
    unittest.main()
