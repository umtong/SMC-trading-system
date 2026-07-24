# State-First L1/Trade-Flow V1 사전등록

작성 시점: 2026-07-24. 4개 고정 날짜의 모델 성과를 계산하기 전.

## 가설

단순 지정가 호가 제공은 선택적 체결 때문에 adverse selection을 가진다. 반대로, 완성된 1초 구간에서 관측된 L1 유동성 상태와 실제 공격 주문흐름을 함께 사용할 때만 다음 수초의 방향성 가격충격이 비용을 넘을 수 있다. 호가 상태가 없는 주문흐름 단독 모델과 주문흐름이 없는 L1 단독 모델을 음성 대조군으로 유지한다.

## 고정 자료와 날짜

공식 Binance Vision USD-M `bookTicker`와 `aggTrades`, 각 ZIP의 인접 CHECKSUM SHA-256 검증.

- 학습: 2023-05-16
- 선택: 2023-06-10
- 독립 검증: 2023-08-18
- 단 한 번의 최종 날짜: 2023-11-09
- BTCUSDT·ETHUSDT

날짜는 기존 queue-calibration 계약에서 고정된 서로 다른 활동·레짐 표본이며 결과를 본 뒤 바꾸지 않는다.

## 정보 시계와 체결

- 초 s의 특징은 `[s, s+1초)`에 발생한 거래와 s+1초 직전까지의 마지막 BBO만 사용한다.
- 신호는 s+1초에 알려진다.
- 진입은 s+1초 이후 최초 BBO의 ask(롱)/bid(숏)에서 taker 체결한다.
- 청산은 실제 진입 BBO event time에서 고정 3·10·30·60초 후 최초 BBO의 bid(롱)/ask(숏)에서 체결한다.
- 1초 이상 진입 BBO가 없거나 필요한 청산 BBO가 없으면 후보를 제거한다.
- 전 종목 통합 동시 포지션은 최대 1개다.

## 특징

L1 상태: spread, depth, imbalance, microprice deviation, quote age, prior-only depth/spread z-score.

주문흐름: 1·5·30초 signed aggressive quote imbalance, 거래대금·거래수 prior-only z-score, buy/sell VWAP deviation, flow acceleration.

가격 상태: 1·5·30초 return, prior realized volatility, price-impact/absorption residual.

현재 초 이후의 quote·trade·수익·체결 결과는 입력에 포함하지 않는다.

## 고정 모델과 후보

- Ridge(alpha=30)
- HistGradientBoosting(max_leaf_nodes=15,max_depth=5,l2=20,min_samples_leaf=100)
- ExtraTrees(300,max_depth=10,min_samples_leaf=100,max_features=0.7)
- horizon 3·10·30·60초
- 학습 예측 절대값 95·97.5·99·99.5·99.9 분위수
- 총 60개 후보

## 비용과 선택

BBO crossing으로 spread를 직접 반영하고, 추가 왕복 비용은 기본 12bp, 스트레스 18bp와 24bp다.

선택·검증 모두에서:
- 거래수 >=100
- 12bp 및 18bp 비용 후 평균 >0
- 18bp PF >=1.10
- 상위 20개 양의 거래 제거 후 평균 >0
- 최대낙폭 <15%

score는 selection과 validation의 18bp 평균 중 작은 값이다. 목표 일수익률과의 거리는 사용하지 않는다. score 1위 하나만 2023-11-09를 개봉한다.

## 경계

4일 결과는 장기 지속 가능성 증명이 아니다. 최종 날짜가 1% 이상이어도 다수 월·다수 연도·다른 거래소·용량·latency 검증 전에는 Paper/Live 후보로 승격하지 않는다.
