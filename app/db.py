"""SQLite 数据访问层：建库、连接、种子数据。"""
import json
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta

DB_PATH = os.environ.get("SHIFT_DB", os.path.join(os.path.dirname(__file__), "data", "shift.db"))
ATTACH_DIR = os.environ.get("SHIFT_ATTACH", os.path.join(os.path.dirname(__file__), "data", "attachments"))

_lock = threading.RLock()


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS processes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    risk_level TEXT NOT NULL DEFAULT 'normal'      -- normal / high
);
CREATE TABLE IF NOT EXISTS equipment (
    id INTEGER PRIMARY KEY,
    process_id INTEGER NOT NULL REFERENCES processes(id),
    code TEXT NOT NULL UNIQUE,
    model TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'worker',           -- worker / foreman / safety / trainer
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    process_id INTEGER NOT NULL REFERENCES processes(id),
    model TEXT,                                    -- 考核针对的设备型号（空=通用）
    grade TEXT NOT NULL,                           -- L1/L2/L3
    score INTEGER NOT NULL,
    pass INTEGER NOT NULL,
    exam_date TEXT NOT NULL,                       -- YYYY-MM-DD
    recorded_by INTEGER NOT NULL REFERENCES employees(id),
    evidence_path TEXT,
    evidence_name TEXT,
    approved_by INTEGER REFERENCES employees(id),  -- 考核审批人（不得=登记人）
    approved_at TEXT,
    appeal_status TEXT NOT NULL DEFAULT 'none',    -- none / pending / approved / rejected
    appeal_note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authorizations (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    process_id INTEGER NOT NULL REFERENCES processes(id),
    model TEXT,                                    -- NULL 表示该工序全型号通用
    grade TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    source_assessment INTEGER REFERENCES assessments(id),
    status TEXT NOT NULL DEFAULT 'active',         -- active / revoked
    revoked_reason TEXT DEFAULT '',
    UNIQUE(employee_id, process_id, model)
);
CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY,
    shift_date TEXT NOT NULL,
    shift_type TEXT NOT NULL,                      -- day / night
    UNIQUE(shift_date, shift_type)
);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
    equipment_id INTEGER NOT NULL REFERENCES equipment(id),
    employee_id INTEGER REFERENCES employees(id),  -- NULL = 缺口
    created_by INTEGER REFERENCES employees(id),
    created_at TEXT NOT NULL,
    UNIQUE(shift_id, equipment_id)
);
CREATE TABLE IF NOT EXISTS substitutions (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL REFERENCES shifts(id),
    equipment_id INTEGER NOT NULL REFERENCES equipment(id),
    vacancy_reason TEXT NOT NULL,                  -- 原岗位空缺说明
    applicant_emp INTEGER NOT NULL REFERENCES employees(id),  -- 拟替岗人员
    applied_by INTEGER NOT NULL REFERENCES employees(id),     -- 申请的班长
    authorization_basis TEXT NOT NULL,             -- 授权依据：authorization id 或 unlicensed
    reviewed_by INTEGER REFERENCES employees(id),
    review_note TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',        -- pending / approved / rejected
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                            -- expiry/review/conflict/appeal/model_change/revoke/unlicensed
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    ref_type TEXT DEFAULT '',
    ref_id INTEGER,
    status TEXT NOT NULL DEFAULT 'queued',         -- queued / delivered
    receipt TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
"""


def init_db(seed=True):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(ATTACH_DIR, exist_ok=True)
    with _lock, get_conn() as conn:
        conn.executescript(SCHEMA)
        count = conn.execute("SELECT COUNT(*) c FROM employees").fetchone()["c"]
        if seed and count == 0:
            _seed(conn)


def _seed(conn):
    today = date.today()
    d = lambda off: (today + timedelta(days=off)).isoformat()

    # 工序：焊接(高风险)、装配、冲压(高风险)、包装
    procs = [("焊接", "high"), ("装配", "normal"), ("冲压", "high"), ("包装", "normal")]
    conn.executemany("INSERT INTO processes(name, risk_level) VALUES (?,?)", procs)
    proc = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM processes")}

    # 设备（含同工序不同型号，用于型号匹配测试）
    equip = [
        ("WLD-01", proc["焊接"], "WX-200"),
        ("WLD-02", proc["焊接"], "WX-300"),
        ("ASM-01", proc["装配"], "AS-10"),
        ("PRS-01", proc["冲压"], "PR-500"),
        ("PKG-01", proc["包装"], "PK-8"),
    ]
    conn.executemany(
        "INSERT INTO equipment(code, process_id, model) VALUES (?,?,?)",
        [(c, p, m) for c, p, m in equip],
    )

    # 员工：2 名班长、1 名安全负责人、1 名培训老师 + 若干工人
    emps = [
        ("陈晨", "foreman"), ("李立", "foreman"),
        ("安平", "safety"), ("傅师", "trainer"),
        ("王焊", "worker"), ("赵强", "worker"), ("孙铆", "worker"),
        ("周装", "worker"), ("吴冲", "worker"), ("郑包", "worker"),
        ("钱新", "worker"),  # 新人：无任何授权，用于无证替岗样例
    ]
    conn.executemany("INSERT INTO employees(name, role) VALUES (?,?)", emps)
    eid = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM employees")}
    now = datetime.now().isoformat(timespec="seconds")

    def assessment(emp, pname, grade, score, ok, exam_off, model=None, recorder="傅师"):
        cur = conn.execute(
            """INSERT INTO assessments(employee_id,process_id,model,grade,score,pass,exam_date,recorded_by,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (eid[emp], proc[pname], model, grade, score, int(ok), d(exam_off), eid[recorder], now),
        )
        return cur.lastrowid

    def auth(emp, pname, grade, from_off, to_off, model=None, aid=None):
        conn.execute(
            """INSERT INTO authorizations(employee_id,process_id,model,grade,valid_from,valid_to,source_assessment)
               VALUES (?,?,?,?,?,?,?)""",
            (eid[emp], proc[pname], model, grade, d(from_off), d(to_off), aid),
        )

    # 王焊：WX-200 合格（即将到期，用于到期提醒）
    a1 = assessment("王焊", "焊接", "L2", 88, True, -350, "WX-200")
    auth("王焊", "焊接", "L2", -350, 10, "WX-200", a1)
    # 赵强：焊接全型号通用（model=NULL），覆盖 WLD-01/WLD-02
    a2 = assessment("赵强", "焊接", "L3", 95, True, -100)
    auth("赵强", "焊接", "L3", -100, 600, None, a2)
    # 孙铆：WX-300 有授权
    a3 = assessment("孙铆", "焊接", "L1", 75, True, -60, "WX-300")
    auth("孙铆", "焊接", "L1", -60, 300, "WX-300", a3)
    # 周装：装配
    a4 = assessment("周装", "装配", "L2", 82, True, -200, "AS-10")
    auth("周装", "装配", "L2", -200, 400, "AS-10", a4)
    # 吴冲：冲压合格
    a5 = assessment("吴冲", "冲压", "L2", 90, True, -120, "PR-500")
    auth("吴冲", "冲压", "L2", -120, 500, "PR-500", a5)
    # 郑包：包装
    a6 = assessment("郑包", "包装", "L1", 70, True, -40, "PK-8")
    auth("郑包", "包装", "L1", -40, 200, "PK-8", a6)
    # 孙铆 装配也合格（可替岗人选）
    a7 = assessment("孙铆", "装配", "L2", 85, True, -80, "AS-10")
    auth("孙铆", "装配", "L2", -80, 300, "AS-10", a7)
    # 王焊有一条考核未通过（冲压），不得因“培训过”而获得资格
    assessment("王焊", "冲压", "L1", 45, False, -30, "PR-500")
    # 王焊有一条授权已被撤销（包装）
    a8 = assessment("王焊", "包装", "L1", 72, True, -200, "PK-8")
    conn.execute(
        """INSERT INTO authorizations(employee_id,process_id,model,grade,valid_from,valid_to,source_assessment,status,revoked_reason)
           VALUES (?,?,?,?,?,?,?, 'revoked', '临时撤销：违章操作')""",
        (eid["王焊"], proc["包装"], "PK-8", "L1", d(-200), d(200), a8),
    )
    # 一条带申诉的考核（赵强 WX-300 考核登记为未通过，本人申诉中）
    assessment("赵强", "焊接", "L2", 58, False, -10, "WX-300")
    conn.execute(
        "UPDATE assessments SET appeal_status='pending', appeal_note=? WHERE employee_id=? AND model='WX-300' AND pass=0",
        ("现场评分表与登记分数不一致，申请复核", eid["赵强"]),
    )

    # 班次：今日白班/夜班，明日白班
    shifts = [(d(0), "day"), (d(0), "night"), (d(1), "day")]
    conn.executemany("INSERT INTO shifts(shift_date,shift_type) VALUES (?,?)", shifts)
    sid = {
        (r["shift_date"], r["shift_type"]): r["id"]
        for r in conn.execute("SELECT id,shift_date,shift_type FROM shifts")
    }

    # 今日白班排满
    s = sid[(d(0), "day")]
    plan_day = [("WLD-01", "王焊"), ("WLD-02", "赵强"), ("ASM-01", "周装"),
                ("PRS-01", "吴冲"), ("PKG-01", "郑包")]
    for code, emp in plan_day:
        eq = conn.execute("SELECT id FROM equipment WHERE code=?", (code,)).fetchone()["id"]
        conn.execute(
            "INSERT INTO assignments(shift_id,equipment_id,employee_id,created_by,created_at) VALUES (?,?,?,?,?)",
            (s, eq, eid[emp], eid["陈晨"], now),
        )

    # 夜班：焊接 WLD-02 是缺口（夜班缺员场景）；包装 PKG-01 同样空缺，
    # 对应下方待安全复核的“无证替岗”样例（钱新无授权，批准前不得上岗）。
    s = sid[(d(0), "night")]
    for code, emp in [("WLD-01", "赵强"), ("WLD-02", None), ("ASM-01", "周装"),
                      ("PRS-01", "吴冲"), ("PKG-01", None)]:
        eq = conn.execute("SELECT id FROM equipment WHERE code=?", (code,)).fetchone()["id"]
        conn.execute(
            "INSERT INTO assignments(shift_id,equipment_id,employee_id,created_by,created_at) VALUES (?,?,?,?,?)",
            (s, eq, eid[emp] if emp else None, eid["李立"], now),
        )

    # 无证替岗样例：夜班包装岗原岗人员请假，李立(班长)申请钱新(无授权)替岗，待安全复核
    s = sid[(d(0), "night")]
    eq_pkg = conn.execute("SELECT id FROM equipment WHERE code='PKG-01'").fetchone()["id"]
    conn.execute(
        """INSERT INTO substitutions(shift_id,equipment_id,vacancy_reason,applicant_emp,applied_by,
               authorization_basis,status,created_at)
           VALUES (?,?,?,?,?,?, 'pending', ?)""",
        (s, eq_pkg, "原岗郑包请病假，夜班包装岗空缺", eid["钱新"], eid["李立"],
         "unlicensed", now),
    )

    # 样例通知
    conn.execute(
        """INSERT INTO notifications(kind,title,body,ref_type,status,created_at)
           VALUES ('expiry','授权即将到期','王焊 的 焊接/WX-200 授权将于 %s 到期，请安排复训。',
                   'authorization','queued',?)""" % d(10),
        (now,),
    )
    conn.execute(
        """INSERT INTO notifications(kind,title,body,ref_type,status,created_at)
           VALUES ('unlicensed','无证替岗申请待复核','夜班 PKG-01：钱新 无包装岗位授权，班长李立已提交无证替岗申请，需安全负责人复核。',
                   'substitution','queued',?)""",
        (now,),
    )
