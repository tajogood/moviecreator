#!/usr/bin/env python3
"""moviecreator — Apple Silicon 로컬 AI 릴스 제작 파이프라인 CLI.

키 이미지(mflux) → 이미지→영상(mlx-video + Wan2.2 TI2V-5B) → RIFE 30fps 보간
→ 1080x1920 정규화 → 합치기 + 음악 → 인스타 릴스 MP4.

사용 예:
    ./mc doctor
    ./mc new cafe_launch --template product
    ./mc keyframes cafe_launch
    ./mc draft cafe_launch --seeds 42,7,99
    ./mc final cafe_launch
    ./mc reel cafe_launch --music music/track.mp3
    ./mc all cafe_launch --music music/track.mp3
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    sys.exit("Python 3.11 이상이 필요합니다. ./setup.sh 로 만든 .venv 를 쓰거나 ./mc 래퍼로 실행하세요.")

ROOT = Path(__file__).resolve().parent
IS_MAC = platform.system() == "Darwin"


# ───────────────────────── 공통 유틸 ─────────────────────────

def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


CFG = load_toml(ROOT / "config.toml")


def p(rel: str) -> Path:
    path = Path(rel).expanduser()
    return path if path.is_absolute() else ROOT / path


def info(msg: str) -> None:
    print(f"\033[36m▶\033[0m {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"\033[33m!\033[0m {msg}", flush=True)


def die(msg: str) -> None:
    print(f"\033[31m✗\033[0m {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def run(cmd: list[str], *, dry: bool = False, cwd: Path | None = None, quiet: bool = False) -> None:
    shown = " ".join(_q(c) for c in cmd)
    if dry:
        print(f"  [dry-run] {shown}")
        return
    if not quiet:
        print(f"  $ {shown}", flush=True)
    res = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL)
    if res.returncode != 0:
        die(f"명령 실패 (exit {res.returncode}): {cmd[0]}")


def _q(s: str) -> str:
    s = str(s)
    return s if re.fullmatch(r"[\w./:=,+@%-]+", s) else "'" + s.replace("'", "'\\''") + "'"


def need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        die(f"'{tool}' 을(를) 찾을 수 없습니다. ./setup.sh 를 먼저 실행하세요.")
    return path


def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    st = data["streams"][0]
    num, den = (int(x) for x in st["r_frame_rate"].split("/"))
    return {
        "width": int(st["width"]), "height": int(st["height"]),
        "fps_num": num, "fps_den": den, "fps": num / den if den else 0,
        "duration": float(data["format"].get("duration", 0)),
    }


def log_event(project: str, **kw) -> None:
    logdir = p(CFG["paths"]["logs"])
    logdir.mkdir(parents=True, exist_ok=True)
    kw["time"] = dt.datetime.now().isoformat(timespec="seconds")
    with open(logdir / f"{project}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\n")


# ───────────────────────── 프로젝트 ─────────────────────────

class Project:
    def __init__(self, name: str):
        self.name = name
        self.dir = p(CFG["paths"]["projects"]) / name
        cfg_path = self.dir / "project.toml"
        if not cfg_path.exists():
            die(f"{cfg_path} 가 없습니다. 먼저 ./mc new {name} --template product 로 만드세요.")
        self.cfg = load_toml(cfg_path)
        self.shots = self.cfg.get("shots", [])
        if not self.shots:
            die("project.toml 에 [[shots]] 가 없습니다.")
        for i, s in enumerate(self.shots):
            s.setdefault("id", f"{i + 1:02d}")
            s["id"] = str(s["id"])
            if "seconds" not in s or "prompt" not in s:
                die(f"shot {s['id']}: seconds 와 prompt 는 필수입니다.")
            s.setdefault("seed", 42)
        total = sum(s["seconds"] for s in self.shots)
        target = self.cfg.get("target_seconds", total)
        if abs(total - target) > 1e-6:
            warn(f"컷 길이 합계 {total}s ≠ target_seconds {target}s")
        for sub in ("keyframes", "drafts", "clips", "work", "final"):
            (self.dir / sub).mkdir(exist_ok=True)

    def total_seconds(self) -> float:
        return float(sum(s["seconds"] for s in self.shots))

    def select(self, only: str | None) -> list[dict]:
        if not only:
            return self.shots
        wanted = {x.strip() for x in only.split(",")}
        sel = [s for s in self.shots if s["id"] in wanted]
        if not sel:
            die(f"--only {only}: 해당 컷이 없습니다.")
        return sel

    def video_prompt(self, shot: dict) -> str:
        parts = [shot["prompt"].strip(), self.cfg.get("style_suffix", "").strip()]
        return " ".join(x for x in parts if x)

    def negative(self) -> str:
        neg = self.cfg.get("negative_prompt")
        if neg:
            return neg.strip()
        return (ROOT / "prompts" / "negative.txt").read_text(encoding="utf-8").strip()

    def keyframe_path(self, shot: dict) -> Path:
        kf = shot.get("keyframe") or f"keyframes/{shot['id']}.png"
        return (self.dir / kf).resolve()


# ───────────────────────── mlx-video 감지 ─────────────────────────

def mlx_video_module() -> str:
    for mod in ("mlx_video.wan_2.generate", "mlx_video.generate_wan"):
        try:
            if importlib.util.find_spec(mod):
                return mod
        except ModuleNotFoundError:
            continue
    die("mlx-video 의 Wan 생성 모듈을 찾지 못했습니다. ./setup.sh 를 실행했는지, .venv 로 실행 중인지 확인하세요.")
    return ""


def mlx_video_flags(module: str) -> dict:
    """버전마다 다른 옵션명을 --help 로 확인해 캐시한다."""
    cache = ROOT / "tools" / ".mlx_video_flags.json"
    if cache.exists():
        data = json.loads(cache.read_text())
        if data.get("module") == module:
            return data
    out = subprocess.run([sys.executable, "-m", module, "--help"], capture_output=True, text=True)
    text = out.stdout + out.stderr

    def pick(*cands):
        return next((c for c in cands if re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", text)), None)

    data = {
        "module": module,
        "steps": pick("--steps", "--num-inference-steps", "--sample-steps", "--num-steps"),
        "image": pick("--image", "--image-path"),
        "negative": pick("--negative-prompt", "--neg-prompt"),
        "frames": pick("--num-frames", "--frames", "-n"),
        "output": pick("--output-path", "--output", "-o"),
        "seed": pick("--seed"),
    }
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, indent=2))
    return data


def frames_for(seconds: float, mode: str, shot: dict) -> int:
    if "frames" in shot and mode == "final":
        n = int(shot["frames"])
    elif mode == "draft":
        n = int(CFG["draft"]["frames"])
    else:
        fps = CFG["video"]["native_fps"]
        n = int((seconds + CFG["final"]["extra_seconds"]) * fps)
    n = (n // 4) * 4 + 1                      # 4n+1
    return max(17, min(n, int(CFG["video"]["max_frames"])))


def memory_check() -> None:
    if not IS_MAC or not shutil.which("memory_pressure"):
        return
    out = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout
    m = re.search(r"free percentage:\s*(\d+)%", out)
    if m and int(m.group(1)) < CFG["video"]["min_free_memory_pct"]:
        warn(f"메모리 여유 {m.group(1)}% — 브라우저·Docker 등 다른 앱을 종료하면 스왑을 줄일 수 있습니다.")


def generate_clip(proj: Project, shot: dict, mode: str, seed: int, out: Path, dry: bool) -> None:
    rc = CFG[mode]
    module = mlx_video_module() if not dry else "mlx_video.wan_2.generate"
    flags = mlx_video_flags(module) if not dry else {
        "steps": "--steps", "image": "--image", "negative": "--negative-prompt",
        "frames": "--num-frames", "output": "--output-path", "seed": "--seed"}
    model = p(CFG["paths"]["wan_model"])
    if not dry and not model.exists():
        die(f"모델 폴더가 없습니다: {model}\n  → ./scripts/download_models.sh 를 먼저 실행하세요.")

    frames = frames_for(shot["seconds"], mode, shot)
    cmd = [sys.executable, "-m", module, "--model-dir", str(model),
           "--prompt", proj.video_prompt(shot),
           "--width", str(rc["width"]), "--height", str(rc["height"]),
           flags["frames"] or "--num-frames", str(frames)]
    if flags.get("negative"):
        cmd += [flags["negative"], proj.negative()]
    if flags.get("steps"):
        cmd += [flags["steps"], str(shot.get(f"{mode}_steps", rc["steps"]))]
    if flags.get("seed"):
        cmd += [flags["seed"], str(seed)]
    kf = proj.keyframe_path(shot)
    if kf.exists():
        if flags.get("image"):
            cmd += [flags["image"], str(kf)]
        else:
            warn("이 mlx-video 버전은 --image 를 지원하지 않아 텍스트→영상으로 생성합니다.")
    else:
        warn(f"[{shot['id']}] 키 이미지 없음 → 텍스트→영상(T2V)으로 생성")
    cmd += [flags["output"] or "--output-path", str(out)]

    if IS_MAC and shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i"] + cmd

    info(f"[{shot['id']}] {mode} {rc['width']}x{rc['height']} · {frames}f · seed {seed}")
    if not dry:
        memory_check()
    start = time.time()
    run(cmd, dry=dry, quiet=True)
    if not dry:
        took = round(time.time() - start)
        info(f"[{shot['id']}] 완료 {took}s → {out.relative_to(ROOT)}")
        log_event(proj.name, step=mode, shot=shot["id"], seed=seed, frames=frames,
                  size=f"{rc['width']}x{rc['height']}", seconds=took, out=str(out.relative_to(ROOT)))


# ───────────────────────── 명령: new / doctor ─────────────────────────

def cmd_new(a) -> None:
    tpl = ROOT / "templates" / f"{a.template}.toml"
    if not tpl.exists():
        die(f"템플릿 없음: {a.template} (product | place | travel)")
    d = p(CFG["paths"]["projects"]) / a.name
    if (d / "project.toml").exists():
        die(f"이미 있습니다: {d}")
    for sub in ("keyframes", "drafts", "clips", "work", "final"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy(tpl, d / "project.toml")
    info(f"생성: {d.relative_to(ROOT)}/project.toml")
    print("  다음: project.toml 의 prompt / keyframe_prompt 를 제품·장소에 맞게 고친 뒤\n"
          f"        ./mc keyframes {a.name}  (실제 사진이 있으면 keyframes/01.png … 로 넣기)")


def cmd_doctor(a) -> None:
    ok = True

    def row(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        mark = "\033[32m✓\033[0m" if good else "\033[31m✗\033[0m"
        print(f" {mark} {name:<28} {detail}")

    print("moviecreator doctor\n")
    row("macOS / Apple Silicon", IS_MAC and platform.machine() == "arm64",
        f"{platform.system()} {platform.machine()}")
    if IS_MAC:
        chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
        mem = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip() or 0)
        gb = mem / 2**30
        row("칩 / 통합 메모리", gb >= 16, f"{chip} · {gb:.0f}GB" + ("  (24GB 미만: draft·4bit 권장)" if gb < 24 else ""))
    row("Python ≥ 3.11", sys.version_info >= (3, 11), sys.version.split()[0] + f" ({sys.executable})")
    for t in ("ffmpeg", "ffprobe", CFG["keyframe"]["command"]):
        row(t, shutil.which(t), shutil.which(t) or "없음 → ./setup.sh")
    try:
        mod = next(m for m in ("mlx_video.wan_2.generate", "mlx_video.generate_wan") if importlib.util.find_spec(m))
        row("mlx-video", True, mod)
    except (StopIteration, ModuleNotFoundError):
        row("mlx-video", False, "없음 → ./setup.sh")
    rife = p(CFG["paths"]["rife_dir"]) / "rife-ncnn-vulkan"
    row("RIFE (보간)", rife.exists(), str(rife.relative_to(ROOT)) if rife.exists() else "없음 → ./setup.sh (없으면 프레임 복제로 대체)")
    model = p(CFG["paths"]["wan_model"])
    row("Wan2.2 TI2V-5B (MLX)", model.exists(), str(model.relative_to(ROOT)) if model.exists() else "없음 → ./scripts/download_models.sh")
    free = shutil.disk_usage(ROOT).free / 2**30
    row("디스크 여유", free > 20, f"{free:.0f}GB" + (" (모델 다운로드엔 60GB 이상 권장)" if free < 60 else ""))
    print("\n준비 완료" if ok else "\n✗ 표시 항목을 해결한 뒤 다시 실행하세요.")


# ───────────────────────── 명령: keyframes / draft / final ─────────────────────────

def cmd_keyframes(a) -> None:
    proj = Project(a.project)
    kc = CFG["keyframe"]
    for shot in proj.select(a.only):
        out = proj.keyframe_path(shot)
        if out.exists() and not a.force:
            info(f"[{shot['id']}] 키 이미지 있음 — 건너뜀 ({out.name})")
            continue
        kp = shot.get("keyframe_prompt")
        if not kp:
            warn(f"[{shot['id']}] keyframe_prompt 없음 — 실제 사진을 {out.relative_to(proj.dir)} 로 넣거나 T2V로 진행")
            continue
        if not a.dry_run:
            need(kc["command"])
        prompt = " ".join(x for x in [kp.strip(), proj.cfg.get("keyframe_suffix", "").strip()] if x)
        seed = shot.get("keyframe_seed", shot["seed"])
        cmd = [kc["command"], "--model", kc["model"], "-q", str(kc["quantize"]), "--steps", str(kc["steps"]),
               "--seed", str(seed), "--width", str(kc["width"]), "--height", str(kc["height"]),
               "--prompt", prompt, "--output", str(out)]
        info(f"[{shot['id']}] 키 이미지 생성 {kc['width']}x{kc['height']} seed {seed}")
        run(cmd, dry=a.dry_run, quiet=True)
        if not a.dry_run:
            log_event(proj.name, step="keyframe", shot=shot["id"], seed=seed, out=str(out.relative_to(ROOT)))


def cmd_import_photo(a) -> None:
    """실제 사진을 키 이미지 규격(704x1280, 9:16 크롭)으로 변환."""
    proj = Project(a.project)
    shot = next((s for s in proj.shots if s["id"] == a.shot), None)
    if not shot:
        die(f"컷 {a.shot} 이 없습니다.")
    kc = CFG["keyframe"]
    out = proj.keyframe_path(shot)
    w, h = kc["width"], kc["height"]
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(Path(a.photo).expanduser()),
         "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
         "-frames:v", "1", str(out)])
    info(f"[{shot['id']}] → {out.relative_to(ROOT)}")


def _gen(a, mode: str) -> None:
    proj = Project(a.project)
    shots = proj.select(a.only)
    cooldown = CFG["video"]["cooldown_seconds"]
    seeds_override = [int(x) for x in a.seeds.split(",")] if getattr(a, "seeds", None) else None
    jobs = []
    for shot in shots:
        for seed in (seeds_override or [shot["seed"]]):
            if mode == "draft":
                out = proj.dir / "drafts" / f"{shot['id']}_s{seed}.mp4"
            else:
                out = proj.dir / "clips" / f"{shot['id']}.mp4"
            jobs.append((shot, seed, out))
    for i, (shot, seed, out) in enumerate(jobs):
        if out.exists() and not a.force:
            info(f"[{shot['id']}] 이미 있음 — 건너뜀 ({out.name}, 다시 만들려면 --force)")
            continue
        generate_clip(proj, shot, mode, seed, out, a.dry_run)
        if i < len(jobs) - 1 and not a.dry_run and cooldown:
            time.sleep(cooldown)


def cmd_draft(a) -> None:
    _gen(a, "draft")


def cmd_final(a) -> None:
    _gen(a, "final")


# ───────────────────────── 명령: reel ─────────────────────────

def interpolate_to_30(src: Path, work: Path, meta: dict, dry: bool) -> Path | None:
    """RIFE v4 의 -n(목표 프레임 수)으로 원본 fps → 30fps. RIFE 없으면 None."""
    out_fps = CFG["output"]["fps"]
    rife = p(CFG["paths"]["rife_dir"]) / "rife-ncnn-vulkan"
    if not rife.exists():
        return None
    fin, fout = work / "in", work / "out"
    for d in (fin, fout):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), str(fin / "%06d.png")], dry=dry, quiet=True)
    n_in = len(list(fin.glob("*.png"))) if not dry else 97
    target = round(n_in * out_fps * meta["fps_den"] / meta["fps_num"])
    model = p(CFG["paths"]["rife_dir"]) / CFG["output"]["rife_model"]
    run([str(rife), "-i", str(fin), "-o", str(fout), "-m", str(model), "-n", str(target)], dry=dry, quiet=True)
    shutil.rmtree(fin, ignore_errors=True)
    return fout


def cmd_reel(a) -> None:
    need("ffmpeg"); need("ffprobe")
    proj = Project(a.project)
    oc = CFG["output"]
    W, H, FPS = oc["width"], oc["height"], oc["fps"]
    work = proj.dir / "work"
    work.mkdir(exist_ok=True)
    enc = ["-c:v", "libx264", "-preset", oc["preset"], "-crf", str(oc["crf"]), "-g", str(FPS),
           "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    listfile = work / "list.txt"
    lines = []

    for shot in proj.shots:
        src = proj.dir / "clips" / f"{shot['id']}.mp4"
        if not src.exists():
            die(f"클립 없음: {src.relative_to(ROOT)}  → ./mc final {proj.name} --only {shot['id']}")
        meta = ffprobe(src)
        sec = float(shot["seconds"])
        info(f"[{shot['id']}] {meta['width']}x{meta['height']} {meta['fps']:.2f}fps {meta['duration']:.2f}s → {sec:g}s")

        inputs = ["-i", str(src)]
        if abs(meta["fps"] - FPS) > 0.01 and not a.no_interp:
            frames_dir = interpolate_to_30(src, work, meta, a.dry_run)
            if frames_dir:
                inputs = ["-framerate", str(FPS), "-i", str(frames_dir / "%08d.png")]
            else:
                warn("  RIFE 없음 → fps 필터(프레임 복제)로 30fps 변환. 끊김이 보이면 ./setup.sh 로 RIFE 설치")

        # 원본이 컷보다 짧으면 마지막 프레임을 늘려 길이를 맞춘다
        short = max(0.0, sec - meta["duration"])
        if short > 0.05:
            warn(f"  클립이 {short:.2f}s 짧아 마지막 프레임으로 채웁니다 (frames 를 늘려 재생성 권장)")
        vf = (f"scale={W}:{H}:flags=lanczos:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"{oc['sharpen']}," + (f"tpad=stop_mode=clone:stop_duration={short + 0.1:.2f}," if short > 0.05 else "")
              + f"fps={FPS},setsar=1,format=yuv420p")
        norm = work / f"{shot['id']}_30.mp4"
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-vf", vf, "-t", f"{sec:g}", "-an", *enc, str(norm)],
            dry=a.dry_run, quiet=True)
        shutil.rmtree(work / "out", ignore_errors=True)
        lines.append(f"file '{norm.name}'")

    listfile.write_text("\n".join(lines) + "\n")
    video = work / "video.mp4"
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c", "copy", str(video)], dry=a.dry_run, quiet=True)

    total = proj.total_seconds()
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    out = proj.dir / "final" / f"{proj.name}_reel_{stamp}.mp4"
    music = a.music or proj.cfg.get("music")
    if music:
        mpath = Path(music).expanduser()
        if not mpath.is_absolute():
            mpath = (ROOT / mpath) if (ROOT / mpath).exists() else (proj.dir / mpath)
        if not mpath.exists() and not a.dry_run:
            die(f"음악 파일 없음: {mpath}")
        af = (f"afade=t=in:d=0.5,afade=t=out:st={max(0, total - 2):g}:d=2,"
              f"loudnorm=I={oc['audio_loudness']}:TP=-1.5:LRA=11")
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video), "-i", str(mpath),
             "-map", "0:v", "-map", "1:a", "-af", af, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-ar", "48000", "-t", f"{total:g}", "-movflags", "+faststart", str(out)], dry=a.dry_run, quiet=True)
    else:
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video),
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-t", f"{total:g}",
             "-movflags", "+faststart", str(out)], dry=a.dry_run, quiet=True)

    if a.dry_run:
        return
    m = ffprobe(out)
    good = m["width"] == W and m["height"] == H and abs(m["fps"] - FPS) < 0.01 and abs(m["duration"] - total) < 0.15
    (info if good else warn)(f"출력: {out.relative_to(ROOT)} · {m['width']}x{m['height']} · {m['fps']:.2f}fps · "
                             f"{m['duration']:.2f}s (목표 {total:g}s)" + ("" if good else "  ← 규격 확인 필요"))
    log_event(proj.name, step="reel", out=str(out.relative_to(ROOT)), duration=m["duration"])
    if not a.keep_work:
        for f in work.glob("*_30.mp4"):
            f.unlink()


def cmd_all(a) -> None:
    a.only = None
    cmd_keyframes(a)
    cmd_final(a)
    cmd_reel(a)


# ───────────────────────── CLI ─────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(prog="mc", description="Apple Silicon 로컬 AI 릴스 제작 파이프라인")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("doctor", help="환경 점검")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("new", help="템플릿으로 새 프로젝트 만들기")
    s.add_argument("name")
    s.add_argument("--template", "-t", default="product", choices=["product", "place", "travel"])
    s.set_defaults(fn=cmd_new)

    def common(sp, seeds=False):
        sp.add_argument("project")
        sp.add_argument("--only", help="특정 컷만 (예: 01,03)")
        sp.add_argument("--force", action="store_true", help="기존 결과 덮어쓰기")
        sp.add_argument("--dry-run", action="store_true", help="명령만 출력")
        if seeds:
            sp.add_argument("--seeds", help="시드 목록 (예: 42,7,99) — 컷마다 여러 개 시안")

    s = sub.add_parser("keyframes", help="mflux 로 키 이미지 생성")
    common(s); s.set_defaults(fn=cmd_keyframes)

    s = sub.add_parser("photo", help="실제 사진을 키 이미지로 (704x1280 크롭)")
    s.add_argument("project"); s.add_argument("shot"); s.add_argument("photo")
    s.set_defaults(fn=cmd_import_photo)

    s = sub.add_parser("draft", help="저해상도 시안 생성")
    common(s, seeds=True); s.set_defaults(fn=cmd_draft)

    s = sub.add_parser("final", help="최종 해상도 클립 생성")
    common(s, seeds=True); s.set_defaults(fn=cmd_final)

    def reel_opts(sp):
        sp.add_argument("--music", help="배경 음악 파일")
        sp.add_argument("--no-interp", action="store_true", help="RIFE 보간 끄기")
        sp.add_argument("--keep-work", action="store_true", help="중간 파일 보존")

    s = sub.add_parser("reel", help="보간·정규화·합치기 → 최종 MP4")
    s.add_argument("project"); s.add_argument("--dry-run", action="store_true"); reel_opts(s)
    s.set_defaults(fn=cmd_reel)

    s = sub.add_parser("all", help="keyframes → final → reel 한 번에")
    common(s, seeds=False); reel_opts(s); s.set_defaults(fn=cmd_all)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
