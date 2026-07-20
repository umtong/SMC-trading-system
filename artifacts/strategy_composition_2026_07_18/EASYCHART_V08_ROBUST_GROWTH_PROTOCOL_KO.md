# EasyChart V0.8 강건 성장 프로토콜 — HTF 목적지와 재무장형 intraday 유동성 전달

작성일: 2026-07-20 KST  
상태: **구현·검증 중 / RESEARCH_ONLY / NO_PAPER_ORDER / NO_LIVE_ORDER**

## 1. 성공 조건

V0.8은 다음 조건을 동시에 만족해야 한다.

```text
동일 전략 코어
× BTC·ETH 전역 공유 잔고
× 전체 시장 pending/open 최대 한 슬롯
× 거래당 현재 equity의 3% 현금위험
× 수수료·단순 slippage 포함
× 2022~2026 각 연도 무작위 28일, trial당 140 운용일
× 완료 거래 수 > 포트폴리오 운용일 수
× 초기 계좌 대비 최소 5배
```

140 운용일 trial은 완료 거래가 최소 141건이어야 한다. 미체결, 취소, 거절, 데이터 끝 미해결 주문은 거래 횟수로 세지 않는다.

정확히 141건이 모두 같은 비용 포함 순R을 낸다고 단순화하면 3% 복리로 5배에 필요한 거래당 기하평균은 약 `+0.383R`이다.

```text
(1 + 0.03 × average_net_R)^141 = 5
average_net_R ≈ +0.383R
```

실제 결과는 손익 순서와 분산의 영향을 받으므로 이 수치는 충분조건이 아니라 경제적 난이도를 보여 주는 기준선이다. 작은 이익 여러 번과 약 `-1R` 손실의 비대칭이 유지되면 141건을 넘겨도 5배가 되지 않는다.

## 2. source가 지지하는 공통 투자 문법

등록된 EasyChart 원본 자막과 기존 프레임 감사에서 다음 역할을 보존한다.

- 상위 구조를 먼저 보고, 방향이 없거나 양방향 구조가 충돌하면 현금 상태를 유지한다. `SOURCE_DIRECT`
- 월·주·일봉은 큰 추세, 12H·4H·1H는 중간 구조·지지저항, 15m·5m·1m은 실제 진입에 사용한다. `SOURCE_DIRECT`
- 주요 고점·저점과 다른 참여자의 손절이 모일 위치를 먼저 표시하고, 위치만으로 진입하지 않고 OB 등 별도 근거를 결합한다. `SOURCE_DIRECT`
- OB 재방문을 거래 위치로 사용하며, 유동성 흡수나 다른 구조와 겹칠 때 의미가 커진다. `SOURCE_DIRECT`
- 직전 파동 고점·저점 또는 유동성을 목표로 사용한다. `SOURCE_DIRECT`
- 관점이 무효화되면 기존 방향 편향을 버리고 상위 구조부터 다시 분석한다. `SOURCE_DIRECT`

V0.8의 exact OHLCV 경계는 source 문장을 그대로 옮긴 것이 아니라 위 역할을 자동 실행 가능하게 닫은 `LOGICAL_SYNTHESIS + ENGINEERING_V0`다.

## 3. SMC 인과 사슬

숙련된 SMC 트레이더가 각 거래를 다음 순서로 반박·검증할 수 있어야 한다.

```text
1. Liquidity cause
   사전에 확인된 swing liquidity 또는 내부 inducement가 존재하는가?

2. Meaningful location
   현재 가격이 HTF 방향과 호환되는 M15 OB 또는 확정 H1 경계에 있는가?

3. Displacement ownership
   사건 뒤 방향성 봉이 사전에 확정된 swing을 직접 종가 돌파했는가?
   그 봉이 평소보다 충분히 크고 몸통 중심의 이동인가?

4. Executable PD array
   직접 displacement가 만든 OB/FVG 또는 그 교집합에 실제 첫 회귀 진입가가 있는가?

5. Structural invalidation
   stop은 sweep extreme 또는 formation이 실제로 소유하는 extreme 바깥인가?

6. Independent draw on liquidity
   목표가 단순한 가까운 FVG/OB가 아니라 사건 전에 존재한 미소비 pivot liquidity인가?

7. Economic room
   비용 포함 목표 공간과 필요한 명목 노출이 합리적인가?
```

이 사슬 중 하나라도 닫히지 않으면 `NO_TRADE`다.

## 4. V0.8 가족 A — HTF SR flip liquidity delivery

### 4.1 장면

```text
사건 전에 확정된 H1 또는 M15 경계
→ M15 FVG B봉이 경계를 방향성 있게 종가 돌파
→ C봉이 돌파 상태를 유지
→ H1/H4 구조 문맥과 방향 일치
→ 평소 M15 범위 대비 material displacement
→ 경계 첫 회귀 지정가
→ 사건 전에 존재한 미소비 H1/H4 pivot liquidity 목표
```

### 4.2 V0.7과의 차이

V0.7은 모든 활성 반대 FVG·OB·pivot 중 가까운 구조를 동급 terminal target으로 인정했다. 실제 결과에서 반대 FVG 목표 거래가 가장 큰 음의 손익을 만들었다.

V0.8은 다음을 바꾼다.

- FVG는 displacement와 실행 위치 역할을 가진다.
- OB는 위치, 실행, 장애물 역할을 가질 수 있다.
- terminal target은 사건 전에 존재한 미소비 `H1/H4 pivot liquidity`만 인정한다.
- H1 trend continuation 또는 H1 range에서 실제 H1 boundary expansion인 경우만 허용한다.
- B봉 범위, 몸통 비율, 경계 종가 관통, C봉 acceptance buffer를 검사한다.
- planned target이 최소 `0.75R`이고, 3% 위험을 만들기 위한 명목 노출이 equity의 `8배` 이하인 경우만 허용한다.

`0.75R`과 `8배`는 source direct가 아니라 첫 경제 경계다. 반복 trial 결과에 따라 유지·수정·폐기한다.

## 5. V0.8 가족 B — M15 위치 내부 M5 liquidity delivery

### 5.1 빈도 확대 원리

기존 V0.5는 활성 M15 OB 안의 **M15 pivot**을 M5가 sweep해야 했다. 이 조합은 양수였지만 매우 드물었다.

새 가족은 같은 상위 위치 역할을 유지하면서, 그 안에서 실제 intraday inducement 역할을 하는 **확정 M5 pivot**을 사용한다.

```text
HTF 방향과 호환되는 활성 M15 OB
→ 그 body 안에 사전에 확정된 M5 internal pivot
→ pivot 첫 sweep·reclaim이 M15 위치에서 완료
→ 12개 M5 봉 이내 prompt delivery
→ 최신 확정 M5 swing을 직접 돌파한 OB 또는 strict FVG
→ 평소 M5 범위 대비 material displacement
→ M15+M5 또는 OB+FVG 교집합 우선, 아니면 실행 footprint
→ 첫 회귀 지정가
→ 사건 전에 존재한 미소비 M15/H1/H4 pivot liquidity 목표
```

### 5.2 재무장

같은 M15 OB가 살아 있더라도 동일 pivot과 동일 displacement를 반복 거래하지 않는다.

- 서로 다른 확정 M5 pivot
- 서로 다른 sweep 사건
- 서로 다른 delivery root

위 세 가지가 새로 생긴 경우에만 새 장면으로 재무장한다. 이는 거래를 억지로 늘리는 고정 일일 quota가 아니다. 시장이 새 유동성 사건을 만들었을 때만 다음 주문 권위가 생긴다.

### 5.3 초기 경제 경계

- delivery는 sweep 뒤 최대 `12개 M5 봉` 안에 완료
- displacement 범위는 직전 20봉 중앙 범위의 최소 `1.10배`
- displacement 몸통은 전체 범위의 최소 `50%`
- 비용 전 planned target 공간 최소 `0.65R`
- 3% 위험을 위한 명목 노출은 equity의 최대 `8배`
- terminal target은 pivot만 허용

이 경계는 작은 고정 grid를 무차별 탐색해 고른 값이 아니다. prompt reaction, body-led displacement, 비용 뒤 의미 있는 공간, 과도한 레버리지 방지라는 각각의 역할에서 시작한 첫 연구값이다.

## 6. 전역 포트폴리오 계약

```text
BTC authority stream ┐
                     ├─ chronological candidate queue
ETH authority stream ┘
→ same cutoff candidate selection
→ one pending/open slot
→ current shared equity × 3% risk
→ close/cancel/reject/censor
→ next candidate
```

- BTC와 ETH를 각자 10,000 USDT로 재시작하지 않는다.
- 같은 UTC 날짜는 두 종목이 겹쳐도 포트폴리오 운용일 하루로 센다.
- score window 전에 생성된 authority는 거래하지 않는다.
- score window 안에서 생성됐어도 score 종료 뒤 처음 체결되는 주문은 취소한다.
- score 종료 전에 체결된 포지션은 exit-extension 데이터 안에서 원래 stop/target 규칙으로 종료할 수 있다.
- 데이터 끝까지 미해결이면 강제손익을 만들지 않고 trial을 무효로 표시한다.
- 동일 cutoff 경쟁은 V0.8 causal scene, literal overlap, planned target R, 좁은 실행 zone 순으로 결정한다.

## 7. 반복 무작위 검증

기본 manifest는 다음과 같다.

```text
연도: 2022, 2023, 2024, 2025, 2026
연도별 score: 28일
trial 포트폴리오 운용일: 140일
warm-up: score 전 35일
exit extension: score 뒤 7일
기본 trial 수: 20
BTC/ETH: 같은 window manifest 공유
```

각 trial의 seed, window, score/data 경계는 JSON에 저장한다. arm마다 다른 기간을 뽑지 않는다.

### 7.1 하드 gate

모든 trial에서 다음을 요구한다.

- 최종 equity multiple `>= 5.0`
- 완료 거래 수 `> 포트폴리오 운용일 수`
- 비용 포함 median average net R `>= 0`
- 모든 trial 중 최악 최대낙폭 `<= 35%`
- 미해결 position/order가 없음

20개 중 한 개만 5배인 결과, 140일 중 일부 창만 141건인 결과, 평균은 좋지만 특정 trial이 파산에 가까운 결과는 통과가 아니다.

## 8. 공식 시장 데이터 재현

`scripts/download_binance_um_5m.py`는 Binance 공식 public data archive를 사용한다.

- USD-M futures BTCUSDT·ETHUSDT 5m kline
- 완전한 월은 monthly archive
- 경계 월은 daily archive
- monthly 파일이 없으면 daily fallback
- 제공 SHA-256 `.CHECKSUM` 검증
- UTC timestamp 정규화
- 중복 제거 뒤 정확한 5분 연속성 확인

다운로드된 CSV는 전략 수익 규칙의 권위가 아니라 OHLCV replay의 관측 자료다.

## 9. 현재 판단

구현된 것은 다음이다.

- 반복 무작위 140일 manifest
- 5배 및 141건 이상 hard gate
- bootstrap 순서 스트레스
- V0.8 HTF liquidity delivery
- V0.8 internal M5 liquidity delivery
- BTC/ETH 전역 shared-equity/shared-slot replay
- 공식 데이터 downloader
- arm 비교 runner와 단위 테스트

아직 입증되지 않은 것은 다음이다.

- 반복 trial에서 실제 5배
- 모든 trial에서 141건 이상
- 최악 낙폭 35% 이하
- paper/live 승격 자격

따라서 현재 상태는 계속 `RESEARCH_ONLY`다. CI와 실제 데이터 결과가 나오기 전에 목표 달성을 선언하지 않는다.
