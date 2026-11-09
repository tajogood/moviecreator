#!/usr/bin/env bash
# moviecreator 설치 스크립트 (macOS · Apple Silicon 전용)
# 설치: Homebrew 도구, Python 3.12 가상환경, mlx-video, mflux, rife-ncnn-vulkan
# 모델 가중치(~35GB)는 별도: ./scripts/download_models.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

say()  { printf "\033[36m▶\033[0m %s\n" "$*"; }
warn() { printf "\033[33m!\033[0m %s\n" "$*"; }
die()  { printf "\033[31m✗\033[0m %s\n" "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "macOS 에서 실행하세요."
[[ "$(uname -m)" == "arm64" ]]  || die "Apple Silicon(arm64) 이 필요합니다. (Rosetta 터미널이면 끄고 다시 실행)"

# 1) Homebrew
if ! command -v brew >/dev/null; then
  say "Homebrew 설치 (관리자 암호를 물을 수 있습니다)"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi
say "Homebrew 패키지: python@3.12 uv ffmpeg git-lfs cloudflared"
brew install python@3.12 uv ffmpeg git-lfs cloudflared

# 2) Python 가상환경
if [[ ! -x .venv/bin/python ]]; then
  say "가상환경 생성 (.venv, Python 3.12)"
  uv venv --python 3.12 .venv
fi
say "Python 패키지: mlx-video, huggingface_hub, torch(가중치 변환용)"
uv pip install --python .venv/bin/python -U "huggingface_hub[cli]" pillow
uv pip install --python .venv/bin/python -U "git+https://github.com/Blaizzy/mlx-video.git"
uv pip install --python .venv/bin/python -U torch
say "웹 UI 패키지: fastapi, uvicorn, tomli-w"
uv pip install --python .venv/bin/python -U fastapi "uvicorn[standard]" python-multipart tomli-w

# 3) mflux (키 이미지 생성, 격리 설치)
say "mflux 설치 (uv tool)"
uv tool install --upgrade mflux || warn "mflux 설치 실패 — 키 이미지는 실제 사진으로 대체 가능 (./mc photo)"

# 4) RIFE (프레임 보간)
RIFE_VER="20221029"
if [[ ! -x tools/rife/rife-ncnn-vulkan ]]; then
  say "rife-ncnn-vulkan ${RIFE_VER} 다운로드"
  mkdir -p tools && tmp="$(mktemp -d)"
  if curl -fL --retry 5 --retry-delay 10 --retry-all-errors -o "$tmp/rife.zip" \
    "https://github.com/nihui/rife-ncnn-vulkan/releases/download/${RIFE_VER}/rife-ncnn-vulkan-${RIFE_VER}-macos.zip"; then
    unzip -q "$tmp/rife.zip" -d "$tmp"
    D="$tmp/rife-ncnn-vulkan-${RIFE_VER}-macos"
    mkdir -p tools/rife   # 바이너리 + v4.6 모델만 보관 (전체 437MB 중 약 35MB)
    cp "$D/rife-ncnn-vulkan" "$D/LICENSE" tools/rife/ && cp -R "$D/rife-v4.6" tools/rife/
    xattr -dr com.apple.quarantine tools/rife 2>/dev/null || true
    chmod +x tools/rife/rife-ncnn-vulkan
  else
    warn "RIFE 다운로드 실패 (GitHub 일시 오류일 수 있음) — 나중에 ./setup.sh 를 다시 실행하세요. 그 전까지는 프레임 복제로 30fps 변환."
  fi
  rm -rf "$tmp"
fi

chmod +x mc web.sh MovieCreator.command scripts/*.sh
mkdir -p models/raw models/mlx projects logs music input
rm -f tools/.mlx_video_flags.json   # mlx-video 버전이 바뀌었을 수 있으니 옵션 캐시 초기화

say "설치 완료. 환경 점검:"
./mc doctor || true
cat <<'EOF'

다음 단계
  1) 모델 다운로드·변환 (~35GB, 한 번만):   ./scripts/download_models.sh
  2) 웹 UI 실행:                            ./web.sh   (또는 MovieCreator.command 더블클릭)
EOF
