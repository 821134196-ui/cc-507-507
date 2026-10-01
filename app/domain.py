"""领域规则：授权判定、设备型号匹配、排班冲突、职责分离、替岗复核。"""
from datetime import date, datetime, timedelta

GRADE_ORDER = {"L1": 1, "L2": 2, "L3": 3}
EXPIRY_WARN_DAYS = 15


def today():
    return date.today().isoformat()


# ---------- 设备 / 员工 ----------

def get_equipment(conn, equipment_id):
    return conn.execute(
        """SELECT e.*, p.name process_name, p.risk_level
           FROM equipment e JOIN processes p ON p.id=e.process_id
           WHERE e.id=?""", (equipment_id,)).fetchone()


def get_employee(conn, employee_id):
    return conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()


# ---------- 授权判定 ----------

def _active_auths(conn, employee_id, process_id, on_date):
    return conn.execute(
        """SELECT a.* FROM authorizations a
           WHERE a.employee_id=? AND a.process_id=? AND a.status='active'
             AND a.valid_from<=? AND a.valid_to>=?
           ORDER BY a.model IS NULL, a.valid_to""",
        (employee_id, process_id, on_date, on_date)).fetchall()


def qualification(conn, employee_id, equipment_id, on_date=None):
    """返回员工对某设备在指定日期的资格结论。

    ok=True 时给出命中的授权；否则 reasons 列出全部不满足原因：
    no_auth / expired / revoked / model_mismatch / equipment_inactive
    """
    on_date = on_date or today()
    eq = get_equipment(conn, equipment_id)
    emp = get_employee(conn, employee_id)
    result = {"ok": False, "grade": None, "model": None, "auth_id": None,
              "reasons": [], "unlicensed": True}
    if eq is None:
        result["reasons"].append("设备不存在")
        return result
    if not eq["active"]:
        result["reasons"].append("设备已停用")
    if emp is None:
        result["reasons"].append("员工不存在")
        return result

    rows = conn.execute(
        """SELECT a.* FROM authorizations a
           WHERE a.employee_id=? AND a.process_id=? AND a.status='active'
           ORDER BY a.model IS NULL""",
        (employee_id, eq["process_id"])).fetchall()

    # 撤销/过期的授权也要给出可读原因
    all_rows = conn.execute(
        "SELECT * FROM authorizations WHERE employee_id=? AND process_id=?",
        (employee_id, eq["process_id"])).fetchall()
    has_revoked = any(r["status"] == "revoked" for r in all_rows)
    if not rows:
        if has_revoked:
            result["reasons"].append("授权已被撤销")
        elif all_rows:
            result["reasons"].append("授权已过期或未生效")
        else:
            result["reasons"].append("无该工序授权")
        return result

    # 型号匹配：精确型号优先，其次 model IS NULL 的全型号通用授权；
    # 同时校验有效期，并区分“过期”与“型号不匹配”两种原因。
    exact = generic = None
    expired, other_models = [], []
    for r in rows:
        in_window = r["valid_from"] <= on_date <= r["valid_to"]
        if r["model"] is None:
            if in_window and generic is None:
                generic = r
            elif not in_window:
                expired.append(r)
        elif r["model"] == eq["model"]:
            if in_window and exact is None:
                exact = r
            else:
                expired.append(r)
        else:
            other_models.append(r)

    hit = exact or generic
    if hit is None:
        if expired:
            latest = max(r["valid_to"] for r in expired)
            result["reasons"].append("授权已过期（%s 到期）" % latest)
        if other_models:
            models = sorted({r["model"] for r in other_models})
            result["reasons"].append(
                "设备型号变更：授权型号 %s 不匹配当前型号 %s" % ("/".join(models), eq["model"]))
        if not expired and not other_models:
            result["reasons"].append("授权不在有效期内")
        return result

    result.update(ok=True, grade=hit["grade"], model=hit["model"] or "全型号",
                  auth_id=hit["id"], unlicensed=False)
    return result


def model_change_affected(conn, equipment_id, new_model):
    """设备换型后，原可上岗、新型号下将失去资格的员工（仅精确型号授权、无通用授权）。"""
    eq = get_equipment(conn, equipment_id)
    out = []
    for emp in conn.execute("SELECT id,name FROM employees WHERE archived=0 ORDER BY id"):
        before = qualification(conn, emp["id"], equipment_id)
        auths = _active_auths(conn, emp["id"], eq["process_id"], today())
        generic = any(a["model"] is None for a in auths)
        exact_new = any(a["model"] == new_model for a in auths)
        if before["ok"] and not generic and not exact_new:
            out.append({"employee_id": emp["id"], "name": emp["name"]})
    return out


# ---------- 排班冲突 ----------

def find_conflict(conn, shift_id, employee_id, ignore_assignment_id=None):
    """同一员工同一班次（同一时段）只能占用一台设备。返回冲突的派工。"""
    q = """SELECT a.*, e.code equipment_code, e.model, p.name process_name,
                  cr.name created_by_name
           FROM assignments a
           JOIN equipment e ON e.id=a.equipment_id
           JOIN processes p ON p.id=e.process_id
           JOIN employees cr ON cr.id=a.created_by
           WHERE a.shift_id=? AND a.employee_id=? AND a.id IS NOT ?"""
    row = conn.execute(q, (shift_id, employee_id, ignore_assignment_id)).fetchone()
    return row


def shift_assignments(conn, shift_id):
    return conn.execute(
        """SELECT a.*, e.code equipment_code, e.model, e.active equip_active,
                  p.name process_name, p.risk_level,
                  emp.name employee_name
           FROM assignments a
           JOIN equipment e ON e.id=a.equipment_id
           JOIN processes p ON p.id=e.process_id
           LEFT JOIN employees emp ON emp.id=a.employee_id
           WHERE a.shift_id=? ORDER BY e.code""", (shift_id,)).fetchall()


def shift_view(conn, shift_id, on_date=None):
    """一个班次的完整视图：每台设备的派工状态 + 可替人员（与缺口同区展示）。"""
    on_date = on_date or today()
    sh = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
    if sh is None:
        return None
    occupied = {a["employee_id"] for a in shift_assignments(conn, shift_id) if a["employee_id"]}
    items = []
    for a in shift_assignments(conn, shift_id):
        if a["employee_id"]:
            q = qualification(conn, a["employee_id"], a["equipment_id"], on_date)
            status = "ok" if q["ok"] else "unlicensed"
        else:
            q, status = None, "vacant"
        # 可替人员：对该设备型号合格且本班次未被占用
        candidates = []
        for emp in conn.execute("SELECT id,name FROM employees WHERE archived=0 ORDER BY id"):
            qual = qualification(conn, emp["id"], a["equipment_id"], on_date)
            if not qual["ok"]:
                continue
            conflict = find_conflict(conn, shift_id, emp["id"]) if emp["id"] in occupied else None
            candidates.append({
                "employee_id": emp["id"], "name": emp["name"],
                "grade": qual["grade"], "model_scope": qual["model"],
                "busy": emp["id"] in occupied,
                "conflict_equipment": conflict["equipment_code"] if conflict else None,
            })
        candidates.sort(key=lambda c: (c["busy"], -GRADE_ORDER.get(c["grade"], 0)))
        items.append({
            "assignment_id": a["id"], "equipment_id": a["equipment_id"],
            "equipment_code": a["equipment_code"], "model": a["model"],
            "process_name": a["process_name"], "risk_level": a["risk_level"],
            "employee_id": a["employee_id"], "employee_name": a["employee_name"],
            "status": status,
            "reasons": q["reasons"] if q else [],
            "auth_grade": q["grade"] if q else None,
            "candidates": candidates,
        })
    return {"shift": dict(sh), "items": items}


def assign_employee(conn, shift_id, equipment_id, employee_id, foreman_id,
                    allow_unlicensed=False, on_date=None, from_assignment_id=None):
    """派工。返回 (assignment_id, warnings)；资格不符且未显式放行时抛 RuleError。

    from_assignment_id：从该员工已占用的另一台设备槽位拖入（移动），
    原槽位自动清空；不传而员工已在他岗，则判为两名班长争用冲突。
    """
    on_date = on_date or today()
    eq = get_equipment(conn, equipment_id)
    sh = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
    if eq is None or sh is None:
        raise RuleError("班次或设备不存在")
    foreman = get_employee(conn, foreman_id)
    if foreman is None or foreman["role"] != "foreman":
        raise RuleError("只有班长可以派工")

    # 移动语义：只有当 from_assignment 确为该员工在本班次的现有派工时才忽略其占用
    move_source = None
    if from_assignment_id:
        move_source = conn.execute(
            "SELECT * FROM assignments WHERE id=? AND shift_id=? AND employee_id=?",
            (from_assignment_id, shift_id, employee_id)).fetchone()
        if move_source is None:
            from_assignment_id = None

    conflict = find_conflict(conn, shift_id, employee_id,
                             ignore_assignment_id=from_assignment_id)
    if conflict:
        raise ConflictError(
            "员工 %s 在该班次已被 %s 派到 %s（%s 提交于 %s）" % (
                _emp_name(conn, employee_id), conflict["created_by_name"],
                conflict["equipment_code"], conflict["created_by_name"],
                conflict["created_at"]),
            conflict=dict(conflict))

    # 目标槽位若已被他人占用，不允许无声顶替
    occupant = conn.execute(
        """SELECT a.employee_id, e.name FROM assignments a
           JOIN employees e ON e.id=a.employee_id
           WHERE a.shift_id=? AND a.equipment_id=? AND a.employee_id IS NOT NULL
             AND a.id IS NOT ?""",
        (shift_id, equipment_id, from_assignment_id)).fetchone()
    if occupant and occupant["employee_id"] != employee_id:
        raise RuleError("目标设备已由 %s 占用，请先清空该槽位再派工" % occupant["name"],
                        code="occupied")

    qual = qualification(conn, employee_id, equipment_id, on_date)
    warnings = []
    if not qual["ok"]:
        if not allow_unlicensed:
            raise RuleError("人员不合格：" + "；".join(qual["reasons"]), code="unlicensed",
                            qual=qual)
        warnings.append("无证派工：" + "；".join(qual["reasons"]))

    now = datetime.now().isoformat(timespec="seconds")
    if move_source is not None and move_source["equipment_id"] != equipment_id:
        conn.execute("UPDATE assignments SET employee_id=NULL WHERE id=?",
                     (move_source["id"],))
    conn.execute(
        """INSERT INTO assignments(shift_id,equipment_id,employee_id,created_by,created_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(shift_id,equipment_id)
           DO UPDATE SET employee_id=excluded.employee_id,
                         created_by=excluded.created_by, created_at=excluded.created_at""",
        (shift_id, equipment_id, employee_id, foreman_id, now))
    row = conn.execute(
        "SELECT id FROM assignments WHERE shift_id=? AND equipment_id=?",
        (shift_id, equipment_id)).fetchone()
    return row["id"], warnings


def _emp_name(conn, emp_id):
    r = get_employee(conn, emp_id)
    return r["name"] if r else "#%s" % emp_id


# ---------- 替岗申请 ----------

class RuleError(Exception):
    def __init__(self, msg, code="rule", qual=None):
        super().__init__(msg)
        self.code = code
        self.qual = qual


class ConflictError(RuleError):
    def __init__(self, msg, conflict):
        super().__init__(msg, code="conflict")
        self.conflict = conflict


def apply_substitution(conn, shift_id, equipment_id, applicant_emp, applied_by,
                       vacancy_reason, on_date=None):
    """班长发起替岗。无证替岗 authorization_basis='unlicensed'，必须经安全复核。"""
    on_date = on_date or today()
    foreman = get_employee(conn, applied_by)
    if foreman is None or foreman["role"] != "foreman":
        raise RuleError("只有班长可以申请替岗")
    if not vacancy_reason.strip():
        raise RuleError("必须说明原岗位空缺原因")
    eq = get_equipment(conn, equipment_id)
    if eq is None:
        raise RuleError("设备不存在")

    conflict = find_conflict(conn, shift_id, applicant_emp)
    if conflict:
        raise ConflictError(
            "%s 已被 %s 安排在 %s，同一时段不能再替岗" % (
                _emp_name(conn, applicant_emp), conflict["created_by_name"],
                conflict["equipment_code"]), conflict=dict(conflict))

    qual = qualification(conn, applicant_emp, equipment_id, on_date)
    basis = str(qual["auth_id"]) if qual["ok"] else "unlicensed"
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        """INSERT INTO substitutions
           (shift_id,equipment_id,vacancy_reason,applicant_emp,applied_by,
            authorization_basis,status,created_at)
           VALUES (?,?,?,?,?,?, 'pending', ?)""",
        (shift_id, equipment_id, vacancy_reason.strip(), applicant_emp, applied_by, basis, now))
    return cur.lastrowid, qual


def review_substitution(conn, sub_id, reviewer_id, approve, note=""):
    """复核替岗。高风险设备必须安全负责人；复核人不能是申请人本人。"""
    sub = conn.execute("SELECT * FROM substitutions WHERE id=?", (sub_id,)).fetchone()
    if sub is None:
        raise RuleError("替岗申请不存在")
    if sub["status"] != "pending":
        raise RuleError("该申请已处理")
    reviewer = get_employee(conn, reviewer_id)
    if reviewer is None:
        raise RuleError("复核人不存在")
    if reviewer_id == sub["applied_by"]:
        raise RuleError("职责分离：替岗申请人不能复核自己的申请")
    eq = get_equipment(conn, sub["equipment_id"])
    unlicensed = sub["authorization_basis"] == "unlicensed"
    if eq["risk_level"] == "high" and reviewer["role"] != "safety":
        raise RuleError("%s 为高风险设备，替岗必须由安全负责人复核" % eq["process_name"])
    if unlicensed and reviewer["role"] != "safety":
        raise RuleError("无证替岗必须由安全负责人复核")

    now = datetime.now().isoformat(timespec="seconds")
    status = "approved" if approve else "rejected"
    conn.execute(
        "UPDATE substitutions SET status=?, reviewed_by=?, review_note=?, reviewed_at=? WHERE id=?",
        (status, reviewer_id, note, now, sub_id))

    warnings = []
    if approve:
        # 批准后落到排班：无证时显式放行并留痕
        aid, warnings = assign_employee(
            conn, sub["shift_id"], sub["equipment_id"], sub["applicant_emp"],
            sub["applied_by"], allow_unlicensed=unlicensed)
    return status, warnings


# ---------- 考核与职责分离 ----------

def register_assessment(conn, employee_id, process_id, model, grade, score, pass_,
                        exam_date, recorder_id, evidence_path=None, evidence_name=None):
    rec = get_employee(conn, recorder_id)
    if rec is None or rec["role"] not in ("trainer", "safety"):
        raise RuleError("只有培训老师可以登记考核")
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        """INSERT INTO assessments
           (employee_id,process_id,model,grade,score,pass,exam_date,recorded_by,
            evidence_path,evidence_name,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (employee_id, process_id, model, grade, score, int(pass_), exam_date,
         recorder_id, evidence_path, evidence_name, now))
    return cur.lastrowid


def approve_assessment(conn, assessment_id, approver_id, valid_days=365):
    """考核审批：通过且审批合法时签发授权。培训老师不能审批自己登记的考核。"""
    a = conn.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if a is None:
        raise RuleError("考核记录不存在")
    if a["approved_by"]:
        raise RuleError("该考核已审批")
    approver = get_employee(conn, approver_id)
    if approver is None:
        raise RuleError("审批人不存在")
    if approver_id == a["recorded_by"]:
        raise RuleError("职责分离：培训老师不能审批自己登记的考核")

    now = datetime.now().isoformat(timespec="seconds")
    auth_id = None
    if a["pass"]:
        exam = date.fromisoformat(a["exam_date"])
        valid_to = exam + timedelta(days=valid_days)
        cur = conn.execute(
            """INSERT INTO authorizations(employee_id,process_id,model,grade,
                   valid_from,valid_to,source_assessment,status)
               VALUES (?,?,?,?,?,?,?, 'active')
               ON CONFLICT(employee_id,process_id,model) DO UPDATE SET
                 grade=excluded.grade, valid_from=excluded.valid_from,
                 valid_to=excluded.valid_to, source_assessment=excluded.source_assessment,
                 status='active', revoked_reason=''""",
            (a["employee_id"], a["process_id"], a["model"], a["grade"],
             a["exam_date"], valid_to.isoformat(), assessment_id))
        auth_id = cur.lastrowid
    conn.execute("UPDATE assessments SET approved_by=?, approved_at=? WHERE id=?",
                 (approver_id, now, assessment_id))
    return auth_id


def resolve_appeal(conn, assessment_id, reviewer_id, overturn, note=""):
    """申诉处理：支持/维持。翻案=改判通过并签发授权；处理人不能是原登记人。"""
    a = conn.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if a is None:
        raise RuleError("考核记录不存在")
    if a["appeal_status"] != "pending":
        raise RuleError("没有待处理的申诉")
    reviewer = get_employee(conn, reviewer_id)
    if reviewer is None or reviewer["role"] not in ("safety", "trainer"):
        raise RuleError("申诉须由安全负责人或培训老师处理")
    if reviewer_id == a["recorded_by"]:
        raise RuleError("职责分离：原登记人不能处理对该考核的申诉")
    new_status = "approved" if overturn else "rejected"
    now = datetime.now().isoformat(timespec="seconds")
    auth_id = None
    if overturn:
        conn.execute(
            "UPDATE assessments SET pass=1, appeal_status='approved', appeal_note=appeal_note||? WHERE id=?",
            (" | 复核结论：" + note, assessment_id))
        if not a["approved_by"]:
            exam = date.fromisoformat(a["exam_date"])
            valid_to = exam + timedelta(days=365)
            cur = conn.execute(
                """INSERT INTO authorizations(employee_id,process_id,model,grade,
                       valid_from,valid_to,source_assessment)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(employee_id,process_id,model) DO UPDATE SET
                     grade=excluded.grade, valid_to=excluded.valid_to, status='active',
                     revoked_reason=''""",
                (a["employee_id"], a["process_id"], a["model"], a["grade"],
                 a["exam_date"], valid_to.isoformat(), assessment_id))
            auth_id = cur.lastrowid
    else:
        conn.execute(
            "UPDATE assessments SET appeal_status='rejected', appeal_note=appeal_note||? WHERE id=?",
            (" | 复核结论：" + note, assessment_id))
    return new_status, auth_id


# ---------- 撤销 / 换型 / 到期扫描 ----------

def revoke_authorization(conn, auth_id, reason, operator_id):
    r = conn.execute("SELECT * FROM authorizations WHERE id=?", (auth_id,)).fetchone()
    if r is None:
        raise RuleError("授权不存在")
    conn.execute("UPDATE authorizations SET status='revoked', revoked_reason=? WHERE id=?",
                 (reason, auth_id))
    return r


def change_equipment_model(conn, equipment_id, new_model, operator_id):
    eq = get_equipment(conn, equipment_id)
    if eq is None:
        raise RuleError("设备不存在")
    affected = model_change_affected(conn, equipment_id, new_model)
    conn.execute("UPDATE equipment SET model=? WHERE id=?", (new_model, equipment_id))
    return eq, affected


def expiring_authorizations(conn, within_days=EXPIRY_WARN_DAYS, on_date=None):
    on = date.fromisoformat(on_date or today())
    horizon = (on + timedelta(days=within_days)).isoformat()
    return conn.execute(
        """SELECT a.*, e.name employee_name, p.name process_name
           FROM authorizations a
           JOIN employees e ON e.id=a.employee_id
           JOIN processes p ON p.id=a.process_id
           WHERE a.status='active' AND a.valid_to>=? AND a.valid_to<=?
           ORDER BY a.valid_to""",
        (on.isoformat(), horizon)).fetchall()


def matrix(conn, on_date=None):
    """技能矩阵：工序行 × 员工列，单元格给等级、型号范围、到期日与状态。"""
    on_date = on_date or today()
    procs = conn.execute("SELECT * FROM processes ORDER BY id").fetchall()
    emps = conn.execute("SELECT * FROM employees WHERE archived=0 ORDER BY id").fetchall()
    equips = conn.execute(
        """SELECT e.*, p.risk_level FROM equipment e
           JOIN processes p ON p.id=e.process_id WHERE e.active=1 ORDER BY e.id""").fetchall()
    grid = []
    for p in procs:
        row = {"process": dict(p), "cells": []}
        models = sorted({e["model"] for e in equips if e["process_id"] == p["id"]})
        for emp in emps:
            auths = conn.execute(
                """SELECT * FROM authorizations WHERE employee_id=? AND process_id=?
                   ORDER BY model IS NULL, valid_to""",
                (emp["id"], p["id"])).fetchall()
            cell = {"grade": None, "model_scope": None, "valid_to": None,
                    "status": "none", "note": ""}
            active = [a for a in auths if a["status"] == "active"
                      and a["valid_from"] <= on_date <= a["valid_to"]]
            if active:
                # 对该工序现有设备型号逐台校验，矩阵显示“可上岗设备”
                covered = set()
                for a in active:
                    for e in equips:
                        if e["process_id"] == p["id"] and (a["model"] is None or a["model"] == e["model"]):
                            covered.add(e["model"])
                soon = min(a["valid_to"] for a in active)
                best = max(active, key=lambda a: GRADE_ORDER.get(a["grade"], 0))
                cell.update(grade=best["grade"], valid_to=soon,
                            model_scope="全型号" if any(a["model"] is None for a in active)
                            else "/".join(sorted(covered)))
                days = (date.fromisoformat(soon) - date.fromisoformat(on_date)).days
                cell["status"] = "expiring" if days <= EXPIRY_WARN_DAYS else "ok"
            elif auths:
                a = auths[0]
                if a["status"] == "revoked":
                    cell["status"], cell["note"] = "revoked", "已撤销：" + (a["revoked_reason"] or "")
                elif a["valid_to"] < on_date:
                    cell["status"], cell["valid_to"] = "expired", a["valid_to"]
                else:
                    cell["status"], cell["valid_to"] = "future", a["valid_to"]
            # 型号盲区：有在期授权但覆盖不到全部在役型号
            if cell["status"] in ("ok", "expiring"):
                uncovered = [m for m in models if m not in (cell["model_scope"] and
                             (models if cell["model_scope"] == "全型号" else cell["model_scope"].split("/")))]
                if uncovered:
                    cell["note"] = "不含型号 " + "/".join(uncovered)
            row["cells"].append(cell)
        grid.append(row)
    return {"processes": [dict(p) for p in procs],
            "employees": [dict(e) for e in emps], "grid": grid,
            "on_date": on_date}
