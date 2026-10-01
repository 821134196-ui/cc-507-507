"use strict";

const state = { employees: [], processes: [], equipment: [], shifts: [], authorizations: [],
                matrix: null, actorId: null, currentShift: null };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const roleName = r => ({ worker: "工人", foreman: "班长", safety: "安全负责人", trainer: "培训老师" }[r] || r);
const shiftLabel = t => t === "day" ? "白班" : "夜班";
const riskTag = r => r === "high"
  ? '<span class="risk-tag risk-high">高风险</span>'
  : '<span class="risk-tag risk-normal">一般</span>';

function toast(title, body, kind = "", actions = []) {
  const host = $("#toast-host");
  const div = document.createElement("div");
  div.className = "toast " + kind;
  div.innerHTML = `<div class="t-title">${esc(title)}</div><div>${esc(body || "")}</div>`;
  if (actions.length) {
    const row = document.createElement("div");
    row.className = "t-actions";
    actions.forEach(a => {
      const b = document.createElement("button");
      b.className = "btn " + (a.cls || "");
      b.textContent = a.label;
      b.onclick = () => { a.run(); div.remove(); };
      row.appendChild(b);
    });
    div.appendChild(row);
  }
  host.appendChild(div);
  setTimeout(() => div.remove(), 9000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok) {
    const err = new Error(data.error || "请求失败 " + res.status);
    err.payload = data;
    throw err;
  }
  return data;
}
const postJSON = (path, obj) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(obj),
});

// ---------- 初始化 ----------
async function init() {
  const s = await api("/api/state");
  Object.assign(state, s);
  state.actorId = state.employees.find(e => e.role === "foreman")?.id || state.employees[0].id;
  buildActorSelect();
  buildStaticSelects();
  renderMatrix();
  renderShiftSelect();
  await loadSchedule(true);
  await loadSubs();
  renderAssessmentsPage();
  await loadNotify();

  $("#actor").addEventListener("change", e => { state.actorId = Number(e.target.value); });
  $$("nav.tabs button").forEach(b => b.addEventListener("click", () => {
    $$("nav.tabs button").forEach(x => x.classList.remove("active"));
    $$(".view").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    $("#view-" + b.dataset.view).classList.add("active");
    if (b.dataset.view === "schedule") loadSchedule();
    if (b.dataset.view === "subs") loadSubs();
    if (b.dataset.view === "training") renderAssessmentsPage();
    if (b.dataset.view === "notify") loadNotify();
  }));
  $("#shift-select").addEventListener("change", () => loadSchedule(true));
  $("#sched-refresh").addEventListener("click", () => loadSchedule());
  $("#sub-apply").addEventListener("click", quickSubstitution);
  $("#subs-refresh").addEventListener("click", loadSubs);
  $("#assessment-form").addEventListener("submit", submitAssessment);
  $("#revoke-btn").addEventListener("click", revokeAuth);
  $("#model-btn").addEventListener("click", changeModel);
  $("#expiry-refresh").addEventListener("click", loadNotify);
  $("#notif-refresh").addEventListener("click", loadNotify);
  $("#notif-pump").addEventListener("click", async () => {
    await postJSON("/api/notify/pump", {});
    await refreshAll();
  });
  // 定时拉取通知状态
  setInterval(() => { if ($("#view-notify").classList.contains("active")) loadNotify(); }, 4000);
}

function buildActorSelect() {
  $("#actor").innerHTML = state.employees
    .map(e => `<option value="${e.id}" ${e.id === state.actorId ? "selected" : ""}>${esc(e.name)}（${roleName(e.role)}）</option>`)
    .join("");
}
function buildStaticSelects() {
  const procs = state.processes.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  $$('select[name="process_id"]').forEach(s => s.innerHTML = procs);
  fillEmployeeSelects();
  // 考核日期默认今天
  $('input[name="exam_date"]').value = new Date().toISOString().slice(0, 10);
  $("#revoke-auth").innerHTML = state.authorizations
    .filter(a => a.status === "active")
    .map(a => `<option value="${a.id}">#${a.id} ${esc(a.employee_name)} / ${esc(a.process_name)} / ${esc(a.model || "全型号")} 至 ${a.valid_to}</option>`)
    .join("");
  $("#model-eq").innerHTML = state.equipment
    .map(e => `<option value="${e.id}">${esc(e.code)} ${esc(e.process_name)}（当前 ${esc(e.model)}）</option>`).join("");
}
function fillEmployeeSelects() {
  const opts = state.employees.map(e => `<option value="${e.id}">${esc(e.name)}</option>`).join("");
  $('select[name="employee_id"]').innerHTML = opts;
  $("#sub-emp").innerHTML = opts;
}

async function refreshAll() {
  const s = await api("/api/state");
  Object.assign(state, s);
  renderMatrix();
  renderShiftSelect();
  await loadSchedule();
  renderAssessmentsPage();
  await loadNotify();
  buildStaticSelects();
}

// ---------- 技能矩阵 ----------
function renderMatrix() {
  const m = state.matrix;
  if (!m) return;
  $("#matrix-date").textContent = "基准日期：" + m.on_date;
  let html = "<thead><tr><th>工序 ＼ 员工</th>" +
    m.employees.map(e => `<th class="emp">${esc(e.name)}<div class="muted" style="font-weight:400">${roleName(e.role)}</div></th>`).join("") +
    "</tr></thead><tbody>";
  m.grid.forEach(row => {
    const p = row.process;
    html += `<tr><td class="proc-cell">${esc(p.name)}${riskTag(p.risk_level)}</td>`;
    row.cells.forEach(c => {
      let inner;
      if (c.status === "ok" || c.status === "expiring") {
        inner = `<span class="cell ${c.status === "expiring" ? "cell-expiring" : ""}">` +
          `<span class="grade">${esc(c.grade)}</span>` +
          `<span class="meta">${esc(c.model_scope)}</span>` +
          `<span class="meta">至 ${c.valid_to}${c.status === "expiring" ? " ⚠" : ""}</span>` +
          (c.note ? `<span class="cell-note">${esc(c.note)}</span>` : "") +
          `</span>`;
      } else if (c.status === "expired") {
        inner = `<span class="cell-expired">已过期<br><span class="meta">${c.valid_to}</span></span>`;
      } else if (c.status === "revoked") {
        inner = `<span class="cell-revoked" title="${esc(c.note)}">已撤销</span>`;
      } else {
        inner = `<span class="cell-none">—</span>`;
      }
      html += `<td class="${c.status === "expiring" ? "cell-expiring" : ""}">${inner}</td>`;
    });
    html += "</tr>";
  });
  html += "</tbody>";
  $("#matrix-table").innerHTML = html;
}

// ---------- 排班 ----------
function renderShiftSelect() {
  const sel = $("#shift-select");
  const prev = sel.value || state.currentShift;
  sel.innerHTML = state.shifts.map(s =>
    `<option value="${s.id}">${s.shift_date} ${shiftLabel(s.shift_type)}</option>`).join("");
  if (prev) sel.value = prev;
  state.currentShift = Number(sel.value);
}

async function loadSchedule(autoloadCandidates) {
  const sid = Number($("#shift-select").value || state.currentShift);
  state.currentShift = sid;
  let view;
  try { view = await api(`/api/shifts/${sid}/view`); }
  catch (e) { toast("加载班次失败", e.message, "error"); return; }
  state.scheduleView = view;

  const gaps = view.items.filter(i => i.status === "vacant");
  const unlicensed = view.items.filter(i => i.status === "unlicensed");
  $("#gap-banner-host").innerHTML =
    (gaps.length || unlicensed.length)
      ? `<div class="gap-banner">本班次 ${gaps.length} 个岗位缺口、${unlicensed.length} 个无证派工；每个槽位下方直接列出可替人员。</div>`
      : `<div class="gap-banner" style="border-color:#bcd6c2;background:var(--green-soft);color:var(--green-ink);">本班次岗位已排满且资格有效。</div>`;

  $("#slots-host").innerHTML = view.items.map(i => {
    const badge = i.status === "ok"
      ? `<span class="badge badge-ok">合格 ${esc(i.auth_grade)}</span>`
      : i.status === "unlicensed"
        ? `<span class="badge badge-danger">无证派工</span>`
        : `<span class="badge badge-muted">缺口</span>`;
    const assignee = i.employee_id
      ? `<div class="slot-assignee">` +
        `<span class="person" draggable="true" data-emp="${i.employee_id}" data-from="${i.assignment_id}" data-from-eq="${i.equipment_id}">${esc(i.employee_name)}</span>` +
        badge +
        `<button class="btn small" data-vacate="${i.assignment_id}">清空</button></div>` +
        (i.status === "unlicensed" ? `<div class="reasons">${i.reasons.map(esc).join("；")}</div>` : "")
      : `<div class="slot-assignee"><span class="badge badge-muted">空缺待派</span></div>`;
    const cands = i.candidates.map(c => personChip(c, i.equipment_id)).join("");
    return `<div class="slot ${i.risk_level === "high" ? "" : ""}" data-equipment="${i.equipment_id}" data-shift="${sid}">
        <div class="slot-head">
          <span class="code">${esc(i.equipment_code)}</span>
          <span class="model">${esc(i.process_name)} · 型号 ${esc(i.model)}</span>
          ${riskTag(i.risk_level)}
        </div>
        ${assignee}
        <div class="candidate-hint">可替人员（拖到本槽位；划线为该班次已在其他设备）：</div>
        <div class="candidate-list">${cands || '<span class="muted">无合格人员</span>'}</div>
      </div>`;
  }).join("");

  // 右侧池子：所有员工（按合格情况分组展示，仍可尝试无证派工）
  const byId = new Map(state.employees.map(e => [e.id, { ...e }]));
  const occupied = new Map();
  view.items.forEach(i => { if (i.employee_id) occupied.set(i.employee_id, i.equipment_code); });
  const chips = state.employees
    .filter(e => e.role === "worker")
    .map(e => {
      const busy = occupied.has(e.id);
      return `<span class="person candidate ${busy ? "busy" : ""}" draggable="true"
          data-emp="${e.id}" title="${busy ? "已派到 " + occupied.get(e.id) : "可拖拽派工"}">${esc(e.name)}${busy ? " · " + occupied.get(e.id) : ""}</span>`;
    }).join("");
  $("#pool-host").innerHTML = `<div class="candidate-list">${chips}</div>
    <p class="muted" style="margin-bottom:0;margin-top:8px;">拖放无证人员到槽位时会被拦截，需走“替岗申请”并经安全负责人复核。</p>`;

  bindDragAndDrop();
  $$("#slots-host [data-vacate]").forEach(b => b.addEventListener("click", async () => {
    await postJSON("/api/assignments/vacate", { assignment_id: Number(b.dataset.vacate) });
    await loadSchedule();
  }));

  $("#sub-eq").innerHTML = view.items.map(i =>
    `<option value="${i.equipment_id}">${esc(i.equipment_code)} ${esc(i.process_name)}（${esc(i.model)}）</option>`).join("");
}

function personChip(c, equipmentId) {
  const title = c.busy ? `本班次已在 ${c.conflict_equipment}，拖入将产生冲突` : "合格可派";
  return `<span class="person candidate ${c.busy ? "busy" : ""}" draggable="true"
      data-emp="${c.employee_id}" data-equipment="${equipmentId}" title="${esc(title)}">
      ${esc(c.name)}<span class="g">${esc(c.grade)}</span>${c.busy ? " · " + esc(c.conflict_equipment) : ""}</span>`;
}

function bindDragAndDrop() {
  let dragged = null;
  document.addEventListener("dragstart", e => {
    const p = e.target.closest(".person[draggable='true']");
    if (!p) return;
    dragged = { emp: Number(p.dataset.emp), from: p.dataset.from || null,
                fromEq: p.dataset.fromEq || null };
    e.dataTransfer.setData("text/plain", String(dragged.emp));
    e.dataTransfer.effectAllowed = "move";
  });
  $$(".slot").forEach(slot => {
    slot.addEventListener("dragover", e => { e.preventDefault(); slot.classList.add("drag-over"); });
    slot.addEventListener("dragleave", () => slot.classList.remove("drag-over"));
    slot.addEventListener("drop", async e => {
      e.preventDefault();
      slot.classList.remove("drag-over");
      if (!dragged) dragged = { emp: Number(e.dataTransfer.getData("text/plain")), from: null, fromEq: null };
      const shiftId = Number(slot.dataset.shift);
      const equipmentId = Number(slot.dataset.equipment);
      await attemptAssign(shiftId, equipmentId, dragged.emp, dragged.from, dragged.fromEq);
      dragged = null;
    });
  });
}

async function attemptAssign(shiftId, equipmentId, employeeId, fromAssignment, fromEquipmentId) {
  try {
    const r = await postJSON("/api/assignments", {
      shift_id: shiftId, equipment_id: equipmentId, employee_id: employeeId,
      foreman_id: state.actorId,
      from_assignment_id: (fromAssignment && Number(fromEquipmentId) !== Number(equipmentId))
        ? Number(fromAssignment) : null,
    });
    (r.warnings || []).forEach(w => toast("派工已记录但带警告", w, "warn"));
    toast("派工成功", "员工已分配到设备");
  } catch (e) {
    if (e.payload?.code === "conflict") {
      const c = e.payload.conflict || {};
      toast("排班冲突（后提交者看到）", e.message, "error", [
        { label: "知道了", cls: "" },
        {
          label: "改为替岗申请", cls: "yellow",
          run: () => {
            $("#sub-emp").value = employeeId;
            $("#sub-eq").value = equipmentId;
            $$("nav.tabs button").find(b => b.dataset.view === "schedule").click();
            $("#sub-reason").focus();
          },
        },
      ]);
    } else if (e.payload?.code === "unlicensed") {
      toast("人员不合格，不能直接派工", (e.payload.reasons || []).join("；"), "error", [
        {
          label: "去发起替岗申请", cls: "yellow",
          run: () => {
            $("#sub-emp").value = employeeId;
            $("#sub-eq").value = equipmentId;
            $("#sub-reason").focus();
          },
        },
      ]);
    } else {
      toast("派工失败", e.message, "error");
    }
  }
  await loadSchedule();
}

async function quickSubstitution() {
  const actor = state.employees.find(e => e.id === state.actorId);
  if (actor.role !== "foreman") {
    toast("只有班长可以申请替岗", "请在右上角切换为班长身份（陈晨 / 李立）", "error");
    return;
  }
  const reason = $("#sub-reason").value.trim();
  if (!reason) { toast("需要填写原岗位空缺说明", "", "error"); return; }
  try {
    const r = await postJSON("/api/substitutions", {
      shift_id: state.currentShift,
      equipment_id: Number($("#sub-eq").value),
      applicant_emp: Number($("#sub-emp").value),
      applied_by: state.actorId,
      vacancy_reason: reason,
    });
    toast(r.unlicensed ? "无证替岗申请已提交，转安全负责人复核" : "替岗申请已提交", "", r.unlicensed ? "warn" : "");
    $("#sub-reason").value = "";
  } catch (e) {
    const label = e.payload?.code === "conflict" ? "排班冲突" : "替岗申请失败";
    toast(label, e.message, "error");
  }
  await refreshAll();
}

// ---------- 替岗申请列表 ----------
async function loadSubs() {
  const data = await api("/api/substitutions");
  const statusBadge = s => ({
    pending: '<span class="badge badge-warn">待复核</span>',
    approved: '<span class="badge badge-ok">已批准</span>',
    rejected: '<span class="badge badge-danger">已驳回</span>',
  }[s]);
  $("#subs-table").innerHTML = `<thead><tr>
    <th>#</th><th>班次</th><th>设备 / 工序</th><th>原岗位空缺</th><th>拟替人员</th>
    <th>授权依据</th><th>申请班长</th><th>状态</th><th>复核人 / 意见</th><th>操作</th></tr></thead><
tbody>` + data.items.map(s => {
    const basis = s.authorization_basis === "unlicensed"
      ? '<span class="badge badge-danger">无证（须安全复核）</span>'
      : `授权 #${s.authorization_basis}`;
    let actions = "";
    if (s.status === "pending") {
      actions = `<button class="btn small primary" data-approve="${s.id}">批准并派工</button>
                 <button class="btn small danger-line" data-reject="${s.id}">驳回</button>`;
    }
    return `<tr class="${s.authorization_basis === "unlicensed" ? "unlicensed-row" : ""}">
      <td>${s.id}</td>
      <td>${s.shift_date} ${shiftLabel(s.shift_type)}</td>
      <td>${esc(s.equipment_code)} · ${esc(s.process_name)} ${riskTag(s.risk_level)}<div class="muted">型号 ${esc(s.model)}</div></td>
      <td>${esc(s.vacancy_reason)}</td>
      <td>${esc(s.applicant_name)}</td>
      <td>${basis}</td>
      <td>${esc(s.applied_by_name)}</td>
      <td>${statusBadge(s.status)}</td>
      <td>${s.reviewed_by_name ? esc(s.reviewed_by_name) + '<div class="muted">' + esc(s.review_note || "") + "</div>" : "—"}</td>
      <td>${actions}</td>
    </tr>`;
  }).join("");
  $$("#subs-table [data-approve],#subs-table [data-reject]").forEach(b => b.addEventListener("click", async () => {
    const approve = b.hasAttribute("data-approve");
    const id = b.dataset.approve || b.dataset.reject;
    const note = prompt(approve ? "复核意见（可留空）：" : "驳回原因：", approve ? "" : "资格不符") ?? "";
    if (!approve && !note) return;
    try {
      const r = await postJSON(`/api/substitutions/${id}/review`,
        { reviewer_id: state.actorId, approve, note });
      toast("已" + (approve ? "批准并派工" : "驳回"), (r.warnings || []).join("；"), approve ? "" : "warn");
    } catch (e) { toast("复核失败", e.message, "error"); }
    await refreshAll();
  }));
}

// ---------- 培训证据 ----------
async function submitAssessment(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  fd.delete("evidence");
  const file = ev.target.evidence.files[0];
  if (file) fd.append("evidence", file);
  fd.append("recorder_id", state.actorId);
  try {
    const r = await fetch("/api/assessments", { method: "POST", body: fd }).then(async res => {
      const d = await res.json();
      if (!res.ok) throw new Error(d.error);
      return d;
    });
    toast("考核已登记", "证据：" + (r.evidence_name || "无附件") + "；等待他人审批后签发授权");
    ev.target.reset();
    $('input[name="exam_date"]').value = new Date().toISOString().slice(0, 10);
  } catch (e) { toast("登记失败", e.message, "error"); }
  await refreshAll();
}

function renderAssessmentsPage() {
  // 拉取最新考核
  api("/api/assessments").then(data => {
    state.assessments = data.items;
    const appealPill = a => ({
      none: "",
      pending: '<span class="appeal-pill appeal-pending">申诉中</span>',
      approved: '<span class="appeal-pill appeal-approved">申诉成立</span>',
      rejected: '<span class="appeal-pill appeal-rejected">申诉驳回</span>',
    }[a.appeal_status]);
    $("#assess-table").innerHTML = `<thead><tr>
      <th>#</th><th>员工</th><th>工序/型号</th><th>等级</th><th>分数</th><th>结果</th>
      <th>考核日期</th><th>登记人</th><th>证据</th><th>审批</th><th>操作</th></tr></thead>
      <tbody>` + data.items.map(a => {
        const passed = a.pass ? '<span class="badge badge-ok">通过</span>'
                              : '<span class="badge badge-danger">未通过</span>';
        const ev = a.evidence_name
          ? `<a class="evidence-link" href="/attachments/${a.evidence_path.split("/").pop()}" download="${esc(a.evidence_name)}">${esc(a.evidence_name)}</a>`
          : '<span class="muted">无</span>';
        const approval = a.approved_by_name
          ? esc(a.approved_by_name) + (a.pass ? '<div class="muted">已签发授权</div>' : "")
          : '<span class="muted">待审批</span>';
        const acts = [];
        if (!a.approved_by) acts.push(`<button class="btn small" data-approve-a="${a.id}">审批</button>`);
        if (a.appeal_status === "none" || a.appeal_status === "rejected")
          acts.push(`<button class="btn small" data-appeal="${a.id}" data-emp="${a.employee_id}">成绩申诉</button>`);
        if (a.appeal_status === "pending")
          acts.push(`<button class="btn small yellow" data-resolve="${a.id}" data-over="1">申诉成立</button>
                     <button class="btn small" data-resolve="${a.id}" data-over="0">维持原成绩</button>`);
        return `<tr>
          <td>${a.id}</td><td>${esc(a.employee_name)}</td>
          <td>${esc(a.process_name)} / ${esc(a.model || "通用")}</td>
          <td>${esc(a.grade)}</td>
          <td>${a.score}</td><td>${passed}${appealPill(a)}</td>
          <td>${a.exam_date}</td><td>${esc(a.recorded_by_name)}</td>
          <td>${ev}</td><td>${approval}</td><td>${acts.join(" ")}</td>
        </tr>`;
      }).join("") + "</tbody>";

    $$("#assess-table [data-approve-a]").forEach(b => b.onclick = async () => {
      try {
        await postJSON(`/api/assessments/${b.dataset.approveA}/approve`,
          { approver_id: state.actorId });
        toast("考核已审批，通过项已签发授权");
      } catch (e) { toast("审批被拒绝", e.message, "error"); }
      await refreshAll();
    });
    $$("#assess-table [data-appeal]").forEach(b => b.onclick = async () => {
      const note = prompt("申诉理由：", "现场评分与登记不一致，申请复核") || "";
      if (!note) return;
      try {
        await postJSON(`/api/assessments/${b.dataset.appeal}/appeal`,
          { employee_id: Number(b.dataset.emp), note });
        toast("申诉已提交，等待安全/培训（非登记人）处理", "", "warn");
      } catch (e) { toast("申诉失败", e.message, "error"); }
      await refreshAll();
    });
    $$("#assess-table [data-resolve]").forEach(b => b.onclick = async () => {
      const note = prompt("复核结论：", b.dataset.over === "1" ? "评分表确有差错，改判通过" : "成绩无误，维持") || "";
      if (!note) return;
      try {
        const r = await postJSON(`/api/assessments/${b.dataset.resolve}/appeal/resolve`,
          { reviewer_id: state.actorId, overturn: b.dataset.over === "1", note });
        toast("申诉处理完成：" + r.appeal_status);
      } catch (e) { toast("处理失败", e.message, "error"); }
      await refreshAll();
    });
  });
}

async function revokeAuth() {
  const id = Number($("#revoke-auth").value);
  const reason = $("#revoke-reason").value.trim();
  if (!id || !reason) { toast("请选择授权并填写撤销原因", "", "error"); return; }
  try {
    await postJSON(`/api/authorizations/${id}/revoke`,
      { reason, operator_id: state.actorId });
    toast("授权已临时撤销，相关人员将立即失去该设备派工资格", "", "warn");
    $("#revoke-reason").value = "";
  } catch (e) { toast("撤销失败", e.message, "error"); }
  await refreshAll();
}

async function changeModel() {
  const id = Number($("#model-eq").value);
  const model = $("#model-new").value.trim();
  if (!model) { toast("请填写新型号", "", "error"); return; }
  try {
    const r = await postJSON(`/api/equipment/${id}/model`,
      { model, operator_id: state.actorId });
    if (r.affected.length)
      toast("型号已变更", r.affected.length + " 人失去资格：" + r.affected.map(a => a.name).join("、"), "warn");
    else toast("型号已变更，无人员资格受影响");
    $("#model-new").value = "";
  } catch (e) { toast("变更失败", e.message, "error"); }
  await refreshAll();
}

// ---------- 到期 / 通知 ----------
async function loadNotify() {
  const [ex, nf] = await Promise.all([api("/api/expiring"), api("/api/notifications")]);
  $("#expiry-table").innerHTML = `<thead><tr><th>员工</th><th>工序</th><th>等级</th><th>型号</th><th>到期日</th></tr></thead>
    <tbody>` + (ex.items.length ? ex.items.map(a =>
      `<tr><td>${esc(a.employee_name)}</td><td>${esc(a.process_name)}</td><td>${esc(a.grade)}</td>
       <td>${esc(a.model || "全型号")}</td><td>${a.valid_to}</td></tr>`).join("")
      : '<tr><td colspan="5" class="muted">15 天内无到期授权</td></tr>') + "</tbody>";
  $("#notif-host").innerHTML = nf.items.length ? nf.items.map(n => `
    <div class="notif-item">
      <span class="notif-kind kind-${n.kind}">${esc(n.kind)}</span>
      <strong>${esc(n.title)}</strong>
      <span class="notif-time" style="float:right">${n.created_at.replace("T", " ")}</span>
      <div style="margin:4px 0">${esc(n.body)}</div>
      <div class="muted">状态：${n.status === "delivered"
        ? `已送达 <span class="receipt">${esc(n.receipt)}</span> @ ${n.delivered_at.replace("T", " ")}`
        : "排队中（进程内任务每 2 秒投递一轮）"}</div>
    </div>`).join("") : '<p class="muted">暂无通知</p>';
}

init().catch(e => toast("初始化失败", e.stack || e.message, "error"));
