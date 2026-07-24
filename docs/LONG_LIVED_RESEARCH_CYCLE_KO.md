# 장기 운용형 SMC/ICT 연구·갱신·검증 사이클

상태: `RESEARCH_GOVERNANCE_IMPLEMENTED / CASH_ALLOWED / NO_AUTOMATIC_LIVE_PROMOTION`

## 목적

이 시스템은 한 번의 백테스트에서 최고 수익을 고르는 도구가 아니다. 데이터가 추가될 때마다 같은 인과성·비용·안정성·운영 검증을 반복하고, 시장 레짐이 몇 달 또는 몇 년 뒤 바뀌더라도 다음 원칙을 유지한다.

1. 판단 시점에 실제로 이용 가능했던 정보만 쓴다.
2. 거래 결과와 최종 MFE·MAE·청산 사유는 같은 거래의 이전 판단 입력으로 쓰지 않는다.
3. 미래 행 추가는 허용하지만 이미 확정된 과거 증거의 수정·삭제는 차단한다.
4. 모든 전략이 기준 미달이면 `CASH`를 champion으로 유지한다.
5. challenger 선택은 사전에 고정한 비용·낙폭·기간 안정성·수익 집중도 gate로 한다.
6. 새 결정은 계산한 cutoff 이전으로 소급하지 않고 다음 deployment window부터 적용한다.
7. 일평균 순수익 1~3%는 최종 도전 목표로 유지하되 후보 선택·하이퍼파라미터·레버리지의 목적함수로 쓰지 않는다.

## 세 개의 시간축

모든 데이터와 특징은 다음 시간을 구분해야 한다.

- `event_time`: 시장 사건 발생 시각
- `available_at`: 시스템이 값을 처음 소비할 수 있었던 시각
- `label_end`: 미래 결과가 완전히 확정된 시각

판단 입력은 `available_at <= decision_time`, 학습·선택용 label은 `label_end <= cutoff`를 만족해야 한다. 미완성 봉을 사용하려면 그 시점의 partial snapshot을 별도로 보존하고 최종 OHLC로 과거 snapshot을 덮어쓰지 않는다. strict pivot과 기타 후행 구조는 `known_at` 이후에만 존재한다.

세부 계약은 `configs/causal_market_data_contract.json`을 따른다.

## 상태 저장형 champion–challenger

`scripts/smc_ict_research_cycle.py`는 각 cutoff에서 다음을 수행한다.

1. 후보별 거래 원장에서 `exit_time <= cutoff`인 성숙 거래만 읽는다.
2. base/stress 비용 시나리오를 동일하게 적용한다.
3. PF, 최대낙폭, 양수 cycle 비율, 상위 5개 이익 집중도, 최고 5거래 제거 후 수익을 계산한다.
4. 사전 등록된 gate를 통과한 후보만 challenger가 될 수 있다.
5. 연속 통과 횟수와 운영 attestation을 모두 만족해야 champion이 바뀐다.
6. 조건을 만족하는 후보가 없으면 `CASH`를 유지한다.
7. cutoff 당시 성숙 증거의 canonical hash를 상태 파일에 저장한다.
8. 다음 실행에서 과거 성숙 행이 바뀌면 `HistoricalRevisionError`로 중단한다.

연구 gate와 실거래 운영 gate는 분리된다. 연구 결과가 좋아도 인과성, 데이터 품질, 부분체결·queue·funding·slippage 체결 모델, shadow 기간, reconciliation이 통과하지 않으면 live 승격은 막힌다.

## 자동 반복 주기

### 매일

- raw archive와 checksum 수집
- 중복·누락·시간 간격·OHLC·거래량·symbol 상태 검사
- 기존 결정 prefix hash 재검증
- paper/shadow 주문과 거래소 상태 reconciliation
- 치명적 오류 시 신규 주문 차단

### 매주

- prefix truncation 및 future-mutation 인과성 테스트
- 비용·미체결·부분체결·slippage 스트레스
- 이벤트율, 특징 분포, spread, fill ratio, 실제 slippage drift 검사
- 동일 frozen artifact 재현성 검사

### 매월

- 직전 cutoff까지 성숙한 label만 사용
- expanding 후보와 최근 구간 rolling 후보를 같은 다음 기간 기준으로 평가
- 규칙·모델·코드·데이터 snapshot hash를 동결
- challenger 결정을 다음 월부터만 적용

### 분기 또는 충분한 shadow 표본 이후

- causality/data/execution/shadow/reconciliation gate 검토
- 자동 주문 권한 부여가 아니라 승인 가능한 release artifact 생성
- 승인 전 기본 상태는 `NO_LIVE_ORDER`

주기 계약은 `configs/research_cycle_schedule.json`에 있다.

## 레짐 변화 대응

레짐은 사후 이름이 아니라 decision time에 계산 가능한 값으로만 정의한다.

- 실현 변동성 분위수
- 추세/범위 구조 상태
- 거래량과 taker imbalance
- open interest 변화
- funding/premium/basis
- spread, depth, 예상 market impact
- 이벤트 발생률과 fill probability

장기 구조를 보존하는 expanding 후보, 최근 미세구조를 반영하는 rolling 후보, 고정 rule 후보, 사전 등록된 regime-conditioned 후보를 동시에 challenger로 비교한다. 최근 성과가 나쁘다는 이유로 search space나 gate를 자동 완화하지 않는다.

## 전략 연구 방향

현행 OHLCV-only 패턴을 독립 알파로 가정하지 않는다. 다음 challenger는 SMC/ICT를 구조적 event generator로 사용한다.

```text
HTF external liquidity / dealing range
→ completed liquidity raid
→ displacement with causal MSS/FVG ownership
→ order-flow and OI state confirmation
→ fill-probability-aware first return
→ pre-fixed invalidation and external draw
→ queue/partial/funding-aware execution
```

필요 데이터는 1분 kline, mark/index price, aggTrades, funding, premium/basis, open interest, 실제 수수료와 symbol filter, leverage bracket, 가능하면 order-book snapshot과 spread/depth다.

## 실행 예

후보 registry와 운영 attestation을 실제 artifact 경로로 복사·수정한 뒤 실행한다.

```bash
python scripts/smc_ict_research_cycle.py \
  --config configs/research_cycle.json \
  --registry configs/candidate_registry.json \
  --attestation artifacts/latest_operational_attestation.json \
  --state artifacts/research_state.json \
  --output-dir build/research-cycle
```

출력:

- `research_state.json`: 이전 판단을 포함한 영속 상태
- `decision_ledger.csv`: cutoff별 champion/challenger 결정

동일 입력으로 재실행하면 이미 처리한 cutoff를 추가하지 않는다. 데이터가 미래 방향으로만 append되면 기존 결정이 유지되고, 과거 증거가 수정되면 실행이 중단된다.

## 승격 기준

현재 기본 연구 gate는 최소 3개 OOS cycle, 80거래, base PF 1.20, stress PF 1.00, MDD 15% 이하, 양수 cycle 60% 이상, 상위 5개 이익 기여 35% 이하, 최고 5거래 제거 후 양의 복리수익이다. live 전에는 별도로 shadow 90일·100체결, reconciliation error 0, p95 slippage 8bp 이하 등을 요구한다.

이 기준을 변경하면 새 계약 버전과 새 후보로 취급한다. 변경 전후 결과를 같은 사양처럼 합치지 않는다.
