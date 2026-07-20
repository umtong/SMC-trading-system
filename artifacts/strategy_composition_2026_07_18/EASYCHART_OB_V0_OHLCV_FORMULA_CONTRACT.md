# EasyChart OB V0.3 OHLCV 판정 수식 계약

상태: `V0_3_FORMULAS_IMPLEMENTED / ECONOMIC_EDGE_UNCONFIRMED / NO_ORDER_AUTHORITY`  
전략 이름: `easychart_ob_v0_3_m15_event_m5_delivery`  
기준일: `2026-07-20`

이 문서는 [V0.3 정책 기준선](EASYCHART_OB_V0_POLICY_DECISION_DRAFT.md)을 OHLCV 프로그램이 같은 방식으로 계산하기 위한 수식 권위다. 현재 적용 범위는 `A1_B1_CONFLUENCE` 한 장면이다.

성과 수치는 이 문서에서 중복 관리하지 않는다. 최신 경제 상태는 [메인 에이전트 상태 기준선](../../MAIN_AGENT_STATE.md), 고정 진입 방식 비교의 상세 근거는 [V0.3 진입 방식 고정 비교 보고서](EASYCHART_OB_V0_3_ENTRY_ARM_COMPARISON_KO.md)를 따른다.

## 1. 데이터와 시간 표기

시간봉 `T`의 완료봉을 다음처럼 쓴다.

```text
B[T,i] = (open, high, low, close, volume, open_time, close_time)
body_low  = min(open, close)
body_high = max(open, close)
tick      = 거래소 tick_size
```

- 봉 구간은 UTC `[open_time, close_time)`이다.
- 신호는 해당 봉의 `close_time`에 확정된다.
- `strictly above level`은 `price >= level + tick`, `strictly below level`은 `price <= level - tick`이다.
- 동가 접촉은 touch에는 포함하지만 strict sweep·break에는 포함하지 않는다.
- 전략 역할은 `H4 context → H1 direction/node → M15 event → M5 delivery/entry` 순서다.

## 2. body-engulf OB

### 2.1 `SIMPLE_2C`

연속 완료봉 `P`, `E`에 대해 다음과 같다.

```text
BULLISH_SIMPLE =
  close(P) < open(P)
  and close(E) > open(E)
  and body_low(E)  <= body_low(P)
  and body_high(E) >= body_high(P)

BEARISH_SIMPLE =
  close(P) > open(P)
  and close(E) < open(E)
  and body_low(E)  <= body_low(P)
  and body_high(E) >= body_high(P)
```

- `P`, `E`는 non-doji다.
- OB zone은 감싸진 `P`의 `[body_low(P), body_high(P)]`다.
- 형성봉은 `P..E`, 완료 시점은 `close_time(E)`다.
- OB 자체 formation extreme은 LONG `min(low(P),low(E))`, SHORT `max(high(P),high(E))`다.

### 2.2 `DOUBLE_3C`

```text
BULLISH_DOUBLE =
  C1 bullish, C2 bearish, C3 bullish
  and body(C2) engulfs body(C1), inclusive
  and body(C3) engulfs body(C2), inclusive

BEARISH_DOUBLE =
  C1 bearish, C2 bullish, C3 bearish
  and body(C2) engulfs body(C1), inclusive
  and body(C3) engulfs body(C2), inclusive
```

- 세 봉 모두 non-doji다.
- zone은 가운데 `C2` body다.
- 형성봉은 `C1..C3`, 완료 시점은 `close_time(C3)`다.
- OB 자체 formation extreme은 LONG `min(low(C1:C3))`, SHORT `max(high(C1:C3))`다.

몸통 장악은 OB 객체를 만드는 조건이다. V0.3 실행 OB가 되려면 §5의 M5 MSS 소유 조건도 별도로 충족해야 한다.

## 3. strict pivot과 상위 구조

### 3.1 strict 5봉 pivot

가운데 봉 `i`는 다음을 만족하고 `i+2` 봉이 닫힐 때 확정된다.

```text
PIVOT_HIGH[T,i] = high[i] > high[i-2], high[i-1], high[i+1], high[i+2]
PIVOT_LOW[T,i]  = low[i]  < low[i-2],  low[i-1],  low[i+1],  low[i+2]
```

기본 `pivot_strength=2`이며 동가 high·low가 섞이면 pivot으로 만들지 않는다.

### 3.2 H1·H4 구조 상태

최근 확정 pivot high 두 개를 `H0,H1`, low 두 개를 `L0,L1`로 둔다.

```text
UP    = H1 > H0 and L1 > L0
DOWN  = H1 < H0 and L1 < L0
RANGE = 그 밖

LONG_ALLOWED  = H1 == UP   and H4 != DOWN
SHORT_ALLOWED = H1 == DOWN and H4 != UP
```

H1이 `RANGE`이면 최신 H1 경계 node의 사건만 허용한다.

```text
latest H1 low  SWEEP_RECLAIM → LONG
latest H1 high SWEEP_RECLAIM → SHORT
latest H1 high BREAK_RETEST   → LONG
latest H1 low  BREAK_RETEST   → SHORT
```

## 4. H1 node에서 완료되는 M15 사건

node는 사건봉이 열릴 때 이미 확정된 H1 strict pivot의 wick 가격이다. 모든 사건은 M15 완료봉으로 판정한다.

### 4.1 `SWEEP_RECLAIM`

H1 pivot low `L`의 LONG 사건:

```text
previous_close > L
and low[event] <= L - tick
and close[event] >= L + tick
```

H1 pivot high `H`의 SHORT 사건:

```text
previous_close < H
and high[event] >= H + tick
and close[event] <= H - tick
```

```text
LONG  event_extreme = low[event]
SHORT event_extreme = high[event]
event known_at = close_time(event)
```

### 4.2 `BREAK_RETEST`

H1 pivot high `H`의 LONG break:

```text
previous_close <= H
and close[break] >= H + tick
```

H1 pivot low `L`의 SHORT break:

```text
previous_close >= L
and close[break] <= L - tick
```

break 뒤 node를 처음 다시 touch한 M15 봉을 retest bar로 사용한다.

```text
LONG_RETEST_ACCEPTED = low[retest] <= H and close[retest] >= H + tick
SHORT_RETEST_ACCEPTED = high[retest] >= L and close[retest] <= L - tick
```

- 첫 retest 전 LONG `close <= H-tick`, SHORT `close >= L+tick`이면 해당 break episode를 끝낸다.
- 첫 retest가 acceptance 종가를 충족하지 못해도 해당 episode를 끝낸다.
- LONG `event_extreme=low[retest]`, SHORT `event_extreme=high[retest]`다.
- `event known_at=close_time(retest)`다.

## 5. M15 사건 뒤 M5 MSS-owning OB

M5 OB의 마지막 형성봉을 `D`, 그 앞 형성봉들을 `PRE`, `D.open_time`까지 확정된 최신 M5 swing을 `S`라고 한다.

```text
EVENT_BEFORE_DELIVERY = M15_event.known_at <= D.open_time

LONG_M5_DELIVERY =
  EVENT_BEFORE_DELIVERY
  and OB.side == LONG
  and S.kind == pivot_high
  and S.known_at <= D.open_time
  and D bullish
  and every close(PRE) <= S.price
  and close(D) >= S.price + tick

SHORT_M5_DELIVERY =
  EVENT_BEFORE_DELIVERY
  and OB.side == SHORT
  and S.kind == pivot_low
  and S.known_at <= D.open_time
  and D bearish
  and every close(PRE) >= S.price
  and close(D) <= S.price - tick
```

같은 방향이고 사건 episode가 유지되는 OB 중 위 식을 처음 충족한 M5 OB를 confirmation으로 선택한다. 단순히 사건 뒤에 나온 첫 같은 방향 OB는 사용하지 않는다.

M5 OB 완료 전까지 M15 종가가 LONG `node-tick` 이하, SHORT `node+tick` 이상으로 되돌아가면 해당 사건 episode는 무효다.

## 6. A1 admission과 진입 zone

A1은 M15 사건보다 먼저 존재해야 한다.

```text
A1 = event.node_id와 같은 확정 H1 pivot
  or 활성 H1/M15 same-side OB

OB_A1_OWNS_NODE =
  A1.zone.low - tick <= event.node_price <= A1.zone.high + tick

A1.known_at <= event.known_at
```

M15 A1 body와 M5 delivery OB body의 교집합 폭이 1 tick 이상이면 교집합을 사용한다. 아니면 M5 delivery OB body를 사용한다.

```text
entry_zone = intersection(M15_A1.body, M5_OB.body), if width >= tick
             else M5_OB.body

LONG planned_entry  = entry_zone.high
SHORT planned_entry = entry_zone.low
```

같은 사건에 H1 A1과 M15 A1이 모두 있으면 M15 A1+M5 delivery 장면을 우선한다. 중첩은 가격 refinement이며 보편적 admission 조건은 아니다.

## 7. 장면 initial stop

```text
formation_low  = min(low of all M5 OB formation bars)
formation_high = max(high of all M5 OB formation bars)

OB_OWNS_STOP = formation_low <= event_extreme <= formation_high

stop_extreme =
  M5_OB formation far wick, if OB_OWNS_STOP
  event_extreme, otherwise

LONG initial_stop  = stop_extreme - tick
SHORT initial_stop = stop_extreme + tick
```

이 값은 주문 전에 고정한다. 실제 1R 반익절이 체결되기 전에는 바꾸지 않는다.

## 8. independent initial target

실행 M5 OB 자체의 impulse extreme은 target 후보를 만들지 않는다. 같은 가격의 독립 pivot·반대 OB·FVG는 각자 권위를 유지한다.

활성 후보는 다음과 같다.

1. 장면 소유 M5/M15 확정 pivot
2. 확정 H1/H4 pivot
3. 반대 M15/H1/H4 OB body zone
4. 반대 M15/H1/H4 FVG zone

FVG:

```text
BULLISH_FVG = low(C) >= high(A) + tick; zone=[high(A), low(C)]
BEARISH_FVG = high(C) <= low(A) - tick; zone=[high(C), low(A)]
```

- 이미 strict하게 돌파된 pivot과 완전히 돌파된 반대 zone은 제외한다.
- 겹치거나 경계를 공유하는 후보는 하나로 합친다.
- LONG은 entry 위 가장 가까운 proximal 가격, SHORT은 entry 아래 가장 가까운 proximal 가격을 고른다.
- 가장 가까운 후보가 최소 1 tick의 유리한 거리와 설정 비용 반영 양의 예상 순손익을 만들지 못하면 `TARGET_SPACE_CONFLICT`다. 더 먼 목표로 건너뛰지 않는다.

## 9. event-created entry arm

현재 생산 confirmation은 `EVENT_CREATED`다. `event_created_entry_mode`는 전략 실행 arm이며 기본값은 `NEXT_BAR_OPEN`이다.

### 9.1 `NEXT_BAR_OPEN` — 기본

- intent는 M5 delivery OB의 완료 시점에 생성된다.
- 그 뒤 첫 합법 M5 bar open 한 번만 진입 시도한다.

```text
LONG_VALID_OPEN  = initial_stop < actual_open < initial_target
SHORT_VALID_OPEN = initial_target < actual_open < initial_stop
```

식이 거짓이면 `ENTRY_REJECTED`다. 참이면 actual open으로 체결하고 §10의 수량을 다시 계산한다.

### 9.2 `LIMIT_FIRST_REVISIT` — 비교 arm

- LONG limit은 entry zone 상단, SHORT limit은 entry zone 하단이다.
- 완료 뒤 첫 touch 한 번만 유효하다.
- stop 안쪽에서 limit을 유리하게 통과한 open은 실제 open에서 체결한다.
- open이 initial stop 바깥이면 체결하지 않는다.
- initial target이 먼저 소진되거나 M15 event가 먼저 무효화되면 pending을 취소한다.
- 고정 봉 수 TTL은 없다.

`PREEXISTING` 실행 OB는 `LIMIT_FIRST_REVISIT`만 허용하지만 현재 V0.3 생산 경로에는 포함하지 않는다.

## 10. 위험예산과 수량

현재 사용자 기본값:

```text
risk_fraction = 0.03
daily_loss_limit_enabled = false
daily_loss_limit_fraction = 0.01
daily_reset_timezone = Asia/Seoul
```

```text
risk_budget = current_equity * risk_fraction
adverse_stop_fill = initial_stop adjusted by configured stop slippage

unit_stop_risk =
  abs(entry - initial_stop)
  + abs(initial_stop - adverse_stop_fill)
  + entry_fee_per_unit
  + stop_fee_per_unit

raw_qty = risk_budget / unit_stop_risk
qty = floor_to_exchange_quantity_step(raw_qty)
```

최소 수량·최소 주문금액을 만족하지 못하면 주문하지 않는다. `NEXT_BAR_OPEN`은 actual open으로 `unit_stop_risk`와 qty를 다시 계산해 같은 cash risk budget 안에 둔다.

완료 거래의 비용 포함 순손익은 다음 거래의 `current_equity`에 반영된다. 일일 제한이 OFF인 현재 설정에서는 일일 손익으로 신규 거래를 막지 않는다. ON이면 KST 날짜 시작 equity 대비 실현 순손익으로 새 주문만 제한한다.

## 11. 체결 뒤 관리

```text
R = abs(actual_entry - initial_stop)

LONG_target_R  = (initial_target - actual_entry) / R
SHORT_target_R = (actual_entry - initial_target) / R
```

- `target_R >= 1.4`: 1R에서 최초 수량 50%를 한 번 익절한다. 실제 체결 뒤 잔량 stop을 actual entry로 옮기고, 잔여 50%는 initial target에서 익절한다.
- `target_R < 1.4`: partial 없이 initial target에서 100% 익절한다.
- 정확히 1.4R은 partial 경로다.
- 진입 뒤 새 구조는 initial target을 바꾸지 않는다.

### 11.1 수익 상태 거래량 전량익절

M5와 M15를 각각 계산한다.

```text
RVOL[T,i] = volume[T,i] / median(volume[T,i-20:i-1])
VOLUME_EXPANDED = RVOL >= 2.0
```

```text
LONG_SIGNAL =
  RVOL >= 2.0
  and signal_close >= actual_entry
  and estimated_net_pnl(signal_close, remaining_qty) > 0

SHORT_SIGNAL =
  RVOL >= 2.0
  and signal_close <= actual_entry
  and estimated_net_pnl(signal_close, remaining_qty) > 0
```

신호 뒤 다음 실행 가능 open에서도 같은 가격 방향과 비용 포함 양의 순손익을 확인한다. 유지되면 현재 잔량 100%를 청산하고, 아니면 거래량 청산 예약만 취소한다.

## 12. 사건 순서와 한 슬롯

- backtest는 제공되는 더 낮은 native OHLCV로 같은 봉의 entry·partial·target·stop 순서를 세분한다.
- 가장 작은 제공 봉에서도 순서를 알 수 없으면 stop을 먼저 적용한다.
- limit entry와 stop이 같은 최저 봉에서 처음 함께 닿으면 `entry → stop`으로 기록한다.
- partial과 stop이 같은 최저 봉에서 겹치면 stop 우선이다. partial이 먼저였음이 하위 봉에서 확인되면 그 체결 뒤부터 잔량 stop을 actual entry로 바꾼다.
- 데이터 끝의 미종료 position은 `OPEN_CENSORED`다.
- 전체 BTC·ETH를 합쳐 pending intent 또는 open position은 한 개만 허용한다.
- pending 중 다른 주문, 보유 중 신규 진입·추가매수는 만들지 않는다.

## 13. V0.3 고정값 요약

| 항목 | 값 |
|---|---|
| context | H1/H4 structure |
| liquidity node | confirmed H1 strict pivot |
| event clock | M15 completed bar |
| delivery | M5 body-engulf OB whose final bar owns MSS |
| location timing | A1 exists no later than M15 event completion |
| overlap | optional M15 A1 + M5 delivery body intersection |
| default entry arm | `NEXT_BAR_OPEN` |
| comparison arm | `LIMIT_FIRST_REVISIT` |
| stop | M5 OB wick if it contains event extreme, otherwise event extreme; then 1 tick outside |
| targets | independent pivots and opposing M15/H1/H4 OB/FVG only |
| partial | target_R ≥ 1.4이면 1R에서 50% 한 번 |
| post-partial stop | actual entry |
| volume exit | M5/M15 RVOL ≥ 2.0 + favorable price + positive estimated net |
| user risk default | current equity의 3% |
| daily limit default | OFF |
| position count | 전체 시스템 최대 한 개 |
