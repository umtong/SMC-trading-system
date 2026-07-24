# V2-A002 — 공식 daily bookTicker 가용성 기반 날짜 교체

상태: `PRE_OUTCOME_LOCK / RESEARCH_ONLY / NO_ORDER_AUTHORITY`

기록 시점: 2026-07-24 UTC. 이전 패널 실행은 원자료 취득 단계에서 중단됐고 candidate PnL, selection, validation, one-shot test 결과는 생성되지 않았다.

## 원인과 제한

초기 결과독립 달력의 2023-01-03 및 2023-04-20은 Binance Vision USD-M daily `bookTicker`의 사용 가능한 보존 구간보다 이르다. 반면 공식 schema 감사에서 2023-06-27의 BTCUSDT archive와 인접 CHECKSUM이 검증됐다. 이 수정은 거래 결과가 아니라 원자료 가용성만 사용한다.

## 교체된 결과독립 날짜

- train: `2023-06-27`
- selection: `2023-08-30`
- validation: `2023-10-25`
- one-shot test: `2023-12-28`

BTCUSDT와 ETHUSDT에 동일한 날짜를 사용한다. 선택일·검증일 결과가 기존 모든 게이트를 동시에 통과할 때만 12월 test를 한 번 연다.

## 변경하지 않는 항목

- 완료된 1초 feature와 `known_at=s+1초`
- 추가 250ms 지연 및 2초 BBO 제한
- 실제 ask/bid taker 진입·청산
- 3·10·30·60초 horizon
- Ridge·HistGradientBoosting·ExtraTrees 및 세 규칙 대조군
- train 날짜만 사용하는 score threshold
- 12·18·24bp 추가 비용과 동일 신호집합 재생
- BTC·ETH 전역 한 슬롯과 기존 selection/validation gate

Candidate PnL observed before amendment: `false`  
Test opened: `false`  
Paper/live enabled: `false`  
Order authority: `none`
