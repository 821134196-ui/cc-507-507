"""零依赖 HTTP 服务：JSON API + 静态页面 + 本地附件。"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import urllib.parse
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db as dbmod
from . import rules, services

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(os.path.dirname(BASE_DIR), "data")
ATTACH_DIR = os.path.join(DATA_DIR, "attachments")
DEFAULT_DB = os.path.join(DATA_DIR, "app.db")

_CT = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


# ---------------- 序列化辅助 ----------------

def _emp(e: sqlite3.Row) -> dict:
    return {"id": e["id"], "name": e["name"], "badge": e["badge"],
            "is_foreman": bool(e["is_foreman"]), "is_trainer": bool(e["is_trainer"]),
            "is_safety": bool(e["is_safety"]), "active": bool(e["active"])}


def matrix_payload(conn) -> dict:
    procs = [dict(r) for r in conn.execute("SELECT * FROM processes ORDER BY seq")]
    emps = [_emp(r) for r in conn.execute(
        "SELECT * FROM employees WHERE active=1 ORDER BY is_foreman DESC, is_safety DESC, id")]
    cells: dict[str, list] = {}
    for a in conn.execute(
            """SELECT a.*, p.code process_code, m.code model_code, m.name model_name
                 FROM authorizations a
                 JOIN processes p ON p.id=a.process_id
                 LEFT JOIN equipment_models m ON m.id=a.model_id
                ORDER BY a.level DESC"""):
        cells.setdefault(f"{a['process_id']}:{a['employee_id']}", []).append({
            "id": a["id"], "level": a["level"], "status": a["status"],
            "valid_from": a["valid_from"], "valid_until": a["valid_until"],
            "model_code": a["model_code"], "model_name": a["model_name"],
            "note": a["note"], "all_models": a["model_code"] is None,
        })
    return {"processes": procs, "employees": emps, "cells": cells,
            "today": rules.today_iso(), "warn_days": services.WARN_DAYS}


def shift_payload(conn, shift_id: int) -> dict:
    sh = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
    if sh is None:
        return {}
    slots = []
    for sl in conn.execute(
            """SELECT sl.*, e.code eq_code, e.name eq_name, e.required_level, e.high_risk,
                      m.code model_code, m.name model_name, p.code process_code
                 FROM shift_slots sl
                 JOIN equipment e ON e.id=sl.equipment_id
                 JOIN equipment_models m ON m.id=e.model_id
                 JOIN processes p ON p.id=e.process_id
                WHERE sl.shift_id=? ORDER BY e.code""", (shift_id,)):
        assigns = []
        for sa in conn.execute(
                """SELECT sa.*, em.name, em.badge
                     FROM shift_assignments sa
                     JOIN employees em ON em.id=sa.employee_id
                    WHERE sa.slot_id=? ORDER BY sa.id""", (sl["id"],)):
            ev = rules.evaluate(conn, sa["employee_id"], sl["equipment_id"])
            assigns.append({
                "id": sa["id"], "employee_id": sa["employee_id"], "name": sa["name"],
                "badge": sa["badge"], "is_temp": bool(sa["is_temp"]),
                "request_id": sa["request_id"],
                "eligible_now": ev["eligible"], "reasons": ev["reasons"],
            })
        qualified, others = rules.eligible_candidates(conn, sl["equipment_id"], shift_id)
        slots.append({
            "id": sl["id"], "equipment_id": sl["equipment_id"], "eq_code": sl["eq_code"],
            "eq_name": sl["eq_name"], "model_code": sl["model_code"],
            "model_name": sl["model_name"], "process_code": sl["process_code"],
            "required_level": sl["required_level"], "high_risk": bool(sl["high_risk"]),
            "required_heads": sl["required_heads"], "note": sl["note"],
            "assignments": assigns,
            "gap": sl["required_heads"] - len(assigns),
            "qualified": qualified, "others": others,
            "required_reviewer": rules.required_reviewer(conn, sl["equipment_id"]),
        })
    reqs = []
    for tr in conn.execute(
            """SELECT tr.*, em.name emp_name, em.badge emp_badge,
                      f.name foreman_name, r.name reviewer_name,
                      e.code eq_code, e.name eq_name, e.high_risk
                 FROM temp_requests tr
                 JOIN employees em ON em.id=tr.employee_id
                 JOIN employees f ON f.id=tr.requested_by
                 LEFT JOIN employees r ON r.id=tr.reviewed_by
                 JOIN shift_slots sl ON sl.id=tr.slot_id
                 JOIN equipment e ON e.id=sl.equipment_id
                WHERE tr.shift_id=? ORDER BY tr.id""", (shift_id,)):
        reqs.append({k: tr[k] for k in tr.keys()} | {
            "emp_name": tr["emp_name"], "foreman_name": tr["foreman_name"],
            "reviewer_name": tr["reviewer_name"], "eq_code": tr["eq_code"],
            "eq_name": tr["eq_name"], "high_risk": bool(tr["high_risk"]),
            "unlicensed": bool(tr["unlicensed"]),
        })
    return {"id": sh["id"], "work_date": sh["work_date"], "shift_no": sh["shift_no"],
            "label": sh["label"], "slots": slots, "requests": reqs}


def assessments_payload(conn) -> list:
    out = []
    for a in conn.execute(
            """SELECT a.*, em.name emp_name, p.code process_code,
                      m.code model_code, rc.name recorder_name, ap.name approver_name
                 FROM assessments a
                 JOIN employees em ON em.id=a.employee_id
                 JOIN processes p ON p.id=a.process_id
                 LEFT JOIN equipment_models m ON m.id=a.model_id
                 JOIN employees rc ON rc.id=a.recorded_by
                 LEFT JOIN employees ap ON ap.id=a.approved_by
                ORDER BY a.exam_date DESC, a.id DESC"""):
        d = {k: a[k] for k in a.keys()}
        d.update({"emp_name": a["emp_name"], "process_code": a["process_code"],
                  "model_code": a["model_code"], "recorder_name": a["recorder_name"],
                  "approver_name": a["approver_name"]})
        out.append(d)
    return out


def trainings_payload(conn) -> list:
    out = []
    for t in conn.execute(
            """SELECT t.*, em.name emp_name, p.code process_code, tr.name trainer_name
                 FROM trainings t
                 JOIN employees em ON em.id=t.employee_id
                 JOIN processes p ON p.id=t.process_id
                 LEFT JOIN employees tr ON tr.id=t.trainer_id
                ORDER BY t.trained_date DESC"""):
        out.append({k: t[k] for k in t.keys()} | {
            "emp_name": t["emp_name"], "process_code": t["process_code"],
            "trainer_name": t["trainer_name"]})
    return out


def equipment_changes_payload(conn) -> list:
    return [dict(r) for r in conn.execute(
        """SELECT c.*, e.code eq_code, m1.code old_model, m2.code new_model,
                  u.name changed_by_name
             FROM equipment_changes c
             JOIN equipment e ON e.id=c.equipment_id
             JOIN equipment_models m2 ON m2.id=c.new_model_id
             LEFT JOIN equipment_models m1 ON m1.id=c.old_model_id
             LEFT JOIN employees u ON u.id=c.changed_by
            ORDER BY c.changed_at DESC""")]


# ---------------- multipart 解析（最小实现） ----------------

def parse_multipart(body: bytes, content_type: str) -> dict:
    """返回 {field: str|bytes}，文件字段值为 bytes（调用方负责落盘）。"""
    m = re.search(r"boundary=([^;]+)", content_type)
    if not m:
        return {}
    boundary = b"--" + m.group(1).strip().strip('"').encode()
    fields: dict = {}
    for part in body.split(boundary):
        if not part or part in (b"--", b"--\r\n"):
            continue
        part = part.strip(b"\r\n")
        if b"\r\n\r\n" not in part:
            continue
        header_blob, content = part.split(b"\r\n\r\n", 1)
        headers = header_blob.decode("utf-8", "replace")
        nm = re.search(r'name="([^"]+)"', headers)
        fm = re.search(r'filename="([^"]*)"', headers)
        if not nm:
            continue
        name = nm.group(1)
        if fm and fm.group(1):
            fields[name] = {"filename": os.path.basename(fm.group(1)), "data": content}
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields


# ---------------- HTTP Handler ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SkillMatrix/1.0"
    db_path = DEFAULT_DB

    def log_message(self, fmt, *args):  # 静默默认访问日志，保留错误
        pass

    # --- 基础响应 ---
    def _json(self, obj, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _err(self, status: int, msg: str):
        self._json({"ok": False, "error": msg}, status)

    def _conn(self):
        # closing 退出时关闭连接；所有写操作在 service 层显式 commit
        return closing(dbmod.connect(self.db_path))

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        ct = self.headers.get("Content-Type", "")
        if "application/json" in ct:
            return json.loads(raw.decode("utf-8"))
        if "multipart/form-data" in ct:
            return parse_multipart(raw, ct)
        return dict(urllib.parse.parse_qsl(raw.decode("utf-8")))

    # --- 路由 ---
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/employees":
                with self._conn() as conn:
                    self._json([_emp(r) for r in conn.execute(
                        "SELECT * FROM employees ORDER BY id")])
            elif path == "/api/matrix":
                with self._conn() as conn:
                    self._json(matrix_payload(conn))
            elif path == "/api/equipment":
                with self._conn() as conn:
                    rows = conn.execute(
                        """SELECT e.*, p.code process_code, m.code model_code, m.name model_name
                             FROM equipment e JOIN processes p ON p.id=e.process_id
                             JOIN equipment_models m ON m.id=e.model_id ORDER BY e.code""").fetchall()
                    self._json({"equipment": [dict(r) for r in rows],
                                "models": [dict(r) for r in conn.execute("SELECT * FROM equipment_models ORDER BY id")],
                                "changes": equipment_changes_payload(conn)})
            elif path == "/api/shifts":
                with self._conn() as conn:
                    rows = conn.execute("SELECT * FROM shifts ORDER BY work_date, shift_no").fetchall()
                    self._json([dict(r) for r in rows])
            elif path == "/api/shift":
                with self._conn() as conn:
                    sid = int(qs.get("id", [0])[0])
                    payload = shift_payload(conn, sid)
                    self._json(payload if payload else {"error": "班次不存在"},
                               200 if payload else 404)
            elif path == "/api/assessments":
                with self._conn() as conn:
                    self._json(assessments_payload(conn))
            elif path == "/api/trainings":
                with self._conn() as conn:
                    self._json(trainings_payload(conn))
            elif path == "/api/notifications":
                with self._conn() as conn:
                    unread = qs.get("unread", ["0"])[0] == "1"
                    self._json(services.list_notifications(conn, unread))
            elif path.startswith("/attachments/"):
                self._serve_attachment(path.split("/attachments/", 1)[1])
            elif path == "/api/employee":
                with self._conn() as conn:
                    eid = int(qs.get("id", [0])[0])
                    e = conn.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
                    if not e:
                        self._err(404, "人员不存在"); return
                    auths = rules.list_authorizations(conn, eid)
                    self._json({"employee": _emp(e),
                                "authorizations": [dict(a) for a in auths]})
            else:
                self._serve_static(path)
        except Exception as exc:  # noqa: BLE001
            self._err(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_json()
            with self._conn() as conn:
                if path == "/api/assign":
                    res = services.assign_employee(
                        conn, int(body["employee_id"]), int(body["slot_id"]),
                        actor_id=int(body.get("actor_id") or 0))
                    self._json(res, 200 if res["ok"] else 409)
                elif path == "/api/unassign":
                    self._json(services.unassign(conn, int(body["assignment_id"])))
                elif path == "/api/temp-requests":
                    res = services.create_temp_request(
                        conn, slot_id=int(body["slot_id"]),
                        employee_id=int(body["employee_id"]),
                        requester_id=int(body["requester_id"]),
                        vacancy_reason=str(body.get("vacancy_reason", "")),
                        basis=str(body.get("basis", "none")))
                    self._json(res, 200 if res["ok"] else 409)
                elif re.fullmatch(r"/api/temp-requests/(\d+)/review", path):
                    rid = int(re.findall(r"\d+", path)[0])
                    res = services.review_temp_request(
                        conn, rid, int(body["reviewer_id"]),
                        bool(body.get("approve")), str(body.get("note", "")))
                    self._json(res, 200 if res["ok"] else 409)
                elif re.fullmatch(r"/api/assessments/(\d+)/appeal", path):
                    aid = int(re.findall(r"\d+", path)[0])
                    res = services.appeal_assessment(
                        conn, aid, int(body["employee_id"]), str(body.get("reason", "")))
                    self._json(res, 200 if res["ok"] else 409)
                elif re.fullmatch(r"/api/assessments/(\d+)/decide-appeal", path):
                    aid = int(re.findall(r"\d+", path)[0])
                    res = services.decide_appeal(
                        conn, aid, int(body["decider_id"]),
                        bool(body.get("uphold")), str(body.get("note", "")))
                    self._json(res, 200 if res["ok"] else 409)
                elif re.fullmatch(r"/api/assessments/(\d+)/approve", path):
                    aid = int(re.findall(r"\d+", path)[0])
                    res = services.approve_assessment(conn, aid, int(body["approver_id"]))
                    self._json(res, 200 if res["ok"] else 409)
                elif path == "/api/assessments":
                    self._json(self._create_assessment(conn, body))
                elif path == "/api/trainings":
                    self._json(self._create_training(conn, body))
                elif path == "/api/notifications/read":
                    services.mark_read(conn, int(body["id"]))
                    self._json({"ok": True})
                elif path == "/api/equipment/change-model":
                    res = services.change_equipment_model(
                        conn, int(body["equipment_id"]), int(body["new_model_id"]),
                        int(body.get("actor_id") or 0), str(body.get("reason", "")))
                    self._json(res, 200 if res["ok"] else 409)
                elif path == "/api/sweep":
                    self._json(services.sweep_authorizations(conn))
                else:
                    self._err(404, "未知接口")
        except Exception as exc:  # noqa: BLE001
            self._err(500, f"{type(exc).__name__}: {exc}")

    # --- 文件上传写入 ---
    def _save_upload(self, field: dict, prefix: str) -> tuple[str, str]:
        os.makedirs(ATTACH_DIR, exist_ok=True)
        safe = re.sub(r"[^\w.\-]+", "_", field["filename"]) or "evidence.bin"
        fname = f"{prefix}_{safe}"
        with open(os.path.join(ATTACH_DIR, fname), "wb") as f:
            f.write(field["data"])
        return fname, field["filename"]

    def _create_assessment(self, conn, body) -> dict:
        emp = int(body["employee_id"]); proc = int(body["process_id"])
        model_raw = body.get("model_id")
        model = int(model_raw) if model_raw not in (None, "", "null") else None
        score = int(body["score"]); pass_score = 60
        result = "pass" if score >= pass_score else "fail"
        ev_file = ev_orig = None
        if isinstance(body.get("evidence_file"), dict):
            ev_file, ev_orig = self._save_upload(body["evidence_file"], f"asm_{emp}")
        cur = conn.execute(
            """INSERT INTO assessments(employee_id,process_id,model_id,exam_date,score,pass_score,
               result,status,recorded_by,approved_by,evidence_file,evidence_orig,created_at)
               VALUES(?,?,?,?,?,?,?, 'pending', ?,NULL,?,?,?)""",
            (emp, proc, model, body.get("exam_date", rules.today_iso()), score, pass_score,
             result, int(body["recorded_by"]), ev_file, ev_orig, rules.now_iso()),
        )
        conn.commit()
        return {"ok": True, "assessment_id": cur.lastrowid,
                "note": "登记成功，待他人审批（培训老师不能审批自己登记的考核）"}

    def _create_training(self, conn, body) -> dict:
        ev_file = ev_orig = None
        if isinstance(body.get("evidence_file"), dict):
            ev_file, ev_orig = self._save_upload(body["evidence_file"], "trn")
        cur = conn.execute(
            """INSERT INTO trainings(employee_id,process_id,course,trained_date,hours,trainer_id,
               evidence_file,evidence_orig,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (int(body["employee_id"]), int(body["process_id"]), body.get("course", ""),
             body.get("trained_date", rules.today_iso()), float(body.get("hours", 0)),
             int(body.get("trainer_id") or 0), ev_file, ev_orig, rules.now_iso()),
        )
        conn.commit()
        return {"ok": True, "training_id": cur.lastrowid}

    # --- 静态资源 ---
    def _serve_static(self, path: str):
        if path in ("", "/"):
            path = "/index.html"
        target = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not target.startswith(STATIC_DIR) or not os.path.isfile(target):
            self._err(404, "资源不存在")
            return
        ext = os.path.splitext(target)[1]
        with open(target, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _CT.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_attachment(self, name: str):
        target = os.path.normpath(os.path.join(ATTACH_DIR, name))
        if not target.startswith(ATTACH_DIR) or not os.path.isfile(target):
            self._err(404, "附件不存在")
            return
        with open(target, "rb") as f:
            data = f.read()
        ext = os.path.splitext(target)[1]
        self.send_response(200)
        self.send_header("Content-Type", _CT.get(ext, "application/octet-stream"))
        self.send_header("Content-Disposition",
                         f'attachment; filename="{urllib.parse.quote(name)}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def build(host: str = "127.0.0.1", port: int = 8000, db_path: str | None = None,
          reset: bool = False) -> ThreadingHTTPServer:
    os.makedirs(ATTACH_DIR, exist_ok=True)
    path = db_path or DEFAULT_DB
    if reset and os.path.exists(path):
        os.remove(path)
    conn = dbmod.connect(path)
    dbmod.init_schema(conn)
    dbmod.seed(conn, attachments_dir=ATTACH_DIR)
    conn.close()

    class _H(Handler):
        pass
    _H.db_path = path
    httpd = ThreadingHTTPServer((host, port), _H)
    httpd.db_path = path
    return httpd


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="技能矩阵与排班授权系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true", help="删除并重建演示数据库")
    ap.add_argument("--no-reminder", action="store_true", help="关闭进程内到期提醒任务")
    ap.add_argument("--interval", type=float, default=60.0, help="提醒扫描间隔秒")
    args = ap.parse_args(argv)

    httpd = build(args.host, args.port, reset=args.reset)
    worker = None
    if not args.no_reminder:
        worker = services.ReminderWorker(httpd.db_path, args.interval)
        worker.start()
    print(f"✔ 服务已启动: http://{args.host}:{args.port}")
    print("  页面: 技能矩阵 / 排班拖拽 / 替岗申请 / 培训证据 / 到期通知")
    print("  样例: 今日夜班 EQ-04 高风险焊位存在「无证替岗」待复核申请")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        if worker:
            worker.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
