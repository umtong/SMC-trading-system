# V0.8 연구 상태 기준선

업데이트: `2026-07-20 KST`  
현재 단계: V0.8 전략 가족·반복 random trial·공식 데이터 경로 구현, CI 및 실제 성과 검증 중  
보존 연구 선두: `V0.3 BREAK_RETEST + V0.5`  
신규 연구 후보: `V0.8 HTF liquidity delivery`, `V0.8 internal M5 liquidity delivery`  
권한 상태: `RESEARCH_ONLY / NO_PAPER_ORDER / NO_LIVE_ORDER`

## 1. 이 문서의 역할

이 문서는 draft PR #2의 V0.8 작업에 대한 최신 상태 기준선이다. V0.8 branch에서는 다음 순서로 판단한다.

1. 사용자의 최신 결정
2. 현재 코드와 실제 실행 결과
3. 이 문서
4. `MAIN_AGENT_STATE.md`의 V0.7 이하 보존 판단
5. 정책·수식·과거 보고서

V0.8이 검증·병합되기 전까지 `MAIN_AGENT_STATE.md`의 보존 결과를 삭제하거나 V0.8 성공 결과로 다시 이름 붙이지 않는다.

## 2. 사용자 하드 불변식

```text
symbols = BTCUSDT + ETHUSDT
max_total_pending_or_open_positions = 1
risk_per_trade_fraction = 0.03
position_addition_after_entry = false
daily_loss_limit_enabled = false
```

반복 검증 조건:

```text
2022~2026 각 연도 무작위 28일
trial당 포트폴리오 운용일 = 140일
완료 거래 수 > 포트폴리오 운용일 수
따라서 최소 완료 거래 수 = 141건
초기 equity 대비 최종 equity >= 5배
```

미체결·취소·거절·미해결 주문은 완료 거래 수로 세지 않는다.

## 3. V0.8 전략 인과관계

모든 신규 거래 후보는 다음을 설명할 수 있어야 한다.

```text
liquidity cause
→ meaningful location
→ displacement ownership
→ executable OB/FVG footprint
→ structural invalidation
→ independent pivot liquidity target
→ cost and exposure room
```

FVG나 OB 자체는 유동성 delivery 또는 실행 위치 역할을 할 수 있지만, 단순히 가까운 반대 구역이라는 이유만으로 terminal target이 되지 않는다.

### 3.1 HTF liquidity delivery

```text
pre-confirmed H1/M15 boundary
→ M15 FVG B-bar directional close break
→ C-bar acceptance
→ H1/H4 compatible context
→ material displacement
→ first return to flipped boundary
→ pre-event unconsumed H1/H4 pivot liquidity target
```

초기 연구 경계:

- planned target 최소 `0.75R`
- 3% 위험 수량의 명목 노출 최대 equity `8배`
- M15 displacement 범위는 직전 20봉 중앙값의 최소 `1.20배`
- displacement body fraction 최소 `55%`

### 3.2 internal M5 liquidity delivery

```text
active, HTF-compatible M15 OB
→ confirmed internal M5 pivot inside the location
→ first sweep and reclaim at the M15 location
→ prompt MSS-owning M5 OB/FVG within 12 bars
→ material displacement
→ first return to the execution footprint
→ pre-event unconsumed M15/H1/H4 pivot liquidity target
```

초기 연구 경계:

- planned target 최소 `0.65R`
- 3% 위험 수량의 명목 노출 최대 equity `8배`
- M5 displacement 범위는 직전 20봉 중앙값의 최소 `1.10배`
- displacement body fraction 최소 `50%`
- 서로 다른 pivot·sweep event·delivery root만 새 장면으로 재무장

## 4. 반복 random trial 계약

- BTC와 ETH가 같은 window manifest를 공유한다.
- warm-up 35일은 feature 형성에만 사용한다.
- score 28일 안에서 완료된 authority만 주문 후보가 된다.
- score 종료 전에 체결된 포지션만 7일 extension에서 자연 청산할 수 있다.
- score 종료 시각 또는 이후 첫 체결은 취소한다.
- 데이터 끝 미해결 포지션을 임의 손익으로 닫지 않으며 trial을 무효로 표시한다.
- 모든 symbol과 연도 window는 하나의 시간순 equity와 occupied slot을 공유한다.

기본 20개 trial 모두에서 다음을 통과해야 한다.

- equity multiple `>= 5.0`
- completed trades `>= 141`
- median average net R `>= 0`
- worst max drawdown `<= 35%`
- censored order/position `= 0`

## 5. 구현 완료

- deterministic repeated random manifests
- hard 5x and completed-trades-above-days gate
- fixed-fraction net-R bootstrap stress
- V0.8 HTF liquidity-delivery builder
- V0.8 internal M5 liquidity-delivery builder
- BTC/ETH chronological shared-equity/shared-slot replay
- score-window entry boundary and no-forced-close handling
- official Binance USD-M 5m downloader with checksum and continuity validation
- leader/V0.8 arm comparison runner
- focused unit tests and pull-request CI
- Korean logic and validation contract report

## 6. 아직 입증되지 않음

다음은 현재 사실로 주장하지 않는다.

- 반복 random trial에서 5배 달성
- 모든 140일 trial에서 완료 거래 141건 이상
- 모든 trial에서 비용 포함 양의 기대값
- 최악 최대낙폭 35% 이하
- paper/live 승격 자격

따라서 V0.8은 활성 주전이 아니며 실제 주문 권한이 없다.

## 7. 현재 검증 순서

1. PR 전체 테스트를 통과시킨다.
2. 공식 BTCUSDT·ETHUSDT USD-M 5분봉을 checksum과 연속성 검증으로 생성한다.
3. 같은 seed와 manifest에서 leader와 세 V0.8 결합 arm을 비교한다.
4. 빈도 미달이면 authority funnel, pending 점유시간, scene 재무장 수를 먼저 분해한다.
5. 5배 미달이면 평균 순R, 손실 분포, 목표 도달 전 MFE/MAE, exit reason을 분해한다.
6. 한 metric을 높이는 대신 `완료 거래 빈도 × 비용 포함 평균 순R × 손실 통제`가 함께 개선되는 변경만 보존한다.
7. hard gate 통과 전에는 paper/live 개발을 성과 완료로 간주하지 않는다.
