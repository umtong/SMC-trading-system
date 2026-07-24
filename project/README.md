# ChatGPT 프로젝트 병렬 연구 제어면

이 디렉터리는 여러 프로젝트 채팅이 동시에 연구하면서 상태를 잃거나 서로 덮어쓰지 않도록 하는 최소 제어면이다.

## 파일

- `PROJECT_INSTRUCTIONS.md`: Project Settings에 넣는 실제 실행 규칙
- `OPERATING_SYSTEM.md`: Drive·GitHub·병렬 lane 운영 계약
- `EVALUATION_CONTRACT.md`: 목표, 무효 조건과 Champion 비교 기준
- `DRIVE_CONTROL_PLANE.md`: Google Drive 라이브 상태 구조
- `prompts/`: 총괄 및 연구 lane 시작 프롬프트
- `schemas/`: Champion과 Run Report 구조
- `scripts/validate_control_plane.py`: 필수 파일·규칙 검증

## 역할

- Google Drive는 고빈도 라이브 상태와 대규모 자료를 보관한다.
- GitHub는 규칙, 코드, manifest, 재현 증거와 변경 이력을 보관한다.
- 연구 lane은 각자 branch·commit·draft PR과 Drive Run Report를 만든다.
- 총괄만 공통 상태와 Champion을 병합한다.

## 검증

```bash
python project/scripts/validate_control_plane.py
```
