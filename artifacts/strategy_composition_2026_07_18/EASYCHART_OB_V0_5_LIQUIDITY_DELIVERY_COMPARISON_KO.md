# EasyChart OB V0.5 M15 위치·M5 유동성 전달 장면 구현 및 비교

기준일: 2026-07-20  
상태: `IMPLEMENTED_AS_RESEARCH_CANDIDATE / POSITIVE_SMALL_SAMPLE / NO_PAPER_OR_LIVE`

## 1. 결론부터

V0.5는 다음 장면을 코드로 옮긴 것이다.

```text
H1 방향 또는 H1 범위 경계
→ 먼저 존재하는 활성 M15 EasyChart OB 위치
→ 그 OB 안의 확정 M15 pivot을 M5가 sweep·reclaim
→ 같은 반응에서 M5 구조 돌파를 직접 만든 OB 또는 FVG
→ 그 실행 구역의 첫 재방문 지정가
→ 주문 한 개
```

같은 여섯 개 14일 구간, 총 84종목일에서 나온 결과는 다음과 같다.

| arm | 거래 | 승 | 승률 | 합계 R | 평균 R | PF | 순손익 | 최대낙폭 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A. V0.3 전체 first-revisit | 10 | 7 | 70.00% | -1.173R | -0.117R | 0.604 | -355.99 | 3.00% |
| B. V0.4 PREEXISTING | 13 | 6 | 46.15% | -2.591R | -0.199R | 0.577 | -761.80 | 8.73% |
| C. **V0.5 단독** | **7** | **6** | **85.71%** | **+1.274R** | **+0.182R** | **2.283** | **+384.80** | **3.00%** |
| D. V0.3 전체 + V0.5 | 17 | 13 | 76.47% | +0.102R | +0.006R | 1.028 | +33.06 | 5.91% |
| E. **V0.3 BREAK_RETEST + V0.5** | **13** | **11** | **84.62%** | **+2.049R** | **+0.158R** | **2.007** | **+613.05** | **3.00%** |

현재 가장 나은 연구 조합은 E다. V0.5는 처음으로 양의 기대값을 보인 독립 장면이고, 기존 V0.3에서는 `BREAK_RETEST`만 함께 둘 가치가 있었다. 반대로 기존 V0.3 `SWEEP_RECLAIM` 네 거래의 `-1.947R`가 D의 이익을 거의 전부 없앴다.

그러나 E도 84종목일 동안 13건, 종목일당 약 `0.155건`이다. 수익 방향은 개선됐지만 사용자가 원하는 의미 있는 거래 빈도에는 아직 크게 못 미친다. V0.5 조건을 약화해 수를 채우기보다, 별도의 EasyChart 장면을 추가하는 것이 다음 과제다.

## 2. 이전 구조에서 무엇을 바꿨는가

V0.4 PREEXISTING은 M5 refinement OB와 나중의 구조 돌파봉이 분리돼 있었다. 따라서 선택한 OB가 실제 displacement를 만든 객체가 아닐 수 있었다.

V0.5에서는 실행 객체가 구조 돌파를 직접 소유한다.

- OB 경로: OB의 마지막 형성봉 자체가 M5 swing을 종가로 돌파해야 한다.
- FVG 경로: A-B-C 세 봉 중 중앙 displacement 봉 B가 M5 swing을 종가로 돌파해야 한다.
- 별도의 나중 돌파봉을 붙이지 않는다.
- 같은 유동성 반응에서 가장 먼저 확정된 적격 실행 객체 하나만 주문을 만든다.

또 하나의 중요한 변화는 sweep 탐지 단위다. M15 pivot의 생애 최초 sweep을 전역으로 한 번만 찾지 않고, `활성 M15 위치 + 그 안의 M15 pivot`이 함께 존재한 뒤의 첫 sweep을 찾는다. 따라서 위치가 생기기도 전에 있었던 사건을 나중 장면에 붙이지 않는다.

## 3. 확정한 OHLCV 판정식

### 3.1 M15 위치와 M15 유동성 node

- 위치는 같은 방향의 활성 M15 body-engulf EasyChart OB다.
- LONG은 M15 strict pivot low, SHORT은 pivot high를 node로 쓴다.
- node 가격은 M15 OB body 안 또는 양쪽 1 tick 허용 범위 안에 있어야 한다.
- location과 pivot이 모두 알려진 뒤의 첫 M5 sweep만 본다.
- H1/H4 방향 또는 H1 범위 경계 조건을 함께 통과해야 한다.

LONG M5 sweep·reclaim:

```text
previous_close > node
and event_low <= node - tick
and event_close >= node + tick
```

SHORT은 대칭이다.

```text
previous_close < node
and event_high >= node + tick
and event_close <= node - tick
```

사건 뒤 delivery가 완성되기 전 다음 종가가 나오면 반응 episode가 끝난다.

```text
LONG  close <= node - tick
SHORT close >= node + tick
```

### 3.2 OB delivery

OB 마지막 형성봉을 `D`, 그 전에 이미 확정된 최신 반대 M5 swing을 `S`라고 한다.

LONG:

```text
event.known_at <= D.open_time
D is bullish
S.kind == pivot_high
S.known_at <= D.open_time
every preceding OB formation close <= S.price
D.close >= S.price + tick
```

SHORT은 방향을 뒤집는다. 단순히 사건 뒤 같은 방향 OB가 나왔다는 이유만으로는 delivery가 아니다.

### 3.3 FVG delivery

연속 완료봉 A-B-C에서 다음 strict gap을 먼저 요구한다.

```text
BULLISH_FVG = C.low >= A.high + tick
zone = [A.high, C.low]

BEARISH_FVG = C.high <= A.low - tick
zone = [C.high, A.low]
```

그 뒤 중앙봉 B가 구조 돌파를 직접 소유해야 한다.

LONG:

```text
event.known_at <= B.open_time
B is bullish
A.close <= S.price
B.close >= S.price + tick
```

SHORT은 대칭이다. C가 나중에 swing을 돌파했지만 B가 돌파하지 못한 FVG는 V0.5 delivery가 아니다.

EasyChart 자막은 중요한 재시험 위치에서 OB 또는 FVG를 진입 도구로 사용할 수 있다고 설명한다. 다만 “중앙봉 B가 M5 swing을 직접 돌파해야 한다”는 수식은 급격한 displacement가 만든 FVG를 프로그램에서 구분하기 위한 V0.5 구현 정의다.

### 3.4 진입 구역

- OB 경로 기본 zone: OB body
- FVG 경로 기본 zone: FVG wick gap
- 기본 zone과 M15 위치가 1 tick 이상 겹치면 `M15+M5` 교집합 사용
- 겹치지 않으면 M5 실행 zone을 그대로 사용
- LONG 지정가: 선택 zone 상단
- SHORT 지정가: 선택 zone 하단
- 실행 객체가 완료된 뒤 첫 재방문만 사용

OB와 FVG가 같은 displacement에서 연이어 보이더라도 주문을 두 개 만들지 않는다. 주문 생성 시점에 이미 확정된 가장 이른 실행 객체 하나가 해당 장면을 소유한다.

### 3.5 최초 손절

기존에 합의한 장면 손절 소유권을 OB와 FVG에 대칭 적용한다.

```text
실행 formation 전체 wick 범위가 event_extreme을 포함
→ 실행 OB 또는 FVG formation이 stop 소유
→ formation 반대편 far wick ± 1 tick

포함하지 않음
→ M15 liquidity node의 M5 sweep event가 stop 소유
→ event_extreme ± 1 tick
```

모든 V0.5 실제 체결 7건은 두 번째 경로, 즉 event extreme 손절을 사용했다. formation-owned 분기는 코드에 존재하지만 이번 체결 표본에서는 발생하지 않았다.

### 3.6 최초 목표

처음 구현에서는 event 시점의 모든 반대 OB·FVG와 pivot 중 가장 가까운 것을 목적지로 취급했다. 그 결과 유효 delivery episode 11개 중 10개가 delivery 확정 전에 목표를 이미 사용한 것으로 처리돼 authority가 1개, 체결은 0개가 됐다.

원인은 외부 유동성 목적지와 단순 경로 장애물을 다시 같은 역할로 섞은 것이었다. 이를 다음처럼 바로잡았다.

1. sweep event 시점에 이미 확정된 반대편 M15 pivot을 우선한다.
2. 없으면 H1, 다시 없으면 H4 반대 pivot을 본다.
3. 같은 시간봉 안에서는 가장 최근 확정된 아직 사용되지 않은 반대 경계를 고른다.
4. M15/H1/H4 pivot이 전혀 없을 때만 event 전에 존재한 반대 H1/H4 OB·FVG를 대체 목적지로 쓴다.
5. delivery나 진입 전에 이 목적지가 먼저 도달되면 주문을 취소하며 다른 목표로 교체하지 않는다.

이 목표는 진입 전 고정된다. 실행 OB/FVG의 자체 impulse extreme이나 delivery 뒤 새로 생긴 가까운 구조물로 바꾸지 않는다.

### 3.7 체결 뒤 관리

기존 확정 규칙을 그대로 사용했다.

- 실제 진입가와 최초 손절가로 R을 다시 계산한다.
- `target_R >= 1.4`: 정확히 1R에서 50% 반익절, 실제 체결 뒤 잔량 stop을 진입가로 이동, 나머지는 최초 목표까지 보유한다.
- `target_R < 1.4`: 최초 목표에서 전량 익절한다.
- M5 또는 M15 `RVOL >= 2.0`, 유리한 종가, 비용 포함 양의 예상 순손익이면 다음 실행 가능 open에서 현재 잔량을 전량 청산한다.
- 그 밖의 진입 후 새 구조는 최초 손절과 최초 목표를 바꾸지 않는다.

## 4. 후보가 거래까지 내려온 과정

여섯 고정 구간 합계 funnel은 다음과 같다.

| 단계 | 수량 | 설명 |
|---|---:|---|
| M15 OB 객체 | 2,336 | 여섯 구간의 M15 EasyChart OB 합계 |
| M15 위치·pivot 조합 | 2,114 | pivot이 위치 안에 있고 사건 후보가 될 수 있는 조합 |
| 방향 맥락까지 통과한 M5 sweep | 57 | location-aware 첫 sweep·reclaim |
| delivery가 없었던 episode | 46 | 유효 반응 중 직접 구조 돌파 OB/FVG가 없음 |
| delivery가 있었던 episode | 11 | OB 또는 FVG가 구조 돌파를 직접 소유 |
| OB 적격 후보 | 87 | 한 episode 안의 후속 후보를 모두 센 진단 수량 |
| FVG 적격 후보 | 162 | 한 episode 안의 후속 후보를 모두 센 진단 수량 |
| delivery 전 목적지 소진 | 1 | 주문 생성 전에 장면 종료 |
| 최종 authority | 10 | 첫 재방문 대기 가능 장면 |
| 진입 전 목표 선소진 취소 | 3 | zone에 돌아오기 전에 목적지 도달 |
| 실제 완료 거래 | 7 | 비용·위험·관리까지 적용된 거래 |

가장 큰 빈도 병목은 M15 OB 수가 아니라 `M15 위치 안의 sweep 뒤 같은 반응에서 직접 구조를 돌파한 delivery`다. 57개 sweep 중 46개에는 적격 delivery가 없었다.

## 5. V0.5 일곱 거래

시간은 UTC다. 순손익은 각 10,000 USDT 독립 창에서 현재 잔고 3% 위험을 사용한 결과다.

| 환경 | 진입 시각 | 방향 | delivery | 진입가 | 손절가 | 목표가 | 목표 R | 1R 반익절 | 최종 청산 | 순 R | 순손익 |
|---|---|---|---|---:|---:|---:|---:|---|---|---:|---:|
| ETH 전환 | 2025-01-03 04:35 | SHORT | FVG | 3,460.97 | 3,476.34 | 3,441.36 | 1.276 | 아니오 | 거래량 | +0.303 | +90.84 |
| ETH 전환 | 2025-01-05 04:20 | LONG | FVG | 3,637.03 | 3,628.14 | 3,644.61 | 0.853 | 아니오 | 최초 목표 | +0.489 | +148.07 |
| ETH 전환 | 2025-01-10 04:40 | SHORT | FVG | 3,250.88 | 3,264.53 | 3,211.87 | 2.858 | 아니오 | 1R 전 거래량 | +0.178 | +54.74 |
| BTC 하락·고변동 | 2026-02-03 09:25 | SHORT | FVG | 78,331.30 | 79,040.10 | 77,909.80 | 0.595 | 아니오 | 최초 목표 | +0.494 | +148.30 |
| ETH 하락·고변동 | 2026-01-25 00:00 | SHORT | OB | 2,952.56 | 2,963.49 | 2,947.21 | 0.489 | 아니오 | 거래량 | +0.024 | +7.12 |
| BTC 상승 | 2026-04-16 17:25 | SHORT | FVG | 74,219.20 | 74,900.00 | 73,235.80 | 1.444 | 아니오 | 최초 손절 | -1.000 | -299.96 |
| BTC 횡보 | 2026-06-09 07:05 | SHORT | FVG | 63,280.10 | 63,433.60 | 62,668.30 | 3.986 | 예 | 거래량 | +0.786 | +235.70 |

요약:

- FVG: 6건, 5승 1패, `+1.250R`, PF 2.27
- OB: 1건, 1승, `+0.024R`
- 거래량 청산: 4건
- 최초 목표 전량익절: 2건
- 최초 손절: 1건
- 1R 반익절 실제 발생: 1건

FVG가 우세해 보이지만 FVG 6건, OB 1건뿐이므로 현재 단계에서 OB 경로를 삭제하지 않는다. 두 경로는 계속 분리 기록한다.

## 6. 한 슬롯 결합 결과

### V0.3 전체와 결합

```text
V0.3 전체  -1.173R
V0.5       +1.274R
결합       +0.102R
```

슬롯 때문에 억제된 authority는 0개였다. 두 가족의 거래가 시간상 충돌하지 않아 결합 결과는 사실상 두 손익의 합이었다. 거래 수는 17건으로 늘었지만 PF 1.028, 평균 +0.006R에 불과했다.

### V0.3 BREAK_RETEST만 결합

| 하위 장면 | 거래 | 승 | 합계 R | PF |
|---|---:|---:|---:|---:|
| V0.3 BREAK_RETEST | 6 | 5 | +0.775R | 1.728 |
| V0.5 FVG | 6 | 5 | +1.251R | 2.271 |
| V0.5 OB | 1 | 1 | +0.024R | 손실 0건 |
| **한 슬롯 결합** | **13** | **11** | **+2.049R** | **2.007** |

결합 arm E의 환경별 순손익은 BTC 하락·고변동, BTC 횡보, ETH 하락·고변동, ETH 상승에서 양수였고, BTC 상승과 ETH 전환에서 음수였다. 환경 이름을 이용한 별도 진입 필터는 추가하지 않았다.

## 7. 현재 결정

연구 후보로 유지:

- V0.5 `M15 location → M5 sweep → displacement-owning OB/FVG → first revisit`
- V0.3 `BREAK_RETEST` first-revisit
- 두 장면만 결합한 E arm

현재 연구 조합에서 제외:

- V0.3 `SWEEP_RECLAIM` 하위형
- V0.4 교정 EVENT_CREATED 전체
- V0.4 PREEXISTING 전체
- 거래 수만 늘리기 위한 D의 무조건 결합

변경하지 않음:

- 현재 잔고의 거래당 3% 위험
- 일일 총 손실 제한 OFF 기본값
- 한 슬롯, 추가 매수 없음
- 진입 전 진입가·최초 손절가·최초 목표가 고정
- 1.4R 분기와 거래량 청산

E는 현재까지 가장 좋은 고정 비교 arm이지만 거래 수가 13건뿐이다. 이 코드는 전략 연구 후보이며 paper/live 주문 권한을 갖지 않는다.

## 8. 다음 방향

빈도를 높이기 위해 V0.5의 핵심 조건을 풀어 아무 OB/FVG를 받는 것은 적절하지 않다. 다음에는 서로 다른 근거를 가진 EasyChart 장면을 하나 더 추가해야 한다.

우선 검토할 장면은 다음이다.

```text
먼저 존재하는 1H+15m 또는 15m+5m 같은 방향 OB 중첩
→ M15 수평 구조 전환 종가 확정
→ 그 뒤 중첩 구역 첫 재방문
→ 주문 한 개
```

이는 실패한 V0.4처럼 M5 sweep과 별도 M5 돌파를 여러 겹 요구하는 구조가 아니다. 기존 다중 시간봉 OB 위치와 구조 전환 뒤 첫 회귀라는 별도 EasyChart 장면으로 다뤄야 한다. 구체 수식과 손절 소유권은 구현 전에 사용자와 정한다.

## 9. 산출물

- V0.5 구현: `src/ictbt/easychart_v0/v05.py`
- 장면 도메인: `src/ictbt/easychart_v0/domain.py`
- 공통 재생·pending 취소: `src/ictbt/easychart_v0/v04.py`
- 주문·위험 엔진: `src/ictbt/easychart_v0/execution.py`
- 비교 실행기: `scripts/compare_easychart_v05_liquidity_delivery.py`
- 요약: `results/easychart_v05_m15_m5_liquidity_delivery/summary.json`
- 전체 거래 원장: `results/easychart_v05_m15_m5_liquidity_delivery/trade_ledger.csv`
- funnel·취소 진단: `results/easychart_v05_m15_m5_liquidity_delivery/diagnostics.csv`
- 집중 테스트: `tests/easychart_v0/test_v05.py`

EasyChart V0 테스트는 92개 모두 통과했다.
