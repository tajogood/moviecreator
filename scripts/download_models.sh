#!/usr/bin/env bash
# Wan2.2 TI2V-5B 원본 다운로드(~35GB) → MLX 변환 + 양자화
# 사용: ./scripts/download_models.sh [8|4]   (기본 8bit, 메모리 부족 시 4)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
BITS="${1:-8}"
RAW="models/raw/Wan2.2-TI2V-5B"
OUT="models/mlx/wan22_ti2v5b_q${BITS}"
PY=".venv/bin/python"
HF=".venv/bin/hf"; [[ -x "$HF" ]] || HF=".venv/bin/huggingface-cli"

[[ -x "$PY" ]] || { echo "먼저 ./setup.sh 를 실행하세요."; exit 1; }
FREE_GB=$(df -g "$ROOT" | awk 'NR==2{print $4}')
if (( FREE_GB < 60 )); then
  echo "! 디스크 여유 ${FREE_GB}GB — 원본+변환본에 약 60GB가 필요합니다. 계속하려면 Enter, 중단은 Ctrl+C"; read -r
fi

echo "▶ 다운로드: Wan-AI/Wan2.2-TI2V-5B → $RAW"
"$HF" download Wan-AI/Wan2.2-TI2V-5B --local-dir "$RAW" \
  || { echo "! 멈추거나 실패하면 HF_HUB_DISABLE_XET=1 ./scripts/download_models.sh 로 다시 실행하세요."; exit 1; }

# mlx-video 버전에 따라 변환 모듈 경로가 다름
CONV=""
for m in mlx_video.models.wan_2.convert mlx_video.wan_2.convert mlx_video.convert_wan; do
  if "$PY" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$m') else 1)" 2>/dev/null; then CONV="$m"; break; fi
done
[[ -n "$CONV" ]] || { echo "✗ mlx-video 변환 모듈을 찾지 못했습니다: $PY -m mlx_video --help 로 확인"; exit 1; }

echo "▶ 변환: $CONV (${BITS}bit) → $OUT"
"$PY" -m "$CONV" --checkpoint-dir "$RAW" --output-dir "$OUT" \
  --model-version 2.2 --quantize --bits "$BITS" --group-size 64

# config.toml 의 모델 경로를 이번 변환본으로 맞춤
sed -i '' -E "s|^wan_model *=.*|wan_model  = \"${OUT}\"  # download_models.sh 가 설정|" config.toml
echo "✓ 완료: $OUT"
echo "  원본($RAW)은 변환 확인 후 외장 SSD로 옮겨도 됩니다."
