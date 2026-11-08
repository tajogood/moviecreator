# 설계 노트

전체 구축 가이드(툴 비교, 워크플로우, 프롬프트, 트러블슈팅): https://claude.ai/artifact/XwijQ4ShBgf8rTnrWkcxLw

## 왜 이 구성인가

| 툴 | 역할 | 24GB Mac 판정 | 메모 |
| --- | --- | --- | --- |
| mlx-video + Wan2.2 TI2V-5B | 이미지→영상 (1안) | 현실적으로 가능 | MLX 네이티브, Apache 2.0, 8bit 권장 |
| mflux (FLUX.1-schnell) | 키 이미지 | 현실적으로 가능 | MLX, schnell 은 상업 이용 가능 |
| rife-ncnn-vulkan v4.6 | 24→30fps 보간 | 현실적으로 가능 | `-n` 목표 프레임으로 비정수 배율 |
| FFmpeg | 정규화·합치기·출력 | 현실적으로 가능 | |
| ComfyUI + Wan 5B GGUF | 노드 UI (2안) | 제한적 | PyTorch MPS, 색 깨짐 시 `--fp32-vae` |
| Phosphene / LTX-2.3 MLX | 영상+오디오 (3안) | 제한적 | 19B급, 라이선스 확인 필요 |
| Wan2.2 A14B, HunyuanVideo | — | 비추천 | 24GB 초과·과열 |

## 파이프라인 규칙

- Wan2.2 TI2V-5B: 24fps, 프레임 = 4n+1, 가로·세로 32의 배수, 최대 121프레임(약 5초)
- final 프레임 = (컷 길이 + `extra_seconds`) × 24 → 4n+1, 최대 121
- reel: 컷마다 RIFE로 30fps → lanczos 1080×1920 크롭 → 컷 길이로 트림(짧으면 마지막 프레임 연장) → concat → 음악 페이드·loudnorm(-14 LUFS)
- 색 태그 bt709, yuv420p, faststart — 릴스 업로드 규격
