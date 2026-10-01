"""进程内通知任务：后台线程扫描 queued 通知，模拟送达并写回执；定时扫描到期授权。"""
import threading
import time
from datetime import datetime

from . import db

_stop = threading.Event()
_thread = None


def enqueue(conn, kind, title, body, ref_type="", ref_id=None):
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        """INSERT INTO notifications(kind,title,body,ref_type,ref_id,status,created_at)
           VALUES (?,?,?,?,?, 'queued', ?)""",
        (kind, title, body, ref_type, ref_id, now))
    return cur.lastrowid


def list_notifications(conn, limit=50):
    return conn.execute(
        "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def _deliver_pending(conn):
    rows = conn.execute("SELECT * FROM notifications WHERE status='queued' ORDER BY id").fetchall()
    for r in rows:
        # 模拟外部通道送达延迟与回执
        time.sleep(0.15)
        now = datetime.now().isoformat(timespec="seconds")
        receipt = "SIM-RCPT-%s-%06d" % (now.replace("-", "").replace(":", "").replace("T", "-")[:15], r["id"])
        conn.execute(
            "UPDATE notifications SET status='delivered', receipt=?, delivered_at=? WHERE id=?",
            (receipt, now, r["id"]))


def _scan_expiry(conn):
    from .domain import expiring_authorizations, EXPIRY_WARN_DAYS
    existing = {
        x["ref_id"]
        for x in conn.execute(
            "SELECT ref_id FROM notifications WHERE kind='expiry' AND ref_type='authorization'"
        ).fetchall()
    }
    for a in expiring_authorizations(conn):
        if ("authorization", a["id"]) in existing:
            continue
        days = (datetime.fromisoformat(a["valid_to"]).date() - datetime.now().date()).days
        enqueue(conn, "expiry",
                "授权即将到期：%s / %s" % (a["employee_name"], a["process_name"]),
                "%s 的 %s（%s，%s）将于 %s 到期（剩 %d 天），请安排复训与考核。"
                % (a["employee_name"], a["process_name"], a["grade"],
                   a["model"] or "全型号", a["valid_to"], days),
                "authorization", a["id"])


def _loop(interval):
    while not _stop.wait(interval):
        try:
            with db._lock, db.get_conn() as conn:
                _scan_expiry(conn)
                _deliver_pending(conn)
        except Exception as exc:  # 后台任务不应拖垮进程
            print("[notify] 任务异常:", exc)


def start(interval=2.0):
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(interval,), daemon=True, name="notifier")
    _thread.start()


def stop():
    _stop.set()


def run_once():
    """测试用：同步执行一轮扫描+送达。"""
    with db._lock, db.get_conn() as conn:
        _scan_expiry(conn)
        _deliver_pending(conn)
