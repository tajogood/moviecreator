"""moviecreator 웹 서버 (FastAPI).

실행: ./web.sh   (로컬 http://localhost:7860, 외부 주소는 Cloudflare 터널)
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import mimetypes
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mc  # noqa: E402

ROOT = mc.ROOT
STATIC = Path(__file__).resolve().parent / "static"
SECRET_FILE = ROOT / "web.secret.toml"
COOKIE = "mc_session"
SESSION_DAYS = 30
CHUNK_DIR = ROOT / "logs" / "uploads"
MAX_UPLOAD = 4 * 1024**3   # 4GB


# ───────────────────────── 비밀번호·세션 ─────────────────────────

def load_secret() -> dict:
    if SECRET_FILE.exists():
        data = mc.load_toml(SECRET_FILE)
        if data.get("password") and data.get("key"):
            return data
    data = {"password": secrets.token_urlsafe(9), "key": secrets.token_hex(32)}
    SECRET_FILE.write_text(f'# 웹 로그인 비밀번호 (바꾸려면 password 값을 고치고 서버 재시작)\n'
                           f'password = "{data["password"]}"\nkey = "{data["key"]}"\n', encoding="utf-8")
    os.chmod(SECRET_FILE, 0o600)
    return data


SECRET = load_secret()


def make_token() -> str:
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(SECRET["key"].encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_token(tok: str | None) -> bool:
    if not tok or "." not in tok:
        return False
    exp, sig = tok.split(".", 1)
    good = hmac.new(SECRET["key"].encode(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()


_fail = collections.deque(maxlen=50)


# ───────────────────────── 작업 큐 ─────────────────────────

STAGES = [("Loading T5", "텍스트 인코더 불러오는 중", 0.02), ("Encoding text", "프롬프트 처리 중", 0.05),
          ("Encoding input image", "사진 분석 중", 0.12), ("Loading transformer", "모델 불러오는 중", 0.14),
          ("Denoising", "영상 생성 중", 0.15), ("Decoding with VAE", "영상 변환 중", 0.85),
          ("Video saved", "저장 완료", 0.99)]


class Job:
    def __init__(self, kind: str, project: str, sid: str | None = None, **opts):
        self.id = uuid.uuid4().hex[:8]
        self.kind, self.project, self.sid, self.opts = kind, project, sid, opts
        self.status = "queued"          # queued / running / done / error / canceled
        self.progress = 0.0
        self.stage = "대기 중"
        self.log: list[str] = []
        self.result = ""
        self.error = ""
        self.created = time.time()
        self.started = 0.0
        self.finished = 0.0
        self.estimate = int(opts.get("estimate") or 0)
        self.cancel = False
        self.label = opts.get("label", "")

    def add_log(self, line: str) -> None:
        line = line.rstrip()
        if line:
            self.log.append(line)
            del self.log[:-200]

    def view(self) -> dict:
        now = time.time()
        return {"id": self.id, "kind": self.kind, "project": self.project, "sid": self.sid,
                "label": self.label, "status": self.status, "progress": round(self.progress, 3),
                "stage": self.stage, "result": self.result, "error": self.error,
                "quality": self.opts.get("quality"), "seed": self.opts.get("seed"),
                "estimate": self.estimate, "created": self.created,
                "elapsed": int((self.finished or now) - self.started) if self.started else 0,
                "log": self.log[-12:]}


class Queue:
    def __init__(self):
        self.jobs: list[Job] = []
        self.cv = threading.Condition()
        threading.Thread(target=self._worker, daemon=True).start()

    def add(self, job: Job) -> Job:
        with self.cv:
            self.jobs.append(job)
            self.jobs = [j for j in self.jobs if j.status in ("queued", "running")
                         or time.time() - j.finished < 6 * 3600][-80:]
            self.cv.notify()
        return job

    def get(self, jid: str) -> Job:
        j = next((j for j in self.jobs if j.id == jid), None)
        if not j:
            raise HTTPException(404, "작업이 없습니다")
        return j

    def _next(self) -> Job:
        with self.cv:
            while True:
                j = next((j for j in self.jobs if j.status == "queued"), None)
                if j:
                    j.status = "running"
                    j.started = time.time()
                    return j
                self.cv.wait()

    def _worker(self) -> None:
        while True:
            job = self._next()
            try:
                if job.cancel:
                    raise mc.McError("취소됨")
                {"generate": run_generate, "keyframe": run_keyframe, "reel": run_reel}[job.kind](job)
                job.status, job.progress, job.stage = "done", 1.0, "완료"
            except mc.McError as e:
                job.status = "canceled" if job.cancel or str(e) == "취소됨" else "error"
                job.error = str(e)
                job.stage = "취소됨" if job.status == "canceled" else "오류"
            except Exception as e:  # noqa: BLE001
                job.status, job.error, job.stage = "error", f"{type(e).__name__}: {e}", "오류"
                job.add_log(traceback.format_exc())
            job.finished = time.time()
            mc_log_event(job)


QUEUE = Queue()


def mc_log_event(job: Job) -> None:
    try:
        d = ROOT / "logs"
        d.mkdir(exist_ok=True)
        import json
        with open(d / "web_jobs.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({k: v for k, v in job.view().items() if k != "log"}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def stream_process(job: Job, cmd: list[str], parse=None) -> None:
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True, bufsize=0)
    buf = b""
    try:
        while True:
            if job.cancel:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise mc.McError("취소됨")
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not chunk:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
                continue
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = re.sub(r"\x1b\[[0-9;]*m", "", raw.decode(errors="replace")).strip()
                if not line:
                    continue
                if parse:
                    parse(line)
                if not re.match(r"^(Diffusion|Fetching)\b.*\|", line):
                    job.add_log(line)
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        tail = " / ".join(job.log[-3:])
        raise mc.McError(f"생성 프로그램 오류 (exit {proc.returncode}) {tail}")


def run_generate(job: Job) -> None:
    proj = mc.Project(job.project)
    shot = proj.shot(job.sid)
    q, seed = job.opts["quality"], int(job.opts["seed"])
    out = mc.take_path(proj, shot, q, seed)
    free = mc.memory_free_pct()
    if free is not None and free < mc.CFG["video"]["min_free_memory_pct"]:
        job.add_log(f"⚠ 메모리 여유 {free}% — 다른 앱을 닫으면 빨라집니다")
    cmd = mc.generate_cmd(proj, shot, q, seed, out)

    def parse(line: str) -> None:
        for key, label, pct in STAGES:
            if line.startswith(key) or f" {key}" in line[:40]:
                job.stage, job.progress = label, max(job.progress, pct)
        m = re.search(r"Diffusion:\s*\d+%\|.*?\|\s*(\d+)/(\d+)", line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            job.stage = f"영상 생성 중 ({done}/{total} 스텝)"
            job.progress = 0.15 + 0.70 * done / max(1, total)

    job.stage = "준비 중"
    stream_process(job, cmd, parse)
    if not out.exists():
        raise mc.McError("영상 파일이 만들어지지 않았습니다.")
    proj.reload()
    sh = proj.shot(job.sid)
    # 첫 결과이거나 더 높은 품질이면 자동 채택
    rank = {"draft": 0, "standard": 1, "final": 2}
    cur = sh.get("take", "")
    cur_q = re.search(r"_(draft|standard|final)_s", cur)
    if not cur or not proj.path(cur).exists() or (cur_q and rank[q] > rank[cur_q.group(1)]):
        sh["take"] = proj.relpath(out)
        proj.save()
    job.result = proj.relpath(out)


def run_keyframe(job: Job) -> None:
    proj = mc.Project(job.project)
    shot = proj.shot(job.sid)
    out = proj.dir / "keyframes" / f"{shot['id']}_gen_{int(time.time())}.png"
    job.stage = "키 이미지 생성 중 (처음엔 모델 다운로드로 오래 걸립니다)"
    job.progress = 0.1
    stream_process(job, mc.keyframe_cmd(proj, shot, out))
    if not out.exists():
        raise mc.McError("이미지가 만들어지지 않았습니다.")
    proj.reload()
    proj.shot(job.sid)["keyframe"] = proj.relpath(out)
    proj.save()
    job.result = proj.relpath(out)


def run_reel(job: Job) -> None:
    proj = mc.Project(job.project)

    def prog(p: float, s: str) -> None:
        job.progress, job.stage = p, s

    out = mc.build_reel(proj, log=mc.Log(job.add_log), progress=prog, stop=lambda: job.cancel)
    job.result = proj.relpath(out)


# ───────────────────────── 앱 ─────────────────────────

app = FastAPI(title="moviecreator", docs_url=None, redoc_url=None, openapi_url=None)
PUBLIC = ("/login", "/api/login", "/static/", "/favicon")


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if path == "/" or any(path.startswith(p) for p in PUBLIC) or valid_token(request.cookies.get(COOKIE)):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp
    if path.startswith("/api/") or path.startswith("/files/"):
        return JSONResponse({"detail": "로그인이 필요합니다"}, status_code=401)
    return RedirectResponse("/")


@app.exception_handler(mc.McError)
async def mc_error(_: Request, exc: mc.McError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    recent = [t for t in _fail if time.time() - t < 300]
    if len(recent) >= 10:
        raise HTTPException(429, "로그인 시도가 너무 많습니다. 5분 후 다시 시도하세요.")
    if not hmac.compare_digest(str(body.get("password", "")), SECRET["password"]):
        _fail.append(time.time())
        time.sleep(1)
        raise HTTPException(401, "비밀번호가 틀렸습니다")
    secure = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, make_token(), max_age=SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", secure=secure)
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
def me():
    return {"ok": True}


@app.get("/api/info")
def info():
    rows = mc.doctor()
    return {
        "aspects": {k: {"label": v["label"], "output": v["output"]} for k, v in mc.CFG["aspect"].items()},
        "qualities": {k: {"label": v["label"], "steps": v["steps"]} for k, v in mc.CFG["quality"].items()},
        "grades": {"none": "기본", "warm": "따뜻하게", "cool": "시원하게", "film": "필름", "vivid": "선명하게", "bw": "흑백"},
        "templates": mc.templates(),
        "doctor": [{"name": n, "ok": ok, "detail": d} for n, ok, d in rows],
        "memory_free": mc.memory_free_pct(),
        "public_url": (ROOT / "logs" / "public_url.txt").read_text().strip()
        if (ROOT / "logs" / "public_url.txt").exists() else "",
    }


@app.get("/api/estimate")
def estimate(aspect: str, quality: str, seconds: float):
    return {"seconds": mc.estimate_seconds(aspect, quality, seconds)}


# ── 프로젝트

def project_view(p: mc.Project) -> dict:
    shots = []
    for s in p.shots:
        v = dict(s)
        if s["type"] == "ai":
            v["takes"] = p.takes(s["id"])
            v["estimates"] = {q: mc.estimate_seconds(p.aspect, q, float(s["seconds"])) for q in mc.QUALITIES}
        else:
            src = p.path(s["video"]) if s.get("video") else None
            v["video_duration"] = mc.ffprobe(src)["duration"] if src and src.exists() else 0
        shots.append(v)
    music = sorted(f"uploads/music/{f.name}" for f in (p.dir / "uploads" / "music").glob("*") if f.is_file()) \
        if (p.dir / "uploads" / "music").exists() else []
    data = {k: v for k, v in p.data.items() if k != "shots"}
    return {"name": p.name, **data, "shots": shots, "finals": p.finals(), "music_files": music,
            "total_seconds": p.total_seconds(), "output": mc.output_size(p.aspect)}


@app.get("/api/projects")
def projects():
    return mc.list_projects()


@app.post("/api/projects")
async def create_project(request: Request):
    b = await request.json()
    name = (b.get("name") or "").strip() or time.strftime("영상_%m%d_%H%M%S")
    p = mc.new_project(name, b.get("template", "blank"), b.get("aspect", "9:16"), b.get("title", "").strip())
    return project_view(p)


@app.get("/api/projects/{name}")
def get_project(name: str):
    return project_view(mc.Project(name))


EDITABLE_PROJECT = {"title", "aspect", "music", "grade", "style_suffix", "keyframe_suffix", "negative_prompt"}


@app.patch("/api/projects/{name}")
async def patch_project(name: str, request: Request):
    b = await request.json()
    p = mc.Project(name)
    for k, v in b.items():
        if k not in EDITABLE_PROJECT:
            continue
        if k == "aspect" and v not in mc.CFG["aspect"]:
            raise HTTPException(400, "알 수 없는 비율")
        if k == "grade" and v not in mc.CFG["grade"]:
            raise HTTPException(400, "알 수 없는 색감")
        p.data[k] = str(v)
    p.save()
    return project_view(p)


@app.delete("/api/projects/{name}")
def delete_project(name: str):
    p = mc.Project(name)
    trash = mc.projects_dir() / ".trash"
    trash.mkdir(exist_ok=True)
    p.dir.rename(trash / f"{name}_{int(time.time())}")
    return {"ok": True}


# ── 컷

EDITABLE_SHOT = {"seconds": float, "role": str, "prompt": str, "keyframe_prompt": str, "seed": int,
                 "start": float, "fit": str, "take": str, "keyframe": str}


def _insert(p: mc.Project, shot: dict, after: str | None) -> None:
    idx = next((i + 1 for i, s in enumerate(p.shots) if s["id"] == after), len(p.shots))
    p.shots.insert(idx, shot)


def new_ai_shot(prompt: str = "", keyframe: str = "", role: str = "", seconds: float = 5) -> dict:
    return {"id": mc.new_shot_id(), "type": "ai", "seconds": seconds, "role": role, "prompt": prompt,
            "keyframe": keyframe, "keyframe_prompt": "", "seed": 0, "take": ""}


@app.post("/api/projects/{name}/shots")
async def add_shot(name: str, request: Request):
    b = await request.json()
    p = mc.Project(name)
    if b.get("type") == "video":
        shot = {"id": mc.new_shot_id(), "type": "video", "seconds": 4.0, "role": b.get("role", "동영상"),
                "video": "", "start": 0.0, "fit": "crop"}
    else:
        shot = new_ai_shot(b.get("prompt", ""), role=b.get("role", f"컷 {len(p.shots) + 1}"))
    _insert(p, shot, b.get("after"))
    p.save()
    return project_view(p)


@app.patch("/api/projects/{name}/shots/{sid}")
async def patch_shot(name: str, sid: str, request: Request):
    b = await request.json()
    p = mc.Project(name)
    s = p.shot(sid)
    for k, v in b.items():
        if k not in EDITABLE_SHOT:
            continue
        try:
            v = EDITABLE_SHOT[k](v) if v not in (None, "") else ("" if EDITABLE_SHOT[k] is str else 0)
        except (TypeError, ValueError):
            raise HTTPException(400, f"잘못된 값: {k}")
        if k == "seconds":
            v = max(1.0, min(v, 5.0 if s["type"] == "ai" else 60.0))
        if k == "fit" and v not in ("crop", "blur", "fit"):
            raise HTTPException(400, "알 수 없는 맞춤 방식")
        if k in ("take", "keyframe") and v and not p.path(v).exists():
            raise HTTPException(400, "파일이 없습니다")
        s[k] = v
    p.save()
    return project_view(p)


@app.delete("/api/projects/{name}/shots/{sid}")
def delete_shot(name: str, sid: str):
    p = mc.Project(name)
    p.data["shots"] = [s for s in p.shots if s["id"] != sid]
    p.save()
    return project_view(p)


@app.post("/api/projects/{name}/shots/{sid}/move")
async def move_shot(name: str, sid: str, request: Request):
    d = int((await request.json()).get("dir", 0))
    p = mc.Project(name)
    i = next(i for i, s in enumerate(p.shots) if s["id"] == sid)
    j = max(0, min(len(p.shots) - 1, i + d))
    p.shots.insert(j, p.shots.pop(i))
    p.save()
    return project_view(p)


@app.post("/api/projects/{name}/shots/{sid}/derive")
async def derive_shot(name: str, sid: str, request: Request):
    """동영상 컷의 한 장면(또는 마지막 장면)에서 이어지는 AI 컷을 만든다."""
    b = await request.json()
    p = mc.Project(name)
    s = p.shot(sid)
    if s["type"] == "video":
        if not s.get("video"):
            raise HTTPException(400, "동영상이 없습니다")
        src = p.path(s["video"])
        at = b.get("at", "end")
        if at == "end":
            at = float(s.get("start") or 0) + float(s["seconds"]) - 0.05
            dur = mc.ffprobe(src)["duration"]
            at = min(at, max(0.0, dur - 0.1))
    else:
        if not s.get("take"):
            raise HTTPException(400, "먼저 영상을 생성하세요")
        src, at = p.path(s["take"]), "end"
    new = new_ai_shot(b.get("prompt") or "Continue the scene naturally with smooth cinematic camera movement.",
                      role="이어지는 장면")
    dst = p.dir / "keyframes" / f"{new['id']}.png"
    mc.extract_frame(src, at, dst, p.aspect)
    new["keyframe"] = p.relpath(dst)
    _insert(p, new, sid)
    p.save()
    return project_view(p)


@app.delete("/api/projects/{name}/file")
def delete_file(name: str, path: str):
    p = mc.Project(name)
    f = p.path(path)
    if not (path.startswith("takes/") or path.startswith("final/")) or not f.exists():
        raise HTTPException(400, "삭제할 수 없는 파일")
    f.unlink()
    for s in p.shots:
        if s.get("take") == path:
            s["take"] = ""
    p.save()
    return project_view(p)


# ── 업로드 (분할 업로드: 외부 터널의 요청 크기 제한 회피)

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"}


def _safe_name(name: str) -> str:
    stem, ext = os.path.splitext(os.path.basename(name))
    stem = re.sub(r"[^\w가-힣.-]+", "_", stem)[:40] or "file"
    return f"{stem}_{secrets.token_hex(2)}{ext.lower()}"


@app.post("/api/projects/{name}/upload")
async def upload(name: str, kind: str = Form(...), upload_id: str = Form(...), index: int = Form(...),
                 total: int = Form(...), filename: str = Form(...), sid: str = Form(""),
                 after: str = Form(""), chunk: UploadFile = File(...)):
    p = mc.Project(name)
    if not re.fullmatch(r"[a-f0-9]{8,32}", upload_id):
        raise HTTPException(400, "잘못된 업로드 id")
    ext = os.path.splitext(filename)[1].lower()
    allowed = {"photo": PHOTO_EXT, "video": VIDEO_EXT, "music": AUDIO_EXT}.get(kind)
    if not allowed or ext not in allowed:
        raise HTTPException(400, f"지원하지 않는 파일 형식입니다: {ext or filename}")
    CHUNK_DIR.mkdir(parents=True, exist_ok=True)
    part = CHUNK_DIR / f"{upload_id}{ext}"
    if index == 0 and part.exists():
        part.unlink()
    with open(part, "ab") as f:
        while data := await chunk.read(1024 * 1024):
            f.write(data)
    if part.stat().st_size > MAX_UPLOAD:
        part.unlink()
        raise HTTPException(413, "파일이 너무 큽니다 (최대 4GB)")
    if index < total - 1:
        return {"ok": True, "received": index + 1}

    # 마지막 조각: 파일 확정 후 처리
    try:
        if kind == "photo":
            dest_dir = p.dir / "uploads" / "photos"
            dest_dir.mkdir(parents=True, exist_ok=True)
            orig = dest_dir / _safe_name(filename)
            shutil.move(part, orig)
            if sid:
                shot = p.shot(sid)
                if shot["type"] != "ai":
                    raise HTTPException(400, "사진은 AI 컷에만 넣을 수 있습니다")
            else:
                shot = new_ai_shot("Slow cinematic push-in, subtle natural motion, soft light shifting.",
                                   role=f"사진 컷 {len(p.shots) + 1}")
                _insert(p, shot, after or None)
                after_id = shot["id"]
            kf = p.dir / "keyframes" / f"{shot['id']}_{int(time.time())}.png"
            mc.crop_image(orig, kf, p.aspect)
            shot["keyframe"] = p.relpath(kf)
            shot["keyframe_source"] = p.relpath(orig)
        elif kind == "video":
            dest_dir = p.dir / "uploads" / "videos"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dst = dest_dir / _safe_name(filename)
            shutil.move(part, dst)
            dur = mc.ffprobe(dst)["duration"]
            if sid:
                shot = p.shot(sid)
                if shot["type"] != "video":
                    raise HTTPException(400, "동영상은 동영상 컷에만 넣을 수 있습니다")
            else:
                shot = {"id": mc.new_shot_id(), "type": "video", "role": "내 영상", "start": 0.0, "fit": "crop"}
                _insert(p, shot, after or None)
            shot["video"] = p.relpath(dst)
            shot["start"] = 0.0
            shot["seconds"] = round(min(max(dur, 1.0), 8.0), 2)
        else:
            dest_dir = p.dir / "uploads" / "music"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dst = dest_dir / _safe_name(filename)
            shutil.move(part, dst)
            p.data["music"] = p.relpath(dst)
    finally:
        if part.exists():
            part.unlink()
    p.save()
    return {"ok": True, "done": True, "project": project_view(p)}


# ── 작업

@app.get("/api/jobs")
def jobs():
    return [j.view() for j in reversed(QUEUE.jobs)]


@app.post("/api/jobs")
async def add_job(request: Request):
    b = await request.json()
    kind, name = b.get("kind"), b.get("project")
    p = mc.Project(name)
    if kind == "generate":
        s = p.shot(b.get("sid"))
        if s["type"] != "ai":
            raise HTTPException(400, "AI 컷만 생성할 수 있습니다")
        if not s.get("prompt", "").strip():
            raise HTTPException(400, "프롬프트를 입력하세요")
        q = b.get("quality", "draft")
        if q not in mc.QUALITIES:
            raise HTTPException(400, "알 수 없는 품질")
        count = max(1, min(int(b.get("count", 1)), 4))
        out = []
        for i in range(count):
            seed = int(b.get("seed") or s.get("seed") or 0) if i == 0 else 0
            seed = seed or random.randint(1, 99999)
            out.append(QUEUE.add(Job("generate", name, s["id"], quality=q, seed=seed,
                                     estimate=mc.estimate_seconds(p.aspect, q, float(s["seconds"])),
                                     label=f"{p.data['title']} · {s.get('role') or s['id']} · {mc.CFG['quality'][q]['label']}")).view())
        return out
    if kind == "keyframe":
        s = p.shot(b.get("sid"))
        return [QUEUE.add(Job("keyframe", name, s["id"], estimate=120,
                              label=f"{p.data['title']} · {s.get('role') or s['id']} · 키 이미지")).view()]
    if kind == "reel":
        missing = [s.get("role") or s["id"] for s in p.shots
                   if (s["type"] == "ai" and not s.get("take")) or (s["type"] == "video" and not s.get("video"))]
        if missing:
            raise HTTPException(400, "영상이 준비되지 않은 컷: " + ", ".join(missing))
        return [QUEUE.add(Job("reel", name, estimate=int(20 + 25 * len(p.shots)),
                              label=f"{p.data['title']} · 최종 영상 만들기")).view()]
    raise HTTPException(400, "알 수 없는 작업")


@app.delete("/api/jobs/{jid}")
def cancel_job(jid: str):
    j = QUEUE.get(jid)
    j.cancel = True
    if j.status == "queued":
        j.status, j.stage, j.finished = "canceled", "취소됨", time.time()
    return j.view()


# ── 파일

@app.get("/files/{name}/{path:path}")
def files(name: str, path: str, download: int = 0):
    p = mc.Project(name)
    f = p.path(path)
    if not f.is_file():
        raise HTTPException(404, "파일이 없습니다")
    mt = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
    headers = {"Cache-Control": "private, max-age=3600"}
    if download:
        return FileResponse(f, media_type=mt, filename=f.name, headers=headers)
    return FileResponse(f, media_type=mt, headers=headers)
