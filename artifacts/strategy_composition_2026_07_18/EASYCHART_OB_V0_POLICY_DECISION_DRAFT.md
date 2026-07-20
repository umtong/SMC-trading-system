# EasyChart OB 중심 전략 V0.3 정책 기준선

상태: `V0_3_IMPLEMENTED / ECONOMIC_EDGE_UNCONFIRMED / NO_ORDER_AUTHORITY`  
전략 이름: `easychart_ob_v0_3_m15_event_m5_delivery`  
기준일: 2026-07-20

최신 판단: 이 문서의 V0.3은 비교 기준선으로 유지한다. V0.5 `M15 위치 → M5 sweep → displacement-owning OB/FVG → 첫 재방문`과 V0.3 BREAK_RETEST의 한 슬롯 조합은 13건 `+2.049R`으로 현재 연구 선두다. 빈도 보완용 V0.6 B+는 두 손절 arm 모두 음수였다. 이어 구현한 V0.7 `확정 경계 돌파·수용 + M15 FVG`도 첫 회귀 지정가 19건 `-1.598R`, 실제 다음 M5 시가 31건 `-1.090R`이었다. 다음 시가 V0.7을 선두와 합치면 거래는 44건으로 늘지만 수익률은 선두 단독 `+6.14%`에서 `+2.37%`로 낮아졌다. 따라서 V0.6·V0.7은 활성 조합에서 제외하고 연구 선두를 유지한다. 최신 상세 결과는 [V0.7 구현 및 비교 보고서](EASYCHART_SR_FLIP_FVG_V0_7_COMPARISON_KO.md)를 따른다.

이 문서는 현재 구현된 V0.3의 투자 판단 흐름을 사람이 읽기 쉽게 정리한 정책 문서다. 정확한 OHLCV 수식은 [EASYCHART_OB_V0_OHLCV_FORMULA_CONTRACT.md](EASYCHART_OB_V0_OHLCV_FORMULA_CONTRACT.md), 프로젝트 현재 상태와 결과는 [MAIN_AGENT_STATE.md](../../MAIN_AGENT_STATE.md)를 따른다.

## 1. 한 줄 흐름

```text
H1·H4 구조로 허용 방향을 정함
→ 사건보다 먼저 존재한 A1 위치와 그 위치의 확정 H1 유동성 node를 선택
→ 그 node에서 M15 sweep·reclaim 또는 break·first-retest 사건이 완료됨
→ 사건 완료 뒤 M5 시장구조를 직접 돌파한 같은 방향 body-engulf OB가 완료됨
→ 위치·사건·M5 delivery OB를 A1_B1_CONFLUENCE 한 장면으로 묶음
→ 진입가·장면 무효화 손절가·독립 구조 목표가를 진입 전에 확정
→ 계좌 위험예산으로 수량을 정하고 주문 한 개를 만듦
→ 체결 뒤 1.4R 관리와 수익 상태 거래량 전량익절만 적용
```

유동성 위치와 OB는 서로 경쟁하지 않는다. A1은 **어디에서 볼 것인지**, M15 사건은 **그 위치에서 무엇이 일어났는지**, M5 OB는 **그 반응이 실제 가격 전달과 구조 전환을 만들었는지**를 각각 담당한다. 어느 하나도 단독으로 주문을 만들지 않는다.

## 2. 시간봉별 역할

| 시간봉 | V0.3 역할 |
|---|---|
| `4H` | H1 방향과 정면 충돌하는 큰 구조가 있는지 확인 |
| `1H` | 진행 방향과 주요 유동성 node를 제공 |
| `15m` | H1 node의 sweep·reclaim 또는 break·first-retest 사건을 완료 |
| `5m` | 실제 진입을 만드는 MSS/displacement 소유 OB와 타점을 제공 |

5분봉과 15분봉은 모두 중요하지만 같은 일을 하지 않는다. 15분봉은 사건을 확정하고, 5분봉은 그 사건 뒤 실제 진입 가능한 전달 구조를 확정한다.

### 2.1 H1·H4 방향 맥락

- H1이 `UP`이고 H4가 `DOWN`이 아니면 LONG만 허용한다.
- H1이 `DOWN`이고 H4가 `UP`이 아니면 SHORT만 허용한다.
- H1이 `RANGE`이면 중앙의 임의 OB를 거래하지 않는다. 최신 확정 H1 경계에서 방향에 맞는 M15 사건이 생긴 경우만 후보로 둔다.
- 방향 맥락은 주문을 만들지 않는다. 아래 위치·사건·M5 delivery가 모두 필요하다.

## 3. A1 위치와 M15 유동성 사건

### 3.1 A1은 사건보다 먼저 존재해야 한다

A1은 다음 중 하나다.

- M15 사건의 node를 직접 소유하는 확정 H1 pivot
- 같은 node 가격을 body zone 안에 포함하는 활성 H1 또는 M15 OB

생산 경로에서는 `A1 known_at <= M15 event known_at`을 요구한다. 사건 뒤에 생긴 위치를 A1이라고 다시 붙이지 않는다.

### 3.2 M15 사건 두 종류

- **`SWEEP_RECLAIM`**: 확정 H1 저점 또는 고점을 wick으로 1 tick 이상 넘은 M15 완료봉이 같은 봉에서 node 안쪽으로 복귀 마감한다.
- **`BREAK_RETEST`**: 확정 H1 node를 종가로 1 tick 이상 돌파한 뒤, 처음 node를 다시 시험한 M15 완료봉이 돌파 방향 바깥에서 마감한다.

LONG 사건의 `event_extreme`은 사건 완료봉의 저가, SHORT 사건의 `event_extreme`은 고가다. 사건만 완료됐다고 진입하지 않는다.

## 4. M5 MSS를 소유한 실행 OB

V0.3의 실행 OB는 단순히 사건 뒤에 나타난 같은 방향 OB가 아니다. 그 OB의 마지막 형성봉이 M5 시장구조 돌파를 직접 만들어야 한다.

- LONG은 마지막 형성봉이 양봉이고, 그 봉이 열릴 때 이미 확정돼 있던 최신 M5 pivot high보다 1 tick 이상 위에서 마감해야 한다.
- SHORT은 마지막 형성봉이 음봉이고, 그 봉이 열릴 때 이미 확정돼 있던 최신 M5 pivot low보다 1 tick 이상 아래에서 마감해야 한다.
- 마지막 봉 앞의 OB 형성봉 종가는 아직 해당 swing의 돌파 전 쪽에 있어야 한다.
- M15 사건은 이 마지막 M5 형성봉이 열리기 전에 완료돼 있어야 한다.
- 사건 반응이 유지되는 동안 위 조건을 처음 만족한 M5 OB만 해당 사건의 B1 confirmation이 된다.

따라서 `사건 뒤 첫 같은 방향 OB`가 아니라 **사건 뒤 첫 MSS 적격 M5 OB**가 주문 후보가 된다.

### 4.1 공통 body-engulf OB

- `SIMPLE_2C`: 반대색 선행봉 `P`의 몸통 전체를 다음 봉 `E`의 몸통이 감싼다. zone은 감싸진 `P`의 몸통이다.
- `DOUBLE_3C`: 반대색 몸통 장악이 두 번 연속 이어진다. zone은 가운데 `C2`의 몸통이다.
- 모든 형성봉은 완료된 non-doji이며, 몸통 경계의 동가 접촉은 장악에 포함한다.
- wick은 zone에 넣지 않는다. 형성 wick 범위는 아래 장면 손절 소유권을 정할 때 사용한다.

### 4.2 선택적 `15m+5m` 가격 정밀화

활성 M15 A1 OB와 M5 delivery OB의 body가 1 tick 이상 겹치면 그 교집합을 진입 zone으로 사용한다. 겹치지 않아도 장면은 유효할 수 있으며 이때는 M5 OB body를 사용한다.

같은 사건에서 H1 위치와 M15 위치가 모두 유효하면 `M15 위치 + M5 delivery`를 우선한다. 이는 별도 주문 두 개가 아니라 같은 장면의 더 좁은 가격 선택이다.

## 5. 진입 전 고정하는 세 가격

주문을 만들기 전에 다음을 모두 정한다.

1. planned entry
2. initial stop
3. 단 하나의 initial target

이후 위험예산과 수량을 계산한다. 어느 하나라도 유효하지 않으면 주문하지 않는다.

### 5.1 진입 zone과 가격

- LONG planned entry는 선택 zone 상단이다.
- SHORT planned entry는 선택 zone 하단이다.
- zone 중첩은 가격 정밀화일 뿐 장면 성립의 필수조건이 아니다.

### 5.2 장면 손절 소유권

M5 실행 OB가 항상 손절을 소유하는 것은 아니다.

```text
M5 OB 전체 형성 wick 범위가 M15 event_extreme을 포함
→ execution_ob가 stop 소유
→ M5 OB의 반대쪽 가장 먼 wick 사용

포함하지 않음
→ liquidity_event가 stop 소유
→ M15 event_extreme 사용
```

- LONG initial stop은 선택된 extreme보다 1 tick 아래다.
- SHORT initial stop은 선택된 extreme보다 1 tick 위다.
- 이 손절가는 진입 전에 고정한다.
- 실제 반익절 전에는 손절가를 바꾸지 않는다.
- H1 A1 wick이나 진입 뒤 새 OB/FVG가 손절을 자동으로 바꾸지 않는다.

### 5.3 독립 구조 목표만 사용

실행 M5 OB 자체의 impulse extreme은 독립 목표가 아니다. 같은 가격에 별도로 확정된 pivot·반대 OB·FVG가 있으면 그 독립 객체의 권위로만 목표 후보가 될 수 있다.

활성 목표 후보는 다음과 같다.

- 장면이 소유한 M5 또는 M15 확정 pivot
- 확정 H1·H4 pivot
- 진행 방향 앞의 반대 M15·H1·H4 OB body zone
- 진행 방향 앞의 반대 M15·H1·H4 FVG zone

LONG은 entry 위의 가장 가까운 proximal 가격, SHORT은 entry 아래의 가장 가까운 proximal 가격을 initial target으로 정한다. 가장 가까운 장애물이 비용 포함 양의 예상 순손익을 만들지 못하면 그것을 건너뛰어 더 먼 목표를 고르지 않고 거래하지 않는다.

## 6. V0.3 event-created 진입 arm

V0.3 M5 실행 OB는 M15 사건의 반응 과정에서 새로 생긴 `EVENT_CREATED` OB다. 두 실행 arm은 과거 비교를 위해 코드에서 분리했으며, 이 값은 계좌 취향이 아니라 전략 장면·버전 값이다. 현재 모든 장면에 적용하는 공통 기본 진입 방식은 없다.

### 6.1 과거 비교 arm: `NEXT_BAR_OPEN`

- M5 delivery OB가 완료된 뒤 첫 합법 M5 봉의 시가에서 진입을 시도한다.
- LONG은 `stop < actual open < target`, SHORT은 `target < actual open < stop`일 때만 체결한다.
- actual open이 위 구조를 벗어나면 해당 진입을 거절한다.
- 실제 시가와 initial stop의 거리, 수수료, stop slippage를 사용해 수량을 다시 계산한다. 실제 위험예산을 넘기지 않는다.
- 이는 OB를 놓친 뒤 따라가는 임의 추격이 아니라 장면이 완성되기 전에 정한 다음-open 실행 방식이다.

### 6.2 현재 연구 선두가 사용하는 arm: `LIMIT_FIRST_REVISIT`

- M5 OB 완료 뒤 선택 zone의 첫 재방문만 기다린다.
- LONG은 zone 상단, SHORT은 zone 하단을 limit 기준가격으로 쓴다.
- stop 안쪽의 더 유리한 open gap은 그 open에서 체결한다.
- 숫자형 TTL은 두지 않는다. initial target이 먼저 소진되거나 M15 사건이 무효화되면 pending을 취소한다.
- 미체결 뒤 별도 시장가 추격을 만들지 않는다.

현재 연구 선두의 V0.3 BREAK_RETEST와 V0.5는 첫 재방문 지정가를 쓴다. V0.7에서는 경계 첫 회귀 지정가와 경계 수용 뒤 실제 다음 M5 시가를 다시 비교했지만 어느 쪽도 양의 기대값을 만들지 못했다. 따라서 다음 시가를 전 전략의 기본값으로 승격하지 않는다. 새 장면은 확인이 끝나는 시점과 가격 전달 논리에 맞춰 진입 방식을 따로 확정한다.

## 7. 위험과 포지션 수

현재 사용자 기본값은 다음과 같다.

```text
risk_per_trade_fraction = 0.03
daily_loss_limit_enabled = false
daily_loss_limit_fraction = 0.01  # 기능을 켤 때의 기본값
daily_reset_timezone = Asia/Seoul
```

- 거래당 위험예산은 현재 전략 equity의 3%다.
- 완료 거래의 비용 포함 순손익을 다음 거래 equity에 반영하므로 수량은 복리식으로 변한다.
- 일일 손실 제한은 현재 OFF다. OFF 상태에서는 하루 손익이나 거래 횟수로 신규 거래를 멈추지 않는다.
- 일일 제한을 켜면 KST 날짜 시작 equity 대비 실현 순손익으로 신규 주문만 제한한다. 기존 position을 강제청산하거나 initial stop을 바꾸지 않는다.
- 전체 BTC·ETH를 합쳐 pending intent 또는 open position은 최대 한 개다.
- 추가매수, 물타기, martingale, 진입 뒤 수량 증가는 없다.
- 하루 거래 횟수의 고정 상한이나 의무 거래 횟수는 없다.

## 8. 체결 뒤 관리

실제 진입가가 정해지면 다음처럼 계산한다.

```text
R = abs(actual_entry - initial_stop)
target_R = 진행 방향의 abs(initial_target - actual_entry) / R
```

### 8.1 `1.4R` 분기

- `target_R >= 1.4`: 정확히 1R에서 최초 수량 50%를 한 번 익절한다. 실제 반익절이 체결된 뒤에만 잔량 stop을 실제 진입가로 옮기고, 나머지 50%는 최초 target까지 보유한다.
- `target_R < 1.4`: 반익절 없이 최초 target에서 현재 수량 100%를 익절한다.
- 정확히 1.4R은 반익절 경로에 포함한다.
- 다른 반익절 규칙이나 진입 뒤 목표 연장은 없다.

### 8.2 수익 상태 거래량 전량익절

M5와 M15 완료봉을 각각 독립적으로 본다.

```text
RVOL = 현재 봉 volume / 직전 20개 완료봉 volume 중앙값
VOLUME_EXPANDED = RVOL >= 2.0
```

다음 조건을 모두 만족하면 다음 실행 가능 open에서 현재 잔량 전부를 익절한다.

- `RVOL >= 2.0`
- LONG은 신호봉 종가가 실제 진입가 이상, SHORT은 이하
- 수수료와 단순 slippage까지 뺀 예상 순손익이 양수

다음 open에서도 가격 방향과 비용 포함 양의 순손익을 다시 계산한다. 조건이 사라지면 거래량 청산만 취소하고 원래 stop·target을 유지한다. 목표 zone 접촉, 거부봉, 반대 OB는 이 전량익절의 추가 조건이 아니다.

## 9. 변하지 않는 실행 경계

- 모든 전략 판단은 해당 시간봉 완료 뒤 활성화한다.
- 한 장면은 주문 한 개만 만든다.
- 전체 시스템 슬롯은 한 개다.
- 진입가·initial stop·initial target은 주문 전에 정한다.
- 진입 뒤 수량을 늘리지 않는다.
- 반익절 전 initial stop을 바꾸지 않는다.
- 실행 OB impulse extreme은 단독 목표가 아니다.
- 데이터 종료만으로 open position을 강제청산하지 않는다.
- 백테스트는 실제 제공되는 OHLCV와 단순 전량 체결·미체결을 사용한다. 호가 대기열과 order-book 체결 모델은 사용하지 않는다.

## 10. 현재 위치

V0.3은 위 규칙을 실행 가능한 코드로 옮긴 연구 기준선이다. 현재 연구 선두는 V0.3 BREAK_RETEST와 V0.5의 한 슬롯 조합이지만 거래 수가 부족하다. V0.6은 5건을 더했으나 선두의 양수 결과를 음수로 만들었고, V0.7은 다음 시가 arm 기준 31건을 더 만들었으나 단독 기대값과 로그성장이 음수였다. 빈도만 늘어난 장면을 합치지 않으며 아직 paper/live 후보는 없다.

V0.7의 가장 뚜렷한 손익 단서는 target 종류였다. 다음 시가 arm에서 pivot 목표는 16건 `+0.934R`, 반대 OB는 7건 `+0.121R`, 반대 FVG는 8건 `-2.144R`이었다. 이것만으로 모든 FVG 목표를 금지하지는 않지만, 다음 장면에서는 FVG·OB가 단순히 가까운 반대 구역인지, 실제 독립 유동성 목적지를 소유하는지를 구분해야 한다. 또한 V0.7 체결이 모두 M15 경계에서 나왔으므로 H1·H4 큰 틀이 실제 선택을 가르는 구조도 함께 구체화한다.

최신 경제 상태와 다음 작업은 [메인 에이전트 상태 기준선](../../MAIN_AGENT_STATE.md), V0.7의 수식·원장·손익 근거는 [V0.7 구현 및 비교 보고서](EASYCHART_SR_FLIP_FVG_V0_7_COMPARISON_KO.md)에서 관리한다.

