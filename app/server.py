"""零依赖 HTTP 服务：静态页面 + JSON API + 本地考核附件。"""
import cgi
import json
import os
import re
import shutil
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import db, domain, notify

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _rows(rows):
    return [dict(r) for r in rows]


class Handler(BaseHTTPRequestHandler):
    server_version = "SkillShift/1.0"

    # ---------- 基础工具 ----------
    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ApiError("请求体不是合法 JSON", 400)

    def _parse_multipart(self):
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            raise ApiError("需要 multipart/form-data", 400)
        fs = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                              environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype},
                              keep_blank_values=True)
        fields, files = {}, {}
        for key in fs.keys():
            item = fs[key]
            if getattr(item, "filename", None):
                files[key] = item
            else:
                fields[key] = item.value
        return fields, files

    def log_message(self, fmt, *args):
        print("[http] " + (fmt % args))

    # ---------- 路由 ----------
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/state":
                return self._state()
            if path == "/api/expiring":
                with db.get_conn() as conn:
                    return self._send_json({"items": _rows(domain.expiring_authorizations(conn))})
            if path == "/api/notifications":
                with db.get_conn() as conn:
                    return self._send_json({"items": _rows(notify.list_notifications(conn))})
            m = re.fullmatch(r"/api/shifts/(\d+)/view", path)
            if m:
                with db.get_conn() as conn:
                    view = domain.shift_view(conn, int(m.group(1)))
                    if view is None:
                        return self._send_json({"error": "班次不存在"}, 404)
                    return self._send_json(view)
            m = re.fullmatch(r"/api/substitutions", path)
            if m:
                with db.get_conn() as conn:
                    rows = conn.execute(
                        """SELECT s.*, sh.shift_date, sh.shift_type, e.code equipment_code,
                                  e.model, p.name process_name, p.risk_level,
                                  a.name applicant_name, f.name applied_by_name,
                                  r.name reviewed_by_name
                           FROM substitutions s
                           JOIN shifts sh ON sh.id=s.shift_id
                           JOIN equipment e ON e.id=s.equipment_id
                           JOIN processes p ON p.id=e.process_id
                           JOIN employees a ON a.id=s.applicant_emp
                           JOIN employees f ON f.id=s.applied_by
                           LEFT JOIN employees r ON r.id=s.reviewed_by
                           ORDER BY s.id DESC""").fetchall()
                    return self._send_json({"items": _rows(rows)})
            m = re.fullmatch(r"/api/assessments", path)
            if m:
                with db.get_conn() as conn:
                    rows = conn.execute(
                        """SELECT a.*, e.name employee_name, p.name process_name,
                                  rec.name recorded_by_name, apr.name approved_by_name
                           FROM assessments a
                           JOIN employees e ON e.id=a.employee_id
                           JOIN processes p ON p.id=a.process_id
                           JOIN employees rec ON rec.id=a.recorded_by
                           LEFT JOIN employees apr ON apr.id=a.approved_by
                           ORDER BY a.id DESC""").fetchall()
                    return self._send_json({"items": _rows(rows)})
            m = re.fullmatch(r"/attachments/([\w.\-]+)", path)
            if m:
                return self._attachment(m.group(1))
            self._send_json({"error": "未找到"}, 404)
        except ApiError as e:
            self._send_json({"error": e.message}, e.status)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            with db._lock, db.get_conn() as conn:
                # ---- 排班 ----
                if path == "/api/assignments":
                    data = self._read_json()
                    aid, warnings = domain.assign_employee(
                        conn, int(data["shift_id"]), int(data["equipment_id"]),
                        int(data["employee_id"]), int(data["foreman_id"]),
                        allow_unlicensed=bool(data.get("allow_unlicensed")),
                        from_assignment_id=(int(data["from_assignment_id"])
                                            if data.get("from_assignment_id") else None))
                    if warnings:
                        notify.enqueue(
                            conn, "unlicensed", "无证派工已记录",
                            "班次 %s 设备 %s：%s。已要求走替岗复核流程。"
                            % (data["shift_id"], data["equipment_id"], "；".join(warnings)),
                            "assignment", aid)
                    conn.commit()
                    return self._send_json({"ok": True, "assignment_id": aid, "warnings": warnings})

                if path == "/api/assignments/vacate":
                    data = self._read_json()
                    conn.execute("UPDATE assignments SET employee_id=NULL WHERE id=?",
                                 (int(data["assignment_id"]),))
                    conn.commit()
                    return self._send_json({"ok": True})

                # ---- 替岗 ----
                if path == "/api/substitutions":
                    data = self._read_json()
                    sub_id, qual = domain.apply_substitution(
                        conn, int(data["shift_id"]), int(data["equipment_id"]),
                        int(data["applicant_emp"]), int(data["applied_by"]),
                        data.get("vacancy_reason", ""))
                    eq = domain.get_equipment(conn, int(data["equipment_id"]))
                    emp = domain.get_employee(conn, int(data["applicant_emp"]))
                    if not qual["ok"]:
                        notify.enqueue(
                            conn, "unlicensed", "无证替岗申请待安全复核",
                            "%s %s：%s 无有效授权（%s），班长已提交替岗申请。"
                            % (eq["code"], eq["process_name"], emp["name"],
                               "；".join(qual["reasons"])),
                            "substitution", sub_id)
                    elif eq["risk_level"] == "high":
                        notify.enqueue(
                            conn, "review", "高风险设备替岗待复核",
                            "%s %s：%s 替岗申请需安全负责人复核。"
                            % (eq["code"], eq["process_name"], emp["name"]),
                            "substitution", sub_id)
                    conn.commit()
                    return self._send_json({"ok": True, "substitution_id": sub_id,
                                            "unlicensed": not qual["ok"]})

                m = re.fullmatch(r"/api/substitutions/(\d+)/review", path)
                if m:
                    data = self._read_json()
                    status, warnings = domain.review_substitution(
                        conn, int(m.group(1)), int(data["reviewer_id"]),
                        bool(data.get("approve")), data.get("note", ""))
                    sub = conn.execute("SELECT * FROM substitutions WHERE id=?",
                                       (int(m.group(1)),)).fetchone()
                    notify.enqueue(
                        conn, "review", "替岗复核结果：" + status,
                        "申请 #%s 复核%s%s" % (m.group(1),
                        "通过" if data.get("approve") else "驳回",
                        "，含无证派工" if sub["authorization_basis"] == "unlicensed" and data.get("approve") else ""),
                        "substitution", sub["id"])
                    conn.commit()
                    return self._send_json({"ok": True, "status": status, "warnings": warnings})

                # ---- 考核（含附件上传） ----
                if path == "/api/assessments":
                    fields, files = self._parse_multipart()
                    evidence_path = evidence_name = None
                    if "evidence" in files and files["evidence"].filename:
                        item = files["evidence"]
                        ext = os.path.splitext(item.filename)[1][:10]
                        fname = uuid.uuid4().hex + ("." + re.sub(r"[^\w]", "", ext) if ext else "")
                        evidence_path = os.path.join(db.ATTACH_DIR, fname)
                        with open(evidence_path, "wb") as f:
                            shutil.copyfileobj(item.file, f)
                        evidence_name = os.path.basename(item.filename)
                    aid = domain.register_assessment(
                        conn, int(fields["employee_id"]), int(fields["process_id"]),
                        fields.get("model") or None, fields["grade"],
                        int(fields["score"]), bool(int(fields.get("pass", "0"))),
                        fields["exam_date"], int(fields["recorder_id"]),
                        evidence_path, evidence_name)
                    conn.commit()
                    return self._send_json({"ok": True, "assessment_id": aid,
                                            "evidence_name": evidence_name})

                m = re.fullmatch(r"/api/assessments/(\d+)/approve", path)
                if m:
                    data = self._read_json()
                    auth_id = domain.approve_assessment(
                        conn, int(m.group(1)), int(data["approver_id"]))
                    conn.commit()
                    return self._send_json({"ok": True, "authorization_id": auth_id})

                m = re.fullmatch(r"/api/assessments/(\d+)/appeal", path)
                if m:
                    data = self._read_json()
                    a = conn.execute("SELECT * FROM assessments WHERE id=?",
                                     (int(m.group(1)),)).fetchone()
                    if a is None:
                        raise ApiError("考核不存在", 404)
                    if int(data["employee_id"]) != a["employee_id"]:
                        raise ApiError("仅被考核人本人可发起申诉", 403)
                    conn.execute(
                        "UPDATE assessments SET appeal_status='pending', appeal_note=? WHERE id=?",
                        (data.get("note", ""), int(m.group(1))))
                    notify.enqueue(conn, "appeal", "培训成绩申诉待处理",
                                   "考核 #%s 的成绩被申诉：%s" % (m.group(1), data.get("note", "")),
                                   "assessment", int(m.group(1)))
                    conn.commit()
                    return self._send_json({"ok": True})

                m = re.fullmatch(r"/api/assessments/(\d+)/appeal/resolve", path)
                if m:
                    data = self._read_json()
                    status, auth_id = domain.resolve_appeal(
                        conn, int(m.group(1)), int(data["reviewer_id"]),
                        bool(data.get("overturn")), data.get("note", ""))
                    conn.commit()
                    return self._send_json({"ok": True, "appeal_status": status,
                                            "authorization_id": auth_id})

                # ---- 授权撤销 ----
                m = re.fullmatch(r"/api/authorizations/(\d+)/revoke", path)
                if m:
                    data = self._read_json()
                    r = domain.revoke_authorization(conn, int(m.group(1)),
                                                    data.get("reason", ""),
                                                    int(data["operator_id"]))
                    notify.enqueue(
                        conn, "revoke", "授权已临时撤销",
                        "%s 的 %s/%s 授权被撤销：%s"
                        % (domain.get_employee(conn, r["employee_id"])["name"],
                           conn.execute("SELECT name FROM processes WHERE id=?",
                                        (r["process_id"],)).fetchone()["name"],
                           r["model"] or "全型号", data.get("reason", "")),
                        "authorization", r["id"])
                    conn.commit()
                    return self._send_json({"ok": True})

                # ---- 设备换型 ----
                m = re.fullmatch(r"/api/equipment/(\d+)/model", path)
                if m:
                    data = self._read_json()
                    eq, affected = domain.change_equipment_model(
                        conn, int(m.group(1)), data["model"].strip(),
                        int(data["operator_id"]))
                    if affected:
                        notify.enqueue(
                            conn, "model_change", "设备型号变更影响排班",
                            "%s 型号 %s → %s，%d 人失去该设备资格：%s"
                            % (eq["code"], eq["model"], data["model"].strip(), len(affected),
                               "、".join(x["name"] for x in affected)),
                            "equipment", eq["id"])
                    conn.commit()
                    return self._send_json({"ok": True, "affected": affected})

                if path == "/api/notify/pump":
                    notify.run_once()
                    return self._send_json({"ok": True})

                self._send_json({"error": "未找到"}, 404)
        except domain.RuleError as e:
            payload = {"error": str(e), "code": e.code}
            if e.code == "conflict":
                payload["conflict"] = e.conflict
            if e.qual is not None:
                payload["reasons"] = e.qual["reasons"]
            self._send_json(payload, 409 if e.code == "conflict" else 422)
        except ApiError as e:
            self._send_json({"error": e.message}, e.status)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    # ---------- 页面 / 附件 ----------
    def _static(self, name, ctype=None):
        name = os.path.normpath(name).lstrip("/")
        full = os.path.join(STATIC_DIR, name)
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            return self._send_json({"error": "未找到"}, 404)
        ctype = ctype or {
            ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _attachment(self, name):
        full = os.path.join(db.ATTACH_DIR, name)
        if not os.path.abspath(full).startswith(os.path.abspath(db.ATTACH_DIR)) or not os.path.isfile(full):
            return self._send_json({"error": "附件不存在"}, 404)
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _state(self):
        with db.get_conn() as conn:
            employees = _rows(conn.execute(
                "SELECT * FROM employees WHERE archived=0 ORDER BY id"))
            processes = _rows(conn.execute("SELECT * FROM processes ORDER BY id"))
            equipment = _rows(conn.execute(
                """SELECT e.*, p.name process_name FROM equipment e
                   JOIN processes p ON p.id=e.process_id ORDER BY e.id"""))
            shifts = _rows(conn.execute(
                "SELECT * FROM shifts ORDER BY shift_date, shift_type"))
            authes = _rows(conn.execute(
                """SELECT a.*, e.name employee_name, p.name process_name
                   FROM authorizations a
                   JOIN employees e ON e.id=a.employee_id
                   JOIN processes p ON p.id=a.process_id ORDER BY a.id"""))
            return self._send_json({
                "employees": employees, "processes": processes,
                "equipment": equipment, "shifts": shifts,
                "authorizations": authes,
                "matrix": domain.matrix(conn),
            })


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = {"error": message}


def serve(host="127.0.0.1", port=8077):
    db.init_db()
    notify.start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print("技能授权与排班系统已启动: http://%s:%d" % (host, port))
    print("样例：夜班 PKG-01 存在无证替岗申请（钱新，待安全负责人安平复核）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        notify.stop()
        httpd.shutdown()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8077)
    args = parser.parse_args()
    serve(args.host, args.port)
