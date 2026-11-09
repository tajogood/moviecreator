#!/usr/bin/env python3
"""moviecreator 코어 + CLI.

웹 서버(web/server.py)와 CLI(./mc)가 함께 쓰는 라이브러리입니다.

  키 이미지(사진·mflux) → 이미지/텍스트→영상(mlx-video + Wan2.2 TI2V-5B)
  → RIFE 30fps 보간 → 비율별 출력 해상도 정규화 → 합치기 + 음악 → MP4

CLI 예:
    ./mc doctor
    ./mc new cafe --template product --aspect 9:16
    ./mc gen cafe --quality draft            # 모든 AI 컷 시안
    ./mc gen cafe --only 01 --quality final --seed 42
    ./mc reel cafe --music music/track.mp3
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import platform
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

try:
    import tomllib
except ModuleNotFoundError:
    sys.exit("Python 3.11 이상이 필요합니다. ./setup.sh 로 만든 .venv 에서 실행하세요 (./mc 래퍼).")

try:
    import tomli_w
except ModuleNotFoundError:  # setup.sh 가 설치. 없으면 최소 직렬화기로 대체
    tomli_w = None

ROOT = Path(__file__).resolve().parent
IS_MAC = platform.system() == "Darwin"
WAN_GEN_MODULES = ("mlx_video.models.wan_2.generate", "mlx_video.wan_2.generate", "mlx_video.generate_wan")
QUALITIES = ("draft", "standard", "final")
ASPECT_PHRASE = {"9:16": "Vertical 9:16 framing for Instagram Reels.",
                 "16:9": "Horizontal 16:9 widescreen cinematic framing.",
                 "1:1": "Square 1:1 framing."}
SHOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
NAME_RE = re.compile(r"^[A-Za-z0-9가-힣_-]{1,48}$")

_lock = threading.RLock()  # 프로젝트 파일 동시 쓰기 방지


def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


CFG = load_toml(ROOT / "config.toml")


def rel(path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else ROOT / p


# ───────────────────────── 로그 ─────────────────────────

class Log:
    """CLI 에서는 터미널, 서버에서는 작업 로그로 보낸다."""

    def __init__(self, sink: Callable[[str], None] | None = None):
        self.sink = sink

    def __call__(self, msg: str) -> None:
        if self.sink:
            self.sink(msg)
        else:
            print(msg, flush=True)


def die(msg: str) -> None:
    raise McError(msg)


class McError(RuntimeError):
    pass


# ───────────────────────── 크기·프레임 ─────────────────────────

def aspects() -> dict:
    return CFG["aspect"]


def gen_size(aspect: str, quality: str) -> tuple[int, int]:
    a = CFG["aspect"].get(aspect) or die(f"알 수 없는 비율: {aspect}")
    if quality not in QUALITIES:
        die(f"알 수 없는 품질: {quality}")
    w, h = a[quality]
    return int(w), int(h)


def output_size(aspect: str) -> tuple[int, int]:
    w, h = CFG["aspect"][aspect]["output"]
    return int(w), int(h)


def frames_for(seconds: float, quality: str) -> int:
    v = CFG["video"]
    q = CFG["quality"][quality]
    if "frames" in q:
        n = int(q["frames"])
    else:
        n = int(round((float(seconds) + v["extra_seconds"]) * v["native_fps"]))
    n = (n // 4) * 4 + 1
    return max(int(v["min_frames"]), min(n, int(v["max_frames"])))


def estimate_seconds(aspect: str, quality: str, seconds: float) -> int:
    """대략적인 생성 시간(초). 기준 실측값에서 토큰 수 비율로 외삽."""
    v = CFG["video"]
    w, h = gen_size(aspect, quality)
    f = frames_for(seconds, quality)
    tokens = (w // 16) * (h // 16) * ((f - 1) // 4 + 1)
    ratio = tokens / v["ref_tokens"]
    step = v["ref_step_seconds"] * ratio ** 1.6   # 어텐션 비용 증가 반영
    steps = CFG["quality"][quality]["steps"]
    return int(v["ref_fixed_seconds"] * max(1.0, ratio) + step * steps)


# ───────────────────────── 외부 도구 ─────────────────────────

def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True)
    if out.returncode != 0:
        die(f"영상 정보를 읽을 수 없습니다: {path.name}")
    data = json.loads(out.stdout)
    if not data.get("streams"):
        die(f"영상 트랙이 없습니다: {path.name}")
    st = data["streams"][0]
    num, den = (int(x) for x in st["r_frame_rate"].split("/"))
    return {"width": int(st["width"]), "height": int(st["height"]),
            "fps_num": num, "fps_den": den or 1, "fps": num / den if den else 0,
            "duration": float(data["format"].get("duration") or 0)}


def has_audio(path: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(out.stdout.strip())


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def mlx_video_module() -> str | None:
    return next((m for m in WAN_GEN_MODULES if _has_module(m)), None)


def rife_bin() -> Path | None:
    b = rel(CFG["paths"]["rife_dir"]) / "rife-ncnn-vulkan"
    return b if b.exists() else None


def model_dir() -> Path:
    return rel(CFG["paths"]["wan_model"])


def run(cmd: list[str], log: Log | None = None, stop: Callable[[], bool] | None = None) -> None:
    """서브프로세스 실행. stop() 이 True 가 되면 중단."""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    try:
        while proc.poll() is None:
            if stop and stop():
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                die("취소됨")
            time.sleep(0.2)
        out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        tail = "\n".join(out.strip().splitlines()[-8:])
        die(f"{Path(cmd[0]).name} 실패 (exit {proc.returncode})\n{tail}")


def memory_free_pct() -> int | None:
    if not IS_MAC or not shutil.which("memory_pressure"):
        return None
    out = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout
    m = re.search(r"free percentage:\s*(\d+)%", out)
    return int(m.group(1)) if m else None


# ───────────────────────── 프로젝트 ─────────────────────────

def projects_dir() -> Path:
    d = rel(CFG["paths"]["projects"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_shot_id() -> str:
    return secrets.token_hex(3)


def _toml_dumps(data: dict) -> str:
    if tomli_w:
        return tomli_w.dumps(data, multiline_strings=False)
    # 최소 직렬화기 (str/int/float/bool, [[shots]])
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        return json.dumps(str(v), ensure_ascii=False)
    lines = [f"{k} = {val(v)}" for k, v in data.items() if not isinstance(v, list)]
    for s in data.get("shots", []):
        lines.append("\n[[shots]]")
        lines += [f"{k} = {val(v)}" for k, v in s.items()]
    return "\n".join(lines) + "\n"


SUBDIRS = ("uploads", "keyframes", "takes", "work", "final")


class Project:
    def __init__(self, name: str):
        if not NAME_RE.match(name):
            die("프로젝트 이름은 한글·영문·숫자·_- 만 쓸 수 있습니다 (48자 이내).")
        self.name = name
        self.dir = projects_dir() / name
        self.file = self.dir / "project.toml"
        if not self.file.exists():
            die(f"프로젝트가 없습니다: {name}")
        self.reload()

    def reload(self) -> None:
        with _lock:
            self.data = load_toml(self.file)
        d = self.data
        d.setdefault("title", self.name)
        d.setdefault("aspect", "9:16")
        d.setdefault("grade", "none")
        d.setdefault("music", "")
        d.setdefault("style_suffix", "")
        d.setdefault("keyframe_suffix", "")
        d.setdefault("shots", [])
        for i, s in enumerate(d["shots"]):
            s["id"] = str(s.get("id") or f"{i + 1:02d}")
            s.setdefault("type", "ai")
            s.setdefault("seconds", 4)
            s.setdefault("role", "")
            if s["type"] == "ai":
                s.setdefault("prompt", "")
                s.setdefault("keyframe", "")
                s.setdefault("keyframe_prompt", "")
                s.setdefault("seed", 0)
                s.setdefault("take", "")
            else:
                s.setdefault("video", "")
                s.setdefault("start", 0.0)
                s.setdefault("fit", "crop")
        for sub in SUBDIRS:
            (self.dir / sub).mkdir(exist_ok=True)

    def save(self) -> None:
        with _lock:
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(_toml_dumps(self.data), encoding="utf-8")
            tmp.replace(self.file)

    # 편의 속성
    @property
    def shots(self) -> list[dict]:
        return self.data["shots"]

    @property
    def aspect(self) -> str:
        return self.data["aspect"]

    def shot(self, sid: str) -> dict:
        s = next((s for s in self.shots if s["id"] == sid), None)
        if not s:
            die(f"컷을 찾을 수 없습니다: {sid}")
        return s

    def path(self, relpath: str) -> Path:
        """프로젝트 폴더 안의 경로만 허용 (경로 탈출 방지)."""
        p = (self.dir / relpath).resolve()
        if self.dir.resolve() not in p.parents and p != self.dir.resolve():
            die("허용되지 않는 경로")
        return p

    def relpath(self, p: Path) -> str:
        return str(p.resolve().relative_to(self.dir.resolve()))

    def total_seconds(self) -> float:
        return float(sum(float(s["seconds"]) for s in self.shots))

    def video_prompt(self, shot: dict) -> str:
        parts = [shot.get("prompt", "").strip(), self.data.get("style_suffix", "").strip(),
                 ASPECT_PHRASE.get(self.aspect, "")]
        return " ".join(x for x in parts if x)

    def negative(self) -> str:
        neg = self.data.get("negative_prompt")
        return (neg or (ROOT / "prompts" / "negative.txt").read_text(encoding="utf-8")).strip()

    def takes(self, sid: str) -> list[dict]:
        out = []
        for f in sorted((self.dir / "takes").glob(f"{sid}_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            m = re.match(rf"{re.escape(sid)}_(draft|standard|final)_s(\d+)_", f.name)
            out.append({"file": f"takes/{f.name}", "quality": m.group(1) if m else "",
                        "seed": int(m.group(2)) if m else None, "time": f.stat().st_mtime})
        return out

    def finals(self) -> list[dict]:
        return [{"file": f"final/{f.name}", "time": f.stat().st_mtime, "size": f.stat().st_size}
                for f in sorted((self.dir / "final").glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)]


def list_projects() -> list[dict]:
    out = []
    for d in projects_dir().iterdir():
        f = d / "project.toml"
        if d.name.startswith(".") or not f.exists():
            continue
        try:
            data = load_toml(f)
        except Exception:
            continue
        shots = data.get("shots", [])
        cover = next((s.get("keyframe") for s in shots if s.get("keyframe")), "")
        out.append({"name": d.name, "title": data.get("title", d.name), "aspect": data.get("aspect", "9:16"),
                    "shots": len(shots), "seconds": sum(float(s.get("seconds", 0)) for s in shots),
                    "cover": cover, "updated": f.stat().st_mtime})
    return sorted(out, key=lambda x: x["updated"], reverse=True)


def templates() -> dict:
    out = {}
    for f in sorted((ROOT / "templates").glob("*.toml")):
        d = load_toml(f)
        out[f.stem] = {"title": d.get("title", f.stem), "shots": len(d.get("shots", [])),
                       "seconds": sum(float(s.get("seconds", 0)) for s in d.get("shots", [])),
                       "description": d.get("description", "")}
    return out


def new_project(name: str, template: str = "blank", aspect: str = "9:16", title: str = "") -> Project:
    if not NAME_RE.match(name):
        die("프로젝트 이름은 한글·영문·숫자·_- 만 쓸 수 있습니다 (48자 이내).")
    if aspect not in CFG["aspect"]:
        die(f"알 수 없는 비율: {aspect}")
    tpl = ROOT / "templates" / f"{template}.toml"
    if not tpl.exists():
        die(f"템플릿이 없습니다: {template}")
    d = projects_dir() / name
    if (d / "project.toml").exists():
        die(f"이미 있는 이름입니다: {name}")
    d.mkdir(parents=True, exist_ok=True)
    data = load_toml(tpl)
    data["aspect"] = aspect
    data["title"] = title or data.get("title", name)
    data.pop("description", None)
    for s in data.get("shots", []):
        s["id"] = new_shot_id()
    (d / "project.toml").write_text(_toml_dumps(data), encoding="utf-8")
    return Project(name)


# ───────────────────────── 미디어 가져오기 ─────────────────────────

def crop_image(src: Path, dst: Path, aspect: str) -> None:
    """사진을 비율에 맞춰 가운데 크롭 (고화질 생성 해상도로 저장)."""
    w, h = gen_size(aspect, "final")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() in (".heic", ".heif") and shutil.which("sips"):  # 아이폰 사진
        jpg = src.with_suffix(".jpg")
        run(["sips", "-s", "format", "jpeg", str(src), "--out", str(jpg)])
        src = jpg
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
         "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
         "-frames:v", "1", "-update", "1", str(dst)])


def extract_frame(video: Path, at: float | str, dst: Path, aspect: str) -> None:
    """영상의 특정 시점(초) 또는 'end'(마지막 프레임)를 키 이미지로 저장."""
    w, h = gen_size(aspect, "final")
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if at == "end":
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-sseof", "-0.3", "-i", str(video),
               "-vf", vf, "-update", "1", "-frames:v", "1", str(dst)]
    else:
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{float(at):.3f}", "-i", str(video),
               "-vf", vf, "-frames:v", "1", str(dst)]
    run(cmd)
    if not dst.exists():  # -sseof 가 실패하는 짧은 영상 대비
        meta = ffprobe(video)
        extract_frame(video, max(0.0, meta["duration"] - 0.1), dst, aspect)


# ───────────────────────── 생성 명령 ─────────────────────────

def generate_cmd(proj: Project, shot: dict, quality: str, seed: int, out: Path) -> list[str]:
    module = mlx_video_module()
    if not module:
        die("mlx-video 가 설치되어 있지 않습니다 (./setup.sh).")
    if not model_dir().exists():
        die("모델이 없습니다. ./scripts/download_models.sh 를 먼저 실행하세요.")
    if not shot.get("prompt", "").strip():
        die("프롬프트를 입력하세요.")
    w, h = gen_size(proj.aspect, quality)
    frames = frames_for(float(shot["seconds"]), quality)
    cmd = [sys.executable, "-m", module, "--model-dir", str(model_dir()),
           "--prompt", proj.video_prompt(shot), "--negative-prompt", proj.negative(),
           "--width", str(w), "--height", str(h), "--num-frames", str(frames),
           "--steps", str(CFG["quality"][quality]["steps"]), "--seed", str(seed),
           "--output-path", str(out)]
    if shot.get("keyframe"):
        kf = proj.path(shot["keyframe"])
        if kf.exists():
            cmd += ["--image", str(kf)]
    if IS_MAC and shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i"] + cmd
    return cmd


def take_path(proj: Project, shot: dict, quality: str, seed: int) -> Path:
    stamp = dt.datetime.now().strftime("%m%d%H%M%S")
    return proj.dir / "takes" / f"{shot['id']}_{quality}_s{seed}_{stamp}.mp4"


def keyframe_cmd(proj: Project, shot: dict, out: Path) -> list[str]:
    kc = CFG["keyframe"]
    exe = shutil.which(kc["command"]) or str(Path.home() / ".local/bin" / kc["command"])
    if not Path(exe).exists():
        die("mflux 가 설치되어 있지 않습니다 (./setup.sh).")
    prompt = (shot.get("keyframe_prompt") or shot.get("prompt") or "").strip()
    if not prompt:
        die("키 이미지 프롬프트(또는 영상 프롬프트)를 입력하세요.")
    prompt = " ".join(x for x in [prompt, proj.data.get("keyframe_suffix", "").strip(),
                                  ASPECT_PHRASE.get(proj.aspect, "")] if x)
    w, h = gen_size(proj.aspect, "final")
    seed = int(shot.get("seed") or random.randint(1, 99999))
    return [exe, "--model", kc["model"], "-q", str(kc["quantize"]), "--steps", str(kc["steps"]),
            "--seed", str(seed), "--width", str(w), "--height", str(h), "--prompt", prompt, "--output", str(out)]


# ───────────────────────── 릴스 조립 ─────────────────────────

def _fit_filter(fit: str, W: int, H: int) -> str:
    if fit == "blur":
        return (f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"boxblur=24:2,eq=brightness=-0.06[bg];[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
    if fit == "fit":  # 검은 여백
        return f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black"
    return f"scale={W}:{H}:flags=lanczos:force_original_aspect_ratio=increase,crop={W}:{H}"


def build_reel(proj: Project, music: str | None = None, log: Log | None = None,
               progress: Callable[[float, str], None] | None = None,
               stop: Callable[[], bool] | None = None) -> Path:
    log = log or Log()
    progress = progress or (lambda p, s: None)
    proj.reload()
    shots = proj.shots
    if not shots:
        die("컷이 없습니다.")
    oc = CFG["output"]
    W, H = output_size(proj.aspect)
    FPS = int(oc["fps"])
    grade = CFG["grade"].get(proj.data.get("grade", "none"), "")
    work = proj.dir / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    enc = ["-c:v", "libx264", "-preset", oc["preset"], "-crf", str(oc["crf"]), "-g", str(FPS),
           "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    rife = rife_bin()
    parts = []

    for i, s in enumerate(shots):
        if stop and stop():
            die("취소됨")
        sec = float(s["seconds"])
        label = f"[{i + 1}/{len(shots)}] {s.get('role') or s['id']}"
        progress(i / (len(shots) + 1), f"{label} 처리 중")
        norm = work / f"{i:02d}.mp4"
        post = f"{oc['sharpen']},fps={FPS},setsar=1" + (f",{grade}" if grade else "") + ",format=yuv420p"

        if s["type"] == "video":
            if not s.get("video"):
                die(f"{label}: 동영상이 없습니다.")
            src = proj.path(s["video"])
            meta = ffprobe(src)
            start = float(s.get("start") or 0)
            avail = max(0.0, meta["duration"] - start)
            pad = max(0.0, sec - avail)
            vf = _fit_filter(s.get("fit", "crop"), W, H) + "," + post
            if pad > 0.05:
                vf += f",tpad=stop_mode=clone:stop_duration={pad + 0.1:.2f}"
                log(f"  {label}: 영상이 {pad:.1f}초 짧아 마지막 장면을 늘립니다")
            run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src),
                 "-t", f"{sec:g}", "-vf", vf, "-t", f"{sec:g}", "-an", *enc, str(norm)], log, stop)
        else:
            if not s.get("take"):
                die(f"{label}: 채택한 영상이 없습니다. 먼저 생성하고 '채택'을 누르세요.")
            src = proj.path(s["take"])
            if not src.exists():
                die(f"{label}: 채택한 영상 파일이 없습니다 ({s['take']}).")
            meta = ffprobe(src)
            dur = meta["duration"] or 0.1
            # 원본이 컷보다 짧으면 보간으로 늘려 슬로모션, 길면 잘라 씀
            target = int(round(max(dur, sec) * FPS))
            fit = _fit_filter("crop", W, H)
            if rife and abs(meta["fps"] - FPS) > 0.01:
                fin, fout = work / "in", work / "out"
                for d in (fin, fout):
                    shutil.rmtree(d, ignore_errors=True)
                    d.mkdir()
                run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), str(fin / "%06d.png")], log, stop)
                progress((i + 0.4) / (len(shots) + 1), f"{label} 30fps 보간")
                run([str(rife), "-i", str(fin), "-o", str(fout),
                     "-m", str(rife.parent / oc["rife_model"]), "-n", str(target)], log, stop)
                inp = ["-framerate", str(FPS), "-i", str(fout / "%08d.png")]
                vf = f"{fit},{post}"
            else:
                factor = max(1.0, sec / dur)
                inp = ["-i", str(src)]
                vf = (f"setpts={factor:.4f}*PTS," if factor > 1.001 else "") + f"{fit},{post}"
            run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inp, "-vf", vf, "-t", f"{sec:g}", "-an",
                 *enc, str(norm)], log, stop)
            shutil.rmtree(work / "in", ignore_errors=True)
            shutil.rmtree(work / "out", ignore_errors=True)
        parts.append(norm.name)
        log(f"  {label} ✓ {sec:g}초")

    progress(len(shots) / (len(shots) + 1), "합치기·음악")
    (work / "list.txt").write_text("".join(f"file '{p}'\n" for p in parts))
    video = work / "video.mp4"
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", str(work / "list.txt"), "-c", "copy", str(video)], log, stop)

    total = proj.total_seconds()
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = proj.aspect.replace(":", "x")
    out = proj.dir / "final" / f"{proj.name}_{tag}_{stamp}.mp4"
    music = music if music is not None else proj.data.get("music", "")
    if music:
        mpath = proj.path(music) if not Path(music).is_absolute() else Path(music)
        if not mpath.exists():
            mpath = rel(music)
        if not mpath.exists():
            die(f"음악 파일이 없습니다: {music}")
        af = (f"afade=t=in:d=0.5,afade=t=out:st={max(0.0, total - 2):g}:d=2,"
              f"loudnorm=I={oc['audio_loudness']}:TP=-1.5:LRA=11")
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video), "-stream_loop", "-1", "-i", str(mpath),
             "-map", "0:v", "-map", "1:a", "-af", af, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-ar", "48000", "-t", f"{total:g}", "-movflags", "+faststart", str(out)], log, stop)
    else:
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video),
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-t", f"{total:g}",
             "-movflags", "+faststart", str(out)], log, stop)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    m = ffprobe(out)
    log(f"완성: {out.name} · {m['width']}x{m['height']} · {m['fps']:.0f}fps · {m['duration']:.2f}초")
    progress(1.0, "완료")
    return out


# ───────────────────────── 환경 점검 ─────────────────────────

def doctor() -> list[tuple[str, bool, str]]:
    rows = []
    rows.append(("macOS / Apple Silicon", IS_MAC and platform.machine() == "arm64",
                 f"{platform.system()} {platform.machine()}"))
    if IS_MAC:
        chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
        mem = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip() or 0)
        rows.append(("칩 / 통합 메모리", mem >= 16 * 2**30, f"{chip} · {mem / 2**30:.0f}GB"))
    rows.append(("Python ≥ 3.11", sys.version_info >= (3, 11), sys.version.split()[0]))
    for t in ("ffmpeg", "ffprobe"):
        rows.append((t, bool(shutil.which(t)), shutil.which(t) or "없음 → ./setup.sh"))
    mf = shutil.which(CFG["keyframe"]["command"]) or (Path.home() / ".local/bin" / CFG["keyframe"]["command"]).exists()
    rows.append(("mflux (키 이미지, 선택)", bool(mf), "있음" if mf else "없음 → ./setup.sh"))
    mod = mlx_video_module()
    rows.append(("mlx-video", bool(mod), mod or "없음 → ./setup.sh"))
    rows.append(("RIFE (보간)", bool(rife_bin()), "있음" if rife_bin() else "없음 (프레임 복제로 대체)"))
    rows.append(("Wan2.2 TI2V-5B (MLX)", model_dir().exists(),
                 str(model_dir().relative_to(ROOT)) if model_dir().exists() else "없음 → ./scripts/download_models.sh"))
    free = shutil.disk_usage(ROOT).free / 2**30
    rows.append(("디스크 여유", free > 20, f"{free:.0f}GB"))
    return rows


# ───────────────────────── CLI ─────────────────────────

def _cli() -> None:
    ap = argparse.ArgumentParser(prog="mc", description="moviecreator CLI (웹 UI: ./web.sh)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="환경 점검")

    s = sub.add_parser("new", help="새 프로젝트")
    s.add_argument("name")
    s.add_argument("--template", "-t", default="blank")
    s.add_argument("--aspect", "-a", default="9:16", choices=list(CFG["aspect"]))

    s = sub.add_parser("photo", help="사진을 컷의 키 이미지로")
    s.add_argument("project"); s.add_argument("shot"); s.add_argument("photo")

    s = sub.add_parser("gen", help="AI 컷 생성")
    s.add_argument("project")
    s.add_argument("--only", help="컷 id (쉼표 구분)")
    s.add_argument("--quality", "-q", default="draft", choices=QUALITIES)
    s.add_argument("--seed", type=int, help="시드 (없으면 컷 seed, 0이면 랜덤)")
    s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("reel", help="최종 영상 조립")
    s.add_argument("project")
    s.add_argument("--music")

    a = ap.parse_args()
    try:
        if a.cmd == "doctor":
            rows = doctor()
            for name, ok, detail in rows:
                print(f" {'✓' if ok else '✗'} {name:<26} {detail}")
            print("\n준비 완료" if all(r[1] for r in rows if "선택" not in r[0]) else "\n✗ 항목을 해결하세요.")
        elif a.cmd == "new":
            p = new_project(a.name, a.template, a.aspect)
            print(f"생성: {p.file.relative_to(ROOT)}")
        elif a.cmd == "photo":
            p = Project(a.project)
            sh = p.shot(a.shot)
            dst = p.dir / "keyframes" / f"{sh['id']}.png"
            crop_image(Path(a.photo).expanduser(), dst, p.aspect)
            sh["keyframe"] = p.relpath(dst)
            p.save()
            print(f"키 이미지: {sh['keyframe']}")
        elif a.cmd == "gen":
            p = Project(a.project)
            wanted = set(a.only.split(",")) if a.only else None
            for sh in p.shots:
                if sh["type"] != "ai" or (wanted and sh["id"] not in wanted):
                    continue
                seed = a.seed if a.seed is not None else int(sh.get("seed") or 0)
                seed = seed or random.randint(1, 99999)
                out = take_path(p, sh, a.quality, seed)
                cmd = generate_cmd(p, sh, a.quality, seed, out)
                print(f"▶ [{sh['id']}] {a.quality} seed {seed} (예상 {estimate_seconds(p.aspect, a.quality, sh['seconds']) // 60}분)")
                if a.dry_run:
                    print("  " + " ".join(cmd))
                    continue
                subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)
                p.reload()
                if not p.shot(sh["id"]).get("take") or a.quality != "draft":
                    p.shot(sh["id"])["take"] = p.relpath(out)
                    p.save()
        elif a.cmd == "reel":
            out = build_reel(Project(a.project), a.music)
            print(f"→ {out.relative_to(ROOT)}")
    except McError as e:
        sys.exit(f"✗ {e}")


if __name__ == "__main__":
    _cli()
