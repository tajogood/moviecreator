# moviecreator

Apple Silicon Mac에서 **인스타그램 릴스용 30초 홍보 영상(1080×1920 · 30fps)** 을 로컬 오픈소스 모델로 만드는 파이프라인입니다.

```
키 이미지 (실제 사진 / mflux)
   → 이미지→영상 (mlx-video + Wan2.2 TI2V-5B, MLX)
   → 30fps 보간 (rife-ncnn-vulkan)
   → 1080×1920 정규화·트림 (FFmpeg)
   → 합치기 + 음악 → 릴스 MP4
```

- 권장 사양: Apple Silicon, 통합 메모리 24GB 이상, 디스크 여유 60GB 이상
- 16GB 이하 Mac: `./scripts/download_models.sh 4` (4bit) + draft 해상도 위주로 사용
- 영상 모델 Wan2.2 TI2V-5B: Apache 2.0 (상업 이용 가능) · 키 이미지 FLUX.1-schnell: Apache 2.0

## 1. 설치 (한 번)

```bash
cd ~/Desktop/devs/moviecreator
./setup.sh                      # Homebrew 도구, .venv(Python 3.12), mlx-video, mflux, RIFE
./scripts/download_models.sh    # Wan2.2 TI2V-5B 다운로드(~35GB) + MLX 8bit 변환
./mc doctor                     # 전부 ✓ 인지 확인
```

## 2. 릴스 1편 만들기

```bash
./mc new cafe_launch --template product     # product | place | travel
# projects/cafe_launch/project.toml 의 prompt, keyframe_prompt 를 내 제품·장소에 맞게 수정

# 키 이미지: 실제 사진 권장(형태 왜곡이 가장 적음), 없으면 mflux 로 생성
./mc photo cafe_launch 01 ~/Pictures/product.jpg
./mc keyframes cafe_launch                  # 사진 없는 컷만 mflux 로 생성

# 시안: 480×864 · 49프레임 · 시드 여러 개 → drafts/ 에서 고르고 project.toml 의 seed 수정
./mc draft cafe_launch --seeds 42,7,99
./mc draft cafe_launch --only 03 --seeds 5,11   # 특정 컷만 다시

# 최종: 704×1280 · (컷 길이+1초) 프레임
./mc final cafe_launch

# 보간 → 1080×1920 → 합치기 → 음악
./mc reel cafe_launch --music music/track.mp3
# → projects/cafe_launch/final/cafe_launch_reel_YYYYMMDD_HHMM.mp4
```

한 번에: `./mc all cafe_launch --music music/track.mp3` (keyframes → final → reel)

자막·로고·컬러 마감은 결과 MP4를 DaVinci Resolve / CapCut 으로 가져가서 합니다. 내보내기는 1080×1920 · 30fps · H.264 · 고정 프레임레이트.

## 3. 폴더 구조

```
moviecreator/
├── mc, mc.py            # CLI (doctor/new/photo/keyframes/draft/final/reel/all)
├── config.toml          # 해상도·스텝·프레임·출력 규격 등 전역 설정
├── setup.sh             # 설치
├── scripts/download_models.sh
├── templates/           # product / place / travel 30초 컷 구성 + 프롬프트
├── prompts/negative.txt # 공통 negative prompt
├── projects/<이름>/
│   ├── project.toml     # 컷 리스트 (id, seconds, seed, prompt, keyframe_prompt)
│   ├── keyframes/       # 컷별 키 이미지 (01.png …)
│   ├── drafts/          # 시안 (01_s42.mp4 …)
│   ├── clips/           # 채택된 최종 원본 (24fps)
│   ├── work/            # 중간 파일 (자동 정리)
│   └── final/           # 완성 릴스
├── models/              # (git 제외) raw 원본, mlx 변환본
├── tools/               # (git 제외) rife
├── music/               # (git 제외) 라이선스 확인된 음원
└── logs/                # (git 제외) 컷별 시드·소요시간 jsonl
```

## 4. 30초 컷 구성 (템플릿)

| 템플릿 | 구성 (초) |
| --- | --- |
| product | 훅 3 · 디테일 4 · 디테일 4 · 라이프스타일 5 · 히어로 4 · 분위기 5 · 엔드카드 5 |
| place | 훅 3 · 진입 4 · 공간 5 · 디테일 4 · 경험 4 · 시그니처 5 · 엔드카드 5 |
| travel | 훅 3 · 도착 4 · 로컬 4 · 걷기 4 · 절경 5 · 감성 5 · 엔드카드 5 |

## 5. 성능 팁 (24GB 기준)

- 생성은 컷당 2~5초(49~121프레임)만. 30초는 편집으로 만든다.
- 시안은 draft(480×864·20스텝)로, 통과한 컷만 final.
- 생성 중에는 브라우저·Docker·Resolve 종료, 전원 연결, 저전력 모드 끄기.
- 스왑이 생기면 `./scripts/download_models.sh 4` 로 4bit 변환본을 만들고 `config.toml` 의 `wan_model` 이 그 경로인지 확인.
- 컷 사이 쿨다운은 `config.toml` 의 `cooldown_seconds`.
- 생성 중 맥이 잠들지 않도록 `caffeinate` 가 자동 적용됩니다.

## 6. 트러블슈팅

| 증상 | 해결 |
| --- | --- |
| `mlx-video 의 Wan 생성 모듈을 찾지 못했습니다` | `./setup.sh` 재실행, `./mc` 래퍼로 실행(.venv 사용) |
| mlx-video 업데이트 후 옵션 오류 | `rm tools/.mlx_video_flags.json` (옵션명을 `--help` 로 다시 감지) |
| 메모리 부족·극단적으로 느림 | 4bit 변환, draft 해상도, `--only` 로 컷 하나씩, 재부팅 후 단독 실행 |
| 다운로드가 0MB/s 에서 멈춤 | `HF_HUB_DISABLE_XET=1 ./scripts/download_models.sh` |
| 결과 영상이 끊김 | RIFE 설치 확인(`./mc doctor`), `--no-interp` 를 쓰지 않았는지 확인 |
| 컷 사이 색감이 다름 | `style_suffix` 에 같은 조명·그레이드 문구 유지, Resolve 에서 일괄 그레이딩 |
| 길이가 30초가 아님 | `project.toml` 의 seconds 합계 확인 (reel 이 컷별로 정확히 트림) |
| `rife-ncnn-vulkan` 이 차단됨 | `xattr -dr com.apple.quarantine tools/rife` |
| ffmpeg `libx264` 없음 | `brew reinstall ffmpeg` |

설계 배경과 툴 비교는 `docs/GUIDE.md` 참고.
