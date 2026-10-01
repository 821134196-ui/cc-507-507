"""核心规则：授权资格、排班冲突、职责分离、替岗审批。

所有判定为纯函数式查询，不修改数据；写操作集中在 services 层。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Optional


# ---------------- 基础工具 ----------------

def today_iso() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _row(conn: sqlite3.Connection, sql: str, params=()):
    return conn.execute(sql, params).fetchone()


def _rows(conn: sqlite3.Connection, sql: str, params=()):
    return conn.execute(sql, params).fetchall()


# ---------------- 授权资格 ----------------

def list_authorizations(conn, employee_id: int, on_date: Optional[str] = None):
    on_date = on_date or today_iso()
    return _rows(
        conn,
        """SELECT a.*, p.code process_code, p.name process_name,
                  m.code model_code, m.name model_name
             FROM authorizations a
             JOIN processes p ON p.id=a.process_id
             LEFT JOIN equipment_models m ON m.id=a.model_id
            WHERE a.employee_id=? ORDER BY p.seq""",
        (employee_id,),
    )


def _active_auth_for(conn, employee_id: int, process_id: int, model_id: int,
                     on_date: str, level_required: int):
    """命中的授权（全型号 NULL 或精确型号），要求日期有效、等级够、状态正常。"""
    return _row(
        conn,
        """SELECT a.* FROM authorizations a
           WHERE a.employee_id=? AND a.process_id=?
             AND (a.model_id IS NULL OR a.model_id=?)
             AND a.status='active'
             AND a.valid_from<=? AND a.valid_until>=?
             AND a.level>=?
           ORDER BY a.model_id DESC LIMIT 1""",
        (employee_id, process_id, model_id, on_date, on_date, level_required),
    )


def evaluate(conn, employee_id: int, equipment_id: int, on_date: Optional[str] = None) -> dict:
    """判定员工是否能上某设备，返回 {eligible, reasons[], auth}。"""
    on_date = on_date or today_iso()
    eq = _row(conn, "SELECT * FROM equipment WHERE id=?", (equipment_id,))
    if eq is None:
        return {"eligible": False, "reasons": ["设备不存在"], "auth": None}
    emp = _row(conn, "SELECT * FROM employees WHERE id=?", (employee_id,))
    if emp is None or not emp["active"]:
        return {"eligible": False, "reasons": ["人员不存在或已停用"], "auth": None}

    reasons: list[str] = []
    # 候选授权：同工序、型号匹配（全型号或精确），不论状态，用于给出具体原因
    cands = _rows(
        conn,
        """SELECT a.*, m.code model_code FROM authorizations a
             LEFT JOIN equipment_models m ON m.id=a.model_id
            WHERE a.employee_id=? AND a.process_id=?
              AND (a.model_id IS NULL OR a.model_id=?)
            ORDER BY a.model_id DESC""",
        (employee_id, eq["process_id"], eq["model_id"]),
    )
    target_model = _row(conn, "SELECT code FROM equipment_models WHERE id=?", (eq["model_id"],))["code"]
    if not cands:
        reasons.append(f"无{target_model}型号及全型号授权（设备型号变更后需重新培训授权）")
    else:
        ok_model = False
        for c in cands:
            if c["status"] != "active":
                label = {"suspended": "已暂停", "revoked": "已撤销"}.get(c["status"], c["status"])
                reasons.append(f"授权{label}" + (f"（{c['note']}）" if c["note"] else ""))
            elif on_date < c["valid_from"]:
                reasons.append(f"授权未生效（{c['valid_from']} 起）")
            elif on_date > c["valid_until"]:
                reasons.append(f"授权已到期（{c['valid_until']} 止）")
            elif c["level"] < eq["required_level"]:
                reasons.append(f"技能等级不足（{c['level']} 级 < 要求 {eq['required_level']} 级）")
            else:
                ok_model = True
        if not ok_model and not reasons:
            reasons.append("型号不匹配")

    # 未决失败考核：同工序近一次考核 fail 且未被申诉成功推翻，也作为强提醒
    last = _row(
        conn,
        """SELECT * FROM assessments WHERE employee_id=? AND process_id=?
           ORDER BY exam_date DESC, id DESC LIMIT 1""",
        (employee_id, eq["process_id"]),
    )
    if last and last["result"] == "fail" and last["appeal_status"] != "upheld":
        tip = "最近一次考核未通过"
        if last["appeal_status"] == "pending":
            tip += "（申诉处理中，暂不恢复资格）"
        reasons.append(tip)

    auth = _active_auth_for(conn, employee_id, eq["process_id"], eq["model_id"],
                            on_date, eq["required_level"])
    eligible = auth is not None and not any(r.startswith("最近一次考核未通过") for r in reasons)
    return {"eligible": eligible, "reasons": reasons, "auth": dict(auth) if auth else None}


def eligible_candidates(conn, equipment_id: int, shift_id: int, on_date: Optional[str] = None):
    """可替人员名单：合格且该班次未被占用；另返回不合格但可申请无证替岗的人员及原因。"""
    on_date = on_date or today_iso()
    emps = _rows(conn, "SELECT * FROM employees WHERE active=1 ORDER BY is_foreman DESC, id")
    busy = {r["employee_id"] for r in _rows(
        conn,
        """SELECT employee_id FROM shift_assignments WHERE shift_id=?
           UNION ALL
           SELECT employee_id FROM temp_requests WHERE shift_id=? AND status='pending'""",
        (shift_id, shift_id),
    )}
    qualified, others = [], []
    for e in emps:
        ev = evaluate(conn, e["id"], equipment_id, on_date)
        item = {"employee_id": e["id"], "name": e["name"], "badge": e["badge"],
                "reasons": ev["reasons"], "busy": e["id"] in busy}
        if ev["eligible"] and not item["busy"]:
            item["auth"] = ev["auth"]
            qualified.append(item)
        else:
            item["busy"] = e["id"] in busy
            others.append(item)
    return qualified, others


# ---------------- 排班冲突 ----------------

def assignment_conflict(conn, employee_id: int, shift_id: int,
                        exclude_assignment_id: Optional[int] = None):
    """同一员工同一班次（同一时段）只能出现在一个工位。"""
    sql = """SELECT sa.id, sa.slot_id, e.code eq_code, e.name eq_name
               FROM shift_assignments sa
               JOIN shift_slots sl ON sl.id=sa.slot_id
               JOIN equipment e ON e.id=sl.equipment_id
              WHERE sa.employee_id=? AND sa.shift_id=? """
    params = [employee_id, shift_id]
    if exclude_assignment_id:
        sql += "AND sa.id<>? "
        params.append(exclude_assignment_id)
    return _row(conn, sql, params)


def pending_request_conflict(conn, employee_id: int, shift_id: int):
    return _row(
        conn,
        """SELECT tr.id, e.code eq_code, e.name eq_name
             FROM temp_requests tr
             JOIN shift_slots sl ON sl.id=tr.slot_id
             JOIN equipment e ON e.id=sl.equipment_id
            WHERE tr.employee_id=? AND tr.shift_id=? AND tr.status='pending'""",
        (employee_id, shift_id),
    )


def check_assign(conn, employee_id: int, equipment_id: int, shift_id: int,
                 on_date: Optional[str] = None):
    """排班/拖拽前统一校验：资格 + 冲突。返回 (ok, problems[])。"""
    problems: list[str] = []
    ev = evaluate(conn, employee_id, equipment_id, on_date)
    if not ev["eligible"]:
        problems.append("无有效授权：" + "；".join(ev["reasons"]) if ev["reasons"] else "无有效授权")
    c = assignment_conflict(conn, employee_id, shift_id)
    if c:
        problems.append(f"时段冲突：该员工本班次已在 {c['eq_code']} {c['eq_name']}")
    c2 = pending_request_conflict(conn, employee_id, shift_id)
    if c2:
        problems.append(f"替岗待批：该员工已申请顶岗 {c2['eq_code']} {c2['eq_name']}")
    return (len(problems) == 0, problems, ev)


# ---------------- 职责分离 ----------------

def can_request_substitute(employee: sqlite3.Row) -> bool:
    """只有班长可以申请替岗。"""
    return bool(employee["is_foreman"])


def required_reviewer(conn, equipment_id: int) -> str:
    """高风险设备替岗 → safety；普通设备 → 非申请人的班长即可。"""
    eq = _row(conn, "SELECT * FROM equipment WHERE id=?", (equipment_id,))
    return "safety" if eq and eq["high_risk"] else "foreman"


def validate_review(conn, request_id: int, reviewer_id: int) -> list[str]:
    """审批/复核人校验。返回问题列表（空=通过）。"""
    problems: list[str] = []
    tr = _row(conn, "SELECT * FROM temp_requests WHERE id=?", (request_id,))
    rv = _row(conn, "SELECT * FROM employees WHERE id=?", (reviewer_id,))
    if tr is None:
        return ["替岗申请不存在"]
    if rv is None:
        return ["审批人不存在"]
    sl = _row(conn, "SELECT * FROM shift_slots WHERE id=?", (tr["slot_id"],))
    if sl is None:
        return ["排班工位不存在"]
    need = required_reviewer(conn, sl["equipment_id"])
    if need == "safety":
        if not rv["is_safety"]:
            problems.append("高风险设备替岗必须由安全负责人复核")
    else:
        if not rv["is_foreman"]:
            problems.append("普通设备替岗须由班长复核")
    if reviewer_id == tr["requested_by"]:
        problems.append("申请人不能复核自己发起的替岗申请")
    return problems


def assessment_approval_allowed(recorder_id: int, approver_id: int) -> bool:
    """培训老师不能审批自己登记的考核。"""
    return recorder_id != approver_id
