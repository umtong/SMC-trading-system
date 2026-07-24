# V0.10 외부 유동성 전달 수익성 연구 설계

## 목적

V0.8과 기존 OHLCV 장면군은 비용 민감도와 낮은 payoff 문제로 운용 승격에 실패했다. V0.10은 검증 엄밀성 자체가 아니라 실제 비용 후 높은 기대값과 발생빈도를 동시에 높이는 것을 목적으로 한다.

## 경제 가설

확정된 15분 외부 유동성 피벗을 5분봉이 sweep/reclaim한 뒤, 같은 에피소드 안에서 5분 MSS와 방향성 displacement FVG가 완성되면 주문 흐름의 방향 전환과 가격 전달이 동시에 나타난다. 손절은 sweep extreme이 소유하고, 목표는 sweep 시점에 이미 알려진 반대편 외부 유동성으로 제한한다.

## 동결 기준 후보

현재 BTC/ETH 다중 레짐 공학 화면에서 기준·1.5배 비용 모두 양수였던 다음 두 arm을 새 데이터 보기 전에 고정한다.

1. `STRICT_W6`
   - sweep depth >= 0.05 ATR
   - 방향성 displacement body >= 0.60 ATR
   - sweep 후 6개 5분봉 이내 MSS와 FVG 모두 확인
   - 시장가: confirmation 다음 5분봉 open
   - target: sweep 시점의 반대 외부 유동성과 3.5R 중 가까운 값
   - 최소 gross target space 0.5R

2. `BROAD_W9`
   - sweep depth >= 0.05 ATR
   - 방향성 displacement body >= 0.60 ATR
   - sweep 후 9개 5분봉 이내 같은 봉에서 MSS와 FVG 확인
   - 시장가: confirmation 다음 5분봉 open
   - target: 반대 외부 유동성과 3.5R 중 가까운 값
   - 최소 gross target space 0.5R

`STRICT_W6`의 순차형 변형은 MSS와 FVG가 최대 2개 봉의 하나의 displacement complex 안에서 완성될 때만 허용하며, 이미 완전히 완화된 FVG는 사용할 수 없다. 결과 확인 후 percentile, body threshold, confirmation window 또는 target R을 미세조정하지 않는다.

## 데이터 패널

- Binance USD-M 공식 monthly 5m klines
- BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT
- 2022-01-01 inclusive ~ 2026-07-01 exclusive
- 각 ZIP의 `.CHECKSUM` SHA-256 검증
- 5분봉 누락·중복·비정상 OHLCV 발생 시 실패
- 개발: 2022-01-01 ~ 2024-12-31
- walk-forward validation: 2025-01-01 ~ 2025-12-31
- terminal holdout: 2026-01-01 ~ 2026-07-01

## 실행 계약

- 확인된 닫힌 봉만 사용
- 피벗은 우측 확인 후부터 사용
- 다음 open 시장가 진입은 taker fee와 불리한 슬리피지 적용
- stop과 target 동시 접촉 시 stop 우선
- target은 maker, stop은 taker
- 미체결 주문과 포지션 합계 전 종목 최대 1개
- 거래당 위험률은 전략 비교 동안 1%로 고정
- 명목금액/자산 상한 10x
- 임의 시간 종료 없음; 데이터 끝의 포지션은 성과에서 검열
- 최종 후보는 execution-v1 수량·비용·funding 함수로 재생

## 비용 스트레스

- 기준: taker 6 bps, maker 2 bps, market-entry slippage 1 bp, stop slippage 2 bps
- 스트레스: 위 비용 1.5배
- 극단 스트레스: 위 비용 2배

## 승격 조건

연구 선두 후보는 다음을 모두 충족해야 한다.

- 개발·validation·terminal holdout 모두 기준 비용 순 R 양수
- 세 구간 모두 1.5배 비용 순 R 양수
- BTC/ETH만 제외하거나 SOL/XRP만 제외해도 전체 순 R 양수
- 최대 한 분기 또는 상위 3개 거래 제거 후에도 전체 순 R 양수
- terminal holdout 거래 수가 운용일의 0.5배 이상; 목표는 1배 이상
- terminal holdout 기하 일평균 순수익 1%를 목표 게이트로 유지

5분 arm이 기대값을 유지하지만 빈도 게이트에 미달하면 동일 frozen 15분 sweep 위치에서 1분 MSS/FVG delivery로만 시간축을 낮춘다. 경제 논리와 stop/target ownership은 변경하지 않는다.

## 현재 권한

- 연구 전용
- paper/live 주문 금지
- 위험률·레버리지·은행계좌 비율 최적화 금지
