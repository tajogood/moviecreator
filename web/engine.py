"""MovieCreator 상주 생성 엔진.

mlx-video(Wan2.2) 구성요소를 그대로 쓰되, 매 작업마다 모델을 다시 불러오지 않고
메모리에 붙잡아 둔다.

  - 텍스트 인코더(umT5-XXL): 8bit 로 한 번 변환해 디스크에 저장 → 이후 6GB 로 상주
    (mlx-video 기본 동작은 매번 11GB 를 float32(22GB)로 올려서 24GB 맥에서 스왑 발생)
  - 트랜스포머(DiT): 마지막에 쓴 모델 1개 상주 (원본 ↔ 증류 전환 시 교체)
  - VAE 디코더: 상주, VAE 인코더: 사진 입력 시에만 잠깐
  - 같은 프롬프트는 텍스트 임베딩을 디스크 캐시에서 재사용
  - 증류(DMD) 모델: 3스텝, CFG 없음 (재노이즈 샘플링)

통신: stdin 으로 JSON 한 줄 명령, 원래 stdout 으로 JSON 한 줄 이벤트.
(mlx-video 의 print 출력은 stderr 로 돌린다.)
"""
from __future__ import annotations

import gc
import hashlib
import json
import math
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

# 프로토콜 채널: 원래 stdout 을 따로 잡고, 이후 print 는 stderr 로
_proto = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

import mlx.core as mx  # noqa: E402
import mlx.nn as nn  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FREE_BEFORE_DECODE = os.environ.get("MC_FREE_BEFORE_DECODE", "1") != "0"
mx.set_cache_limit(int(float(os.environ.get("MC_CACHE_LIMIT_GB", "2")) * 2**30))
CACHE = ROOT / "models" / "cache"
DMD_SIGMAS = [1.0, 0.757, 0.522, 0.0]


def emit(**kw) -> None:
    _proto.write(json.dumps(kw, ensure_ascii=False) + "\n")
    _proto.flush()


class Canceled(Exception):
    pass


# ───────────────────────── 설정 ─────────────────────────

def load_config(model_dir: Path):
    from mlx_video.models.wan_2.config import WanModelConfig

    raw = json.loads((model_dir / "config.json").read_text())
    quant = raw.get("quantization")
    d = {k: v for k, v in raw.items() if k in WanModelConfig.__dataclass_fields__}
    for key in ("patch_size", "vae_stride", "window_size", "sample_guide_scale"):
        if key in d and isinstance(d[key], list):
            d[key] = tuple(d[key])
    cfg = WanModelConfig(**d)
    if cfg.in_dim == 48 and cfg.vae_z_dim != 48:  # mlx-video 와 같은 보정
        cfg = WanModelConfig(**{**{f: getattr(cfg, f) for f in cfg.__dataclass_fields__},
                                "vae_z_dim": 48, "vae_stride": (4, 16, 16), "sample_fps": 24})
    dmd = raw.get("fastwan_dmd")
    return cfg, quant, dmd, raw


# ───────────────────────── 엔진 ─────────────────────────

class Engine:
    def __init__(self):
        self.cancel = threading.Event()
        self.dit = None
        self.dit_key = None
        self.dit_call = None
        self.cfg = None
        self.quant = None
        self.dmd = None
        self.t5 = None
        self.tok = None
        self.vae_dec = None
        self.vae_key = None
        self.last_used = time.time()

    # ── 상태
    def status(self) -> dict:
        return {"dit": self.dit_key, "t5": self.t5 is not None, "vae": self.vae_dec is not None,
                "active_gb": round(mx.get_active_memory() / 2**30, 2),
                "peak_gb": round(mx.get_peak_memory() / 2**30, 2),
                "idle": int(time.time() - self.last_used)}

    def unload(self, what: str = "all") -> None:
        if what in ("all", "dit"):
            self.dit = self.dit_call = None
            self.dit_key = None
        if what in ("all", "t5"):
            self.t5 = None
        if what in ("all", "vae"):
            self.vae_dec = None
            self.vae_key = None
        gc.collect()
        mx.clear_cache()

    def _check(self) -> None:
        if self.cancel.is_set():
            raise Canceled()

    # ── 로딩
    def ensure_dit(self, model_dir: Path) -> None:
        key = str(model_dir.resolve())
        if self.dit_key == key:
            return
        from mlx_video.models.wan_2.utils import load_wan_model

        self.unload("dit")
        cfg, quant, dmd, _ = load_config(model_dir)
        t = time.time()
        self.dit = load_wan_model(model_dir / "model.safetensors", cfg, quant)
        self.dit_call = mx.compile(self.dit)
        self.dit_key, self.cfg, self.quant, self.dmd = key, cfg, quant, dmd
        print(f"[engine] DiT 로드 {time.time() - t:.1f}s ({model_dir.name})")

    def ensure_t5(self, t5_source: Path) -> None:
        if self.t5 is not None:
            return
        from mlx_video.models.wan_2.text_encoder import T5Encoder
        from transformers import AutoTokenizer

        cfg = self.cfg
        enc = T5Encoder(vocab_size=cfg.t5_vocab_size, dim=cfg.t5_dim, dim_attn=cfg.t5_dim_attn,
                        dim_ffn=cfg.t5_dim_ffn, num_heads=cfg.t5_num_heads, num_layers=cfg.t5_num_layers,
                        num_buckets=cfg.t5_num_buckets, shared_pos=False)

        def pred(_, m):
            if isinstance(m, nn.Linear):
                return m.weight.shape[-1] % 64 == 0
            if isinstance(m, nn.Embedding):
                return m.weight.shape[0] > 10000 and m.weight.shape[-1] % 64 == 0
            return False

        q8 = CACHE / "t5_encoder_q8.safetensors"
        t = time.time()
        if q8.exists():
            nn.quantize(enc, group_size=64, bits=8, class_predicate=pred)
            enc.load_weights(str(q8))
        else:
            # 최초 1회: bf16 원본을 8bit 로 변환해 저장 (float32 로 올리지 않음)
            enc.load_weights(list(mx.load(str(t5_source)).items()))
            nn.quantize(enc, group_size=64, bits=8, class_predicate=pred)
            mx.eval(enc.parameters())
            CACHE.mkdir(parents=True, exist_ok=True)
            enc.save_weights(str(q8))
            print(f"[engine] 텍스트 인코더 8bit 캐시 저장: {q8}")
        mx.eval(enc.parameters())
        self.t5 = enc
        tok_dir = t5_source.parent
        try:
            self.tok = AutoTokenizer.from_pretrained(str(tok_dir)) if (tok_dir / "tokenizer.json").exists() \
                else AutoTokenizer.from_pretrained("google/umt5-xxl")
        except Exception:
            self.tok = AutoTokenizer.from_pretrained("google/umt5-xxl")
        gc.collect()
        mx.clear_cache()
        print(f"[engine] 텍스트 인코더 로드 {time.time() - t:.1f}s")

    def ensure_vae(self, vae_path: Path) -> None:
        key = str(vae_path.resolve())
        if self.vae_key == key:
            return
        from mlx_video.models.wan_2.utils import load_vae_decoder

        self.vae_dec = load_vae_decoder(vae_path, self.cfg)
        self.vae_key = key

    # ── 텍스트
    def encode(self, text: str, t5_source: Path) -> mx.array:
        from mlx_video.models.wan_2.utils import encode_text

        h = hashlib.sha1(("umt5|" + text).encode()).hexdigest()[:20]
        f = CACHE / "text" / f"{h}.safetensors"
        if f.exists():
            return mx.load(str(f))["ctx"]
        self.ensure_t5(t5_source)
        ctx = encode_text(self.t5, self.tok, text, self.cfg.text_len)
        mx.eval(ctx)
        f.parent.mkdir(parents=True, exist_ok=True)
        mx.save_safetensors(str(f), {"ctx": ctx})
        return ctx

    # ── 생성
    def generate(self, job: dict, progress) -> dict:
        from mlx_video.models.wan_2.i2v_utils import build_i2v_mask, preprocess_image
        from mlx_video.models.wan_2.postprocess import save_video
        from mlx_video.models.wan_2.scheduler import FlowUniPCScheduler
        from mlx_video.models.wan_2.utils import load_vae_encoder
        from mlx_video.models.ltx_2.video_vae.tiling import TilingConfig

        self.cancel.clear()
        self.last_used = time.time()
        timings = {}
        model_dir = Path(job["model_dir"])
        t5_source = Path(job["t5_source"])
        vae_path = Path(job["vae_path"])
        width, height, frames = int(job["width"]), int(job["height"]), int(job["frames"])
        seed = int(job["seed"])

        t0 = time.time()
        progress("모델 준비 중", 0.02)
        self.ensure_dit(model_dir)
        cfg = self.cfg
        distilled = bool(self.dmd) or job.get("sampler") == "dmd"
        timings["load_dit"] = round(time.time() - t0, 1)
        self._check()

        # 텍스트
        t1 = time.time()
        progress("프롬프트 처리 중", 0.05)
        context = self.encode(job["prompt"], t5_source)
        context_null = None if distilled else self.encode(job.get("negative") or cfg.sample_neg_prompt, t5_source)
        timings["text"] = round(time.time() - t1, 1)
        self._check()

        # 크기
        ps, vs = cfg.patch_size, cfg.vae_stride
        aw, ah = ps[2] * vs[2], ps[1] * vs[1]
        width, height = max(aw, width // aw * aw), max(ah, height // ah * ah)
        assert (frames - 1) % 4 == 0, "프레임 수는 4n+1 이어야 합니다"
        t_lat, h_lat, w_lat = (frames - 1) // vs[0] + 1, height // vs[1], width // vs[2]
        shape = (cfg.vae_z_dim, t_lat, h_lat, w_lat)
        seq_len = math.ceil((h_lat * w_lat) / (ps[1] * ps[2]) * t_lat)

        mx.random.seed(seed)
        np.random.seed(seed % (2**32))

        # 사진 → 첫 프레임 잠금 (TI2V)
        z_img = mask = mask_tok = None
        if job.get("image"):
            t2 = time.time()
            progress("사진 분석 중", 0.10)
            img = preprocess_image(job["image"], width, height)
            enc = load_vae_encoder(vae_path, cfg)
            z_img = enc.encode(img)
            mx.eval(z_img)
            z_img = z_img[0].transpose(3, 0, 1, 2)
            mask, mask_tok = build_i2v_mask(shape, ps)
            del enc, img
            gc.collect()
            mx.clear_cache()
            timings["image"] = round(time.time() - t2, 1)

        # 텍스트 임베딩·크로스어텐션 K/V·RoPE 미리 계산
        m = self.dit
        if distilled:
            emb = m.embed_text([context])
            ctx_in = emb[0:1]
        else:
            emb = m.embed_text([context, context_null])
            ctx_in = mx.concatenate([emb[0:1], emb[1:2]], axis=0)
        kv = m.prepare_cross_kv(ctx_in)
        grid = (t_lat // ps[0], h_lat // ps[1], w_lat // ps[2])
        rope = m.prepare_rope([grid] if distilled else [grid, grid])
        mx.eval(ctx_in, kv, rope)

        noise = mx.random.normal(shape)
        latents = (1.0 - mask) * z_img + mask * noise if z_img is not None else noise

        def t_input(tv: float, batch: int):
            if mask_tok is not None:
                tt = mask_tok * tv
                pad = seq_len - tt.shape[1]
                if pad > 0:
                    tt = mx.concatenate([tt, mx.full((1, pad), tv)], axis=1)
                return mx.concatenate([tt] * batch, axis=0) if batch > 1 else tt
            return mx.array([tv] * batch)

        t3 = time.time()
        if distilled:
            sig = list((self.dmd or {}).get("sigmas") or DMD_SIGMAS)
            steps = len(sig) - 1
            for i in range(steps):
                self._check()
                progress(f"영상 생성 중 ({i + 1}/{steps} 스텝)", 0.15 + 0.6 * i / steps)
                s, s_next = float(sig[i]), float(sig[i + 1])
                tv = s * cfg.num_train_timesteps
                v = self.dit_call([latents], t=t_input(tv, 1), context=ctx_in, seq_len=seq_len,
                                  cross_kv_caches=kv, y=None, rope_cos_sin=rope)[0]
                x0 = latents - s * v
                if s_next > 0:
                    latents = (1.0 - s_next) * x0 + s_next * mx.random.normal(shape)
                else:
                    latents = x0
                if z_img is not None:
                    latents = (1.0 - mask) * z_img + mask * latents
                mx.eval(latents)
        else:
            steps = int(job.get("steps") or cfg.sample_steps)
            gs = job.get("guide") or cfg.sample_guide_scale
            gs = float(gs[0] if isinstance(gs, (tuple, list)) else gs)
            sched = FlowUniPCScheduler(num_train_timesteps=cfg.num_train_timesteps)
            sched.set_timesteps(steps, shift=float(job.get("shift") or cfg.sample_shift))
            ts = sched.timesteps.tolist()
            for i in range(steps):
                self._check()
                progress(f"영상 생성 중 ({i + 1}/{steps} 스텝)", 0.15 + 0.6 * i / steps)
                tv = ts[i]
                preds = self.dit_call([latents, latents], t=t_input(tv, 2), context=ctx_in, seq_len=seq_len,
                                      cross_kv_caches=kv, y=None, rope_cos_sin=rope)
                pred = preds[1] + gs * (preds[0] - preds[1])
                latents = sched.step(pred[None], tv, latents[None]).squeeze(0)
                if z_img is not None:
                    latents = (1.0 - mask) * z_img + mask * latents
                mx.eval(latents)
        timings["denoise"] = round(time.time() - t3, 1)
        del kv, rope, ctx_in, emb, m
        # 디코딩 전 메모리 확보: 24GB 맥에서는 DiT·텍스트 인코더를 내려야 VAE 디코딩이 스왑 없이 돈다.
        # (다시 올리는 데 DiT 1초, 8bit 텍스트 인코더 수 초 — 같은 프롬프트는 캐시라 불필요)
        if FREE_BEFORE_DECODE:
            self.unload("dit")
            self.unload("t5")
        gc.collect()
        mx.clear_cache()
        self._check()

        # 디코딩
        t4 = time.time()
        progress("영상 변환 중", 0.80)
        self.ensure_vae(vae_path)
        from mlx_video.models.wan_2.vae22 import denormalize_latents

        z = denormalize_latents(latents.transpose(1, 2, 3, 0)[None])
        tiling = TilingConfig.auto(height, width, frames)
        video = self.vae_dec.decode_tiled(z, tiling) if tiling is not None else self.vae_dec(z)
        mx.eval(video)
        video = np.array(video[0])
        video = np.clip((video + 1.0) / 2.0 * 255.0, 0, 255).astype(np.uint8)
        timings["decode"] = round(time.time() - t4, 1)
        del latents, z
        gc.collect()
        mx.clear_cache()

        out = Path(job["output"])
        out.parent.mkdir(parents=True, exist_ok=True)
        save_video(video, str(out), fps=cfg.sample_fps)
        timings["total"] = round(time.time() - t0, 1)
        self.last_used = time.time()
        return {"output": str(out), "timings": timings, "frames": int(video.shape[0]),
                "size": [width, height], "seed": seed, "distilled": distilled}


# ───────────────────────── 메인 루프 ─────────────────────────

def main() -> None:
    eng = Engine()
    jobs: queue.Queue = queue.Queue()
    idle_unload = float(os.environ.get("MC_IDLE_UNLOAD_MIN", "30")) * 60

    def reader():
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("cmd") == "cancel":
                eng.cancel.set()
            else:
                jobs.put(msg)
        jobs.put({"cmd": "quit"})

    threading.Thread(target=reader, daemon=True).start()
    emit(event="ready", pid=os.getpid())

    while True:
        try:
            msg = jobs.get(timeout=30)
        except queue.Empty:
            if eng.dit is not None and time.time() - eng.last_used > idle_unload:
                eng.unload("all")
                emit(event="unloaded", reason="idle")
            continue
        cmd, jid = msg.get("cmd"), msg.get("id")
        if cmd == "quit":
            break
        if cmd == "status":
            emit(event="status", id=jid, **eng.status())
        elif cmd == "unload":
            eng.unload(msg.get("what", "all"))
            emit(event="status", id=jid, **eng.status())
        elif cmd == "generate":
            try:
                res = eng.generate(msg, lambda s, p: emit(event="progress", id=jid, stage=s, progress=round(p, 3)))
                emit(event="done", id=jid, **res, memory=eng.status())
            except Canceled:
                gc.collect()
                mx.clear_cache()
                emit(event="canceled", id=jid)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                gc.collect()
                mx.clear_cache()
                emit(event="error", id=jid, error=f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
