# EasyChart–SMC Causal Delivery V1

작성일: 2026-07-20 KST  
상태: **RESEARCH_ONLY**

## 핵심 명제

이 전략은 OB나 FVG의 모양을 독립 신호로 거래하지 않는다.

> 사전에 확정된 draw on liquidity → 완료된 유동성 사건 → 의미 있는 기존 swing을 직접 돌파한 displacement → 같은 displacement가 만든 fresh execution array의 회귀 → 인과적 무효화 가격 → 진입 전에 존재한 다음 미소진 구조 목적지

위 연결 중 하나라도 없거나 서로 충돌하면 `NO_TRADE`다. 기존 GitHub V0 전략은 권위가 아니라 비교·반증 대상으로 둔다.

## 하나의 전략, 두 시장 서사

### REVERSAL

```text
유효한 HTF location
→ external liquidity sweep
→ 완료봉 reclaim
→ 같은 반응이 기존 swing을 종가 돌파
→ event-owned OB/FVG/overlap 형성
→ 첫 clean return
→ sweep extreme 바깥 stop
→ 반대편 첫 구조 목적지
```

sweep만 있거나, 위치 없는 MSS만 있거나, 이벤트를 직접 소유하지 않은 FVG만 있으면 거래하지 않는다.

### CONTINUATION

```text
사전에 존재한 HTF draw on liquidity
→ H1 방향과 일치하는 경계 종가 돌파
→ 돌파 상태 acceptance
→ 같은 전달이 기존 swing을 displacement로 돌파
→ owned FVG/OB/broken boundary 첫 retest
→ causal origin 바깥 stop
→ 진행 방향 첫 구조 목적지
```

wick 돌파만으로 지속을 선언하지 않는다. 돌파 뒤 반대 external sweep이 새로 발생하거나 H1 방향 불일치·H4 정면 충돌이 있으면 기존 지속 서사를 폐기한다.

### DELIVERED_OB_REENTRY

재진입은 세 번째 시장 서사가 아니라, 유효한 continuation authority의 새 episode다.

```text
이전 포지션 완전 종료
→ 원 authority 무효화 없음
→ 최소 1.0R 재이탈
→ fresh micro delivery 확인
→ 새 독립 주문
```

같은 포지션에 수량을 더하지 않는다. authority 생명은 72시간, 최대 episode는 4회이며 stop이 발생하면 즉시 폐기한다.

## 역할 분리

```text
H4 = H1 아이디어와 정면 충돌하는 큰 delivery 확인
H1 = 방향과 external draw on liquidity 제공
M15 = sweep/reclaim 또는 break/acceptance 사건 완성
M5 = 사건을 직접 소유한 displacement와 실행 배열 제공
```

실행 OB/FVG는 이벤트 뒤 생성되고, 같은 방향이며, 이벤트 전에 확정된 swing을 종가 돌파하고, body가 최소 0.5 ATR이어야 한다. 근처에 OB와 FVG가 동시에 있다는 이유만으로 confluence라 부르지 않는다.

## 진입·무효화·목표

허용 실행 배열은 event-owned EasyChart OB, event-owned FVG, 실제 OB/FVG 교집합, accepted broken boundary다. 진입가는 owned zone 내부여야 하며 displacement 추격을 기본 경로로 사용하지 않는다.

손절은 고정 거리보다 가설 무효화 위치를 따른다.

- reversal: sweep extreme 바깥
- continuation: break를 소유한 displacement origin 또는 protected swing 바깥
- reentry: 아직 유효한 authority formation extreme 바깥

목표는 진입 전에 존재하고 아직 소비되지 않은 구조만 인정한다.

1. external liquidity
2. confirmed pivot
3. opposing order block
4. 별도 liquidity owner와 겹치는 FVG

FVG 단독은 terminal target이 아니다. V0.7에서 모든 반대 FVG를 동급 목표로 사용한 방식이 약했으므로 실행 배열과 목적지 역할을 분리한다. 진입 방향에서 가장 가까운 유효 장애물이 day-trade exit를 소유하며, 먼 목표의 R을 얻기 위해 가까운 장애물을 건너뛰지 않는다.

비용 후 첫 장애물까지 0.35R 미만이면 거래하지 않는다. 1.4R 미만이면 첫 장애물에서 전량익절하고, 1.4R 이상이면 1R에서 50%가 실제 체결된 뒤 잔량 stop을 본절로 옮기고 첫 장애물까지 보유한다.

## BTC/ETH 전역 라우팅

pending과 open을 합쳐 BTC/ETH 전체 최대 하나다. 모든 boolean gate를 통과한 후보끼리만 다음 순서로 비교한다.

```text
먼저 실행 가능한 entry time
→ external target
→ target 종류
→ 가까운 장애물까지 비용 후 net R
→ displacement 강도
→ 더 낮은 required leverage
→ symbol과 stable ID
```

같은 symbol·같은 decision time에 long과 short가 동시에 성립하면 family priority로 하나를 고르지 않고 `NO_TRADE`로 처리한다.

## 고정 3% 위험

```text
risk_budget = 주문 직전 current equity × 0.03

all_in_stop_loss_per_unit
= adverse stop price loss
+ entry fee
+ stop fee
+ stop adverse slippage

quantity = risk_budget / all_in_stop_loss_per_unit
```

수량과 비용 포함 stop 손실의 곱이 정확히 당시 equity의 3%가 되어야 한다. 이 위험을 만들기 위해 요구되는 notional이 정책상 10x를 넘으면 손절을 임의로 바꾸지 않고 거래를 거절한다.

## 상태기계

```text
OBSERVING → ARMED → ENTRY_PENDING → OPEN
OPEN → stop: INVALIDATED
OPEN → profit: DEPARTURE_REQUIRED
DEPARTURE_REQUIRED → 1R departure + fresh micro delivery → REARMED
lifetime 초과 → EXPIRED
```

`ADD_SIZE`, `AVERAGE_DOWN`, BTC와 ETH의 동시 open 상태는 존재하지 않는다.

## 구현과 테스트

독립 패키지:

- `src/ictbt/easychart_smc_v1/policy.py`
- `src/ictbt/easychart_smc_v1/__init__.py`
- `tests/easychart_smc_v1/test_policy.py`

13개 계약 테스트가 다음을 확인한다.

- 완전한 reversal causal chain 승인
- 구조 전달 없는 OB/FVG 거절
- reclaim 없는 sweep 거절
- external sweep과 첫 clean return 강제
- FVG 단독 terminal target 금지
- 가까운 장애물을 건너뛰는 목표 금지
- continuation acceptance·HTF 정렬·반대 sweep 검사
- 비용 포함 정확한 3% 수량
- 과도한 required leverage 거래 거절
- 같은 symbol 반대 서사 충돌 거절
- BTC/ETH 전역 한 슬롯
- 완전 종료·재이탈·fresh delivery 뒤에만 재무장
- stop 뒤 authority 폐기와 물타기 금지

## blind proxy 결과의 해석 경계

동결한 기존 three-family proxy를 겹치지 않는 seed 8,000–9,999의 random 140일 표본 2,000개에 적용한 결과:

| 지표 | 결과 |
|---|---:|
| 최악 계좌배수 | 3.110배 |
| 하위 1% | 4.827배 |
| 하위 5% | 7.513배 |
| 중앙값 | 20.786배 |
| 5배 이상 | 98.75% |
| 중앙 거래 수 | 1,187.5건 |
| 중앙 승률 | 83.24% |
| 중앙 평균 순R | +0.0894R |
| 최대 관측 MDD | 43.85% |

이 결과는 edge 후보가 여러 표본에서 재현됐다는 증거이지만, 새 구조 목적지 로직의 수익성을 증명하지 않는다. 최악 표본은 5배 미만이고 최대 MDD도 35% 승격선보다 높으므로 paper/live 권한은 없다.

## 다음 승격 계약

새 V1 detector, displacement ownership graph, fresh-array lifecycle, pre-entry target map, actual-target replay, exact-risk adapter를 연결한 뒤 규칙을 동결하고 미사용 seed 10,000–19,999를 한 번에 재생한다.

```text
모든 random 140일 sample >= 5.0x
하위 1% 평균 순R >= +0.08R
모든 sample 평균 순R > 0
모든 sample 완료 거래 > 140
최대 MDD <= 35%
어느 한 연도도 전체 log growth의 50% 이상 독점하지 않음
```

maker fill miss, taker 체결, stop slippage, M5 지연, 부분체결, 동일봉 adverse path, gap-through stop, 재접속·주문 reconciliation 스트레스도 통과해야 한다.

## 전문가 설명용 한 문장

```text
WHERE     = HTF location와 draw on liquidity
WHY NOW   = sweep/reclaim 또는 break/acceptance
WHO OWNS  = 기존 swing을 직접 돌파한 displacement
WHERE IN  = 그 displacement가 만든 fresh array의 회귀
WRONG     = causal invalidation extreme
WHERE OUT = 가장 가까운 pre-existing unconsumed structural objective
HOW MUCH  = 비용 포함 current equity의 정확한 3%
HOW MANY  = BTC/ETH 전역 pending/open 1개
```

V1의 목적은 SMC 용어를 많이 넣는 것이 아니라, 위치·사건·전달·실행·무효화·목적지의 시간 순서를 닫는 것이다.
