# State-First L1 + Trade Flow V2 사전등록

상태: `PRE_OUTCOME_LOCK / RESEARCH_ONLY / NO_ORDER_AUTHORITY`

## 고정 표본

SHA-256 달력으로 분기별 한 날짜를 결과와 무관하게 고정한다.

- train: `2023-01-03`
- selection: `2023-04-20`
- validation: `2023-08-30`
- one-shot test: `2023-12-28`

BTCUSDT·ETHUSDT에 같은 날짜를 사용한다. test 자료는 selection·validation 동시 게이트를
통과한 후보가 있을 때만 평가한다.

## 정보와 실행 시계

- Binance Vision 공식 USD-M daily `bookTicker`와 `aggTrades`; 각 ZIP의 인접 CHECKSUM 검증.
- 초 `s`의 feature는 `[s,s+1)`에 도착한 거래와 `s+1초` 이전 마지막 BBO만 사용한다.
- 신호 known_at은 `s+1초`이다.
- 주문 지연 250ms 후 처음 도착하는 실제 BBO에서 taker 진입한다. 2초 안에 BBO가 없으면 신호 폐기.
- 청산은 실제 진입시각에서 3·10·30·60초 뒤 처음 도착하는 BBO이며, 2초 안에 없으면 폐기.
- long은 ask 진입→bid 청산, short는 bid 진입→ask 청산이므로 실제 관측 spread를 직접 포함한다.
- BTC·ETH 통합 전역 한 슬롯.

## 고정 후보

모델:

- Ridge(alpha=100)
- HistGradientBoosting(max_iter=160, max_leaf_nodes=15, l2=30, learning_rate=0.04)
- ExtraTrees(240 trees, min_samples_leaf=80, max_features=0.7)

규칙 대조군:

- `flow_depth_continuation`
- `flow_depth_absorption_reversal`
- `microprice_continuation`

horizon `3,10,30,60초`; prediction absolute quantile `0.95,0.975,0.99,0.995,0.999`.
실제 BBO spread 외 추가 왕복비용 `12,18,24bp`. 목표 일수익률과의 거리는 후보 선택에 사용하지 않는다.

## 게이트

selection과 validation 각각:

- 12bp·18bp 거래 50건 이상.
- 12bp·18bp 로그성장 양수.
- 12bp PF >=1.10.
- 12bp 상위 5거래 양의 수익 집중도 <=35%.
- 12bp MDD <=15%.

통과 후보 중 selection·validation 18bp 로그성장의 최솟값이 가장 큰 하나만 test를 한 번 연다.
test가 양수여도 다중일·다중레짐 확장, queue/impact 용량, Paper/Live 공통 OMS 감사 전에는 승격하지 않는다.
