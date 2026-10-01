/* 技能矩阵与排班授权 —— 前端逻辑（原生 JS，无依赖） */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  actor: null,
  employees: [],
  matrix: null,
  shifts: [],
  shift: null,
  equipment: null,
  assessments: [],
  trainings: [],
};

// ---------- 工具 ----------
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "' " : "&#39;" }[c]));
}
function roleTags(e) {
  const t = [];
  if (e.is_foreman) t.push('<span class="badge-role">班长</span>');
  if (e.is_safety) t.push('<span class="badge-role">安全</span>');
  if (e.is_trainer) t.push('<span class="badge-role">培训师</span>');
  return t.join("");
}
async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body ? "POST" : "GET"), headers: {} };
  if (opts.body instanceof FormData) {
    init.body = opts.body;
  } else if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = (data.problems || [data.error || `HTTP ${res.status}`]).join("；");
    const err = new Error(msg);
    err.payload = data;
    throw err;
  }
  return data;
}
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toastStack").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}
function dstr(s) { return s ? s.slice(5).replace("-", "/") : ""; } // MM/DD

// ---------- Tabs ----------
$$(".tab").forEach(tab => tab.addEventListener("click", () => {
  $$(".tab").forEach(t => t.classList.toggle("active", t === tab));
  const name = tab.dataset.tab;
  $$(".tab-panel").forEach(p => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "notifications") loadNotifications();
  if (name === "training") loadTrainingData();
  if (name === "schedule") loadShift();
}));

// ---------- 操作人 ----------
function currentEmployee() {
  return state.employees.find(e => e.id === Number($("#actorSelect").value)) || null;
}
async function loadEmployees() {
  state.employees = await api("/api/employees");
  $("#actorSelect").innerHTML = state.employees
    .map(e => `<option value="${e.id}">${esc(e.name)} · ${esc(e.badge)}` +
      `${e.is_foreman ? "（班长）" : e.is_safety ? "（安全负责人）" : e.is_trainer ? "（培训师）" : ""}</option>`)
    .join("");
  // 默认以班长周建国操作
  const foreman = state.employees.find(e => e.badge === "B1001");
  if (foreman) $("#actorSelect").value = foreman.id;
  state.actor = currentEmployee();
}
$("#actorSelect").addEventListener("change", () => {
  state.actor = currentEmployee();
  renderShift();
  renderAssessments();
});

// =========================================================
// 技能矩阵
// =========================================================
function cellClass(c, today, warnDays) {
  if (c.status !== "active") return "cell-off";
  if (c.valid_until < today) return "cell-off";
  const days = (new Date(c.valid_until) - new Date(today)) / 86400000;
  return days <= warnDays ? "cell-warn" : "cell-ok";
}
const STATUS_LABEL = { expired: "已到期", suspended: "已暂停", revoked: "已撤销" };

async function loadMatrix() {
  state.matrix = await api("/api/matrix");
  const { processes, employees, cells, today, warn_days } = state.matrix;
  let html = "<thead><tr><th class='proc-col'>工序 ＼ 人员</th>";
  html += employees.map(e => `<th>${esc(e.name)}${roleTags(e)}<span class="proc-sub">${esc(e.badge)}</span></th>`).join("");
  html += "</tr></thead><tbody>";
  for (const p of processes) {
    html += `<tr><th scope="row">${esc(p.name)}<span class="proc-sub">${esc(p.code)}</span></th>`;
    for (const e of employees) {
      const list = cells[`${p.id}:${e.id}`] || [];
      if (!list.length) {
        html += `<td><span class="cell-empty">— 无授权 —</span></td>`;
        continue;
      }
      html += "<td>";
      for (const c of [...list].sort((a, b) => b.level - a.level)) {
        const cls = cellClass(c, today, warn_days);
        const tag = STATUS_LABEL[c.status]
          ? `<span class="tag">${STATUS_LABEL[c.status]}</span>`
          : cls === "cell-warn" ? '<span class="tag">临期</span>' : "";
        const mdl = c.all_models ? "全型号" : `${c.model_code}`;
        html += `<div class="cell ${cls}" title="${esc(c.note || "")}">
          <span class="lv">L${c.level}</span>${tag}
          <span class="dt">至 ${esc(c.valid_until.slice(2))}</span>
          <span class="mdl">${esc(mdl)}</span></div>`;
      }
      html += "</td>";
    }
    html += "</tr>";
  }
  html += "</tbody>";
  $("#matrixTable").innerHTML = html;
}

// =========================================================
// 排班
// =========================================================
async function loadShifts() {
  state.shifts = await api("/api/shifts");
  $("#shiftSelect").innerHTML = state.shifts
    .map(s => `<option value="${s.id}">${s.work_date} · ${s.shift_no === "day" ? "白班" : "夜班"}（${esc(s.label)}）</option>`)
    .join("");
  const todayStr = new Date().toISOString().slice(0, 10);
  const night = state.shifts.find(s => s.work_date === todayStr && s.shift_no === "night");
  if (night) $("#shiftSelect").value = night.id;
}
$("#shiftSelect").addEventListener("change", loadShift);
$("#refreshShift").addEventListener("click", loadShift);

async function loadShift() {
  const id = $("#shiftSelect").value;
  if (!id) return;
  state.shift = await api(`/api/shift?id=${id}`);
  renderShift();
}

function showConflict(problems) {
  const el = $("#conflictBanner");
  el.hidden = false;
  el.innerHTML = "⚠ " + problems.map(esc).join("；") +
    ' <button class="btn btn-sm" style="float:right" onclick="this.parentNode.hidden=true">知道了</button>';
}

function renderShift() {
  const s = state.shift;
  if (!s) return;
  const actor = state.actor;
  const isForeman = actor && actor.is_foreman;

  const board = $("#board");
  board.innerHTML = s.slots.map(slot => {
    const gap = Math.max(0, slot.gap);
    const assignees = slot.assignments.map(a => {
      const cls = a.is_temp ? (a.eligible_now ? "temp" : "unlicensed") : "";
      const tags = [];
      if (a.is_temp) tags.push('<span class="tag-temp">临时替岗</span>');
      if (a.is_temp && !a.eligible_now) tags.push('<span class="tag-unlic">无证</span>');
      const reasons = (!a.eligible_now && a.reasons.length)
        ? `<div class="reason-mini">${a.reasons.map(esc).join("；")}</div>` : "";
      return `<div class="assignee ${cls}">
          <div><span class="who">${esc(a.name)} <small>${esc(a.badge)}</small></span>${tags.join("")}${reasons}</div>
          <button class="x-btn" title="移出班次" data-unassign="${a.id}">×</button>
        </div>`;
    }).join("");

    const dropzone = gap > 0
      ? `<div class="dropzone" data-slot="${slot.id}">拖拽合格人员到此处（还缺 <b>${gap}</b> 人）</div>` : "";

    const q = slot.qualified.map(c => `
      <div class="cand" draggable="true" data-emp="${c.employee_id}">
        <div><span class="name">${esc(c.name)}</span>
          <div class="small">${esc(c.badge)} · L${c.auth.level} · 至 ${esc(c.auth.valid_until.slice(2))}</div>
          <div class="auth-basis">授权 #${c.auth.id} · ${c.auth.model_id == null ? "全型号覆盖" : "型号匹配"}</div>
        </div>
        <button class="btn btn-sm btn-yellow" data-assign="${c.employee_id}" data-slot="${slot.id}">排入</button>
      </div>`).join("");

    const others = slot.others.map(c => {
      const why = c.busy ? "本班次已占用" : (c.reasons.length ? c.reasons.join("；") : "无授权");
      return `
      <div class="cand cand-other ${c.busy ? "cand-busy" : ""}" ${c.busy ? 'draggable="false"' : ""}
           ${!c.busy ? `draggable="true" data-emp="${c.employee_id}"` : ""}>
        <div><span class="name">${esc(c.name)}</span>
          <div class="small" style="${c.busy ? "" : "color:var(--red-ink)"}">${esc(why)}</div></div>
        <div class="row-actions">
          <button class="btn btn-sm" data-drop="${c.employee_id}" data-slot="${slot.id}"
                  ${isForeman && !c.busy ? "" : "disabled"}
                  title="${isForeman ? "申请替岗（需复核）" : "仅班长可申请替岗"}">申请替岗</button>
        </div>
      </div>`;
    }).join("");

    return `
    <div class="slot-card" data-slot-card="${slot.id}">
      <div class="slot-head">
        <div>
          <div class="slot-title">${esc(slot.eq_code)} ${esc(slot.eq_name)}
            ${slot.high_risk ? '<span class="hr-mark">◆ 高风险</span>' : ""}</div>
          <div class="slot-meta">${esc(slot.process_code)} · ${esc(slot.model_code)} ·
            要求 L${slot.required_level} · 需 ${slot.required_heads} 人
            ${slot.note ? ` · ${esc(slot.note)}` : ""}</div>
        </div>
        <span class="gap-pill ${gap ? "has-gap" : ""}">${gap ? `缺口 ${gap}` : "已配齐"}</span>
      </div>
      <div class="slot-body">
        ${assignees}
        ${dropzone}
        <div class="sub-title"><span>可替人员（合格）</span><span>缺口与候选同屏</span></div>
        ${q || '<div class="hint">暂无可直接排入人员</div>'}
        <div class="sub-title"><span>不可直接排入（可走替岗）</span></div>
        ${others || ""}
      </div>
    </div>`;
  }).join("");

  // 事件绑定
  $$("#board [data-unassign]").forEach(b => b.addEventListener("click", async () => {
    try { await api("/api/unassign", { body: { assignment_id: Number(b.dataset.unassign) } });
      toast("已移出班次", "success"); loadShift();
    } catch (e) { toast(e.message, "error"); }
  }));
  $$("#board [data-assign]").forEach(b => b.addEventListener("click", () =>
    doAssign(Number(b.dataset.emp), Number(b.dataset.slot))));
  $$("#board [data-drop]").forEach(b => b.addEventListener("click", () =>
    openSubstituteModal(Number(b.dataset.slot), Number(b.dataset.drop))));

  // 拖拽
  let dragEmp = null;
  $$("#board .cand[draggable='true']").forEach(el => {
    el.addEventListener("dragstart", ev => {
      dragEmp = Number(el.dataset.emp);
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", String(dragEmp));
    });
  });
  $$("#board .slot-card").forEach(card => {
    card.addEventListener("dragover", ev => { ev.preventDefault(); card.classList.add("drag-over"); });
    card.addEventListener("dragleave", () => card.classList.remove("drag-over"));
    card.addEventListener("drop", async ev => {
      ev.preventDefault();
      card.classList.remove("drag-over");
      const emp = Number(ev.dataTransfer.getData("text/plain") || dragEmp);
      const slotId = Number(card.dataset.slotCard);
      // 优先尝试直接排入；后端判定无资格时提示走替岗流程
      try {
        await doAssign(emp, slotId, { quiet: true });
      } catch (e) {
        if (e.message.includes("冲突") || e.message.includes("待批")) {
          showConflict(e.payload.problems || [e.message]);
        } else {
          openSubstituteModal(slotId, emp, e.message);
        }
      }
    });
  });

  renderRequests();
}

async function doAssign(empId, slotId, opts = {}) {
  try {
    const r = await api("/api/assign", {
      body: { employee_id: empId, slot_id: slotId, actor_id: state.actor.id },
    });
    if (!opts.quiet) toast("已排入工位", "success");
    $("#conflictBanner").hidden = true;
    loadShift();
    return r;
  } catch (e) {
    if (!opts.quiet) {
      if (e.payload && e.payload.problems) showConflict(e.payload.problems);
      else toast(e.message, "error");
    }
    throw e;
  }
}

// ---------- 替岗申请弹窗 ----------
function openSubstituteModal(slotId, empId, preMsg = "") {
  const slot = state.shift.slots.find(x => x.id === slotId);
  const emp = state.employees.find(x => x.id === empId);
  if (!slot || !emp) return;
  const cand = slot.qualified.find(c => c.employee_id === empId);
  const other = slot.others.find(c => c.employee_id === empId);
  const unlicensed = !cand;
  const auths = cand ? [cand.auth] : [];
  const need = slot.required_reviewer === "safety" ? "安全负责人" : "另一名班长（不能是申请人）";

  $("#modalTitle").textContent = "临时替岗申请";
  $("#modalBody").innerHTML = `
    ${preMsg ? `<div class="modal-warn">直接排入失败：${esc(preMsg)}<br>请说明空缺并走替岗复核。</div>` : ""}
    ${unlicensed ? `<div class="modal-warn">⚠ ${esc(emp.name)} 不具备 ${esc(slot.eq_code)}（${esc(slot.model_code)}）的有效授权，
      本次属于<b>无证替岗</b>，将全程留痕，${slot.high_risk ? "高风险设备须安全负责人复核。" : "须另一名班长复核。"}</div>`
      : `<div class="modal-info">${esc(emp.name)} 持有有效授权，替岗后由${need}复核。</div>`}
    <div class="modal-info">工位：${esc(slot.eq_code)} ${esc(slot.eq_name)} · ${esc(slot.model_code)} · 要求 L${slot.required_level}
      ${slot.high_risk ? " · ◆高风险" : ""}</div>
    <label>授权依据
      <select id="basisSelect">
        ${auths.length ? `<option value="auth:${auths[0].id}">授权 #${auths[0].id}（L${auths[0].level}，有效期至 ${auths[0].valid_until}）</option>` : ""}
        <option value="none" ${auths.length ? "" : "selected"}>无有效授权（无证替岗例外登记）</option>
      </select></label>
    <label>原岗位空缺说明（必填）
      <textarea id="vacancyInput" placeholder="如：原焊工张晨授权到期、夜班无其他持证焊工，由刘洋临时顶岗一个班次"></textarea></label>
    <div class="modal-actions">
      <button class="btn" id="mCancel">取消</button>
      <button class="btn btn-yellow" id="mSubmit">提交申请</button>
    </div>`;
  $("#modalMask").hidden = false;
  $("#mCancel").onclick = closeModal;
  $("#mSubmit").onclick = async () => {
    const vacancy = $("#vacancyInput").value.trim();
    if (!vacancy) { toast("必须填写原岗位空缺说明", "error"); return; }
    try {
      const r = await api("/api/temp-requests", { body: {
        slot_id: slotId, employee_id: empId, requester_id: state.actor.id,
        vacancy_reason: vacancy, basis: $("#basisSelect").value,
      }});
      closeModal();
      toast(r.unlicensed ? "无证替岗申请已提交，等待复核" : "替岗申请已提交", r.unlicensed ? "" : "success");
      loadShift();
    } catch (e) { toast(e.message, "error"); }
  };
}
function closeModal() { $("#modalMask").hidden = true; }
$("#modalClose").addEventListener("click", closeModal);
$("#modalMask").addEventListener("click", e => { if (e.target.id === "modalMask") closeModal(); });

// ---------- 替岗申请列表 ----------
function renderRequests() {
  const reqs = state.shift.requests || [];
  const actor = state.actor;
  const hint = $("#reviewRuleHint");
  hint.textContent = "规则：班长发起；普通设备由另一名班长复核，高风险设备（◆）由安全负责人复核；申请人不能自审。";
  if (!reqs.length) { $("#requestList").innerHTML = '<p class="hint">本班次暂无替岗申请。</p>'; return; }

  $("#requestList").innerHTML = reqs.map(r => {
    const needSafety = r.high_risk;
    const selfApplicant = actor && actor.id === r.requested_by;
    let canReview = false; let why = "";
    if (r.status === "pending") {
      if (selfApplicant) why = "申请人不能复核自己的申请";
      else if (needSafety && !(actor && actor.is_safety)) why = "高风险设备，需安全负责人复核";
      else if (!needSafety && !(actor && actor.is_foreman)) why = "需另一名班长复核";
      else canReview = true;
    }
    const acts = r.status === "pending" ? (canReview ? `
      <input type="text" id="note-${r.id}" placeholder="复核意见（可选）">
      <button class="btn btn-sm btn-yellow" data-rv="${r.id}" data-ok="1">批准并落班</button>
      <button class="btn btn-sm" data-rv="${r.id}" data-ok="0">驳回</button>`
      : `<span class="hint">${esc(why)}（当前：${esc(actor ? actor.name : "")}）</span>`)
      : `<span class="review-note">${r.reviewed_by_name ? `复核人：${esc(r.reviewer_name)} ·` : ""} ${esc(r.review_note || "")}</span>`;

    return `<div class="request-card ${r.status} ${r.unlicensed ? "unlicensed" : ""}">
      <div class="request-top">
        <div>
          <div class="request-title">${esc(r.eq_code)} ${esc(r.eq_name)}
            ${r.high_risk ? '<span class="hr-mark">◆高风险</span>' : ""}
            ${r.unlicensed ? '<span class="tag-unlic">无证替岗</span>' : ""}</div>
          <div class="request-meta">拟由 <b>${esc(r.emp_name)}</b>（${esc(r.emp_badge)}）顶岗 ·
            申请人 ${esc(r.foreman_name)} · 依据：${r.basis === "none" ? "无授权（例外）" : esc(r.basis)} ·
            ${r.created_at.replace("T", " ").slice(0, 16)}</div>
        </div>
        <span class="status-pill ${r.status}">${{ pending: "待复核", approved: "已批准", rejected: "已驳回" }[r.status]}</span>
      </div>
      <div class="vacancy">原岗位空缺：${esc(r.vacancy_reason)}</div>
      <div class="request-actions">${acts}</div>
    </div>`;
  }).join("");

  $$("#requestList [data-rv]").forEach(b => b.addEventListener("click", async () => {
    const id = Number(b.dataset.rv);
    const noteEl = $(`#note-${id}`);
    try {
      const r = await api(`/api/temp-requests/${id}/review`, { body: {
        reviewer_id: actor.id, approve: b.dataset.ok === "1",
        note: noteEl ? noteEl.value : "",
      }});
      toast(b.dataset.ok === "1" ? "已批准并排入班次" : "已驳回", "success");
      loadShift();
    } catch (e) { toast(e.message, "error"); }
  }));
}

// =========================================================
// 培训 / 考核
// =========================================================
async function loadEquipmentMeta() {
  state.equipment = await api("/api/equipment");
}
function fillTrainingSelects() {
  const procs = state.matrix.processes;
  const models = state.equipment.models;
  $$('#assessmentForm [name="process_id"], #trainingForm [name="process_id"]').forEach(sel => {
    sel.innerHTML = procs.map(p => `<option value="${p.id}">${esc(p.code)} ${esc(p.name)}</option>`).join("");
  });
  $$('#assessmentForm [name="employee_id"], #trainingForm [name="employee_id"]').forEach(sel => {
    sel.innerHTML = state.employees.map(e => `<option value="${e.id}">${esc(e.name)}（${esc(e.badge)}）</option>`).join("");
  });
  const am = $('#assessmentForm [name="model_id"]');
  am.innerHTML = '<option value="">全型号</option>' +
    models.map(m => `<option value="${m.id}">${esc(m.code)}</option>`).join("");
  const tr = $('#trainingForm [name="trainer_id"]');
  tr.innerHTML = state.employees
    .map(e => `<option value="${e.id}">${esc(e.name)}${e.is_trainer ? "（培训师）" : ""}</option>`)
    .join("");
  const today = new Date().toISOString().slice(0, 10);
  $('#assessmentForm [name="exam_date"]').value = today;
  $('#trainingForm [name="trained_date"]').value = today;
  $('#trainingForm [name="trainer_id"]').value =
    (state.employees.find(e => e.is_trainer) || {}).id || "";
}

async function loadTrainingData() {
  const [asms, trns] = await Promise.all([api("/api/assessments"), api("/api/trainings")]);
  state.assessments = asms; state.trainings = trns;
  renderAssessments(); renderTrainings();
}

function renderAssessments() {
  const actor = state.actor;
  const rows = state.assessments.map(a => {
    const isSelf = actor && actor.id === a.employee_id;
    const recordedSelf = actor && actor.id === a.recorded_by;
    const acts = [];
    if (a.status === "pending") {
      acts.push(`<button class="btn btn-sm ${recordedSelf ? "" : "btn-yellow"}" data-appr="${a.id}"
        ${recordedSelf ? "disabled title='培训老师不能审批自己登记的考核'" : ""}>审批通过</button>`);
    }
    if (a.result === "fail" && isSelf && a.appeal_status !== "pending") {
      acts.push(`<button class="btn btn-sm" data-appeal="${a.id}">成绩申诉</button>`);
    }
    if (a.appeal_status === "pending" && actor && actor.id !== a.recorded_by) {
      acts.push(`<button class="btn btn-sm btn-yellow" data-uphold="${a.id}" data-v="1">申诉成立</button>`);
      acts.push(`<button class="btn btn-sm" data-uphold="${a.id}" data-v="0">驳回申诉</button>`);
    }
    const appealMap = { none: "—", pending: "申诉中", upheld: "申诉成立", rejected: "申诉驳回" };
    const ev = a.evidence_file
      ? `<a href="/attachments/${encodeURIComponent(a.evidence_file)}" target="_blank">${esc(a.evidence_orig || a.evidence_file)}</a>`
      : "—";
    return `<tr>
      <td>${a.exam_date}</td><td>${esc(a.emp_name)}</td><td>${esc(a.process_code)}</td>
      <td>${esc(a.model_code || "全型号")}</td>
      <td><b>${a.score}</b></td>
      <td><span class="pill pill-${a.result}">${a.result === "pass" ? "通过" : "未通过"}</span></td>
      <td><span class="pill pill-${a.status}">${a.status === "approved" ? "已审批" : "待审批"}</span></td>
      <td>${esc(a.recorder_name)} / ${esc(a.approver_name || "—")}</td>
      <td>${ev}</td>
      <td><span class="pill pill-${a.appeal_status === "none" ? "approved" : a.appeal_status}">${appealMap[a.appeal_status]}</span>
        ${a.appeal_reason ? `<div class="hint">${esc(a.appeal_reason)}</div>` : ""}</td>
      <td><div class="row-actions">${acts.join("") || "—"}</div></td>
    </tr>`;
  }).join("");
  $("#assessmentTable").innerHTML = `
    <thead><tr><th>考核日期</th><th>人员</th><th>工序</th><th>型号</th><th>成绩</th>
      <th>结果</th><th>审批</th><th>登记/审批人</th><th>证据附件</th><th>申诉</th><th>操作</th></tr></thead>
    <tbody>${rows}</tbody>`;

  $$("#assessmentTable [data-appr]").forEach(b => b.onclick = async () => {
    try { await api(`/api/assessments/${b.dataset.appr}/approve`, { body: { approver_id: actor.id } });
      toast("考核已审批", "success"); loadTrainingData();
    } catch (e) { toast(e.message, "error"); }
  });
  $$("#assessmentTable [data-appeal]").forEach(b => b.onclick = () => {
    openReasonModal("培训成绩申诉", "说明申诉理由（如评分点异议、设备状态等）", async reason => {
      try { await api(`/api/assessments/${b.dataset.appeal}/appeal`, {
        body: { employee_id: actor.id, reason } });
        toast("申诉已提交，等待裁决", "success"); closeModal(); loadTrainingData();
      } catch (e) { toast(e.message, "error"); }
    });
  });
  $$("#assessmentTable [data-uphold]").forEach(b => b.onclick = () => {
    const uphold = b.dataset.v === "1";
    openReasonModal(uphold ? "裁决：申诉成立" : "裁决：驳回申诉", "裁决说明（可选）", async note => {
      try { await api(`/api/assessments/${b.dataset.uphold}/decide-appeal`, {
        body: { decider_id: actor.id, uphold, note } });
        toast("裁决已记录", "success"); closeModal(); loadTrainingData();
      } catch (e) { toast(e.message, "error"); }
    }, true);
  });
}

function renderTrainings() {
  const rows = state.trainings.map(t => `<tr>
    <td>${t.trained_date}</td><td>${esc(t.emp_name)}</td><td>${esc(t.process_code)}</td>
    <td>${esc(t.course)}</td><td>${t.hours}</td><td>${esc(t.trainer_name || "—")}</td>
    <td>${t.evidence_file ? `<a href="/attachments/${encodeURIComponent(t.evidence_file)}" target="_blank">附件</a>` : "—"}</td>
  </tr>`).join("");
  $("#trainingTable").innerHTML = `
    <thead><tr><th>日期</th><th>人员</th><th>工序</th><th>课程</th><th>课时</th><th>培训师</th><th>证据</th></tr></thead>
    <tbody>${rows}</tbody>`;
}

function openReasonModal(title, label, onOk, optional = false) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = `
    <label>${label}<textarea id="reasonInput"></textarea></label>
    <div class="modal-actions">
      <button class="btn" id="mCancel">取消</button>
      <button class="btn btn-yellow" id="mOk">确定</button>
    </div>`;
  $("#modalMask").hidden = false;
  $("#mCancel").onclick = closeModal;
  $("#mOk").onclick = () => {
    const v = $("#reasonInput").value.trim();
    if (!optional && !v) { toast("请填写内容", "error"); return; }
    onOk(v);
  };
}

function bindUploadForm(formId, path, build) {
  $(formId).addEventListener("submit", async ev => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    try {
      const r = await api(path, { body: fd });
      toast(r.note || "已保存", "success");
      ev.target.reset();
      const today = new Date().toISOString().slice(0, 10);
      $(formId + ' [type="date"]').value = today;
      loadTrainingData();
    } catch (e) { toast(e.message, "error"); }
  });
}

// =========================================================
// 通知
// =========================================================
async function loadNotifications() {
  const list = await api("/api/notifications");
  state.notifications = list;
  const unread = list.filter(n => !n.read_at).length;
  const bell = $("#bellCount");
  bell.hidden = unread === 0; bell.textContent = unread;
  const typeMap = {
    auth_expiring: "即将到期", auth_expired: "已到期",
    temp_request: "替岗申请", temp_reviewed: "替岗结果",
    appeal: "成绩申诉", appeal_done: "申诉结果", model_change: "型号变更",
  };
  $("#notificationList").innerHTML = list.length ? list.map(n => `
    <div class="notif ${n.read_at ? "read" : ""}">
      <div>
        <div class="notif-title">[${typeMap[n.type] || n.type}] ${esc(n.title)}</div>
        <div class="notif-body">${esc(n.body)}</div>
        <div class="notif-meta">${n.created_at.replace("T", " ").slice(0, 16)} ·
          回执 <span class="receipt">${esc(n.receipt)}</span>${n.read_at ? " · 已读" : ""}</div>
      </div>
      ${n.read_at ? "" : `<button class="btn btn-sm" data-read="${n.id}">确认收到</button>`}
    </div>`).join("") : '<p class="hint">暂无通知。</p>';
  $$("#notificationList [data-read]").forEach(b => b.onclick = async () => {
    await api("/api/notifications/read", { body: { id: Number(b.dataset.read) } });
    loadNotifications();
  });
}
$("#bellBtn").addEventListener("click", () => {
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === "notifications"));
  $$(".tab-panel").forEach(p => p.classList.toggle("active", p.id === "tab-notifications"));
  loadNotifications();
});
$("#runSweep").addEventListener("click", async () => {
  try { const r = await api("/api/sweep", { body: {} });
    toast(`扫描完成：到期 ${r.expired} 条，临期提醒 ${r.expiring} 条`, "success");
    loadNotifications(); loadMatrix();
  } catch (e) { toast(e.message, "error"); }
});

// =========================================================
// 启动
// =========================================================
(async function init() {
  await loadEmployees();
  await Promise.all([loadMatrix(), loadEquipmentMeta(), loadShifts()]);
  fillTrainingSelects();
  bindUploadForm("#assessmentForm", "/api/assessments");
  bindUploadForm("#trainingForm", "/api/trainings");
  await loadShift();
  await loadTrainingData();
  await loadNotifications();
  setInterval(loadNotifications, 30000); // 进程内任务配套的轮询
})();
