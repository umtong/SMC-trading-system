# V2-A001 — 학습일 전용 임계값 보정

상태: `PRE_OUTCOME_LOCK / RESEARCH_ONLY / NO_ORDER_AUTHORITY`

기록 시점: 2026-07-24 UTC, 후보 손익·selection·validation·test 결과를 열기 전.

## 문제

초기 구현은 모델 자체는 2023-01-03 자료로만 학습했지만, 절대 예측값의 0.95–0.999 분위 임계값을 2023-04-20 selection 하루 전체의 예측 분포로 계산했다. 수익이나 미래 라벨은 사용하지 않았으나, selection 초반의 의사결정이 그날 뒤에 도착하는 feature 분포에 의존하므로 실시간 정보 가용성 계약을 충족하지 못한다.

## 고정 수정

- 모든 모델·규칙·horizon의 절대 예측 분위 임계값은 완료된 train 날짜 `2023-01-03`의 예측값에서만 계산한다.
- 같은 임계값을 selection `2023-04-20`, validation `2023-08-30`, 조건부 one-shot test `2023-12-28`에 그대로 적용한다.
- 12·18·24bp 비용 프로필은 기본 임계값이 승인한 동일 거래 집합을 재생하며, 비용에 따라 신호를 삭제하거나 다시 선택하지 않는다.
- 기존 날짜, 모델, feature, horizon, 분위 격자, 게이트, 전역 단일 슬롯, 250ms 지연, 실제 BBO 체결 계약은 변경하지 않는다.

## 안전 상태

- candidate PnL observed before amendment: `false`
- test opened: `false`
- paper/live enabled: `false`
- order authority: `none`
