#!/usr/bin/env bash
# moviecreator 실행 래퍼: 저장소의 .venv 파이썬으로 mc.py 를 실행합니다.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"   # uv tool(mflux), Homebrew(ffmpeg)
exec "$PY" "$ROOT/mc.py" "$@"
