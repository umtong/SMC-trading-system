# EasyChart V0.9 미시구조 확인형 유동성 전달 연구 설계

작성일: 2026-07-23 KST  
상태: 사전등록·데이터 구축 전  
권한: `RESEARCH_ONLY / NO_PAPER_ORDER / NO_LIVE_ORDER`

## 1. 기존 접근의 비판적 결론

V0.3~V0.8은 5분 OHLCV에서 위치·스윕·재확보·구조 돌파·OB/FVG를 인과적으로 구현했다. 그러나 단순한 장면 정의만으로는 다음 문제가 남았다.

1. OHLCV의 wick과 종가는 실제 공격적 매수·매도 흐름을 직접 보여주지 않는다.
2. 동일한 `sweep/reclaim` 모양 안에도 진짜 흡수·강제청산·단순 변동성 확대가 섞인다.
3. 높은 승률이라도 작은 terminal target과 큰 stop의 비대칭 때문에 비용 후 기대값이 음수가 될 수 있다.
4. 아시아 범위, 전일 고저, killzone을 단독 규칙으로 만든 후보는 새 잠금 구간에서 재현되지 않았다.

따라서 V0.9는 5분봉 SMC 규칙을 더 복잡하게 만드는 버전이 아니다. 기존 장면은 **위치와 사건을 제안하는 scaffold**로만 사용하고, 실제 진입 허용은 체결 자료에서 관측되는 taker 흐름 전환이 담당한다.

## 2. 핵심 가설

가격이 외부 유동성을 쓸고 원래 범위로 돌아오는 것만으로는 부족하다. 유효한 반전·전달 장면이라면 다음 두 흐름이 시간순으로 관측되어야 한다.

```text
외부 유동성 접근
→ 진행 방향의 공격적 거래 집중과 제한된 가격 진전(흡수 후보)
→ 경계 재확보
→ 반대 방향 taker delta 전환과 가격 효율 회복
→ 다음 독립 유동성 목적지로 전달
```

주가설은 다음과 같다.

> causal SMC liquidity scene + pre-event aggressive-flow exhaustion + post-reclaim taker-flow reversal은 OHLCV 장면 단독보다 비용 후 기대값과 OOS 안정성을 개선한다.

## 3. 자료 계약

### 3.1 가격·장면 자료

- Binance USD-M perpetual의 checksum 검증 1m/5m OHLCV
- 현재 `easychart_v0`의 완료봉 resampling과 causal pivot/OB/FVG 검출
- 장면 확정 시점 이후에만 진입 가능
- warm-up 기간은 feature 생성에만 사용하고 거래·성과 분모에서 제외

### 3.2 미시구조 자료

공식 Binance public archive의 USD-M `aggTrades`를 사용한다.

정규화 필드:

- aggregate trade id
- 거래 가격
- base quantity
- quote quantity
- first/last underlying trade id
- transaction timestamp
- buyer-is-maker
- signed quote volume

부호 계약:

```text
buyer_is_maker = false → buyer가 taker → signed_quote > 0
buyer_is_maker = true  → seller가 taker → signed_quote < 0
```

ZIP과 `.CHECKSUM`을 함께 보존한다. aggregate trade id·timestamp 중복, 시간 역전, 비정상 가격·수량, 월 경계 밖 행은 실행 실패다. 누락 데이터를 0으로 보간하지 않는다.

### 3.3 펀딩 자료

공식 funding history의 `fundingTime`, `fundingRate`, 해당 정산의 `markPrice`를 사용한다. archive가 제공되는 기간에는 archive와 checksum을 우선하고, 부득이하게 REST를 사용할 때는 원 응답과 요청 범위를 immutable raw artifact로 남긴다.

포지션이 실제 정산 시각을 통과한 경우에만 다음 cash flow를 적용한다.

```text
funding_cash = -side_sign × quantity × mark_price × funding_rate
LONG side_sign = +1
SHORT side_sign = -1
```

양수 funding rate에서는 long이 지급하고 short이 수취한다. 누락된 정산 관측치를 0으로 간주하지 않는다.

## 4. 장면 모집단

V0.9는 결과를 보기 전에 다음 세 모집단을 고정한다.

1. `V0.3 BREAK_RETEST`
2. `V0.5 M15-location M5-liquidity-delivery`
3. `V0.7/V0.8 boundary acceptance` 장면

장면별 기존 stop·external target·known_at을 보존한다. 같은 root scene에서 여러 표현이 겹치면 root 하나로 묶고, 미시구조 특징은 동일한 사건 시계에서 한 번만 계산한다.

V0.9는 새로운 wick 모양을 추가해 거래 수를 늘리지 않는다. 모집단의 질을 먼저 측정하고, BTC·ETH에서 표본이 불충분할 때만 결과를 보기 전에 고정한 추가 유동성 상위 perpetual universe로 확장한다.

## 5. causal feature 계약

모든 feature는 진입 결정을 내리는 완료 1분봉의 close 시점까지 알려진 자료만 사용한다.

### 5.1 거래흐름

- 15초·60초·5분 signed quote delta
- taker buy/sell quote volume
- cumulative volume delta 변화
- aggregate trade arrival rate
- 거래당 평균 quote size
- 상위 1% 거래가 차지하는 quote volume 비중

### 5.2 흡수·효율

- 방향성 taker quote volume / 동일 방향 가격 진전
- 고저 범위 대비 종가 진행률
- event extreme 부근의 공격적 거래량 집중도
- 동일한 signed flow에서 과거 대비 낮아진 price impact

### 5.3 상대화

절대 USD threshold는 쓰지 않는다. 각 symbol에서 과거 완료 120개 1분 구간의 median·MAD 또는 trailing empirical percentile로 상대화한다. 현재 구간을 기준 분포에 포함하지 않는다.

### 5.4 시간·위치

- UTC 및 America/New_York 세션
- 전일·전주 고저까지 거리
- H1/H4 causal structure state
- entry-stop 거리와 external-target R
- funding 정산까지 남은 시간

시간 feature는 필터를 사후 선택하기 위한 변수가 아니라 walk-forward 모델 입력으로만 사용한다.

## 6. 두 진입 arm

### 6.1 고정 규칙 arm

결과를 보기 전 다음 규칙을 고정한다.

LONG 예:

1. long 장면의 event window에서 signed delta가 trailing 20 percentile 이하
2. 가격이 event node 또는 external low를 sweep
3. 완료 1분봉이 node를 재확보
4. 그 완료 1분봉 signed delta가 trailing 80 percentile 이상
5. 해당 봉의 close가 stop과 target 사이이며 비용 후 target PnL이 양수
6. 다음 1분봉 실제 open에서 taker 진입

SHORT은 대칭이다.

20/80 percentile은 해석 가능한 방향 전환 기준으로 사전 고정하며 grid-search하지 않는다.

### 6.2 walk-forward meta-filter arm

- 모델: L2 regularized logistic regression
- 입력: 5절의 사전 고정 feature
- label: 동일 frozen stop/target/replay에서 비용·funding 후 net R > 0
- 학습: expanding 또는 rolling chronological train only
- validation: purged time split와 사건 최대 보유시간만큼 embargo
- test 월의 예측은 그 월 시작 전에 끝난 자료만 사용
- threshold: train에서 예측 순 R을 최대화하지 않는다. `estimated expected net R > 0`에 대응하는 고정 확률 threshold 또는 train prevalence-corrected 0.5를 사용한다.
- class weight, regularization, feature set은 최초 locked run 뒤 변경하지 않는다.

표본 수가 충분하지 않으면 모델 결과를 내지 않고 `INSUFFICIENT_SAMPLE`로 실패시킨다.

## 7. 체결·위험 현실성

- next-open arm은 market/taker 진입으로 비용을 계산
- 지정가 진입과 시장가 진입의 수수료율을 분리
- 시장가 진입 slippage를 별도로 적용
- stop·volume exit도 taker 비용
- BTC·ETH 전체 pending/open 최대 한 개
- 거래당 위험률 3%는 연구 비교용으로 고정
- `position_notional / equity <= 10`을 생산 게이트로 강제
- 5×·20×는 민감도 진단이며 결과를 보고 기본값을 선택하지 않음
- funding 포함
- intra-bar stop/target 순서를 알 수 없으면 기존 보수적 stop-first 규칙 유지

## 8. 데이터 분리와 walk-forward

### 개발·모델 학습

- BTCUSDT·ETHUSDT
- 2021-01-01 ~ 2023-06-30
- 최소 28일 feature warm-up
- 월 단위 expanding walk-forward

### 잠금 검증

- BTCUSDT·ETHUSDT
- 2023-07-01 ~ 2023-12-31
- 모든 feature·규칙·모델 설정을 고정한 뒤 한 번만 평가

2024~2026 V0.3~V0.8 자료는 최종 외부 레짐 진단에 사용할 수 있지만 V0.9 threshold 선택에는 쓰지 않는다. 잠금 결과를 본 뒤 같은 기간에서 규칙을 수정하지 않는다.

## 9. 비용 스트레스와 자동 판정

모든 arm을 다음 조건으로 재생한다.

- 기준 비용
- 1.5배 비용
- 2배 비용
- 10× notional cap
- funding 포함

연구 선두 후보 조건:

1. 잠금 구간 기준 비용 net R > 0
2. 잠금 구간 1.5배 비용 net R > 0
3. PF > 1.15
4. 최대낙폭 < 10%
5. 최악 월 net R > -2R
6. 상위 3개 거래를 제거해도 net R > 0
7. BTC와 ETH 어느 하나가 총 이익의 80%를 초과하지 않음
8. 같은 장면의 OHLCV-only baseline보다 out-of-sample log growth가 큼

최종 목표 게이트:

- 완료 거래 수 >= 포트폴리오 운용일
- funding·비용 후 기하 일평균 수익률 >= 1%
- 1.5배 비용에서도 양수
- 10× cap에서도 동일 방향

## 10. 실패 시 전환 원칙

V0.9가 실패하면 다음을 하지 않는다.

- 20/80을 19/81처럼 미세조정
- 손실 거래를 설명하는 세션 예외를 추가
- 잠금 월을 교체
- 결과가 좋은 symbol만 남김

대신 다음 중 하나로 독립 전환한다.

1. order-book depth/bookTicker를 추가한 absorption model
2. 더 넓은 사전 고정 liquid perpetual universe
3. cross-sectional relative-strength + liquidity-event 전략
4. SMC를 설명 feature로만 두고 pure intraday quant model과 비교

## 11. 구현 순서

1. checksum 검증 aggTrades/funding downloader
2. raw-to-normalized 변환과 manifest
3. 1초·1분 flow aggregation 테스트
4. 기존 scene root와 point-in-time join
5. 고정 규칙 arm
6. purged walk-forward meta-filter
7. 동일 execution core에 taker entry·notional cap·funding 연결
8. 개발 결과
9. 설정 동결
10. 잠금 실행

이 문서는 결과 확인 전에 작성한 계약이다. 변경이 필요하면 기존 문서를 덮어쓰지 않고 새 버전과 새 잠금 기간을 사용한다.
