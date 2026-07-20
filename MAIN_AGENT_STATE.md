# 메인 에이전트 상태 기준선

업데이트: `2026-07-20 KST`  
현재 단계: Phase 3 — V0.7 SR Flip + FVG 장면 구현·두 진입 arm 전역 비교 완료  
현재 전략: 활성 주전 미확정 / 연구 선두는 `V0.3 BREAK_RETEST + V0.5` 한 슬롯 조합  
권한 상태: RESEARCH_ONLY / NO_PAPER_ORDER / NO_LIVE_ORDER

## 최신 V0.7 판단

- `확정 H1/M15 경계 → M15 FVG B봉의 방향성 돌파 → C봉 수용 → 경계·FVG 연결` 장면을 별도 `SR_FLIP_FVG` 가족으로 구현했다.
- 같은 장면·손절·목표에서 경계 첫 회귀 지정가와 실제 다음 M5 시가를 분리해 비교했다.
- 84종목일·70포트폴리오일 전역 공유 잔고 결과에서 지정가 단독은 19건 `-1.598R`·`-5.05%`, 다음 시가 단독은 31건 `-1.090R`·`-3.55%`였다.
- 기존 선두와 다음 시가 V0.7을 합치면 44건 `+0.959R`·`+2.37%`였지만, 선두 단독 13건 `+2.049R`·`+6.14%`보다 일당 로그성장과 최대낙폭이 나빠졌다.
- 완료된 선두 13건은 결합군에서도 모두 동일했다. 성과 저하는 슬롯 충돌이 아니라 V0.7의 음의 로그성장이 추가된 결과다.
- 다음 시가는 80.65% 승률이었지만 평균 승리 `+0.196R`, 평균 손실 약 `-1R`이었다. 목표 R 중앙값은 `0.293R`이고 1.4R 반익절 대상은 0건이었다.
- 목표별로 다음 시가의 pivot 16건은 `+0.934R`, 반대 OB 7건은 `+0.121R`, 반대 FVG 8건은 `-2.144R`이었다. 현재의 `모든 활성 반대 FVG를 동급 terminal target으로 인정`한 규칙이 우선 수정 대상이다.
- 체결된 V0.7 거래는 모두 M15 경계였고 H1 경계 거래는 0건이었다. 큰 시간봉 문맥이 실제 선택을 가르지 못한 점도 다음 설계에서 다룬다.
- 따라서 V0.7 두 arm을 활성 조합에 넣지 않는다. 연구 선두는 계속 `V0.3 BREAK_RETEST + V0.5`다.
- 자세한 수식·funnel·전역 원장·손익 원인은 [V0.7 구현 및 비교 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_SR_FLIP_FVG_V0_7_COMPARISON_KO.md)를 따른다.

## 최신 V0.6 판단

- `M15 OB 마지막 방향봉의 직접 구조 돌파 → H1/M5 OB 중첩 → 이탈 뒤 첫 회귀` B+ 장면을 V0.6으로 구현했다.
- 두 손절 arm은 같은 83개 장면·진입·목표를 공유했다. 하나는 M15 anchor 형성극단, 다른 하나는 최신 반대 M15 보호 swing을 사용했다.
- 84종목일에서 B+ 단독은 두 arm 모두 5건만 체결됐고, 형성극단은 `-2.438R`, 보호 swing은 `-2.528R`이었다.
- 기존 연구 선두와 결합하면 거래가 13건에서 18건으로 늘지만, `+2.049R`이 각각 `-0.389R`, `-0.479R`로 악화됐다.
- 83개 장면 중 70개는 첫 회귀 전에 고정 목표가 먼저 사용되어 주문이 취소됐다. 실제 회귀가 먼저 온 다섯 장면은 2승 3패였다.
- 보호 swing은 형성극단보다 대체로 훨씬 넓어 목표 R과 1.4R 관리 가능 횟수를 줄였지만 패배 장면을 회복하지 못했다. 핵심 문제는 손절보다 B+ 진입 장면의 음의 기대값이다.
- 따라서 B+ 두 arm 모두 선두 조합에 넣지 않으며 paper/live 후보로 승격하지 않는다. 연구 선두는 계속 `V0.3 BREAK_RETEST + V0.5`다.
- 자세한 수식·funnel·다섯 거래·손절 비교는 [V0.6 B+ 구현 및 손절 비교 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_6_OWNED_M15_OVERLAP_STOP_COMPARISON_KO.md)를 따른다.

## 보존하는 V0.5 판단

- `M15 OB 위치 → 그 안의 M15 pivot M5 sweep·reclaim → 직접 displacement를 만든 M5 OB 또는 FVG → 첫 재방문` 장면을 V0.5로 구현했다.
- 같은 여섯 개 14일 구간, 총 84종목일에서 V0.5 단독은 7건, 6승, `+1.274R`, PF 2.283이었다.
- V0.3 전체와 결합하면 17건, `+0.102R`에 그쳤다. 기존 `SWEEP_RECLAIM` 네 건의 `-1.947R`가 새 장면의 이익을 거의 상쇄했다.
- `V0.3 BREAK_RETEST + V0.5` 한 슬롯 조합은 13건, 11승, `+2.049R`, PF 2.007, 최대낙폭 약 3.0%였다.
- 현재 가장 나은 연구 조합은 위 13건 arm이다. 다만 종목일당 약 0.155건으로 거래 빈도 목표에는 크게 못 미치므로 활성 전략이나 paper/live 후보로 승격하지 않는다.
- 자세한 수식·funnel·일곱 거래·결합 결과는 [V0.5 구현 및 비교 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_5_LIQUIDITY_DELIVERY_COMPARISON_KO.md)를 따른다.

## 1. 최종 목표

EasyChart의 실제 투자 논리를 중심으로 BTC·ETH에서 운용할 수 있는 프로그램을 만든다. 목표는 단순히 OB 발생 횟수를 세는 것이 아니라, **유동성 위치에서 시작해 다음 유동성·구조 목적지로 전달되는 과정 안에서 OB의 역할을 프로그램화**하는 것이다.

최종 계좌 목표는 다음 세 가지를 함께 요구한다.

- 거래 기회를 억지로 만들지 않으면서도 의미 있는 빈도를 유지한다.
- 수수료와 단순 실행비용 뒤 양의 기대값을 추구한다.
- 거래별 위험, 손절, 포지션 수를 통제해 장기적인 우상향 가능성을 높인다.

현재 V0.3~V0.7은 이 목표를 위한 연구 버전이다. V0.5와 V0.3 BREAK_RETEST 조합이 첫 양수 연구 결과를 냈지만 거래 수가 부족하다. V0.6과 V0.7은 빈도를 보완하려 했으나 새 장면 자체의 기대값이 음수여서 활성 조합에서 제외했다. 아직 paper/live 전환 자격은 없다.

## 2. 현행 권위 순서

내용이 충돌하면 다음 순서로 판단한다.

1. 사용자의 최신 결정
2. 현재 코드와 실제 실행 결과
3. 이 상태 기준선
4. 아래 정책·수식 문서
5. 과거 V0.1·V0.2 보고서와 이전 계획

현행 문서:

- [EasyChart OB V0.3 정책 기준선](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_POLICY_DECISION_DRAFT.md)
- [EasyChart OB V0.3 OHLCV 수식 계약](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_OHLCV_FORMULA_CONTRACT.md)
- [EasyChart OB V0.3 진입 방식 고정 비교 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_3_ENTRY_ARM_COMPARISON_KO.md)
- [EasyChart OB V0.5 구현 및 비교 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_5_LIQUIDITY_DELIVERY_COMPARISON_KO.md)
- [EasyChart OB V0.6 B+ 구현 및 손절 비교 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_6_OWNED_M15_OVERLAP_STOP_COMPARISON_KO.md)
- [EasyChart SR Flip+FVG V0.7 구현 및 전역 비교 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_SR_FLIP_FVG_V0_7_COMPARISON_KO.md)
- [EasyChart OB 전략 재구성 V2](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_STRATEGY_RECONSTRUCTION_V2.md)

정책 문서는 투자 흐름, 수식 계약은 정확한 OHLCV 판정, 이 문서는 현재 구현·결과·다음 작업을 담당한다.

## 3. V0.3 기준선, V0.5 연구 선두와 V0.6·V0.7 제외 장면

```text
H1·H4 context
→ 사건 전에 존재한 A1 위치와 확정 H1 liquidity node
→ H1 node에서 완료되는 M15 liquidity event
→ 사건 뒤 M5 MSS를 직접 만든 event-created body-engulf OB
→ A1_B1_CONFLUENCE 한 장면
→ entry·scene stop·independent target 사전 확정
→ risk-based quantity
→ 한 주문·한 포지션
→ 1.4R 관리 또는 수익 상태 volume full exit
```

### 3.1 역할 분리

- `4H`: H1 방향과 정면 충돌하는 큰 구조 확인
- `1H`: 방향과 주요 liquidity node 제공
- `15m`: sweep·reclaim 또는 break·first-retest 사건 완료
- `5m`: MSS/displacement를 직접 만든 실행 OB와 타점 제공

A1 위치, M15 사건, M5 OB는 경쟁하는 신호가 아니다. 세 요소가 각자의 역할을 충족할 때 한 장면과 주문 한 개가 생긴다.

### 3.2 A1과 M15 사건

- A1은 event node를 소유하는 H1 pivot 또는 같은 node를 포함하는 활성 H1/M15 OB다.
- 생산 경로에서 A1은 M15 사건 완료 시점보다 늦게 생길 수 없다.
- M15 사건은 `SWEEP_RECLAIM` 또는 `BREAK_RETEST` 중 하나다.
- LONG event extreme은 사건 완료봉 저가, SHORT은 고가다.

### 3.3 M5 delivery OB

- 단순히 사건 뒤 처음 나타난 같은 방향 OB를 쓰지 않는다.
- M5 OB의 마지막 형성봉이 그 봉의 시작 전에 확정된 최신 M5 swing을 1 tick 이상 종가 돌파해야 한다.
- 그 돌파를 직접 만든 첫 MSS 적격 M5 OB가 event-created confirmation이 된다.
- 활성 M15 A1과 M5 delivery body가 겹치면 `15m+5m` 교집합으로 진입가격을 좁힌다. 중첩이 없으면 M5 body를 쓰며 장면 자체를 버리지는 않는다.

### 3.4 V0.5 M15 위치·M5 유동성 전달

```text
H1 방향 또는 H1 범위 경계
→ 활성 M15 EasyChart OB와 그 안의 확정 M15 pivot
→ 위치·pivot 조합이 존재한 뒤 첫 M5 sweep·reclaim
→ 같은 반응에서 M5 swing을 직접 돌파한 body-engulf OB 또는 strict FVG
→ M15+M5 교집합이 있으면 교집합, 아니면 M5 실행 zone
→ 첫 재방문 지정가 한 개
```

- OB는 마지막 형성봉이 구조 돌파를 직접 소유한다.
- FVG는 중앙 displacement 봉이 구조 돌파를 직접 소유한다.
- 아무 FVG나 허용하지 않으며 같은 displacement의 OB와 FVG를 주문 두 개로 만들지 않는다.
- stop은 실행 formation이 event extreme을 포함하면 formation far wick, 아니면 event extreme이 소유한다.
- event 반대편의 최신 미소비 M15 pivot을 최초 목표로 고정하고, 없으면 H1·H4 순서로 확장한다.

### 3.5 V0.6 B+ M15-owned 중첩 장면

```text
M15 EasyChart OB의 마지막 방향봉이 최신 M15 pivot을 직접 종가 돌파
→ 같은 방향 H1 또는 M5 EasyChart OB와 1 tick 이상 몸통 중첩
→ 중첩 구역 확정 뒤 M5 완료 종가가 진행 방향으로 이탈
→ 그 이후 첫 회귀 지정가 한 개
```

- `M15+M5`는 추가매수가 아니라 flat 상태의 최초 진입 한 개로 구현했다.
- same-displacement simple/double OB는 root 하나로 묶었다.
- 목표는 anchor 완성 때 존재한 가장 가까운 독립 M15/H1/H4 pivot·반대 OB·FVG다.
- 형성극단 손절과 보호 M15 swing 손절을 같은 장면 모집단에서 비교했다.
- 이 장면은 거래 수를 늘렸지만 기대값을 훼손했으므로 구현된 연구 arm으로만 남고 활성 조합에서는 제외한다.

### 3.6 V0.7 SR Flip+FVG 장면

```text
사건 전에 확정된 H1 또는 M15 지지·저항 경계
→ M15 FVG 중앙 B봉이 경계를 몸통으로 돌파
→ C봉도 경계 돌파 상태를 유지해 수용 확인
→ 경계선이 해당 FVG 안에 있을 때 한 장면으로 연결
→ 경계 첫 회귀 지정가 또는 실제 다음 M5 시가를 별도 arm으로 비교
```

- H1·M15 후보가 동시에 있으면 더 좁은 M15 경계를 우선하고, 같은 시간봉에서는 최신 경계를 쓴다.
- zone은 경계선 하나이며 stop은 A·B·C 세 봉의 진행 반대편 극단 바깥 1 tick이다.
- C봉 종료 시점의 가장 가까운 활성 반대 M15/H1/H4 pivot·OB·FVG를 최초 목표로 고정한다.
- 두 진입 arm 모두 음의 기대값이었다. 특히 반대 FVG를 독립적인 유동성 목적지인지 구별하지 않고 terminal target으로 인정한 부분이 가장 큰 개선 후보다.
- 연구 코드는 보존하지만 현재 선두 조합에는 넣지 않는다.

## 4. 진입·손절·목표

### 4.1 event-created entry arm

모든 장면에 공통인 하나의 기본 진입 방식은 두지 않는다. 진입 방식은 계좌 설정이 아니라 각 장면의 형성과 가격 전달 논리에 속한다.

- 연구 선두인 V0.3 `BREAK_RETEST`와 V0.5는 첫 재방문 지정가를 유지한다.
  - 실행 zone의 첫 재방문에서만 체결을 시도한다.
  - initial target 선소진 또는 해당 장면 무효화 때 pending을 취소한다.
  - 미체결 뒤 별도 시장가 추격은 하지 않는다.
- V0.7은 같은 장면에서 `FIRST_RETURN_LIMIT`와 `BOUNDARY_ACCEPT_NEXT_OPEN`을 비교했다.
  - 다음 시가 arm은 C봉 뒤 실제 첫 M5 시가가 initial stop과 target 사이일 때만 체결한다.
  - 실제 진입가 기준으로 수량을 다시 계산해 cash risk budget을 넘기지 않는다.
  - 두 arm 모두 음의 기대값이므로 어느 쪽도 활성 기본값으로 채택하지 않는다.

새 장면을 추가할 때도 지정가와 다음 시가 중 하나를 관성적으로 재사용하지 않고, 해당 장면에서 진입 확인이 언제 끝나는지를 먼저 정한다.

### 4.2 scene initial stop

```text
M5 OB 전체 형성 wick 범위가 M15 event extreme을 포함
→ M5 OB 반대쪽 far wick이 stop 소유

포함하지 않음
→ M15 event extreme이 stop 소유

LONG stop  = 선택 extreme - 1 tick
SHORT stop = 선택 extreme + 1 tick
```

initial stop은 주문 전에 고정한다. 실제 1R 반익절 전에는 바꾸지 않는다. 진입 뒤 새 OB·FVG나 A1 wick이 stop을 새로 만들지 않는다.

### 4.3 independent initial target

실행 M5 OB 자체 impulse extreme은 독립 목표가 아니다. 후보는 다음 독립 구조뿐이다.

- 장면 소유 M5/M15 확정 pivot
- 확정 H1/H4 pivot
- 반대 M15/H1/H4 OB body zone
- 반대 M15/H1/H4 FVG zone

진행 방향에서 가장 가까운 유효 후보 하나를 진입 전에 고정한다. 가장 가까운 장애물이 비용 포함 양의 예상 순손익을 만들지 못하면 더 먼 목표로 건너뛰지 않는다.

위 문단은 V0.3 기준선의 목표 선택이다. V0.5는 M5 sweep event가 기대하는 반대편 range boundary를 별도 역할로 사용한다. M15, H1, H4 순서로 가장 최근 확정된 반대 pivot을 고정하며, 반대 pivot이 전혀 없을 때만 event 전에 존재한 H1/H4 반대 OB·FVG를 대체 목적지로 쓴다. 가까운 장애물이 V0.5 외부 목적지를 자동으로 교체하지 않는다.

## 5. 위험과 포지션 설정

현재 사용자 기본값:

```text
risk_per_trade_fraction = 0.03
daily_loss_limit_enabled = false
daily_loss_limit_fraction = 0.01
daily_reset_timezone = Asia/Seoul
```

- 한 거래의 cash risk budget은 현재 strategy equity의 3%다.
- 완료 거래의 비용 포함 순손익을 다음 수량 계산에 반영한다.
- 일일 손실 제한은 현재 OFF다. 기능을 켜면 KST 날짜 시작 equity 대비 실현 순손익으로 신규 주문만 제한한다.
- 전체 BTC·ETH를 합쳐 pending intent 또는 open position은 최대 한 개다.
- 추가매수, 물타기, martingale, 진입 뒤 수량 증가는 없다.
- 하루 거래 수의 고정 상한이나 의무 거래 수는 없다.

`SingleSlotRouter`는 한 실행 안에서 pending/open 슬롯 하나를 강제한다. 현재 CLI historical replay는 입력 종목 하나를 처리하므로, BTC·ETH 통합 실행에서는 두 종목이 같은 전역 router를 사용하도록 연결해야 한다.

## 6. 체결 뒤 관리

### 6.1 `1.4R / 1R / 50%`

실제 체결가로 R을 다시 계산한다.

- `target_R >= 1.4`: 정확히 1R에서 최초 수량 50%를 한 번 익절한다. 실제 반익절 체결 뒤 잔량 stop을 실제 진입가로 옮기고 나머지는 최초 target까지 보유한다.
- `target_R < 1.4`: 반익절 없이 최초 target에서 100% 익절한다.
- 다른 반익절 조건, 진입 뒤 목표 연장, 새 구조에 따른 갑작스러운 stop은 없다.

### 6.2 수익 상태 거래량 전량익절

M5와 M15 완료봉을 각각 계산한다.

```text
RVOL = current volume / median(previous 20 completed-bar volumes)
```

`RVOL >= 2.0`, 실제 진입가에 유리한 종가, 비용 포함 양의 예상 순손익을 모두 만족하면 다음 실행 가능 open에서 현재 잔량 전부를 익절한다. 다음 open에서도 같은 경제 조건을 만족하지 못하면 거래량 청산만 취소하고 원래 stop·target을 유지한다.

## 7. 현재 구현 범위

구현됨:

- 5m CSV에서 15m·1H·4H 완료봉 생성
- strict pivot, body-engulf OB, FVG 탐지
- H1/H4 context와 M15 liquidity event
- M5 MSS-owning OB confirmation
- A1 pre-event admission과 선택적 `15m+5m` refinement
- scene stop ownership
- independent target 선택
- 두 event-created entry arm
- next-open actual-price 위험 수량 재산정
- 1.4R 관리와 거래량 전량익절
- OHLCV historical replay와 한-slot router
- 거래당 3%, 일일 제한 OFF 사용자 기본값
- V0.5 location-aware M5 sweep, displacement-owning OB/FVG, 외부 pivot 목표
- V0.3 BREAK_RETEST와 V0.5를 한 슬롯에서 함께 재생하는 비교 arm
- V0.6 M15-owned H1/M5 OB 중첩 첫 회귀 장면과 두 손절 비교 arm
- V0.7 확정 경계 돌파·수용과 M15 FVG를 결합한 `SR_FLIP_FVG` 장면
- V0.7 경계 첫 회귀 지정가와 실제 다음 M5 시가 비교 arm
- BTC·ETH 여섯 구간을 시간순으로 합친 단일 잔고·단일 포지션 historical 비교 runner

아직 구현되지 않은 범위:

- BTC·ETH를 한 시계로 합치는 다종목 실시간 orchestrator
- 거래소 private 주문·체결 adapter
- 계좌·주문 reconciliation, persistence, process recovery
- paper/live 공용 실시간 애플리케이션과 UI
- 실제 계좌 주문 권한

따라서 현재 코드는 OHLCV 전략 연구와 historical replay 코어다. V0.7 비교에서는 BTC·ETH가 전역 잔고와 포지션 슬롯 하나를 공유하지만, 실시간 데이터·주문·복구를 담당하는 orchestrator는 아직 없다. paper나 live 거래 프로그램이 완성됐다고 보지 않는다.

## 8. backtest·paper·live 구분

- **backtest**: 과거 OHLCV를 순서대로 재생하고 단순 전량 체결·미체결, 수수료, 단순 slippage, gap, pending 취소를 계산한다. 호가 대기열·order-book 부분체결·latency·market impact는 다루지 않는다.
- **paper**: 실시간 데이터와 실시간 주문 상태를 사용하는 가상자금 실행이다. backtest의 연장이나 정밀판이 아니다.
- **live**: paper와 같은 전략·위험·주문·저장 런타임을 사용하되 실제 계좌와 승인된 private execution adapter를 쓴다.

향후 paper와 live는 포지션과 pending 주문이 없는 상태에서 account·credential·mode만 바꿀 수 있을 만큼 가깝게 구현한다. 전략 코드를 두 벌로 만들지 않는다.

## 9. 결과 상태

### 9.0 최신 V0.7 전역 공유 잔고 비교

비교 조건은 초기자산 `10,000 USDT`, 거래당 현재 잔고 `3%` 위험, 일일 손실 제한 OFF, BTC·ETH 전체에서 포지션 최대 한 개다. 여섯 개 14일 구간은 총 84종목일이며, 중복 날짜를 합치면 70포트폴리오일이다.

| arm | 거래 | 승률 | 합계 R | PF | 최종자산 | 수익률 | 최대낙폭 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 연구 선두 `V0.3 BREAK_RETEST + V0.5` | 13 | 84.62% | +2.049R | 1.976 | 10,614.29 | +6.14% | 3.00% |
| V0.7 첫 회귀 지정가 단독 | 19 | 68.42% | -1.598R | 0.717 | 9,494.56 | -5.05% | 8.73% |
| V0.7 실제 다음 M5 시가 단독 | 31 | 80.65% | -1.090R | 0.805 | 9,644.72 | -3.55% | 5.91% |
| 연구 선두 + V0.7 지정가 | 32 | 75.00% | +0.451R | 1.032 | 10,077.78 | +0.78% | 11.00% |
| 연구 선두 + V0.7 다음 시가 | 44 | 81.82% | +0.959R | 1.094 | 10,236.93 | +2.37% | 7.09% |

V0.7 다음 시가를 합치면 거래는 13건에서 44건으로 늘지만, 선두 단독보다 최종수익·포트폴리오일당 로그성장·최대낙폭이 모두 나빠진다. 완료된 선두 13건은 결합군에서도 동일하므로 슬롯 경쟁이 원인이 아니다. V0.7 다음 시가는 25번 이겼어도 평균 승리가 `+0.196R`, 여섯 손실이 각각 약 `-1R`이어서 합계가 음수였다. 따라서 빈도 증가는 확인했지만 활성 전략에는 채택하지 않는다.

세부 수식, 모집단 funnel, 모든 거래 원장과 목표 종류별 손익은 [V0.7 구현 및 비교 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_SR_FLIP_FVG_V0_7_COMPARISON_KO.md)를 따른다.

### 9.1 보존하는 V0.6 고정 비교

최신 비교는 각 창 10,000 USDT, 거래당 현재 잔고 3%, 일일 제한 OFF, 창 내부 한 슬롯 조건이다.

| arm | 거래 | 승률 | 합계 R | PF | 순손익 |
|---|---:|---:|---:|---:|---:|
| V0.3 전체 first-revisit | 10 | 70.00% | -1.173R | 0.604 | -355.99 |
| V0.4 PREEXISTING | 13 | 46.15% | -2.591R | 0.577 | -761.80 |
| V0.5 단독 | 7 | 85.71% | +1.274R | 2.283 | +384.80 |
| V0.3 전체 + V0.5 | 17 | 76.47% | +0.102R | 1.028 | +33.06 |
| V0.3 BREAK_RETEST + V0.5 | 13 | 84.62% | +2.049R | 2.007 | +613.05 |

동일 조건에서 V0.6 B+를 비교한 결과는 다음과 같다.

| arm | 거래 | 승률 | 합계 R | PF | 순손익 |
|---|---:|---:|---:|---:|---:|
| 기존 선두 `V0.3 BREAK_RETEST + V0.5` | 13 | 84.62% | +2.049R | 2.007 | +613.05 |
| B+ M15 형성극단 손절 단독 | 5 | 40.00% | -2.438R | 0.180 | -730.65 |
| B+ 보호 M15 swing 손절 단독 | 5 | 40.00% | -2.528R | 0.155 | -753.29 |
| 기존 선두 + B+ 형성극단 | 18 | 72.22% | -0.389R | 0.915 | -128.11 |
| 기존 선두 + B+ 보호 swing | 18 | 72.22% | -0.479R | 0.900 | -150.18 |

V0.6은 빈도를 5건 늘렸지만 기존 선두의 양의 결과를 음수로 만들었다. 두 손절 가운데 하나를 고르는 것으로 해결되지 않으므로 B+ 장면 전체를 현재 조합에서 제외한다.

V0.5의 실제 완료 거래는 FVG 6건과 OB 1건이다. 4건은 확정된 거래량 급증 규칙으로 청산됐고, 2건은 최초 목표 전량익절, 1건은 최초 손절이었다. 일곱 거래 모두 event-extreme stop을 사용했다.

아래 9.2~9.4는 이전 V0.3 당시 거래당 위험 `1%`로 계산한 기록이다. 최신 3% 비교와 섞어 읽지 않으며, 현재 판단은 위 9.0과 V0.5 보고서를 우선한다.

### 9.2 여섯 구간 독립 합계

각 구간은 `10,000 USDT`에서 독립적으로 다시 시작한다.

| arm | 완료 거래 | 승리 | 승률 | 순손익 | 기대값 | PF | 양수 환경 | 취소 | 진입 거절 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| V0.2 교정 기준선 | 59 | 35 | 59.32% | -1,364.55 USDT | -0.2340R | 0.3995 | 1/6 | 97 | 0 |
| V0.3 `NEXT_BAR_OPEN` | 12 | 8 | 66.67% | -243.09 USDT | -0.2020R | 0.3920 | 2/6 | 0 | 12 |
| V0.3 `LIMIT_FIRST_REVISIT` | 10 | 7 | 70.00% | -117.73 USDT | -0.1173R | 0.6075 | 3/6 | 14 | 0 |

### 9.3 BTC·ETH 전역 단일계좌·단일슬롯

V0.3 두 arm은 구간을 시간순으로 연결하고 BTC·ETH 전체에서 pending 또는 open 슬롯 하나만 공유한 결과도 계산했다.

| arm | 초기자산 | 최종자산 | 순손익 | 최대낙폭 |
|---|---:|---:|---:|---:|
| `NEXT_BAR_OPEN` | 10,000.00 | 9,757.92 | -242.08 | 262.04 |
| `LIMIT_FIRST_REVISIT` | 10,000.00 | 9,881.70 | -118.30 | 199.01 |

V0.2 교정 자료는 각 창을 독립 실행했고 선택되지 않은 기회 흐름을 보존하지 않았으므로, 전역 단일계좌 결과로 다시 이름 붙이지 않는다. 세부 조건·환경별 결과·전역 체결 원장은 [V0.3 진입 방식 고정 비교 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_3_ENTRY_ARM_COMPARISON_KO.md)에 정리한다.

### 9.4 경제 판단

- 두 V0.3 arm 모두 순손익과 기대값이 음수이며, 완료 거래도 10~12건으로 부족하다.
- `LIMIT_FIRST_REVISIT`가 손실, 기대값, PF, 양수 환경 수와 최대낙폭에서 상대적으로 우세하지만 운영 승격이나 최종 기본값 확정 근거는 아니다.
- 전역 원장에서 작은 목표 이익 여러 건이 손절 몇 건을 상쇄하지 못했다. 높은 승률만으로 손익 비대칭이 해결되지 않았다.
- 현재 `EVENT_CREATED` 장면만 선택하는 구조를 완성 전략이나 paper/live 후보로 보지 않는다.

과거의 `83거래 / 33승 / -2,782.28 USDT`는 target 수명·pending 취소·시간 순서 교정 전 결과다. 현행 기준선이나 현재 V0.3 성과로 사용하지 않는다.

현재 경제 판단:

- EasyChart OB 중심 전략 전체: 계속 개발
- V0.5 단독과 `V0.3 BREAK_RETEST + V0.5`: 양수 소표본 연구 후보
- V0.3 `SWEEP_RECLAIM`: 현재 연구 조합에서 제외
- V0.6 B+ M15-owned OB 중첩 첫 회귀: 현재 연구 조합에서 제외
- V0.7 SR Flip+FVG 두 진입 arm: 빈도는 늘었지만 음의 기대값이므로 현재 연구 조합에서 제외
- paper/live 후보: 아님
- 실제 주문 권한: 없음

## 10. 자료와 판단 기준

- EasyChart 자막·화면·사례가 전략 의미의 1차 기준이다.
- 현재 등록된 18개 자막은 기본 자료 묶음이며, 필요한 정의가 닫히지 않으면 다른 EasyChart 자료를 추가로 본다.
- SMC·ICT 원자료는 용어와 빠진 전제를 이해하는 보조 자료다. EasyChart 규칙을 자동으로 대체하지 않는다.
- 공개 구현과 기술 문서는 주문 상태·거래소 제약·애플리케이션 구조를 참고하는 자료다. 수익 규칙의 권위가 아니다.
- 중요한 모호성이 진입·손절·목표·위험·아키텍처를 바꾸면 사용자와 바로 의논해 결정한다.

## 11. 작업 우선순위

시간은 최종 목표에 직접 기여하는 일에 먼저 쓴다.

1. V0.7에서 손익을 가장 크게 훼손한 목표 소유권을 구체화한다. 특히 FVG·OB를 단순히 가까운 반대 구역이라는 이유만으로 terminal target으로 쓰지 않고, 독립적인 유동성 목적지와 연결되는 조건을 정한다.
2. H1·H4 큰 틀과 M15 위치·사건, M5 타점을 실제로 역할 분담시키는 다음 장면을 정한다. V0.7처럼 M15만 사실상 선택되는 구조는 그대로 반복하지 않는다.
3. 수익 논리와 빈도 보완 근거가 함께 있는 장면 하나를 이름 붙여 구현한다.
4. BTC·ETH 통합 한-slot runtime
5. paper와 live가 공유하는 실시간 애플리케이션

완료된 결정을 반복 조사하지 않는다. 반대로 전략 결과를 실제로 바꿀 중요한 구현과 자료 확인은 필요한 만큼 수행한다. 서브 에이전트나 고성능 모델이 정상적으로 맡은 작업을 진행 중이면 충분히 기다리고, 범위를 벗어난 반복 작업으로 흐를 때만 방향을 바로잡는다.

## 12. 바로 다음 작업

1. 연구 선두를 `V0.3 BREAK_RETEST + V0.5`로 두고 V0.3 SWEEP_RECLAIM은 현재 조합에서 제외한다.
2. V0.6 B+ 두 손절 arm은 음의 기대값이므로 현재 조합에서 제외한다.
3. V0.7 SR Flip+FVG 두 진입 arm도 음의 기대값이므로 현재 조합에서 제외한다.
4. V0.7 목표 결과를 출발점으로 pivot·OB·FVG의 terminal target 자격, 특히 FVG의 독립 유동성 소유 조건을 의논해 확정한다.
5. 그 목표 규칙과 H1/H4 문맥이 실제 선택을 가르는 다음 장면 하나를 별도 가족으로 구현한다.
6. 양의 장면 조합의 거래 빈도가 의미 있게 늘면 BTC·ETH 통합 한-slot runtime으로 연결한다.
7. 그 뒤 paper/live 공용 실시간 애플리케이션으로 진행한다.

