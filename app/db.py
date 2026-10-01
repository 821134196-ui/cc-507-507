"""SQLite 连接、建表与种子数据。"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS processes(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS equipment_models(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  badge TEXT UNIQUE NOT NULL,
  is_foreman INTEGER NOT NULL DEFAULT 0,
  is_trainer INTEGER NOT NULL DEFAULT 0,
  is_safety  INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS equipment(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  process_id INTEGER NOT NULL REFERENCES processes(id),
  model_id INTEGER NOT NULL REFERENCES equipment_models(id),
  required_level INTEGER NOT NULL DEFAULT 1,
  high_risk INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS equipment_changes(
  id INTEGER PRIMARY KEY,
  equipment_id INTEGER NOT NULL REFERENCES equipment(id),
  old_model_id INTEGER REFERENCES equipment_models(id),
  new_model_id INTEGER NOT NULL REFERENCES equipment_models(id),
  reason TEXT,
  changed_by INTEGER REFERENCES employees(id),
  changed_at TEXT NOT NULL
);

-- 技能授权：model_id 为 NULL 表示该工序全型号
CREATE TABLE IF NOT EXISTS authorizations(
  id INTEGER PRIMARY KEY,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  process_id INTEGER NOT NULL REFERENCES processes(id),
  model_id INTEGER REFERENCES equipment_models(id),
  level INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',  -- active/suspended/revoked
  valid_from TEXT NOT NULL,
  valid_until TEXT NOT NULL,
  last_assessment_id INTEGER,
  note TEXT,
  UNIQUE(employee_id, process_id, model_id)
);

CREATE TABLE IF NOT EXISTS auth_events(
  id INTEGER PRIMARY KEY,
  auth_id INTEGER NOT NULL REFERENCES authorizations(id),
  event_type TEXT NOT NULL,   -- grant/suspend/restore/expire/model_change
  reason TEXT,
  actor_id INTEGER REFERENCES employees(id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assessments(
  id INTEGER PRIMARY KEY,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  process_id INTEGER NOT NULL REFERENCES processes(id),
  model_id INTEGER REFERENCES equipment_models(id),
  exam_date TEXT NOT NULL,
  score INTEGER NOT NULL,
  pass_score INTEGER NOT NULL DEFAULT 60,
  result TEXT NOT NULL,           -- pass/fail
  status TEXT NOT NULL DEFAULT 'approved', -- pending/approved
  recorded_by INTEGER NOT NULL REFERENCES employees(id),
  approved_by INTEGER REFERENCES employees(id),
  evidence_file TEXT,
  evidence_orig TEXT,
  note TEXT,
  appeal_status TEXT NOT NULL DEFAULT 'none', -- none/pending/upheld/rejected
  appeal_reason TEXT,
  appeal_decided_by INTEGER REFERENCES employees(id),
  appeal_decided_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trainings(
  id INTEGER PRIMARY KEY,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  process_id INTEGER NOT NULL REFERENCES processes(id),
  course TEXT NOT NULL,
  trained_date TEXT NOT NULL,
  hours REAL NOT NULL DEFAULT 0,
  trainer_id INTEGER REFERENCES employees(id),
  evidence_file TEXT,
  evidence_orig TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shifts(
  id INTEGER PRIMARY KEY,
  work_date TEXT NOT NULL,
  shift_no TEXT NOT NULL CHECK(shift_no IN ('day','night')),
  label TEXT NOT NULL,
  UNIQUE(work_date, shift_no)
);

CREATE TABLE IF NOT EXISTS shift_slots(
  id INTEGER PRIMARY KEY,
  shift_id INTEGER NOT NULL REFERENCES shifts(id),
  equipment_id INTEGER NOT NULL REFERENCES equipment(id),
  required_heads INTEGER NOT NULL DEFAULT 1,
  note TEXT,
  UNIQUE(shift_id, equipment_id)
);

CREATE TABLE IF NOT EXISTS shift_assignments(
  id INTEGER PRIMARY KEY,
  slot_id INTEGER NOT NULL REFERENCES shift_slots(id),
  shift_id INTEGER NOT NULL REFERENCES shifts(id),
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  is_temp INTEGER NOT NULL DEFAULT 0,
  request_id INTEGER,
  created_by INTEGER REFERENCES employees(id),
  created_at TEXT NOT NULL,
  UNIQUE(slot_id, employee_id)
);
-- 同一员工同一班次只能在一个工位（跨工位即时段冲突）
CREATE UNIQUE INDEX IF NOT EXISTS ux_assign_employee_shift
  ON shift_assignments(employee_id, shift_id);

CREATE TABLE IF NOT EXISTS temp_requests(
  id INTEGER PRIMARY KEY,
  shift_id INTEGER NOT NULL REFERENCES shifts(id),
  slot_id INTEGER NOT NULL REFERENCES shift_slots(id),
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  vacancy_reason TEXT NOT NULL,        -- 原岗位空缺说明
  basis TEXT NOT NULL,                  -- auth:<id> 或 none
  unlicensed INTEGER NOT NULL DEFAULT 0,
  requested_by INTEGER NOT NULL REFERENCES employees(id),
  status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected
  reviewed_by INTEGER REFERENCES employees(id),
  review_note TEXT,
  created_at TEXT NOT NULL,
  reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  ref_type TEXT,
  ref_id INTEGER,
  receipt TEXT NOT NULL,
  read_at TEXT,
  created_at TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def iso(d: date) -> str:
    return d.isoformat()


def seed(conn: sqlite3.Connection, today: date | None = None,
         attachments_dir: str | None = None) -> None:
    """灌入演示数据。日期相对 today 取偏移，保证任何时候启动样例都有效。"""
    if conn.execute("SELECT COUNT(*) c FROM processes").fetchone()["c"]:
        return
    t = today or date.today()

    def d(n: int) -> str:
        return iso(t + timedelta(days=n))

    cur = conn.cursor()
    # 工序
    procs = [
        ("P-CNC", "CNC加工", 1),
        ("P-WELD", "焊接", 2),
        ("P-ASM", "装配", 3),
        ("P-QC", "质检", 4),
    ]
    cur.executemany("INSERT INTO processes(code,name,seq) VALUES(?,?,?)", procs)
    # 设备型号
    models = [
        ("CNC-A100", "CNC A100 立加"),
        ("CNC-B200", "CNC B200 卧加"),
        ("WELD-MIG", "MIG 气体保护焊"),
        ("ASM-STD", "通用装配工位"),
        ("QC-STD", "通用质检台"),
    ]
    cur.executemany("INSERT INTO equipment_models(code,name) VALUES(?,?)", models)
    pid = {r["code"]: r["id"] for r in cur.execute("SELECT id,code FROM processes")}
    mid = {r["code"]: r["id"] for r in cur.execute("SELECT id,code FROM equipment_models")}

    # 人员：班长×2、培训老师、安全负责人、工人×6
    people = [
        ("周建国", "B1001", 1, 0, 0),
        ("孙立军", "B1002", 1, 0, 0),
        ("何秀云", "T2001", 0, 1, 0),
        ("马文涛", "S3001", 0, 0, 1),
        ("王磊", "W4001", 0, 0, 0),
        ("李娜", "W4002", 0, 0, 0),
        ("张晨", "W4003", 0, 0, 0),
        ("赵强", "W4004", 0, 0, 0),
        ("刘洋", "W4005", 0, 0, 0),
        ("陈静", "W4006", 0, 0, 0),
    ]
    cur.executemany(
        "INSERT INTO employees(name,badge,is_foreman,is_trainer,is_safety) VALUES(?,?,?,?,?)",
        people,
    )
    eid = {r["badge"]: r["id"] for r in cur.execute("SELECT id,badge FROM employees")}
    ename = {r["name"]: r["id"] for r in cur.execute("SELECT id,name FROM employees")}

    # 设备。EQ-02 已发生型号变更 A100 -> B200
    equip = [
        ("EQ-01", "1号CNC加工单元", "P-CNC", "CNC-A100", 2, 0),
        ("EQ-02", "2号CNC加工单元", "P-CNC", "CNC-B200", 2, 0),
        ("EQ-03", "3号机器人焊接站", "P-WELD", "WELD-MIG", 3, 1),
        ("EQ-04", "4号手工焊接位", "P-WELD", "WELD-MIG", 2, 1),
        ("EQ-05", "5号装配线", "P-ASM", "ASM-STD", 1, 0),
        ("EQ-06", "6号质检台", "P-QC", "QC-STD", 1, 0),
    ]
    cur.executemany(
        """INSERT INTO equipment(code,name,process_id,model_id,required_level,high_risk)
           VALUES(?,?,?,?,?,?)""",
        [(c, n, pid[p], mid[m], lv, hr) for c, n, p, m, lv, hr in equip],
    )
    eqid = {r["code"]: r["id"] for r in cur.execute("SELECT id,code FROM equipment")}
    cur.execute(
        """INSERT INTO equipment_changes(equipment_id,old_model_id,new_model_id,reason,changed_by,changed_at)
           VALUES(?,?,?,?,?,?)""",
        (eqid["EQ-02"], mid["CNC-A100"], mid["CNC-B200"],
         "设备升级：A100 置换为 B200 卧加", ename["马文涛"], d(-15)),
    )

    # 考核记录（培训老师何秀云登记，安全负责人马文涛审批 —— 职责分离）
    def assessment(emp, proc, model, exam_d, score, result, recorded, approved,
                   status="approved", appeal="none", note="", ev="考核成绩单"):
        fn = f"assessment_{emp}_{proc}_{exam_d}.txt".lower()
        path = os.path.join(attachments_dir, fn) if attachments_dir else None
        if path:
            os.makedirs(attachments_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"考核证据（模拟）\n人员：{emp}\n工序：{proc}\n型号：{model or '全型号'}\n"
                        f"日期：{exam_d}\n成绩：{score}\n结论：{result}\n登记：{recorded} 审批：{approved}\n")
        cur.execute(
            """INSERT INTO assessments(employee_id,process_id,model_id,exam_date,score,
               pass_score,result,status,recorded_by,approved_by,evidence_file,evidence_orig,
               note,appeal_status,created_at)
               VALUES((SELECT id FROM employees WHERE name=?),
                      (SELECT id FROM processes WHERE code=?),
                      (SELECT id FROM equipment_models WHERE code=?),
                      ?,?,?,?,?, (SELECT id FROM employees WHERE name=?),
                      (SELECT id FROM employees WHERE name=?),
                      ?,?,?,?,?)""",
            (emp, proc, model, exam_d, score, 60, result, status, recorded, approved,
             fn, ev + ".txt", note, appeal, d(-30)),
        )
        return cur.lastrowid

    a_wanglei_cnc = assessment("王磊", "P-CNC", "CNC-A100", d(-40), 88, "pass", "何秀云", "马文涛")
    a_wanglei_weld = assessment("王磊", "P-WELD", "WELD-MIG", d(-60), 92, "pass", "何秀云", "马文涛")
    assessment("李娜", "P-CNC", "CNC-B200", d(-50), 81, "pass", "何秀云", "马文涛")
    assessment("李娜", "P-QC", None, d(-45), 79, "pass", "何秀云", "马文涛")
    assessment("张晨", "P-WELD", "WELD-MIG", d(-90), 74, "pass", "何秀云", "马文涛",
               note="授权已到期，待复审")
    assessment("张晨", "P-ASM", None, d(-20), 85, "pass", "何秀云", "马文涛")
    # 赵强最近考核未通过 → 授权暂停；本人已申诉（处理中）
    a_zhao_fail = assessment("赵强", "P-CNC", "CNC-A100", d(-6), 52, "fail", "何秀云", "马文涛",
                             appeal="pending", note="赵强主张实操评分有误，申诉处理中")
    assessment("刘洋", "P-ASM", None, d(-25), 76, "pass", "何秀云", "马文涛")
    assessment("刘洋", "P-QC", None, d(-35), 70, "pass", "何秀云", "马文涛")
    assessment("陈静", "P-CNC", "CNC-B200", d(-10), 90, "pass", "何秀云", "马文涛",
               note="设备型号变更后补训考核")

    # 培训记录
    def training(emp, proc, course, date_, hours):
        cur.execute(
            """INSERT INTO trainings(employee_id,process_id,course,trained_date,hours,trainer_id,created_at)
               VALUES((SELECT id FROM employees WHERE name=?),
                      (SELECT id FROM processes WHERE code=?),?,?,?,
                      (SELECT id FROM employees WHERE name=?),?)""",
            (emp, proc, course, date_, hours, "何秀云", d(-31)),
        )

    training("陈静", "P-CNC", "B200 卧加换装与对刀", d(-12), 8)
    training("赵强", "P-CNC", "A100 精加工复训", d(-8), 6)
    training("刘洋", "P-WELD", "MIG 焊基础（未完成考核）", d(-5), 4)

    # 授权
    def auth(emp, proc, model, level, status, vfrom, vuntil, assess_id=None, note=None):
        cur.execute(
            """INSERT INTO authorizations(employee_id,process_id,model_id,level,status,
               valid_from,valid_until,last_assessment_id,note)
               VALUES((SELECT id FROM employees WHERE name=?),
                      (SELECT id FROM processes WHERE code=?),
                      (SELECT id FROM equipment_models WHERE code=?),
                      ?,?,?,?,?,?)""",
            (emp, proc, model, level, status, vfrom, vuntil, assess_id, note),
        )
        aid = cur.lastrowid
        cur.execute(
            """INSERT INTO auth_events(auth_id,event_type,reason,actor_id,created_at)
               VALUES(?,?,?,(SELECT id FROM employees WHERE name='马文涛'),?)""",
            (aid, "grant", "考核通过授权", d(-30)),
        )
        return aid

    auth("王磊", "P-CNC", "CNC-A100", 2, "active", d(-200), d(5), a_wanglei_cnc,
         note="5 天后到期，需复审")
    auth("王磊", "P-WELD", "WELD-MIG", 3, "active", d(-200), d(200), a_wanglei_weld)
    auth("李娜", "P-CNC", "CNC-B200", 2, "active", d(-200), d(300))
    auth("李娜", "P-QC", None, 1, "active", d(-200), d(300))
    auth("张晨", "P-WELD", "WELD-MIG", 2, "active", d(-200), d(-5),
         note="已到期")  # 到期由扫描任务标记
    auth("张晨", "P-ASM", None, 1, "active", d(-15), d(300))
    zhao_auth = auth("赵强", "P-CNC", "CNC-A100", 2, "suspended", d(-200), d(200),
                     a_zhao_fail, note="考核未通过，暂停授权（申诉中）")
    cur.execute(
        "INSERT INTO auth_events(auth_id,event_type,reason,actor_id,created_at) VALUES(?,?,?,?,?)",
        (zhao_auth, "suspend", "考核未通过（52分）", ename["马文涛"], d(-6)),
    )
    auth("刘洋", "P-ASM", None, 1, "active", d(-200), d(300))
    auth("刘洋", "P-QC", None, 1, "active", d(-200), d(300))
    liu_weld = auth("刘洋", "P-WELD", "WELD-MIG", 2, "revoked", d(-200), d(300),
                    note="违章操作，临时撤销")
    cur.execute(
        "INSERT INTO auth_events(auth_id,event_type,reason,actor_id,created_at) VALUES(?,?,?,?,?)",
        (liu_weld, "revoke", "近期违章操作，临时撤销焊接授权", ename["马文涛"], d(-3)),
    )
    auth("陈静", "P-CNC", "CNC-B200", 2, "active", d(-9), d(300))

    # 班次与排班
    cur.execute("INSERT INTO shifts(work_date,shift_no,label) VALUES(?, 'day', ?)", (d(0), "今日白班"))
    s_day = cur.lastrowid
    cur.execute("INSERT INTO shifts(work_date,shift_no,label) VALUES(?, 'night', ?)", (d(0), "今日夜班"))
    s_night = cur.lastrowid
    cur.execute("INSERT INTO shifts(work_date,shift_no,label) VALUES(?, 'day', ?)", (d(1), "明日白班"))
    s_next = cur.lastrowid

    def slot(shift, eq, heads=1, note=None):
        cur.execute("INSERT INTO shift_slots(shift_id,equipment_id,required_heads,note) VALUES(?,?,?,?)",
                    (shift, eqid[eq], heads, note))
        return cur.lastrowid

    def assign(slot_id, shift, emp, by="周建国", is_temp=0, req=None):
        cur.execute(
            """INSERT INTO shift_assignments(slot_id,shift_id,employee_id,is_temp,request_id,
               created_by,created_at)
               VALUES(?,?,?,?,?,(SELECT id FROM employees WHERE name=?),?)""",
            (slot_id, shift, ename[emp], is_temp, req, by, d(0) + "T08:00"),
        )

    # 白班：3号焊接站=王磊(L3)、装配=刘洋、质检=李娜；1号/2号CNC、4号焊位缺口
    sl = slot(s_day, "EQ-01", 1, "白班缺口：待拖拽合格人员")
    slot(s_day, "EQ-02")
    assign(slot(s_day, "EQ-03"), s_day, "王磊")
    slot(s_day, "EQ-04", 1, "高风险工位，需安全负责人复核替岗")
    assign(slot(s_day, "EQ-05"), s_day, "刘洋")
    assign(slot(s_day, "EQ-06"), s_day, "李娜")

    # 夜班：1号CNC=王磊（跨班次允许）、装配=张晨；4号焊位夜班缺员
    assign(slot(s_night, "EQ-01"), s_night, "王磊")
    slot(s_night, "EQ-02")
    slot(s_night, "EQ-03")
    n4 = slot(s_night, "EQ-04", 1, "夜班缺员：发起替岗申请（样例为无证替岗）")
    assign(slot(s_night, "EQ-05"), s_night, "张晨")
    slot(s_night, "EQ-06")

    # 无证替岗样例：班长周建国申请刘洋（焊接授权已撤销）顶岗4号焊位
    cur.execute(
        """INSERT INTO temp_requests(shift_id,slot_id,employee_id,vacancy_reason,basis,
           unlicensed,requested_by,status,created_at)
           VALUES(?,?,?,
           '原焊工张晨授权到期，夜班无持证焊工可调','none',1,
           (SELECT id FROM employees WHERE name='周建国'),'pending',?)""",
        (s_night, n4, ename["刘洋"], d(0) + "T15:00"),
    )

    # 明日
    assign(slot(s_next, "EQ-02"), s_next, "李娜", by="孙立军")
    slot(s_next, "EQ-06")

    conn.commit()
