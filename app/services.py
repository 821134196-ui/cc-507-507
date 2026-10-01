"""写操作服务：排班、替岗、考核/申诉、通知、到期扫描。"""
from __future__ import annotations

import sqlite3
import threading
import time
import itertools
from datetime import date, timedelta

from . import rules

WARN_DAYS = 7
_receipt_seq = itertools.count(1)


# ---------------- 通知（进程内任务 + 模拟回执） ----------------

def _simulated_receipt(kind: str) -> str:
    # 不使用随机数/时钟，保证结果可复现、可测试
    return f"SIM-{kind}-{next(_receipt_seq):04d}"


def notify(conn, ntype: str, title: str, body: str,
           ref_type: str | None = None, ref_id: int | None = None,
           commit: bool = True) -> dict:
    receipt = _simulated_receipt("NTF")
    row = conn.execute(
        """INSERT INTO notifications(type,title,body,ref_type,ref_id,receipt,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (ntype, title, body, ref_type, ref_id, receipt, rules.now_iso()),
    )
    if commit:
        conn.commit()
    return {"id": row.lastrowid, "receipt": receipt, "title": title, "body": body}


def list_notifications(conn, unread_only: bool = False, limit: int = 50):
    sql = "SELECT * FROM notifications"
    if unread_only:
        sql += " WHERE read_at IS NULL"
    sql += " ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, (limit,))]


def mark_read(conn, notif_id: int):
    conn.execute("UPDATE notifications SET read_at=? WHERE id=? AND read_at IS NULL",
                 (rules.now_iso(), notif_id))
    conn.commit()


# ---------------- 排班拖拽 ----------------

def assign_employee(conn, employee_id: int, slot_id: int, *,
                    actor_id: int, is_temp: bool = False, request_id: int | None = None,
                    force_unlicensed: bool = False, on_date: str | None = None):
    """把人排到工位。返回 {ok, problems, assignment_id}。"""
    sl = conn.execute("SELECT * FROM shift_slots WHERE id=?", (slot_id,)).fetchone()
    if sl is None:
        return {"ok": False, "problems": ["工位不存在"], "assignment_id": None}
    ok, problems, ev = rules.check_assign(conn, employee_id, sl["equipment_id"],
                                          sl["shift_id"], on_date)
    if not ok and not force_unlicensed:
        return {"ok": False, "problems": problems, "assignment_id": None}
    if force_unlicensed and not any("时段冲突" in p or "替岗待批" in p for p in problems):
        # 无证替岗：仅允许"资格类"问题被审批覆盖，冲突绝不允许
        pass
    elif not ok:
        return {"ok": False, "problems": problems, "assignment_id": None}

    cur = conn.execute(
        """INSERT INTO shift_assignments(slot_id,shift_id,employee_id,is_temp,request_id,
           created_by,created_at) VALUES(?,?,?,?,?,?,?)""",
        (slot_id, sl["shift_id"], employee_id, 1 if is_temp else 0, request_id,
         actor_id, rules.now_iso()),
    )
    conn.commit()
    return {"ok": True, "problems": problems, "assignment_id": cur.lastrowid}


def unassign(conn, assignment_id: int):
    conn.execute("DELETE FROM shift_assignments WHERE id=?", (assignment_id,))
    conn.commit()
    return {"ok": True}


# ---------------- 替岗申请 ----------------

def create_temp_request(conn, *, slot_id: int, employee_id: int, requester_id: int,
                        vacancy_reason: str, basis: str, on_date: str | None = None):
    requester = conn.execute("SELECT * FROM employees WHERE id=?", (requester_id,)).fetchone()
    if not rules.can_request_substitute(requester):
        return {"ok": False, "problems": ["只有班长可以申请替岗"]}
    sl = conn.execute("SELECT * FROM shift_slots WHERE id=?", (slot_id,)).fetchone()
    if sl is None:
        return {"ok": False, "problems": ["工位不存在"]}
    if not vacancy_reason.strip():
        return {"ok": False, "problems": ["必须说明原岗位空缺原因"]}

    ev = rules.evaluate(conn, employee_id, sl["equipment_id"], on_date)
    unlicensed = not ev["eligible"]
    if basis == "none":
        unlicensed = True
    c = rules.assignment_conflict(conn, employee_id, sl["shift_id"])
    if c:
        return {"ok": False, "problems": [f"时段冲突：该员工本班次已在 {c['eq_code']} {c['eq_name']}"]}
    c2 = rules.pending_request_conflict(conn, employee_id, sl["shift_id"])
    if c2:
        return {"ok": False, "problems": [f"该员工已有待批替岗申请：{c2['eq_code']} {c2['eq_name']}"]}

    cur = conn.execute(
        """INSERT INTO temp_requests(shift_id,slot_id,employee_id,vacancy_reason,basis,
           unlicensed,requested_by,status,created_at) VALUES(?,?,?,?,?,?,?, 'pending',?)""",
        (sl["shift_id"], slot_id, employee_id, vacancy_reason.strip(),
         basis, 1 if unlicensed else 0, requester_id, rules.now_iso()),
    )
    req_id = cur.lastrowid
    need = rules.required_reviewer(conn, sl["equipment_id"])
    eq = conn.execute("SELECT e.* FROM equipment e JOIN shift_slots s ON s.equipment_id=e.id WHERE s.id=?",
                      (slot_id,)).fetchone()
    who = "安全负责人复核（高风险设备）" if need == "safety" else "另一名班长复核"
    emp = conn.execute("SELECT name FROM employees WHERE id=?", (employee_id,)).fetchone()["name"]
    warn = "；该员工无有效授权，属无证替岗" if unlicensed else ""
    notify(conn, "temp_request",
           f"替岗申请待{who}",
           f"{eq['code']} {eq['name']} 拟由 {emp} 顶岗{warn}。空缺原因：{vacancy_reason}",
           "temp_request", req_id, commit=False)
    conn.commit()
    return {"ok": True, "request_id": req_id, "unlicensed": unlicensed,
            "required_reviewer": need}


def review_temp_request(conn, request_id: int, reviewer_id: int,
                        approve: bool, note: str = ""):
    problems = rules.validate_review(conn, request_id, reviewer_id)
    if problems:
        return {"ok": False, "problems": problems}
    tr = conn.execute("SELECT * FROM temp_requests WHERE id=?", (request_id,)).fetchone()
    if tr["status"] != "pending":
        return {"ok": False, "problems": [f"申请已处理（{tr['status']}）"]}

    status = "approved" if approve else "rejected"
    conn.execute(
        "UPDATE temp_requests SET status=?, reviewed_by=?, review_note=?, reviewed_at=? WHERE id=?",
        (status, reviewer_id, note, rules.now_iso(), request_id),
    )
    emp = conn.execute("SELECT name FROM employees WHERE id=?", (tr["employee_id"],)).fetchone()["name"]
    eq = conn.execute(
        "SELECT e.code,e.name FROM equipment e JOIN shift_slots s ON s.equipment_id=e.id WHERE s.id=?",
        (tr["slot_id"],)).fetchone()
    if approve:
        # 审批即落班：无证也允许（force），冲突仍被 assign 拦截
        res = assign_employee(conn, tr["employee_id"], tr["slot_id"], actor_id=reviewer_id,
                              is_temp=True, request_id=request_id, force_unlicensed=True)
        if not res["ok"]:
            conn.rollback()
            return {"ok": False, "problems": res["problems"]}
        tag = "（无证替岗，已登记授权例外）" if tr["unlicensed"] else ""
        notify(conn, "temp_reviewed", "替岗已批准",
               f"{emp} 顶岗 {eq['code']} {eq['name']} 已批准{tag}。{note}",
               "temp_request", request_id, commit=False)
    else:
        notify(conn, "temp_reviewed", "替岗已驳回",
               f"{emp} 顶岗 {eq['code']} {eq['name']} 被驳回。{note}",
               "temp_request", request_id, commit=False)
    conn.commit()
    return {"ok": True, "status": status}


# ---------------- 考核申诉 ----------------

def appeal_assessment(conn, assessment_id: int, employee_id: int, reason: str):
    a = conn.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if a is None:
        return {"ok": False, "problems": ["考核记录不存在"]}
    if a["employee_id"] != employee_id:
        return {"ok": False, "problems": ["只能对本人的考核提出申诉"]}
    if a["result"] != "fail":
        return {"ok": False, "problems": ["仅可对未通过的考核申诉"]}
    if a["appeal_status"] == "pending":
        return {"ok": False, "problems": ["已在申诉处理中"]}
    conn.execute(
        "UPDATE assessments SET appeal_status='pending', appeal_reason=? WHERE id=?",
        (reason, assessment_id),
    )
    notify(conn, "appeal", "收到培训成绩申诉",
           f"考核 #{assessment_id} 申诉处理中：{reason}", "assessment", assessment_id, commit=False)
    conn.commit()
    return {"ok": True}


def decide_appeal(conn, assessment_id: int, decider_id: int, uphold: bool, note: str = ""):
    a = conn.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if a is None or a["appeal_status"] != "pending":
        return {"ok": False, "problems": ["无待处理申诉"]}
    # 登记人不能裁决自己登记考核的申诉（与"不能审批自己登记的考核"同源）
    if decider_id == a["recorded_by"]:
        return {"ok": False, "problems": ["培训老师不能裁决自己登记考核的申诉"]}
    result = "upheld" if uphold else "rejected"
    conn.execute(
        "UPDATE assessments SET appeal_status=?, appeal_decided_by=?, appeal_decided_at=? WHERE id=?",
        (result, decider_id, rules.now_iso(), assessment_id),
    )
    body = f"考核 #{assessment_id} 申诉{'成立，恢复授权流程' if uphold else '被驳回'}。{note}"
    notify(conn, "appeal_done", "申诉结果", body, "assessment", assessment_id, commit=False)
    conn.commit()
    return {"ok": True, "result": result}


def approve_assessment(conn, assessment_id: int, approver_id: int):
    """考核审批：登记人与审批人不可为同一人。"""
    a = conn.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if a is None:
        return {"ok": False, "problems": ["考核记录不存在"]}
    if not rules.assessment_approval_allowed(a["recorded_by"], approver_id):
        return {"ok": False, "problems": ["培训老师不能审批自己登记的考核（职责分离）"]}
    conn.execute("UPDATE assessments SET status='approved', approved_by=? WHERE id=?",
                 (approver_id, assessment_id))
    conn.commit()
    return {"ok": True}


# ---------------- 授权状态维护（到期/撤销） ----------------

def sweep_authorizations(conn, today_iso: str | None = None) -> dict:
    """到期扫描：过期 active 授权标记 expire；产出通知。重复执行不重复通知。"""
    t = today_iso or rules.today_iso()
    soon = (date.fromisoformat(t) + timedelta(days=WARN_DAYS)).isoformat()
    expired_rows = conn.execute(
        "SELECT * FROM authorizations WHERE status='active' AND valid_until<?", (t,)
    ).fetchall()
    expiring_rows = conn.execute(
        """SELECT * FROM authorizations WHERE status='active'
           AND valid_until>=? AND valid_until<=?""", (t, soon)
    ).fetchall()
    notified = {r["ref_id"] for r in conn.execute(
        "SELECT DISTINCT ref_id FROM notifications WHERE type IN ('auth_expired','auth_expiring')"
    )}
    n_exp = n_warn = 0
    for a in expired_rows:
        conn.execute("UPDATE authorizations SET status='expired' WHERE id=?", (a["id"],))
        conn.execute(
            "INSERT INTO auth_events(auth_id,event_type,reason,actor_id,created_at) VALUES(?,?,?,?,?)",
            (a["id"], "expire", f"授权于 {a['valid_until']} 到期", None, rules.now_iso()),
        )
        if a["id"] not in notified:
            emp = conn.execute("SELECT name FROM employees WHERE id=?", (a["employee_id"],)).fetchone()
            notify(conn, "auth_expired", "技能授权已到期",
                   f"{emp['name']} 的授权 #{a['id']} 已于 {a['valid_until']} 到期，停止排岗",
                   "authorization", a["id"], commit=False)
            n_exp += 1
    for a in expiring_rows:
        if a["id"] in notified:
            continue
        days = (date.fromisoformat(a["valid_until"]) - date.fromisoformat(t)).days
        emp = conn.execute("SELECT name FROM employees WHERE id=?", (a["employee_id"],)).fetchone()
        notify(conn, "auth_expiring", "技能授权即将到期",
               f"{emp['name']} 的授权 #{a['id']} 将于 {a['valid_until']}（{days} 天后）到期，请安排复审",
               "authorization", a["id"], commit=False)
        n_warn += 1
    conn.commit()
    return {"expired": len(expired_rows), "expiring": n_warn, "warnings": n_warn}


def change_equipment_model(conn, equipment_id: int, new_model_id: int,
                           actor_id: int, reason: str):
    """设备型号变更：原型号专属授权不再覆盖新型号（全型号授权仍有效）。"""
    eq = conn.execute("SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone()
    if eq["model_id"] == new_model_id:
        return {"ok": False, "problems": ["新型号与当前型号相同"]}
    conn.execute("UPDATE equipment SET model_id=? WHERE id=?", (new_model_id, equipment_id))
    conn.execute(
        """INSERT INTO equipment_changes(equipment_id,old_model_id,new_model_id,reason,changed_by,changed_at)
           VALUES(?,?,?,?,?,?)""",
        (equipment_id, eq["model_id"], new_model_id, reason, actor_id, rules.now_iso()),
    )
    notify(conn, "model_change", "设备型号变更",
           f"设备 #{equipment_id} 型号变更，原型号授权不再自动适用，请重新培训考核。{reason}",
           "equipment", equipment_id, commit=False)
    conn.commit()
    return {"ok": True}


# ---------------- 进程内定时任务 ----------------

class ReminderWorker(threading.Thread):
    """守护线程：周期性跑到期扫描。演示/测试用，进程退出即止。"""

    def __init__(self, db_path: str, interval: float = 60.0, connect_fn=None):
        super().__init__(daemon=True, name="reminder-worker")
        self.db_path = db_path
        self.interval = interval
        self._stop = threading.Event()
        self._connect = connect_fn
        self.last_result: dict | None = None

    def run_once(self):
        from .db import connect
        conn = self._connect(self.db_path) if self._connect else connect(self.db_path)
        try:
            self.last_result = sweep_authorizations(conn)
        finally:
            conn.close()

    def run(self):
        self.run_once()
        while not self._stop.wait(self.interval):
            try:
                self.run_once()
            except Exception:  # 后台任务不应杀死进程
                pass

    def stop(self):
        self._stop.set()
