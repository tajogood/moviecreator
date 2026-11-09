#!/usr/bin/env bash
# MovieCreator 웹 서버 실행
#   ./web.sh            로컬 + 외부 주소(Cloudflare 터널)
#   ./web.sh --local    로컬·같은 와이파이만 (외부 접속 없음)
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
PORT="${PORT:-7860}"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || { echo "먼저 ./setup.sh 를 실행하세요."; exit 1; }
mkdir -p logs
rm -f logs/public_url.txt

"$PY" -c "import fastapi, uvicorn, multipart, tomli_w" 2>/dev/null || {
  echo "▶ 웹 서버 패키지 설치"; uv pip install --python "$PY" -q fastapi "uvicorn[standard]" python-multipart tomli-w; }

# 이미 떠 있는 서버 정리
lsof -ti tcp:"$PORT" 2>/dev/null | xargs kill 2>/dev/null; sleep 0.5

cleanup() { kill "${SERVER_PID:-}" "${TUNNEL_PID:-}" "${AWAKE_PID:-}" 2>/dev/null; rm -f logs/public_url.txt; echo; echo "서버를 껐습니다."; }
trap cleanup EXIT INT TERM

# 서버 (같은 와이파이에서도 접속 가능하도록 0.0.0.0)
"$PY" -m uvicorn web.server:app --host 0.0.0.0 --port "$PORT" --log-level warning --proxy-headers \
  --forwarded-allow-ips='*' >> logs/web.log 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.5; done
curl -s -o /dev/null "http://127.0.0.1:$PORT/" || { echo "✗ 서버 시작 실패. logs/web.log 확인:"; tail -20 logs/web.log; exit 1; }

# 맥이 잠들지 않게 (전원 연결 시 덮개를 닫아도 외부 모니터 없으면 잠들 수 있음)
caffeinate -dimsu -w "$SERVER_PID" & AWAKE_PID=$!

PASSWORD=$(sed -n 's/^password = "\(.*\)"/\1/p' web.secret.toml)
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || true)

PUBLIC=""
if [[ "${1:-}" != "--local" ]]; then
  if ! command -v cloudflared >/dev/null; then echo "▶ cloudflared 설치"; brew install cloudflared; fi
  cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" > logs/tunnel.log 2>&1 &
  TUNNEL_PID=$!
  for _ in $(seq 1 60); do
    PUBLIC=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' logs/tunnel.log | head -1)
    [[ -n "$PUBLIC" ]] && break; sleep 1
  done
  [[ -n "$PUBLIC" ]] && echo "$PUBLIC" > logs/public_url.txt
fi

cat <<EOF

────────────────────────────────────────────
  MovieCreator 실행 중
  이 맥에서:        http://localhost:$PORT
EOF
[[ -n "$LAN_IP" ]] && echo "  같은 와이파이:    http://$LAN_IP:$PORT"
if [[ -n "$PUBLIC" ]]; then echo "  외부(인터넷):     $PUBLIC"
elif [[ "${1:-}" != "--local" ]]; then echo "  외부 주소 발급 실패 → logs/tunnel.log 확인"; fi
cat <<EOF
  비밀번호:         $PASSWORD
                    (바꾸려면 web.secret.toml 수정 후 재시작)

  외부 주소는 서버를 다시 켤 때마다 바뀝니다.
  이 창을 닫거나 Ctrl+C 를 누르면 서버가 꺼집니다.
────────────────────────────────────────────
EOF

open "http://localhost:$PORT" 2>/dev/null
wait "$SERVER_PID"
