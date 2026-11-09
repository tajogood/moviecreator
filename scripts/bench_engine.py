"""상주 엔진 성능 측정: 같은 엔진으로 연속 2회 생성해 첫 실행(모델 로드)과 상주 상태 시간을 비교.

사용: .venv/bin/python scripts/bench_engine.py [fast|base] [standard|draft|final] [9:16|16:9|1:1]
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mc  # noqa: E402

model = sys.argv[1] if len(sys.argv) > 1 else "fast"
quality = sys.argv[2] if len(sys.argv) > 2 else "standard"
aspect = sys.argv[3] if len(sys.argv) > 3 else "9:16"
m = mc.CFG["models"][model]
base = mc.rel(mc.CFG["models"]["base"]["dir"])
w, h = mc.gen_size(aspect, quality)
frames = int(m.get("fixed_frames") or mc.frames_for(5, quality))
steps = int(m.get("steps") or mc.CFG["quality"][quality]["steps"])
out_dir = ROOT / "logs" / "bench"
out_dir.mkdir(parents=True, exist_ok=True)

proc = subprocess.Popen([sys.executable, str(ROOT / "web" / "engine.py")], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, text=True, bufsize=1, cwd=ROOT)
print(json.loads(proc.stdout.readline()))
prompts = ["A matte black stainless-steel tumbler on wet slate, steam rising, slow orbit camera, warm rim light. "
           "Premium commercial product film, shallow depth of field.",
           "A cozy minimalist cafe interior, morning sunlight through tall windows, dust in light beams, "
           "slow dolly forward. Cinematic commercial film."]
results = []
for i, prompt in enumerate(prompts):
    job = {"cmd": "generate", "id": f"b{i}", "model_dir": str(mc.rel(m["dir"])),
           "t5_source": str(base / "t5_encoder.safetensors"), "vae_path": str(base / "vae.safetensors"),
           "prompt": prompt + " " + mc.ASPECT_PHRASE[aspect], "negative": (ROOT / "prompts/negative.txt").read_text().strip(),
           "image": None, "seed": 42, "output": str(out_dir / f"{model}_{quality}_{i}.mp4"),
           "width": w, "height": h, "frames": frames, "steps": steps, "sampler": m.get("sampler", "")}
    t = time.time()
    proc.stdin.write(json.dumps(job) + "\n")
    proc.stdin.flush()
    for line in proc.stdout:
        ev = json.loads(line)
        if ev.get("event") == "progress":
            print(f"  [{i + 1}] {ev['stage']}", flush=True)
        elif ev.get("event") in ("done", "error"):
            ev["wall"] = round(time.time() - t, 1)
            print(f"[{i + 1}] {json.dumps(ev, ensure_ascii=False)}", flush=True)
            results.append(ev)
            break
proc.stdin.close()
proc.wait(30)
print(f"\n== {model} · {quality} · {w}x{h} · {frames}프레임 · {steps}스텝")
for i, r in enumerate(results):
    print(f"  {i + 1}회차: {r.get('wall')}초  {r.get('timings')}  메모리 {r.get('memory', {}).get('active_gb')}GB "
          f"(최대 {r.get('memory', {}).get('peak_gb')}GB)")
