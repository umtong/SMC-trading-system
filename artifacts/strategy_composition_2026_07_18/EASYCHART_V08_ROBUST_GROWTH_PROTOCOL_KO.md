# EasyChart V0.8 Hardened 강건 성장 프로토콜

작성일: `2026-07-20 KST`  
상태: **IMPLEMENTED / CI_VALIDATED / PERFORMANCE_UNPROVEN / RESEARCH_ONLY**  
주문 권한: `NO_PAPER_ORDER / NO_LIVE_ORDER`

## 1. 목표와 판단 기준

이 프로젝트의 목적은 승률, 거래 수, 손익비 또는 백테스트 정밀도 하나를 최대화하는 것이 아니다.

```text
의미 있는 완료 거래 빈도
× 거래당 비용 포함 양의 평균 순R
× 통제된 손실·노출
= 장기적인 계좌 성장 속도와 안정성
```

현재 보존 연구 선두는 `V0.3 BREAK_RETEST + V0.5`다. 과거 비교 표본에서 13건, `+2.049R`, 약 `+6.14%`였지만 빈도가 실사용 목표에 훨씬 못 미친다. V0.7은 다음 시가 arm에서 31건·80.65% 승률을 만들었어도 평균 승리 `+0.196R`, 평균 손실 약 `-1R`, 총 `-1.090R`이었다. 따라서 V0.8 hardened는 거래를 억지로 추가하지 않고 다음 질문을 주문 전에 닫는다.

> 이 진입은 실제로 어떤 유동성 목적지를 향하며, 그 목적지까지 더 가까운 구조를 건너뛰지 않고, 비용과 손절을 차감한 뒤에도 계좌를 성장시킬 공간이 있는가?

모든 변경은 다음 하나로 평가한다.

> 완료 거래 빈도, 비용 포함 평균 순R, 손실·노출 통제를 함께 고려할 때 반복 표본의 장기 복리 성장 경로가 개선되는가?

## 2. 증거 권위와 분류

### 2.1 공식 corpus

의미 기준선은 [`registrations/easychart_caption_corpus_2026_07.json`](../../registrations/easychart_caption_corpus_2026_07.json)에 등록된 `EASYCHART_ACTIVE_18_2026_07`이다.

- 등록된 18개 한국어 원본 자막의 SHA-256을 보존한다.
- known republication edge와 compilation은 파일 수만큼 독립 근거로 세지 않는다.
- 자막 corpus는 전략 의미와 시각 감사 사례를 찾는 권위다.
- 자막 파일 수를 성과 표본 수로 해석하지 않는다.
- 공식 시장 OHLCV는 causal replay의 관측 자료이며 전략 의미의 권위가 아니다.
- 일반 SMC/ICT 자료는 source가 exact 자동화 경계를 주지 않은 부분의 메커니즘 참고에만 사용한다.

### 2.2 규칙 표지

| 표지 | 의미 |
|---|---|
| `SOURCE_DIRECT` | 공식 EasyChart 자막·보존 프레임에서 역할이나 행동을 직접 확인 |
| `LOGICAL_SYNTHESIS` | 서로 호환되는 여러 공식 장면의 역할을 하나의 전략 인과관계로 연결 |
| `USER_INVARIANT` | 사용자가 고정한 위험·포지션·검증 조건 |
| `ENGINEERING_V0` | causal 자동화·안전·재현성을 위해 명시적으로 선택한 경계 |
| `RESEARCH_HYPOTHESIS` | 성과로 검증해야 하며 source 원규칙으로 주장하지 않는 초기 값 |
| `UNRESOLVED` | 주문 결과를 바꿀 수 있으나 아직 하나로 닫히지 않은 부분 |

코드의 exact OHLCV 부등식, 1 tick buffer, 20봉 중앙값, 8배 노출 상한, 12봉 prompt window, 동시 fill tie-break는 EasyChart가 그대로 말한 문장이 아니다. source 역할을 미래정보 없이 실행하기 위한 `ENGINEERING_V0` 또는 `RESEARCH_HYPOTHESIS`다.

## 3. 공식 자료가 직접 지지하는 공통 투자 문법

### 3.1 구조와 시간봉 역할

- `xlYuENlNZN0 01:51:48–01:52:34`: 포지션을 먼저 정하고 구조를 찾지 않는다. 구조를 먼저 보고 아무것도 없거나 양방향 구조가 충돌하면 현금 상태를 유지한다. `SOURCE_DIRECT`
- `V3kCjvJy3bg 00:48–01:47`: 월·주·일봉은 큰 추세, 12H·4H·1H는 중간 구조·지지저항, 15m·5m·1m은 실제 진입에 사용한다. `SOURCE_DIRECT`
- `xlYuENlNZN0 01:17:28–01:18:17`: 낮은 시간봉의 작은 반대 OB보다 큰 구조가 우세한 장면이 있다. 모든 반대 OB가 자동 방향 veto는 아니다. `SOURCE_DIRECT`
- `xlYuENlNZN0 02:12:31–02:13:55`: 관점이 무효화되면 기존 작도와 방향 편향을 버리고 상위 구조부터 다시 분석한다. `SOURCE_DIRECT`

### 3.2 유동성 위치와 실행 근거

- `HReT0PtawRA 02:41–04:24`: 주요 저점, 깨진 지지, 다른 참여자의 손절이 모일 가격을 먼저 표시하고 그 위치의 OB를 별도 근거로 결합한다. `SOURCE_DIRECT`
- `F3exGqdN2Go 04:39–04:58`: OB 구역의 재방문을 거래 위치로 사용하고 유동성 흡수나 다른 구조와 겹칠 때 더 의미 있게 본다. `SOURCE_DIRECT`
- `GGFqHk_JPDI 01:47–02:34`: 중요 고점·저점 또는 FVG 내부의 OB를 우선한다. `SOURCE_DIRECT`
- `HiH15zxEDnk 10:22–10:41, 12:18–12:37`: OB 하나만으로 충분하지 않고 FVG·추세선 등 독립 역할의 근거가 겹치는 장면을 설명한다. `SOURCE_DIRECT`

이 자료는 다음 역할을 지지한다.

```text
상위 구조와 유동성 위치
→ 그 위치에서 별도 반응·displacement 근거
→ OB/FVG 실행 위치
```

정확한 sweep·reclaim·MSS·displacement 부등식은 `LOGICAL_SYNTHESIS + ENGINEERING_V0`다.

### 3.3 구조적 손절과 현금 위험

- `F3exGqdN2Go 05:04–05:10`과 이중장악형 사례들은 형성 구조 반대편 wick extreme을 무효화 기준으로 사용하는 역할을 지지한다. `SOURCE_DIRECT`
- `SMtQ59w-kfw 05:40–06:05`: 거래 전에 손절 가격과 최대 현금손실을 정하고, 진입가와 손절가 차이로 수량을 계산하는 실제 과정을 설명한다. `SOURCE_DIRECT`

사용자의 `현재 equity의 3%`는 EasyChart 원문 비율을 주장하는 것이 아니라 이 현금 위험 방식을 시스템에 고정한 `USER_INVARIANT`다. 실제 수량 계산에는 진입 수수료, 불리한 손절 fill, 손절 수수료를 포함한다.

### 3.4 목표와 관리

- `F3exGqdN2Go 05:42–06:14`, `-Tp2fhvVVGM 07:51–08:06`, `V3kCjvJy3bg 12:21–12:27`, `CxVUB0E9OJU 09:16–10:00`: 직전 파동 고점·저점 또는 유동성을 목표로 사용한다. `SOURCE_DIRECT`
- 같은 장면들에는 목표 일부를 실현하고 잔량 stop을 본절로 옮기는 행동이 반복된다. 다만 모든 거래의 필수 원칙으로 일반화하지 않는다.
- `CxVUB0E9OJU 07:07–07:59`, `cZqgERyz8wc 08:13–08:27`: 수익 진행 뒤 거래량 급증을 보고 예정한 먼 목표보다 먼저 전량익절하는 장면이 있다. `SOURCE_DIRECT`

V0의 partial/volume exit exact 경계는 기존 실행 계약을 보존한다. V0.8 hardened의 핵심 변경은 exit 최적화보다 **주문 전 목적지와 경로 권위**다.

## 4. V0.7에서 확인된 설계 결함

V0.7은 M15 FVG가 기존 H1/M15 경계를 돌파·수용한 장면을 구현했다. 장면 발견 자체는 충분했다.

```text
M15 FVG 1,559
→ 경계 존재 1,511
→ 방향성 돌파 481
→ C봉 수용 425
→ 경계가 FVG 내부 274
→ 목표를 가진 authority 124
```

그러나 다음 문제가 있었다.

### 4.1 목표 권위가 너무 넓음

가장 가까운 활성 반대 pivot·OB·FVG를 모두 동급 terminal target으로 인정했다. 과거 비교에서 목표별 합계는 다음처럼 갈렸다.

| 목표 종류 | V0.7 다음 시가 순R |
|---|---:|
| pivot | `+0.934R` |
| 반대 OB | `+0.121R` |
| 반대 FVG | `-2.144R` |

이 결과만 보고 FVG라는 이름을 사후 금지하는 것이 아니다. 공식 장면에서 FVG는 목표·반응 위치로도 등장한다. 문제는 **독립 유동성 소유권 없이 단독 FVG 하나를 최종 목적지와 동급으로 본 것**이다.

### 4.2 HTF 경계가 실행 정밀도 규칙에서 사라짐

같은 FVG에 H1과 M15 경계가 연결되면 V0.7은 M15를 먼저 선택했다. 실제 완료 거래는 모두 M15 경계였고 H1은 0건이었다. 따라서 H1/H4 문맥이 후보 자격을 실질적으로 가르기 전에 H1 후보가 삭제될 수 있었다.

### 4.3 진입 시 남은 경제 공간이 작음

다음 시가 arm의 중앙 목표 R은 `0.293R`였고 31건 중 28건이 `1R` 미만이었다. 승률 80.65%라도 평균 승리 `+0.196R`에 약 `-1R` 손실 여섯 번이면 총 기대값이 음수가 됐다.

V0.8 hardened는 과거 손실 목록을 외워 거르는 대신 다음 세 구조를 주문 전에 고정한다.

1. pivot-owned terminal destination
2. HTF qualification before boundary deduplication
3. cost-inclusive economic room and first-obstacle path

## 5. 공통 SMC 인과 사슬

숙련된 SMC 데이트레이더가 각 거래를 다음 순서로 반박·검증할 수 있어야 한다.

```text
1. Liquidity cause
   사건 전에 확인된 external swing liquidity 또는 internal inducement가 있는가?

2. Meaningful location
   현재 가격이 HTF 방향과 호환되는 M15 OB 또는 확정 H1 경계에 있는가?

3. Displacement ownership
   사건 뒤 방향성 봉이 사전에 확정된 swing 또는 경계를 직접 종가 돌파했는가?
   이동이 최근 범위보다 충분히 크고 body-led인가?

4. Executable PD array
   직접 displacement가 소유하는 OB/FVG 또는 그 교집합에 첫 회귀 가격이 있는가?

5. Structural invalidation
   stop이 sweep extreme 또는 formation-owned extreme 바깥에 있는가?

6. Independent draw on liquidity
   terminal target이 사건 전에 존재한 미소비 pivot liquidity인가?

7. First-obstacle path
   진입과 terminal pivot 사이의 더 가까운 활성 구조를 건너뛰지 않는가?

8. Economic room
   목표 수수료와 adverse stop 비용을 포함한 순R이 최소 경계를 넘는가?

9. Exposure room
   현재 equity 3% 위험 수량의 명목 노출이 공통 상한 안인가?
```

하나라도 닫히지 않으면 `NO_TRADE`다.

## 6. 목표 유동성 소유권과 first-obstacle 규칙

### 6.1 terminal destination

- terminal destination은 `pivot`만 소유한다. `LOGICAL_SYNTHESIS + ENGINEERING_V0`
- HTF 가족은 사건 전 미소비 H1/H4 pivot을 사용한다.
- intraday 가족은 기존 정의의 사건 전 미소비 M15/H1/H4 pivot을 사용한다.
- pivot은 trade side 앞에 최소 1 tick 이상 있어야 한다.
- target은 authority의 decision clock 뒤에 새로 생긴 구조를 사용하지 않는다.
- target은 진입 뒤 바꾸지 않는다.

### 6.2 OB/FVG의 역할

OB와 FVG는 다음 역할을 가질 수 있다.

- HTF/LTF 의미 있는 위치
- displacement footprint
- 실제 지정가 zone
- target과 겹치는 confluence
- terminal target 전의 반응 장애물

단독 OB/FVG를 terminal destination으로 인정하지 않는다. 이것은 OB/FVG를 중요하지 않게 보는 것이 아니라, **유동성을 소유하는 가격과 delivery를 표현하는 배열의 역할을 분리**하는 것이다.

### 6.3 first obstacle

진입가에서 terminal pivot까지 진행 방향으로 다음 활성 구조를 찾는다.

- 같은 방향 draw가 되는 미소비 pivot
- 반대 side의 활성 OB
- 반대 side의 미소비 FVG
- 시간봉: M15, H1, H4

어떤 구조의 최초 접촉 가격이 terminal pivot보다 최소 1 tick 앞이면 `intervening_structure`로 거래를 거절한다. terminal pivot과 1 tick 이내면 같은 위치의 confluence로 보고 허용한다.

멀리 있는 목표로 R을 키우기 위해 가까운 반응 구조를 건너뛰는 것을 금지한다. 가까운 OB/FVG에서 부분익절하는 새 규칙을 추가한 것이 아니라, 현재 단일 목표 계약으로 설명할 수 없는 경로를 `NO_TRADE`로 만든 것이다.

## 7. 가족 A — Hardened HTF SR Flip Liquidity Delivery

### 7.1 장면

```text
사건 전에 확정된 H1 또는 M15 경계 전부 수집
→ M15 strict FVG의 B봉이 경계를 방향성 있게 종가 돌파
→ C봉이 경계 밖 acceptance 유지
→ 각 경계별 H1/H4 context 평가
→ material M15 displacement
→ flipped boundary 첫 회귀 지정가
→ 사건 전 미소비 H1/H4 pivot target
→ first-obstacle 통과
→ cost-inclusive minimum target R 통과
→ common exposure cap 통과
→ 같은 scene root의 authority 하나 선택
```

### 7.2 H1/M15 후보 순서

V0.7처럼 M15 정밀도를 HTF 문맥보다 먼저 적용하지 않는다.

1. 같은 FVG에 연결된 H1·M15 경계를 모두 보존한다.
2. 각 경계가 독립적으로 H1 trend continuation 또는 H1 range expansion 문맥을 만족하는지 본다.
3. displacement, destination, first-obstacle, 비용, 노출을 모두 검사한다.
4. 자격을 얻은 후보끼리만 하나의 scene root로 중복 제거한다.
5. 둘 다 자격이 있으면 H1 경계를 우선하고, 이후 비용 포함 target R과 pivot recency로 결정한다.

이 순서는 H1을 무조건 거래한다는 뜻이 아니다. H1 후보가 문맥 검사도 받지 못한 채 M15 때문에 삭제되는 것을 막는다.

### 7.3 초기 연구 경계

| 항목 | 값 | 분류 |
|---|---:|---|
| minimum cost-inclusive target R | `0.75R` | `RESEARCH_HYPOTHESIS` |
| displacement range / prior-20 median range | `>= 1.20` | `RESEARCH_HYPOTHESIS` |
| displacement body fraction | `>= 0.55` | `ENGINEERING_V0` |
| maximum notional / equity | `<= 8.0` | `RESEARCH_HYPOTHESIS` |

## 8. 가족 B — Hardened Internal M5 Liquidity Delivery

### 8.1 빈도 확대 원리

기존 V0.5는 활성 M15 OB 안의 M15 pivot을 M5가 sweep해야 해 양수였지만 매우 드물었다. 새 가족은 상위 위치 역할을 유지하면서, 그 안의 확정 M5 internal pivot을 실제 intraday inducement로 사용한다.

```text
HTF 방향과 호환되는 활성 M15 OB
→ M15 body 안의 사전 확정 M5 internal pivot
→ pivot 첫 sweep and reclaim
→ 최대 12개 M5 봉 안의 prompt delivery
→ 최신 확정 M5 swing을 직접 돌파한 MSS-owning OB/FVG
→ material body-led displacement
→ M15+M5 또는 OB+FVG 교집합 우선 실행 zone
→ 첫 회귀 지정가
→ 사건 전 미소비 M15/H1/H4 pivot target
→ first-obstacle 통과
→ cost-inclusive minimum target R 통과
→ common exposure cap 통과
```

### 8.2 재무장

같은 M15 OB가 살아 있다는 이유만으로 반복 주문하지 않는다. 다음 세 lineage가 모두 새로워야 한다.

- confirmed M5 pivot
- sweep event
- displacement/delivery root

고정 일일 거래 quota가 아니라 시장이 새 유동성 사건을 만든 경우에만 authority가 재무장된다.

### 8.3 초기 연구 경계

| 항목 | 값 | 분류 |
|---|---:|---|
| maximum delivery delay | `12 M5 bars` | `RESEARCH_HYPOTHESIS` |
| minimum cost-inclusive target R | `0.65R` | `RESEARCH_HYPOTHESIS` |
| displacement range / prior-20 median range | `>= 1.10` | `RESEARCH_HYPOTHESIS` |
| displacement body fraction | `>= 0.50` | `ENGINEERING_V0` |
| maximum notional / equity | `<= 8.0` | `RESEARCH_HYPOTHESIS` |

## 9. 비용 포함 순R과 3% 수량

### 9.1 all-in adverse stop loss

LONG의 예시는 다음과 같다.

```text
stop_fill = stop_price × (1 - stop_slippage_bps / 10,000)
price_loss = entry_price - stop_fill
all_in_stop_loss_per_unit
  = price_loss
  + entry_price × entry_fee_rate
  + stop_fill × stop_fee_rate
```

SHORT은 방향을 반대로 적용한다.

### 9.2 target net R

```text
net_target_profit_per_unit
  = favorable_price_distance
  - entry_price × entry_fee_rate
  - target_price × target_fee_rate

cost_inclusive_target_R
  = net_target_profit_per_unit / all_in_stop_loss_per_unit
```

minimum target R은 이 값에 적용한다. 단순 `(target-entry)/(entry-stop)` gross R로 후보를 허용하지 않는다.

### 9.3 위험 수량

```text
risk_budget = current_shared_equity × 0.03
quantity_raw = risk_budget / all_in_stop_loss_per_unit
quantity = exchange step에 맞춰 내림
```

반올림된 quantity의 실제 adverse-stop 손실이 risk budget을 넘으면 주문을 거절한다. planned notional이 `current_shared_equity × 8`을 넘으면 전략 가족과 관계없이 거절한다.

## 10. BTC·ETH 전역 open-position 포트폴리오

### 10.1 사용자 조건 해석

사용자는 BTC·ETH를 합쳐 **포지션 최대 1개**를 요구했다. 이는 대기 주문까지 하나만 허용한다는 뜻으로 확장하지 않는다.

과거 pending/open 합계 한 슬롯 모델의 문제:

```text
먼 가격의 오래 대기하는 주문 A
→ 실제 포지션은 없음
→ 이후 생성된 더 좋은 주문 B를 제출하지 못함
→ 거래 빈도와 후보 품질이 pending 순서에 종속
```

Hardened 모델:

```text
flat
→ 여러 causally valid pending limits
→ 가장 이른 실제 fill 하나 선택
→ siblings 즉시 cross-cancel
→ open position 최대 1개
→ close 뒤 다시 flat
```

### 10.2 chronological event contract

- BTC·ETH authority를 UTC decision clock으로 합친다.
- flat 상태에서 현재 시각까지 확정된 후보를 주문으로 만든다.
- 각 주문은 같은 현재 shared equity를 기준으로 3% 위험 수량을 고정한다.
- 다음 authority 생성 시각과 각 pending order의 fill/cancel 시각 중 가장 이른 사건으로 이동한다.
- 가장 이른 fill이 슬롯을 얻는다.
- 포지션 보유 중 생성된 authority는 제출하지 않는다. 이미 지나간 첫 회귀를 나중에 재구성하지 않는다.
- 포지션 종료 손익을 shared equity에 반영한 뒤 이후 주문 수량을 다시 계산한다.
- BTC와 ETH를 각자 별도 10,000 USDT로 재시작하지 않는다.

### 10.3 같은 시각 fill

5분 OHLCV는 서로 다른 종목 또는 같은 봉 안의 세부 선후를 항상 알 수 없다. 같은 timestamp fill은 사전에 고정한 다음 순위로 하나를 선택한다.

1. target kind: pivot, impulse, OB, FVG
2. 더 큰 cost-inclusive target R
3. literal execution overlap
4. 더 좁은 zone / entry price
5. symbol
6. authority ID

전략 버전 접두어는 우선순위에 사용하지 않는다. 같은 봉의 실제 미세 선후를 아는 것처럼 주장하지 않으며 tie-break 사용 횟수를 결과 진단에 남긴다.

### 10.4 live 전환에 필요한 추가 조건

historical cross-cancel은 원자적이라고 가정한다. 실제 paper/live에서는 다음이 별도로 필요하다.

- 중앙 주문 조정기와 idempotent client order ID
- fill event 수신 즉시 sibling cancel
- cancel 중 두 주문이 동시에 체결될 가능성에 대한 emergency flatten/hedge 금지 정책
- websocket 단절 뒤 exchange open-order/position reconciliation
- 프로세스 재시작 시 shared slot 복구

이 조건이 없으면 historical gate를 통과해도 live 권한을 부여하지 않는다.

## 11. 반복 random trial 계약

### 11.1 manifest

```text
years = 2022, 2023, 2024, 2025, 2026
score_days_per_year = 28
portfolio_operating_days_per_trial = 140
warmup_days = 35
exit_extension_days = 7
symbols = BTCUSDT, ETHUSDT
same manifest for every arm
```

- 연도별 시작일은 seed로 결정론적으로 뽑는다.
- 각 trial의 seed와 score/data 경계를 JSON에 저장한다.
- arm마다 다른 기간을 뽑지 않는다.
- warm-up authority는 주문하지 않는다.
- score 종료 전에 체결된 포지션만 extension에서 원래 규칙으로 종료할 수 있다.
- score 종료 시각 또는 이후의 첫 진입은 취소한다.
- 데이터 끝 미해결 포지션을 임의 가격으로 닫지 않고 trial을 censored로 무효 처리한다.

### 11.2 hard economic gate

기본 20개 trial 모두에서 다음을 요구한다.

```text
equity_multiple >= 5.0
completed_trades > operating_days
completed_trades >= 141
median_average_net_R >= 0
worst_max_drawdown <= 35%
censored_position_or_order = 0
```

20개 중 한 개만 5배인 결과, 일부 trial만 141건인 결과, 평균은 좋지만 특정 trial이 파산에 가까운 결과는 통과가 아니다.

### 11.3 5배 feasibility

정확히 141건이 모두 같은 비용 포함 순R을 낸다고 단순화하면 다음이 필요하다.

```text
(1 + 0.03 × constant_net_R)^141 = 5
constant_net_R ≈ +0.38266R
```

실제 경로는 손익 순서와 분산에 영향을 받는다. 이 값은 충분조건이 아니라 최소 거래 수에서 5배가 얼마나 높은 경제적 장벽인지 보여 준다. V0.7처럼 작은 이익과 약 `-1R` 손실이 반복되면 거래 수만 늘려도 통과하지 못한다.

### 11.4 반복 표본 중복

여러 random trial은 날짜 민감도를 보는 데 유용하지만 서로 겹치는 scored day는 독립 관측이 아니다. 결과에 다음을 기록한다.

- nominal trial 수
- scored-day observations
- unique scored days
- unique-day fraction
- trial pair별 shared days
- maximum pairwise overlap fraction

trial 수가 늘었다는 이유만으로 증거량이 같은 비율로 늘었다고 주장하지 않는다.

### 11.5 bootstrap의 권한

거래 순서를 iid로 재표본하는 bootstrap은 다음 진단에만 사용한다.

- 같은 net-R 분포에서 순서 변화의 낙폭 민감도
- 파산 또는 큰 drawdown 경로의 대략적 취약성

regime, 날짜 군집, BTC/ETH 동조, feature drift를 보존하지 않으므로 holdout을 대체하지 않는다.

## 12. Discovery와 정책 고정 Holdout

### 12.1 Discovery

- 전략 논리와 초기 경계를 비교한다.
- 실패 funnel과 경제성을 분석한다.
- 결과를 본 뒤 정책을 수정할 수 있다.
- 수치가 hard gate를 통과해도 paper/live 승격 권한이 없다.

### 12.2 Holdout

- 전략, 비용, slippage, 우선순위, exit, 위험, 노출 경계가 commit SHA로 고정돼야 한다.
- `--frozen-policy-sha`가 없으면 holdout promotion 불가다.
- hard economic gate와 no-censor를 모두 통과해야 한다.
- 결과를 보고 정책을 바꾸면 해당 holdout은 discovery가 되며 새 미사용 기간이 필요하다.

### 12.3 승격 단계

```text
discovery candidate
→ frozen policy SHA
→ unused holdout hard gate
→ deterministic replay reproducibility
→ paper order-state/restart/cross-cancel tests
→ sustained paper observation
→ explicit live approval
```

historical 5배만으로 live 전환하지 않는다.

## 13. 비교 arm

Hardened runner는 같은 manifest에서 다음을 비교한다.

1. `leader`
2. `leader_plus_v08_hardened_htf`
3. `leader_plus_v08_hardened_intraday`
4. `leader_plus_all_v08_hardened`

새 가족이 거래를 추가해도 기존 선두의 좋은 거래를 슬롯 경쟁에서 빼앗거나 전체 로그성장을 훼손할 수 있다. 따라서 family 단독 집계뿐 아니라 실제 전역 공유 잔고 경로를 비교한다.

주요 진단:

- authority funnel과 rejection reason
- first-obstacle rejection
- cost-inclusive target-space rejection
- exposure rejection
- pending orders created
- maximum concurrent pending
- sibling cross-cancel count
- open-position 동안 suppressed authorities
- completed trades / operating day
- average net R와 win/loss asymmetry
- equity multiple, log growth, max drawdown
- target kind, exit reason, symbol, year별 분해

## 14. 공식 시장 데이터

`scripts/download_binance_um_5m.py`는 Binance USD-M futures public archive를 사용한다.

- BTCUSDT·ETHUSDT 5m kline
- 완전한 월은 monthly archive
- 경계 월은 daily archive
- monthly 미제공 시 daily fallback
- 제공 `.CHECKSUM` SHA-256 검증
- UTC timestamp 정규화
- 중복 제거
- 정확한 5분 연속성 확인

다운로드 데이터는 커밋된 성과 결과가 아니다. 동일 snapshot을 재현하려면 파일 checksum 또는 별도 data manifest를 결과와 함께 보존해야 한다.

## 15. 실행

### 15.1 Discovery

```powershell
python scripts\run_easychart_v08_hardened_random_trials.py ^
  --data-dir data\binance_um_5m ^
  --trials 20 ^
  --seed 20260720 ^
  --risk-fraction 0.03 ^
  --phase discovery ^
  --output-dir results\easychart_v08_hardened_discovery
```

### 15.2 Holdout

```powershell
python scripts\run_easychart_v08_hardened_random_trials.py ^
  --data-dir data\binance_um_5m ^
  --trials 20 ^
  --seed 20260817 ^
  --risk-fraction 0.03 ^
  --phase holdout ^
  --frozen-policy-sha <commit-sha> ^
  --output-dir results\easychart_v08_hardened_holdout
```

결과물:

- `summary.json`
- `trial_results.csv`
- `trade_ledger.csv`
- `build_diagnostics.csv`
- `trial_manifests/*.json`

## 16. 자동 테스트와 현재 검증 상태

자동 테스트는 다음 불변식을 고정한다.

- adverse stop 비용과 같은 denominator를 쓰는 target net R
- 잘못된 long/short 기하 거절
- terminal pivot 전의 opposing FVG/OB/pivot 차단
- terminal pivot 1 tick 이내 구조를 confluence로 처리
- missing pre-event pivot 거절
- H1 후보가 M15 preselection 전에 보존됨
- first-obstacle가 HTF·intraday authority를 거절함
- 141건·3%·5배 feasibility 계산
- 반복 trial의 날짜 중복 요약
- discovery promotion 금지와 holdout frozen SHA 요구
- 오래 대기하는 주문보다 나중에 생성된 빠른 fill이 슬롯을 얻음
- 포지션 보유 중 authority 억제
- 모든 가족의 공통 notional cap

PR Research CI는 전략 테스트, Python compile, hardened runner import/`--help` 계약을 검사한다.

## 17. 아직 입증되지 않은 것

다음은 현재 사실로 주장하지 않는다.

- 반복 random trial에서 5배 달성
- 모든 trial에서 141건 이상 완료
- 반복 표본의 비용 포함 양의 기대값
- 최악 최대낙폭 35% 이하
- 현재 `0.75R`, `0.65R`, `8배`, `12봉` 경계의 최적성
- OHLCV same-bar tie-break와 실제 체결 순서의 완전한 일치
- 원자적 cross-cancel과 재시작 복구를 포함한 paper/live 안전성
- 장기 계좌 우상향 보장

따라서 현재 권한은 계속 `RESEARCH_ONLY`다. 실제 공식 데이터의 repeated discovery와 정책 고정 holdout 결과가 나오기 전에 목표 달성을 선언하지 않는다.

## 18. 다음 연구 순서

1. 공식 BTC·ETH 데이터 snapshot과 checksum을 고정한다.
2. 여러 discovery seed에서 four-arm 비교를 실행한다.
3. 빈도 미달이면 생성 장면 수가 아니라 first-obstacle, 미체결, 포지션 보유 억제, 노출 거절을 분해한다.
4. 기대값 미달이면 평균 승리·평균 손실, initial stop loss, target kind, MFE/MAE, 비용 민감도를 분해한다.
5. 논리 역할이 분명한 변경 하나만 적용하고 동일 discovery manifest에서 비교한다.
6. 후보가 충분히 형성되면 전략·실행 정책 commit SHA를 고정한다.
7. 새 미사용 holdout manifest에서 hard gate를 실행한다.
8. 통과한 경우에만 동일 코어의 paper 주문 상태·복구·cross-cancel을 구현하고 관찰한다.
9. 별도 명시적 승인 전에는 live 주문 권한을 열지 않는다.
