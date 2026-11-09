/* MovieCreator 웹 UI (의존성 없음) */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const app = $("#app");
const state = { info: null, project: null, jobs: [], lastDone: new Set(), polling: null };

// ───────── 유틸 ─────────
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fileUrl = (proj, path, dl) => `/files/${encodeURIComponent(proj)}/${path.split("/").map(encodeURIComponent).join("/")}${dl ? "?download=1" : ""}`;
const fmtMin = (sec) => {
  if (!sec) return "";
  if (sec < 90) return `약 ${Math.max(1, Math.round(sec / 10) * 10)}초`;
  const m = Math.round(sec / 60);
  return m < 90 ? `약 ${m}분` : `약 ${(sec / 3600).toFixed(1)}시간`;
};
const fmtElapsed = (s) => (s < 60 ? `${s}초` : `${Math.floor(s / 60)}분 ${s % 60}초`);
const arBox = (ar) => { const [w, h] = ar.split(":").map(Number); const k = 14 / Math.max(w, h); return `<span class="ar-icon" style="width:${Math.round(w * k)}px;height:${Math.round(h * k)}px"></span>`; };
const arCss = (ar) => ar.replace(":", "/");

function toast(msg, err = false, ms = 3200) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

async function api(path, opts = {}) {
  const o = { headers: {}, ...opts };
  if (o.body && !(o.body instanceof FormData)) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(o.body); }
  const r = await fetch(path, o);
  if (r.status === 401 && path !== "/api/login") { renderLogin(); throw new Error("로그인이 필요합니다"); }
  let data = null;
  try { data = await r.json(); } catch { /* 빈 응답 */ }
  if (!r.ok) throw new Error((data && data.detail) || `오류 (${r.status})`);
  return data;
}
const guard = (fn) => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, true, 5000); } };

// ───────── 라우팅 ─────────
async function route() {
  const h = location.hash.replace(/^#/, "") || "/";
  const m = h.match(/^\/p\/(.+)$/);
  try {
    if (!state.info) state.info = await api("/api/info");
  } catch { return; }
  $("#logoutBtn").hidden = false;
  startPolling();
  if (m) return renderEditor(decodeURIComponent(m[1]));
  if (h === "/new") return renderNew();
  return renderHome();
}
window.addEventListener("hashchange", route);

// ───────── 로그인 ─────────
function renderLogin() {
  stopPolling();
  $("#logoutBtn").hidden = true;
  $("#jobsBtn").hidden = true;
  app.innerHTML = `
    <form class="login" id="loginForm">
      <span class="logo">▶</span>
      <h1>MovieCreator</h1>
      <p class="muted small">로컬 AI 홍보 영상 스튜디오. 서버를 켤 때 터미널에 표시된 비밀번호를 입력하세요.</p>
      <input type="password" id="pw" placeholder="비밀번호" autocomplete="current-password" required autofocus>
      <button class="primary">로그인</button>
    </form>`;
  $("#loginForm").onsubmit = guard(async (e) => {
    e.preventDefault();
    await api("/api/login", { method: "POST", body: { password: $("#pw").value } });
    state.info = null;
    route();
  });
}
$("#logoutBtn").onclick = guard(async () => { await api("/api/logout", { method: "POST" }); renderLogin(); });

// ───────── 홈 ─────────
async function renderHome() {
  state.project = null;
  state.info = await api("/api/info");
  const list = await api("/api/projects");
  const bad = state.info.doctor.filter((d) => !d.ok && !d.name.includes("선택"));
  app.innerHTML = `
    <div class="hero">
      <div><h1>내 영상 프로젝트</h1>
      <div class="muted small">프롬프트·사진·동영상으로 홍보 영상을 만듭니다. 생성은 이 맥북에서 직접 돌아갑니다.</div></div>
      <a href="#/new"><button class="primary">+ 새 영상 만들기</button></a>
    </div>
    ${engineLine()}
    ${bad.length ? `<div class="notice" style="margin-bottom:14px"><b>설치 확인 필요:</b> ${bad.map((d) => esc(d.name) + " — " + esc(d.detail)).join(" · ")}</div>` : ""}
    ${list.length ? `<div class="grid">${list.map((p) => `
      <a class="pcard" href="#/p/${encodeURIComponent(p.name)}">
        <div class="cover" style="${p.cover ? `background-image:url('${fileUrl(p.name, p.cover)}')` : ""}">${p.cover ? "" : "미리보기 없음"}</div>
        <div class="meta"><strong>${esc(p.title)}</strong>
          <span class="muted small">${arBox(p.aspect)}${esc(p.aspect)} · 컷 ${p.shots}개 · ${Math.round(p.seconds)}초</span></div>
      </a>`).join("")}</div>`
    : `<div class="empty-state">아직 프로젝트가 없습니다.<br><br><a href="#/new"><button class="primary">첫 영상 만들기</button></a></div>`}`;
  const ub = $("#unloadBtn");
  if (ub) ub.onclick = guard(async () => { await api("/api/engine/unload", { method: "POST" }); toast("모델을 메모리에서 내렸습니다. 다음 생성 때 다시 올립니다"); setTimeout(renderHome, 1500); });
}

function engineLine() {
  const e = state.info.engine || {};
  if (!e.running) return `<div class="notice" style="margin-bottom:14px">생성 엔진 대기 중 · 첫 생성 때 모델을 메모리에 올립니다 (이후 계속 상주)</div>`;
  const m = e.dit ? (e.dit.includes("fastwan") ? "빠른 모델" : "원본 모델") : "없음";
  return `<div class="notice" style="margin-bottom:14px">생성 엔진 실행 중 · 메모리에 올라간 모델: <b>${m}</b>${e.t5 ? " + 텍스트 인코더" : ""} · 사용 메모리 ${e.active_gb ?? "?"}GB
    <button class="small ghost" id="unloadBtn" style="margin-left:8px">메모리에서 내리기</button></div>`;
}

// ───────── 새 프로젝트 ─────────
function renderNew() {
  const sel = { aspect: "9:16", template: "blank" };
  const tpls = state.info.templates;
  const order = ["blank", "product", "place", "travel"].filter((k) => tpls[k]).concat(Object.keys(tpls).filter((k) => !["blank", "product", "place", "travel"].includes(k)));
  app.innerHTML = `
    <a href="#/" class="muted small">← 프로젝트 목록</a>
    <h1>새 영상 만들기</h1>
    <div class="panel" style="display:grid;gap:18px;margin-top:14px">
      <label class="f">제목<input type="text" id="nTitle" placeholder="예: 카페 오픈 홍보" maxlength="60"></label>
      <div><div class="muted small" style="margin-bottom:6px">화면 비율</div>
        <div class="choices" id="arChoices">${Object.entries(state.info.aspects).map(([k, v]) => `
          <button class="choice ${k === sel.aspect ? "on" : ""}" data-ar="${k}"><b>${arBox(k)}${esc(k)}</b><span>${esc(v.label)} · ${v.output[0]}×${v.output[1]}</span></button>`).join("")}</div></div>
      <div><div class="muted small" style="margin-bottom:6px">시작 방식</div>
        <div class="choices" id="tplChoices">${order.map((k) => `
          <button class="choice ${k === sel.template ? "on" : ""}" data-tpl="${k}"><b>${esc(tpls[k].title)}</b><span>${esc(tpls[k].description || "")}</span></button>`).join("")}</div></div>
      <div class="btns"><button class="primary" id="createBtn">만들기</button><a href="#/"><button class="ghost">취소</button></a></div>
    </div>`;
  app.querySelectorAll("[data-ar]").forEach((b) => (b.onclick = () => { sel.aspect = b.dataset.ar; app.querySelectorAll("[data-ar]").forEach((x) => x.classList.toggle("on", x === b)); }));
  app.querySelectorAll("[data-tpl]").forEach((b) => (b.onclick = () => { sel.template = b.dataset.tpl; app.querySelectorAll("[data-tpl]").forEach((x) => x.classList.toggle("on", x === b)); }));
  $("#createBtn").onclick = guard(async () => {
    const title = $("#nTitle").value.trim();
    const p = await api("/api/projects", { method: "POST", body: { title, template: sel.template, aspect: sel.aspect } });
    location.hash = `#/p/${encodeURIComponent(p.name)}`;
  });
}

// ───────── 편집기 ─────────
async function renderEditor(name, keepScroll = false) {
  const y = window.scrollY;
  const p = await api(`/api/projects/${encodeURIComponent(name)}`);
  state.project = p;
  const info = state.info;
  const busyBySid = {};
  state.jobs.filter((j) => j.project === name && ["queued", "running"].includes(j.status)).forEach((j) => { (busyBySid[j.sid || "_reel"] ||= []).push(j); });
  const musicName = (f) => f.split("/").pop().replace(/_[0-9a-f]{4}(\.\w+)$/, "$1");

  app.innerHTML = `
    <a href="#/" class="muted small">← 프로젝트 목록</a>
    <div class="ed-head">
      <input class="ed-title" id="pTitle" value="${esc(p.title)}" maxlength="60" aria-label="제목">
      <div class="ed-bar">
        <label class="f">화면 비율<select id="pAspect">${Object.entries(info.aspects).map(([k, v]) => `<option value="${k}" ${k === p.aspect ? "selected" : ""}>${esc(v.label)}</option>`).join("")}</select></label>
        <label class="f">생성 모델<select id="pModel">${Object.entries(info.models).map(([k, v]) => `<option value="${k}" ${k === p.model ? "selected" : ""} ${v.available ? "" : "disabled"}>${esc(v.label)}${v.available ? "" : " (미설치)"}</option>`).join("")}</select></label>
        <label class="f">색감<select id="pGrade">${Object.entries(info.grades).map(([k, v]) => `<option value="${k}" ${k === p.grade ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
        <label class="f">배경 음악<select id="pMusic"><option value="">없음</option>${p.music_files.map((f) => `<option value="${esc(f)}" ${f === p.music ? "selected" : ""}>${esc(musicName(f))}</option>`).join("")}<option value="__upload">+ 음악 파일 올리기…</option></select></label>
      </div>
      <details class="more"><summary>전체 스타일 문구 (모든 AI 컷 프롬프트 뒤에 붙음)</summary>
        <textarea id="pStyle" rows="2" placeholder="예: Cinematic commercial film, warm color grade, no text, no watermark.">${esc(p.style_suffix)}</textarea>
      </details>
    </div>

    <div class="summary">
      <div><b>${p.shots.length}컷 · ${Math.round(p.total_seconds * 10) / 10}초</b>
        <span class="muted small"> → 최종 ${p.output[0]}×${p.output[1]} · 30fps MP4</span></div>
      <div class="btns" style="align-items:center">
        <select id="autoQ" style="width:auto">
          <option value="standard">표준 화질 (${fmtMin(p.auto_estimates.standard)})</option>
          <option value="final">고화질 (${fmtMin(p.auto_estimates.final)})</option></select>
        <button class="primary" id="autoBtn" ${busyBySid._reel ? "disabled" : ""}>${busyBySid._reel ? "최종 영상 만드는 중…" : "바로 최종 영상 만들기"}</button>
        <button class="ghost small" id="reelBtn" ${busyBySid._reel ? "disabled" : ""} title="이미 만든 컷 영상으로 합치기만">합치기만</button>
      </div>
    </div>
    <p class="muted small"><b>바로 최종 영상 만들기</b>: 영상이 없는 AI 컷을 모두 생성한 뒤 30fps·음악까지 합쳐 완성본을 만듭니다. 시안 확인은 필요 없습니다.
      AI 컷은 프롬프트만 쓰면 텍스트로, 사진을 넣으면 그 사진에서 움직이는 영상으로 만듭니다. 프롬프트는 영어가 가장 잘 됩니다.</p>

    <div class="shots" id="shots">${p.shots.map((s, i) => (s.type === "video" ? videoCard(p, s, i, busyBySid[s.id]) : aiCard(p, s, i, busyBySid[s.id]))).join("")}</div>

    <div class="add-bar">
      <button data-add="ai"><b>+ 프롬프트 컷</b><span>글로 장면을 설명해서 생성</span></button>
      <button data-add="photos"><b>+ 사진으로 컷 추가</b><span>여러 장 선택 가능 · 사진마다 컷 1개</span></button>
      <button data-add="video"><b>+ 내 동영상 넣기</b><span>그대로 쓰거나 이어지는 장면을 AI로</span></button>
    </div>

    <h2>완성된 영상</h2>
    ${p.finals.length ? `<div class="finals">${p.finals.map((f) => `
      <div class="final"><video src="${fileUrl(name, f.file)}#t=0.1" controls playsinline preload="metadata" style="aspect-ratio:${arCss(p.aspect)}"></video>
        <div class="row"><span class="muted small">${new Date(f.time * 1000).toLocaleString("ko-KR")} · ${(f.size / 1048576).toFixed(1)}MB</span></div>
        <div class="btns"><a href="${fileUrl(name, f.file, 1)}"><button class="small primary">다운로드</button></a>
          <button class="small ghost danger" data-delfile="${esc(f.file)}">삭제</button></div></div>`).join("")}</div>`
    : `<div class="notice">모든 컷에 영상이 준비되면 <b>최종 영상 만들기</b>를 누르세요. 30fps 보간·해상도 맞춤·음악까지 자동으로 처리합니다.</div>`}

    <details class="more" style="margin-top:28px"><summary>프로젝트 삭제</summary>
      <button class="danger small" id="delProject">이 프로젝트 삭제 (휴지 폴더로 이동)</button></details>`;

  if (keepScroll) window.scrollTo(0, y);
  bindEditor(p);
}

function mediaFor(p, s) {
  const style = `style="aspect-ratio:${arCss(p.aspect)}"`;
  if (s.type === "video") {
    if (!s.video) return `<div class="media empty" ${style} data-pick="video" data-sid="${s.id}">동영상을 올려주세요<br>(클릭 또는 끌어다 놓기)</div>`;
    return `<div class="media" ${style}><video src="${fileUrl(p.name, s.video)}#t=${Math.max(0.1, +s.start || 0)}" controls playsinline preload="metadata" muted></video><span class="tag">내 영상</span></div>`;
  }
  if (s.motion === "camera" && s.keyframe) {
    if (s.camera_preview) { const [f, q] = s.camera_preview.split("?"); return `<div class="media" ${style}><video src="${fileUrl(p.name, f)}?${q}" controls playsinline loop muted autoplay></video><span class="tag">카메라 무빙</span></div>`; }
    return `<div class="media" ${style}><img src="${fileUrl(p.name, s.keyframe)}" alt="사진"><span class="tag">사진 그대로 · 카메라 무빙</span></div>`;
  }
  if (s.take) return `<div class="media" ${style}><video src="${fileUrl(p.name, s.take)}#t=0.1" controls playsinline loop muted preload="metadata" ${s.keyframe ? `poster="${fileUrl(p.name, s.keyframe)}"` : ""}></video><span class="tag">채택</span></div>`;
  if (s.keyframe) return `<div class="media" ${style}><img src="${fileUrl(p.name, s.keyframe)}" alt="키 이미지"><span class="tag">사진</span></div>`;
  return `<div class="media empty" ${style} data-pick="photo" data-sid="${s.id}">사진 없이 프롬프트로 생성<br><br>사진을 넣으려면 클릭 또는 끌어다 놓기</div>`;
}

function busyBlock(jobs) {
  if (!jobs || !jobs.length) return "";
  const run = jobs.find((j) => j.status === "running");
  const n = jobs.length;
  if (run) return `<div class="busy" data-job="${run.id}">⏳ ${esc(run.stage)}${n > 1 ? ` · 대기 ${n - 1}개` : ""}<div class="bar"><i style="width:${Math.round(run.progress * 100)}%"></i></div></div>`;
  return `<div class="busy">⏸ 대기 중 ${n}개 (앞 작업이 끝나면 시작)</div>`;
}

function shotHeader(s, i, total) {
  return `<div class="shot-num"><span class="n">#${i + 1}</span>
    <input type="text" data-f="role" value="${esc(s.role)}" placeholder="컷 이름" style="flex:1;padding:5px 9px">
    <button class="small ghost" data-move="-1" ${i === 0 ? "disabled" : ""} title="위로">↑</button>
    <button class="small ghost" data-move="1" ${i === total - 1 ? "disabled" : ""} title="아래로">↓</button>
    <button class="small ghost danger" data-delshot title="삭제">✕</button></div>`;
}

function aiCard(p, s, i, jobs) {
  const est = s.estimates || {};
  return `<section class="shot" data-sid="${s.id}">
    ${mediaFor(p, s)}
    <div class="shot-body">
      ${shotHeader(s, i, p.shots.length)}
      <label class="f">장면 설명 (프롬프트)
        <textarea data-f="prompt" rows="3" placeholder="예: A matte black tumbler on wet slate, steam rising, slow orbit camera, warm rim light.">${esc(s.prompt)}</textarea></label>
      <div class="row">
        <label class="f" style="flex:0 1 110px">길이(초, 1~5)<input type="number" data-f="seconds" min="1" max="5" step="0.5" value="${s.seconds}"></label>
        <label class="f" style="flex:0 1 130px">시드 (0=랜덤)<input type="number" data-f="seed" min="0" value="${s.seed || 0}"></label>
        <div class="shot-tools" style="flex:1 1 220px;align-self:end">
          <button class="small" data-pick="photo" data-sid="${s.id}">${s.keyframe ? "사진 바꾸기" : "사진 넣기"}</button>
          ${s.keyframe ? `<button class="small ghost" data-clearkf>사진 빼기</button>` : ""}
          <button class="small ghost" data-kfgen title="mflux로 키 이미지 생성 (처음엔 모델 다운로드)">AI로 사진 만들기</button>
        </div>
      </div>
      ${s.keyframe ? `<div class="row">
        <label class="f">움직임 방식<select data-f="motion">
          <option value="camera" ${s.motion === "camera" ? "selected" : ""}>사진 그대로 + 카메라 무빙 (즉시, 실사 유지)</option>
          <option value="ai" ${s.motion !== "camera" ? "selected" : ""}>AI로 움직이기 (원본 모델, 느림)</option></select></label>
        ${s.motion === "camera" ? `<label class="f">카메라 무빙<select data-f="camera">${Object.entries(state.info.camera_moves).map(([k, v]) => `<option value="${k}" ${k === s.camera ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
        <div style="flex:0 1 auto;align-self:end"><button class="small" data-campreview>미리보기</button></div>` : ""}
      </div>` : ""}
      ${s.motion === "camera" && s.keyframe ? `<div class="muted small">AI 생성 없이 사진을 그대로 쓰고 카메라만 움직입니다. 최종 영상 만들 때 자동으로 처리됩니다.</div>` : `
      ${s.keyframe && s.model_used === "base" ? `<div class="muted small">사진 컷은 사진을 유지하기 위해 원본 모델로 생성합니다 (컷당 수 분).</div>` : ""}
      <div class="gen-btns">
        ${["draft", "standard", "final"].map((q) => `<button class="${q === "draft" ? "primary" : ""}" data-gen="${q}">
          ${esc(state.info.qualities[q].label)} 생성<small>${fmtMin(est[q])}</small></button>`).join("")}
      </div>
      ${busyBlock(jobs)}
      ${s.takes && s.takes.length ? `<div><div class="muted small" style="margin-bottom:6px">만든 영상 ${s.takes.length}개 · 하나를 채택하면 최종 영상에 쓰입니다</div>
        <div class="takes">${s.takes.map((t) => `<div class="take ${t.file === s.take ? "on" : ""}">
          <video src="${fileUrl(p.name, t.file)}#t=0.1" muted loop playsinline preload="metadata" onmouseenter="this.play()" onmouseleave="this.pause()" onclick="this.paused?this.play():this.pause()"></video>
          <div class="tl"><span>${esc(state.info.qualities[t.quality]?.label || t.quality)}${t.model === "fast" ? "·빠름" : ""}</span><span>#${t.seed ?? ""}</span></div>
          <div class="tb">${t.file === s.take ? `<button disabled>✓ 채택</button>` : `<button data-take="${esc(t.file)}">채택</button>`}
            <button class="ghost" data-seed="${t.seed ?? 0}" title="이 시드로 고정">시드</button>
            <button class="ghost danger" data-delfile="${esc(t.file)}" title="삭제">✕</button></div></div>`).join("")}</div></div>` : ""}
      <div class="shot-tools">${s.take ? `<button class="small ghost" data-derive>이 영상 끝에서 이어지는 컷 만들기</button>` : ""}</div>`}
    </div></section>`;
}

function videoCard(p, s, i, jobs) {
  const dur = s.video_duration || 0;
  return `<section class="shot" data-sid="${s.id}">
    ${mediaFor(p, s)}
    <div class="shot-body">
      ${shotHeader(s, i, p.shots.length)}
      ${s.video ? `<div class="muted small">원본 길이 ${dur.toFixed(1)}초 · 시작 지점부터 정한 길이만큼 사용합니다 (소리는 쓰지 않고 배경 음악으로 대체)</div>
      <div class="row">
        <label class="f">시작 지점(초)<input type="number" data-f="start" min="0" max="${Math.max(0, dur - 0.5).toFixed(1)}" step="0.1" value="${s.start || 0}"></label>
        <label class="f">사용 길이(초)<input type="number" data-f="seconds" min="1" max="60" step="0.5" value="${s.seconds}"></label>
        <label class="f">화면 맞춤<select data-f="fit">
          <option value="crop" ${s.fit === "crop" ? "selected" : ""}>꽉 채우기 (가장자리 잘림)</option>
          <option value="blur" ${s.fit === "blur" ? "selected" : ""}>전체 보이기 + 흐린 배경</option>
          <option value="fit" ${s.fit === "fit" ? "selected" : ""}>전체 보이기 + 검은 여백</option></select></label>
      </div>
      <div class="shot-tools">
        <button class="small" data-pick="video" data-sid="${s.id}">다른 영상으로 바꾸기</button>
        <button class="small primary" data-derive>이 영상 끝에서 이어지는 AI 컷 만들기</button>
        <button class="small ghost" data-derive-at>현재 재생 위치 장면으로 AI 컷 만들기</button>
      </div>` : `<div class="muted small">MP4·MOV 등 동영상을 올리면 이 자리에 들어갑니다.</div>
      <div class="shot-tools"><button class="small primary" data-pick="video" data-sid="${s.id}">동영상 올리기</button></div>`}
      ${busyBlock(jobs)}
    </div></section>`;
}

function bindEditor(p) {
  const name = p.name;
  const patchP = guard(async (body) => { state.project = await api(`/api/projects/${encodeURIComponent(name)}`, { method: "PATCH", body }); });
  $("#pTitle").onchange = (e) => patchP({ title: e.target.value });
  $("#pAspect").onchange = guard(async (e) => {
    const hasKf = p.shots.some((s) => s.keyframe);
    if (hasKf) toast("이미 넣은 사진은 이전 비율로 잘려 있습니다. 필요하면 사진을 다시 넣어주세요.", false, 6000);
    await patchP({ aspect: e.target.value });
    renderEditor(name, true);
  });
  $("#pGrade").onchange = (e) => patchP({ grade: e.target.value });
  $("#pModel").onchange = guard(async (e) => { await patchP({ model: e.target.value }); renderEditor(name, true); });
  $("#autoBtn").onclick = guard(async () => {
    for (const card of app.querySelectorAll(".shot")) await savePending(card);
    const q = $("#autoQ").value;
    const jobs = await api("/api/jobs", { method: "POST", body: { kind: "auto", project: name, quality: q } });
    const gens = jobs.filter((j) => j.kind === "generate").length;
    toast(gens ? `${gens}개 컷 생성 → 최종 영상까지 자동으로 진행합니다` : "모든 컷이 준비되어 바로 최종 영상을 만듭니다", false, 5000);
    await refreshJobs(); renderEditor(name, true);
  });
  $("#pStyle").onchange = (e) => patchP({ style_suffix: e.target.value });
  $("#pMusic").onchange = guard(async (e) => {
    if (e.target.value === "__upload") {
      e.target.value = p.music || "";
      const files = await pickFiles("audio/*", false);
      if (files.length) { await uploadFile(name, files[0], "music"); renderEditor(name, true); }
      return;
    }
    await patchP({ music: e.target.value });
  });
  $("#reelBtn").onclick = guard(async () => {
    await api("/api/jobs", { method: "POST", body: { kind: "reel", project: name } });
    toast("최종 영상 만들기를 시작했습니다");
    await refreshJobs(); renderEditor(name, true);
  });
  const delP = $("#delProject");
  if (delP) delP.onclick = guard(async () => {
    if (!confirm(`'${p.title}' 프로젝트를 삭제할까요? (projects/.trash 폴더로 옮겨집니다)`)) return;
    await api(`/api/projects/${encodeURIComponent(name)}`, { method: "DELETE" });
    location.hash = "#/";
  });

  app.querySelectorAll("[data-add]").forEach((b) => (b.onclick = guard(async () => {
    const kind = b.dataset.add;
    if (kind === "ai") { await api(`/api/projects/${encodeURIComponent(name)}/shots`, { method: "POST", body: { type: "ai" } }); }
    else if (kind === "photos") {
      const files = await pickFiles("image/*", true);
      for (const f of files) await uploadFile(name, f, "photo");
    } else {
      const files = await pickFiles("video/*", true);
      for (const f of files) await uploadFile(name, f, "video");
    }
    await renderEditor(name, true);
    if (kind === "ai") window.scrollTo(0, document.body.scrollHeight);
  })));

  app.querySelectorAll("[data-delfile]").forEach((b) => (b.onclick = guard(async () => {
    if (!confirm("이 영상을 삭제할까요?")) return;
    await api(`/api/projects/${encodeURIComponent(name)}/file?path=${encodeURIComponent(b.dataset.delfile)}`, { method: "DELETE" });
    renderEditor(name, true);
  })));

  app.querySelectorAll(".shot").forEach((card) => {
    const sid = card.dataset.sid;
    const base = `/api/projects/${encodeURIComponent(name)}/shots/${sid}`;
    const patch = guard(async (body, rerender = false) => { await api(base, { method: "PATCH", body }); if (rerender) renderEditor(name, true); });

    card.querySelectorAll("[data-f]").forEach((el) => (el.onchange = () => patch({ [el.dataset.f]: el.value }, ["seconds", "fit", "start", "motion", "camera"].includes(el.dataset.f))));
    const cprev = card.querySelector("[data-campreview]");
    if (cprev) cprev.onclick = guard(async () => {
      cprev.disabled = true; cprev.textContent = "만드는 중…";
      await api(`${base}/camera_preview`, { method: "POST" });
      renderEditor(name, true);
    });
    card.querySelectorAll("[data-move]").forEach((b) => (b.onclick = guard(async () => { await api(`${base}/move`, { method: "POST", body: { dir: +b.dataset.move } }); renderEditor(name, true); })));
    const del = card.querySelector("[data-delshot]");
    if (del) del.onclick = guard(async () => { if (!confirm("이 컷을 삭제할까요?")) return; await api(base, { method: "DELETE" }); renderEditor(name, true); });

    card.querySelectorAll("[data-pick]").forEach((b) => (b.onclick = guard(async () => {
      const kind = b.dataset.pick;
      const files = await pickFiles(kind === "photo" ? "image/*" : "video/*", false);
      if (!files.length) return;
      await uploadFile(name, files[0], kind, sid);
      renderEditor(name, true);
    })));
    const clr = card.querySelector("[data-clearkf]");
    if (clr) clr.onclick = () => patch({ keyframe: "" }, true);
    const kfg = card.querySelector("[data-kfgen]");
    if (kfg) kfg.onclick = guard(async () => {
      await savePending(card);
      await api("/api/jobs", { method: "POST", body: { kind: "keyframe", project: name, sid } });
      toast("키 이미지 생성을 작업 목록에 넣었습니다");
      await refreshJobs(); renderEditor(name, true);
    });

    card.querySelectorAll("[data-gen]").forEach((b) => (b.onclick = guard(async () => {
      await savePending(card);
      const q = b.dataset.gen;
      const jobs = await api("/api/jobs", { method: "POST", body: { kind: "generate", project: name, sid, quality: q } });
      toast(`${state.info.qualities[q].label} 생성 시작 (${fmtMin(jobs[0].estimate)})`);
      await refreshJobs(); renderEditor(name, true);
    })));
    card.querySelectorAll("[data-take]").forEach((b) => (b.onclick = () => patch({ take: b.dataset.take }, true)));
    card.querySelectorAll("[data-seed]").forEach((b) => (b.onclick = () => { patch({ seed: +b.dataset.seed }, true); toast(`시드 ${b.dataset.seed} 로 고정했습니다. 표준·고화질 생성 시 같은 움직임을 재현합니다`); }));

    const der = card.querySelector("[data-derive]");
    if (der) der.onclick = guard(async () => {
      await api(`${base}/derive`, { method: "POST", body: { at: "end" } });
      toast("마지막 장면을 시작 이미지로 하는 새 AI 컷을 추가했습니다. 프롬프트를 고친 뒤 생성하세요", false, 5000);
      renderEditor(name, true);
    });
    const derAt = card.querySelector("[data-derive-at]");
    if (derAt) derAt.onclick = guard(async () => {
      const v = card.querySelector("video");
      const at = v ? v.currentTime : 0;
      await api(`${base}/derive`, { method: "POST", body: { at } });
      toast(`${at.toFixed(1)}초 장면으로 새 AI 컷을 추가했습니다`);
      renderEditor(name, true);
    });

    // 끌어다 놓기
    const media = card.querySelector(".media");
    const shot = p.shots.find((x) => x.id === sid);
    card.addEventListener("dragover", (e) => { e.preventDefault(); card.classList.add("drag"); });
    card.addEventListener("dragleave", () => card.classList.remove("drag"));
    card.addEventListener("drop", guard(async (e) => {
      e.preventDefault(); card.classList.remove("drag");
      const f = e.dataTransfer.files[0];
      if (!f) return;
      const isVid = f.type.startsWith("video/");
      if (shot.type === "ai" && isVid) return toast("AI 컷에는 사진을 넣어주세요. 동영상은 '+ 내 동영상 넣기'로 추가합니다", true);
      if (shot.type === "video" && !isVid) return toast("동영상 컷에는 동영상을 넣어주세요", true);
      await uploadFile(name, f, isVid ? "video" : "photo", sid);
      renderEditor(name, true);
    }));
    if (media && shot.type === "video" && media.querySelector("video")) {
      media.querySelector("video").addEventListener("loadedmetadata", (e) => { e.target.currentTime = +shot.start || 0; }, { once: true });
    }
  });
}

async function savePending(card) {
  // 입력 중인 값(포커스 상태)을 생성 전에 저장
  const el = document.activeElement;
  if (el && card.contains(el) && el.dataset && el.dataset.f) {
    await api(`/api/projects/${encodeURIComponent(state.project.name)}/shots/${card.dataset.sid}`, { method: "PATCH", body: { [el.dataset.f]: el.value } });
  }
  const pr = card.querySelector('[data-f="prompt"]');
  if (pr && pr.value !== pr.defaultValue) await api(`/api/projects/${encodeURIComponent(state.project.name)}/shots/${card.dataset.sid}`, { method: "PATCH", body: { prompt: pr.value } });
}

// ───────── 파일 선택·업로드 ─────────
function pickFiles(accept, multiple) {
  return new Promise((resolve) => {
    const inp = $("#filePicker");
    inp.value = ""; inp.accept = accept; inp.multiple = multiple;
    inp.onchange = () => resolve([...inp.files]);
    inp.click();
  });
}

const CHUNK = 8 * 1024 * 1024;
async function uploadFile(project, file, kind, sid = "") {
  const id = [...crypto.getRandomValues(new Uint8Array(8))].map((b) => b.toString(16).padStart(2, "0")).join("");
  const total = Math.max(1, Math.ceil(file.size / CHUNK));
  let last = null;
  for (let i = 0; i < total; i++) {
    const fd = new FormData();
    fd.append("kind", kind); fd.append("upload_id", id); fd.append("index", i); fd.append("total", total);
    fd.append("filename", file.name); fd.append("sid", sid);
    fd.append("chunk", file.slice(i * CHUNK, (i + 1) * CHUNK), file.name);
    toast(`${file.name} 올리는 중… ${Math.round(((i + 1) / total) * 100)}%`, false, 60000);
    last = await api(`/api/projects/${encodeURIComponent(project)}/upload`, { method: "POST", body: fd });
  }
  toast(`${file.name} 올림 완료`);
  return last;
}

// ───────── 작업 목록 ─────────
async function refreshJobs() {
  try { state.jobs = await api("/api/jobs"); } catch { return; }
  const active = state.jobs.filter((j) => ["queued", "running"].includes(j.status));
  const btn = $("#jobsBtn");
  btn.hidden = !state.jobs.length;
  btn.classList.toggle("busy", active.length > 0);
  const run = active.find((j) => j.status === "running");
  $("#jobsLabel").textContent = run ? `${Math.round(run.progress * 100)}% · ${active.length}개 진행` : active.length ? `대기 ${active.length}개` : "작업 완료";
  renderJobList();

  // 이 프로젝트의 작업이 끝나면 화면 갱신, 진행 중이면 진행 막대만 갱신
  let changed = false;
  for (const j of state.jobs) {
    if (["done", "error", "canceled"].includes(j.status) && !state.lastDone.has(j.id)) {
      state.lastDone.add(j.id);
      if (state._primed) {
        if (j.status === "done") toast(`완료: ${j.label}`);
        else if (j.status === "error") toast(`실패: ${j.label} — ${j.error}`, true, 8000);
        if (state.project && j.project === state.project.name) changed = true;
      }
    }
  }
  state._primed = true;
  if (changed && !document.activeElement?.closest?.(".shot-body textarea, .shot-body input")) renderEditor(state.project.name, true);
  else document.querySelectorAll(".busy[data-job]").forEach((el) => {
    const j = state.jobs.find((x) => x.id === el.dataset.job);
    if (j) { el.firstChild.textContent = `⏳ ${j.stage} `; el.querySelector(".bar i").style.width = `${Math.round(j.progress * 100)}%`; }
  });
}

function renderJobList() {
  const list = $("#jobList");
  if (!state.jobs.length) { list.innerHTML = `<div class="job muted small">작업이 없습니다</div>`; return; }
  const label = { queued: "대기", running: "진행 중", done: "완료", error: "실패", canceled: "취소됨" };
  list.innerHTML = state.jobs.map((j) => `
    <div class="job ${j.status}">
      <div class="jt"><a href="#/p/${encodeURIComponent(j.project)}"><b>${esc(j.label)}</b></a><span class="muted">${label[j.status]}</span></div>
      <div class="bar"><i style="width:${Math.round((j.status === "done" ? 1 : j.progress) * 100)}%"></i></div>
      <div class="js">${j.status === "error" ? esc(j.error) : esc(j.stage)}
        ${j.status === "running" ? ` · ${fmtElapsed(j.elapsed)} 경과${j.estimate ? ` / 예상 ${fmtMin(j.estimate)}` : ""}` : ""}
        ${j.status === "done" && j.elapsed ? ` · ${fmtElapsed(j.elapsed)}` : ""}</div>
      ${["queued", "running"].includes(j.status) ? `<div><button class="small ghost danger" data-cancel="${j.id}">취소</button></div>` : ""}
      ${j.status === "error" && j.log.length ? `<details><summary class="muted small">로그</summary><pre>${esc(j.log.join("\n"))}</pre></details>` : ""}
    </div>`).join("");
  list.querySelectorAll("[data-cancel]").forEach((b) => (b.onclick = guard(async () => { await api(`/api/jobs/${b.dataset.cancel}`, { method: "DELETE" }); refreshJobs(); })));
}

$("#jobsBtn").onclick = () => { $("#drawer").hidden = !$("#drawer").hidden; };
$("#drawerClose").onclick = () => { $("#drawer").hidden = true; };

function startPolling() {
  if (state.polling) return;
  refreshJobs();
  state.polling = setInterval(refreshJobs, 2500);
}
function stopPolling() { clearInterval(state.polling); state.polling = null; }

// ───────── 시작 ─────────
(async () => {
  try { await api("/api/me"); route(); } catch { /* renderLogin 이미 호출됨 */ }
})();
