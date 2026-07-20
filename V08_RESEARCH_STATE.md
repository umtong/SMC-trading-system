# V0.8 hardened 연구 상태 기준선

업데이트: `2026-07-20 KST`  
현재 단계: 전략·체결·반복 검증 규약 구현 및 CI 통과, 공식 시장 데이터 성과 검증 전  
보존 연구 선두: `V0.3 BREAK_RETEST + V0.5`  
신규 연구 후보: `V0.8 hardened HTF liquidity delivery`, `V0.8 hardened internal M5 liquidity delivery`  
권한 상태: `RESEARCH_ONLY / NO_PAPER_ORDER / NO_LIVE_ORDER`

## 1. 이 문서의 역할

이 문서는 draft PR #2의 최신 판단 기준선이다. 내용이 충돌하면 다음 순서로 판단한다.

1. 사용자의 최신 결정
2. 현재 코드와 실제 실행 결과
3. 이 문서
4. `MAIN_AGENT_STATE.md`의 V0.7 이하 보존 판단
5. 정책·수식·과거 보고서

V0.8이 실제 반복 표본에서 검증되기 전까지 기존 선두 결과를 삭제하거나 V0.8의 성공 결과로 다시 이름 붙이지 않는다.

## 2. 사용자 하드 불변식과 실행 해석

```text
symbols = BTCUSDT + ETHUSDT
max_total_open_positions = 1
risk_per_filled_trade_fraction = 0.03
position_addition_after_entry = false
daily_loss_limit_enabled = false
```

`총 포지션 최대 1개`는 다음처럼 구현한다.

- 포지션이 없을 때는 서로 다른 종목·장면의 인과적으로 유효한 지정가가 동시에 대기할 수 있다.
- 실제로 가장 먼저 체결되는 주문 하나만 BTC·ETH 전역 포지션 슬롯을 얻는다.
- 같은 체결 시각의 경쟁은 사전에 고정한 구조·비용 우선순위로 하나를 선택한다.
- 선택 즉시 나머지 대기 주문은 교차 취소한다.
- 포지션 보유 중 새로 생긴 authority는 신규 주문으로 제출하지 않는다.
- 실제 paper/live에서는 위 교차 취소가 원자적으로 보장되는 주문 조정기가 필요하다.

과거 `pending 또는 open 합계 한 슬롯` 모델은 사용자의 포지션 제한보다 더 강했다. 오래 기다리는 미체결 주문 하나가 이후의 더 좋은 후보를 막을 수 있으므로 hardened 연구 경로에서는 사용하지 않는다.

수량은 실제 주문 생성 시점의 공유 equity와 진입가–손절가의 비용 포함 최대손실로 계산한다. 모든 가족에 공통으로 명목 노출 `equity × 8` 상한을 둔다. `8배`는 EasyChart 원문이 아니라 과도하게 가까운 손절이 만드는 비현실적 노출을 막기 위한 초기 `ENGINEERING_V0` 경계다.

## 3. 반복 검증 성공 조건

```text
2022~2026 각 연도 무작위 28일
trial당 포트폴리오 운용일 = 140일
완료 거래 수 > 포트폴리오 운용일 수
따라서 최소 완료 거래 수 = 141건
초기 equity 대비 최종 equity >= 5배
거래당 현재 equity 위험 = 3%
BTC·ETH 전체 최대 open position = 1
수수료와 손절 slippage 포함
```

미체결·취소·거절·미해결 주문은 완료 거래 수로 세지 않는다.

141건이 모두 같은 비용 포함 순R을 낸다고 단순화하면 필요한 거래당 결과는 다음과 같다.

```text
(1 + 0.03 × net_R)^141 = 5
net_R ≈ +0.38266R
```

이는 충분조건이나 성과 추정치가 아니다. 5배와 최소 빈도를 동시에 요구할 때 작은 이익과 약 `-1R` 손실의 비대칭을 그대로 둘 수 없다는 경제적 난이도 기준이다.

## 4. hardened 전략 인과관계

모든 신규 거래 후보는 다음 순서를 설명할 수 있어야 한다.

```text
liquidity cause
→ meaningful HTF-compatible location
→ displacement ownership
→ executable OB/FVG footprint
→ structural invalidation
→ pivot-owned draw on liquidity
→ first-obstacle path validation
→ cost-inclusive target room
→ portfolio exposure room
```

하나라도 닫히지 않으면 `NO_TRADE`다.

### 4.1 목표 유동성 소유권

- terminal target은 사건 전에 이미 확인된 미소비 pivot liquidity만 소유할 수 있다.
- HTF 가족은 H1/H4 pivot을 사용한다.
- intraday 가족은 기존 정의의 M15/H1/H4 pre-event pivot을 사용하되, 아래 first-obstacle 검사를 다시 통과해야 한다.
- OB와 FVG는 위치·displacement·실행 footprint·반응 장애물 역할을 가진다.
- 진입과 먼 pivot 사이에 더 가까운 활성 pivot, 반대 OB 또는 반대 FVG가 있으면 먼 목표를 건너뛰지 않고 거래를 거절한다.
- 구조의 최초 접촉 가격이 terminal pivot과 1 tick 이내면 별도 장애물이 아니라 같은 위치의 confluence로 본다.

이 규칙은 V0.7에서 반대 FVG를 단독 terminal target으로 인정해 음의 기대값 장면이 늘어난 문제를 직접 다룬다. 특정 과거 결과를 삭제하는 사후 필터가 아니라 주문 전 목적지 권위를 정의하는 구조 규칙이다.

### 4.2 HTF liquidity delivery

```text
사건 전에 확정된 H1 또는 M15 경계 전부 보존
→ M15 FVG B봉의 방향성 종가 돌파
→ C봉 acceptance
→ 각 경계별 H1/H4 문맥 검사
→ material displacement
→ 경계 첫 회귀 지정가
→ 사건 전 미소비 H1/H4 pivot 목적지
→ 더 가까운 활성 구조가 없는 경로
→ 비용 포함 최소 순R과 노출 상한
→ 같은 장면의 최종 authority 하나 선택
```

중요한 변경은 H1과 M15가 같은 FVG에 연결될 때 M15를 먼저 고르지 않는 것이다. 모든 경계를 상위 문맥 검사까지 보존한 뒤, 실제로 자격을 얻은 후보끼리만 중복을 제거한다. 이로써 V0.7의 M15 실행 정밀도 규칙이 H1 range-expansion 장면 자체를 삭제하지 못한다.

초기 연구 경계:

- 비용 포함 target space 최소 `+0.75R`
- M15 displacement 범위는 직전 20봉 중앙 범위의 최소 `1.20배`
- displacement body fraction 최소 `55%`
- 명목 노출 최대 equity `8배`

`minimum_target_r`는 이제 단순 가격거리 R이 아니다. 목표 수수료를 차감한 순이익을, 진입 수수료·손절 수수료·불리한 손절 slippage를 포함한 최대손실로 나눈 순R이다.

### 4.3 internal M5 liquidity delivery

```text
HTF 방향과 호환되는 활성 M15 OB
→ 그 위치 안의 사전 확정 M5 internal pivot
→ 첫 sweep and reclaim
→ 12개 M5 봉 이내 prompt MSS-owning OB/FVG
→ material displacement
→ 실행 footprint 첫 회귀
→ 사건 전 미소비 M15/H1/H4 pivot 목적지
→ 더 가까운 활성 M15/H1/H4 구조가 없는 경로
→ 비용 포함 최소 순R과 노출 상한
```

초기 연구 경계:

- 비용 포함 target space 최소 `+0.65R`
- M5 displacement 범위는 직전 20봉 중앙 범위의 최소 `1.10배`
- displacement body fraction 최소 `50%`
- delivery 지연 최대 `12개 M5 봉`
- 명목 노출 최대 equity `8배`
- 서로 다른 pivot·sweep event·delivery root만 새 장면으로 재무장

## 5. 전역 포트폴리오 계약

```text
BTC authority stream ┐
                     ├─ chronological authority clock
ETH authority stream ┘
→ flat 상태에서 인과적으로 유효한 pending book 구성
→ 각 주문을 현재 공유 equity의 3% 위험으로 고정
→ 가장 이른 실제 fill 탐색
→ 같은 fill 시각이면 사전 고정 priority 적용
→ winner 하나 체결, siblings 교차 취소
→ open position 종료 전 새 authority 억제
→ 비용 포함 손익을 shared equity에 반영
→ flat 상태로 복귀
```

동시 fill 우선순위는 전략 버전 이름을 보지 않는다.

1. pivot-owned target
2. 더 큰 비용 포함 target R
3. literal execution overlap
4. 더 좁은 실행 zone
5. symbol과 authority ID의 결정론적 tie-break

과거처럼 `v08-` 접두어라는 이유만으로 선두 거래보다 우선하지 않는다.

## 6. 반복 random trial와 연구 거버넌스

- BTC와 ETH는 같은 window manifest를 공유한다.
- warm-up 35일은 feature 형성에만 사용한다.
- score 28일 안에서 생성된 authority만 주문 후보가 된다.
- score 종료 전에 체결된 포지션만 7일 extension에서 원래 stop/target 규칙으로 종료할 수 있다.
- score 종료 시각 또는 이후 첫 체결은 취소한다.
- 데이터 끝 미해결 포지션을 임의 손익으로 닫지 않으며 trial을 무효로 표시한다.
- 모든 symbol과 연도 window는 하나의 시간순 equity와 최대 open position 하나를 공유한다.
- 반복 trial이 같은 날짜를 재사용할 수 있으므로 nominal trial 수를 독립 표본 수처럼 해석하지 않는다. scored-day 고유 비율과 trial 간 중복을 결과 JSON에 기록한다.
- iid trade bootstrap은 순서 민감도 진단일 뿐, regime·날짜 의존성을 제거한 독립 검증으로 취급하지 않는다.

기본 20개 trial 모두에서 다음 hard gate를 요구한다.

- equity multiple `>= 5.0`
- completed trades `>= 141`
- median average net R `>= 0`
- worst max drawdown `<= 35%`
- censored position/order `= 0`

### 6.1 discovery와 holdout

- `discovery`: 후보 비교와 실패 원인 분석에만 사용한다. 수치가 좋아도 paper/live 승격 불가다.
- `holdout`: 전략·체결·비용·우선순위를 고정한 commit SHA가 필요하다.
- holdout에서도 hard economic gate를 모두 통과하고 censored trial이 없어야 승격 후보가 된다.
- 같은 holdout을 보고 정책을 바꾸면 그 기간은 다시 discovery가 되며 새 미사용 holdout이 필요하다.

## 7. 구현·자동검증 상태

구현 완료:

- pivot-owned terminal destination
- nearer active structure를 건너뛰지 않는 first-obstacle 검사
- H1/M15 경계를 HTF qualification 전까지 보존하는 builder
- 비용 포함 순R 계산
- portfolio-wide 8배 노출 상한
- flat 상태 multi-pending / earliest-fill-wins / cross-cancel 포트폴리오 재생
- deterministic repeated random manifests
- 5배·141건 hard gate
- trial scored-day 중복 요약
- discovery/holdout/frozen-policy promotion gate
- 공식 Binance USD-M 5m downloader와 checksum·연속성 검증
- hardened arm 비교 runner
- focused regression tests와 PR Research CI

아직 입증되지 않음:

- 반복 random trial에서 5배 달성
- 모든 140일 trial에서 완료 거래 141건 이상
- 모든 trial에서 비용 포함 양의 기대값
- 최악 최대낙폭 35% 이하
- 실제 exchange에서 원자적 cross-cancel을 포함한 paper runtime
- paper/live 승격 자격

따라서 V0.8 hardened는 활성 주전이 아니며 실제 주문 권한이 없다.

## 8. 다음 검증 순서

1. 공식 BTCUSDT·ETHUSDT USD-M 5분봉을 checksum과 연속성 검증으로 생성한다.
2. hardened runner를 `discovery`로 여러 seed에 실행한다.
3. leader와 세 hardened 결합 arm의 빈도·순R·낙폭·슬롯 충돌을 같은 manifest에서 비교한다.
4. 빈도 미달이면 authority funnel, first-obstacle 거절, pending 생성·교차 취소, 포지션 보유 중 억제를 분해한다.
5. 5배 미달이면 평균 순R, 손실 분포, 목표 전 MFE/MAE, exit reason, 비용·slippage 민감도를 분해한다.
6. 논리 역할이 분명한 변경만 discovery에서 비교한다. 결과를 보고 임의 grid를 확대하지 않는다.
7. 정책을 고정한 commit SHA로 새 holdout manifest를 실행한다.
8. holdout hard gate와 실시간 주문 상태 복구 시험 전에는 paper/live 권한을 부여하지 않는다.

평가 기준은 항상 다음 하나다.

> 이 변경이 완료 거래 빈도, 거래당 비용 포함 평균 순R, 손실과 노출 통제를 함께 개선해 장기 계좌 성장 속도와 안정성을 높이는가?
