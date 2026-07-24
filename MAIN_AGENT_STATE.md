# 메인 에이전트 상태 기준선

업데이트: `2026-07-22 KST`  
현재 단계: 장기 인과 연구·중앙계좌 위험·오프라인 treasury 계약 구현  
운용 종목: `BTCUSDT / ETHUSDT / SOLUSDT / XRPUSDT`  
현재 live champion: `CASH`  
현재 연구 challenger: `ETH midnight PDH/PDL raid reversal` 소표본  
권한 상태: `RESEARCH_ONLY / NO_PAPER_ORDER / NO_LIVE_ORDER`

## 1. 현재 최상위 판단

- 일평균 순수익 1~3%는 최종 도전 목표지만 후보 선택·위험률·레버리지의 목적함수로 사용하지 않는다.
- 현재까지 장기간·비용 후·다중 레짐에서 주전으로 승격할 전략은 없다.
- 기존 V0.3~V0.7 연구와 이후 ETH·SOL 후보는 연구 기록으로 보존하지만 현재 live 또는 공식 주전이 아니다.
- 현재 남은 ETH midnight raid 후보는 42거래뿐이고 거래 빈도도 완전 운용일당 약 `0.0196`건이므로 실거래 승격 근거가 아니다.
- 따라서 실제 배포 위험률은 `0%`, 배포 자금 상태는 `CASH`다.

## 2. 현행 권위 순서

내용이 충돌하면 다음 순서로 판단한다.

1. 사용자의 최신 결정
2. 현재 코드와 실제 재현 결과
3. 이 상태 기준선
4. `docs/LIVE_CAPITAL_AND_TREASURY_KO.md`
5. `docs/LONG_LIVED_RESEARCH_CYCLE_KO.md`
6. 버전별 과거 정책·수식·연구 보고서

과거 EasyChart 보고서는 연구 이력이며, 최신 종목·자금·위험·승격 계약보다 우선하지 않는다.

## 3. 운용 우주와 전역 슬롯

```text
allowed_symbols = BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT
pending_entry_orders + open_positions <= 1 across all symbols
```

- 미체결 진입 주문도 슬롯을 점유한다.
- 어떤 종목에서 pending 또는 open 상태가 있으면 나머지 세 종목의 신규 주문을 제출하지 않는다.
- 추가매수, 물타기, martingale, 진입 뒤 수량 증가는 금지한다.
- 거래소 REST 상태와 user-data stream을 reconciliation한 뒤에만 슬롯이 비었다고 판단한다.

## 4. 고정손실 수량 계약

정산 완료 총자산을 다음처럼 정의한다.

```text
total_equity
= settled trading-account equity
+ manually recorded settled bank-account equity

maximum_loss
= total_equity × frozen risk_fraction

configured_unit_loss
= |entry_price - stop_price|
+ entry_price × entry_fee_rate
+ stop_price × stop_fee_rate

quantity
= floor_to_exchange_step(maximum_loss / configured_unit_loss)
```

- 은행잔고는 API로 읽지 않는다. 운영자가 확인하고 기록한 정산 잔액만 사용한다.
- 아직 이동 중인 입출금은 총자산에 포함하지 않는다.
- 거래소 용량이 부족해도 수량을 몰래 줄이지 않는다.
- 용량 부족 시 주문을 차단하고 필요한 수동 입금액을 출력한다.
- 손절 slippage는 위 설정 공식에 소급 삽입하지 않고 같은 수량의 stress loss로 별도 기록한다.

## 5. 위험률 선택

기본 후보 위험률은 다음과 같다.

```text
0.25%, 0.50%, 0.75%, 1.00%, 1.25%,
1.50%, 2.00%, 2.50%, 3.00%
```

1~3%를 우선 연구 범위로 보지만, 손실 군집·용량·추정오차가 요구하면 그 아래도 허용한다. 위험률은 거래마다 바꾸지 않고 동결 전략과 다음 배포기간에 대해 고정한다.

분기 단위 block bootstrap에서 다음을 모두 통과한 후보 중 중앙 최종 계좌배수가 가장 높은 위험률을 연구상 선택한다.

```text
p95 maximum drawdown <= 15%
p05 final multiple > 1
ruin probability <= 0.1%
p99 position geometry fits venue leverage and minimum bank reserve
```

전략이 별도 연구·인과성·체결·shadow gate를 통과하지 못하면 계산상 연구 최적 위험률이 있어도 실제 배포 위험률은 0이다.

## 6. 자동매매계좌와 은행계좌

```text
bank_connector_enabled = false
bank_transfer_mode = manual_operator_action_only
```

- 자동매매계좌는 Binance USDⓈ-M 또는 Bybit linear에 연결한다.
- 은행계좌는 연결하지 않는다.
- 프로그램은 목표 거래소 잔액, 목표 은행잔액, 수동 입금·출금 권고만 만든다.
- 실제 이체 후 운영자가 새 잔액 snapshot을 확인·기록해야 한다.
- pending/open 슬롯이 있으면 treasury 이동 권고를 보류한다.

기본 연구값:

```text
provisional trading-account fraction = 60%
minimum bank fraction = 35%
rebalance hysteresis = total equity 5 percentage points
selected leverage cap = 5x
margin buffer = 25%
loss buffer = maximum loss × 2
```

한 슬롯이므로 동시에 여러 종목의 증거금을 준비하지 않는다. 동결 전략의 p99 주문 notional을 감당하는 최소 거래소 자금만 두고 나머지를 은행에 보관하는 방향으로 최적화한다.

## 7. 거래 빈도

암호화폐는 24시간 시장이므로 데이터가 완전한 달력일을 운용일로 센다.

```text
completed trades / complete operating days >= 1.0
```

위 값은 데이트레이딩 주전의 권장 기준이지 무조건적인 탈락 조건은 아니다. 낮은 빈도 전략은 독립 알파 challenger로 보존할 수 있지만 주전 점수와 중앙계좌 활용률에서 불이익을 받는다.

## 8. 현재 ETH 소표본 위험 진단

입력 후보는 ETH midnight PDH/PDL raid reversal의 1.25R·1.50R 청산 arm이다.

공통:

- 거래 수: 42
- 평가 범위: 2020-01-11 ~ 2025-11-26
- 완전 운용일: 2,147
- 빈도: 약 0.01956 거래/일
- 20,000회 분기 block bootstrap

### 1.25R arm

- 연구상 위험 후보: 1.25%
- 관측 최종배수: 약 1.2175
- 관측 최대낙폭: 약 4.90%
- bootstrap 중앙 최종배수: 약 1.1992
- bootstrap p05 최종배수: 약 1.0671
- bootstrap p95 최대낙폭: 약 5.87%

### 1.50R arm

- 연구상 위험 후보: 1.25%
- 관측 최종배수: 약 1.2657
- 관측 최대낙폭: 약 4.90%
- bootstrap 중앙 최종배수: 약 1.2415
- bootstrap p05 최종배수: 약 1.0833
- bootstrap p95 최대낙폭: 약 5.99%

두 arm에서 1.25% 위험을 사용하려면 p99 geometry 기준 거래소 약 61.87%, 은행 약 38.13%가 필요하다. 1.50% 이상은 현행 5배 레버리지·buffer·최소 은행 35% 계약에서 용량 gate를 통과하지 못한다.

이 값은 전략 승격 뒤에만 사용할 연구상 후보다. 현재 배포값은 위험률 0%, 은행 100%다.

## 9. 장기 자동 갱신 절차

- 매일: 공식 archive와 checksum 수집, 데이터 무결성, 거래소 reconciliation
- 매주: prefix/future-mutation 인과성, 비용·미체결·slippage·drift 검사
- 매월: cutoff 이전 성숙 label만으로 champion–challenger 평가
- 분기: shadow·체결·reconciliation·위험률·treasury release 검토

과거 결정은 새 데이터 때문에 소급 수정하지 않는다. 이미 성숙한 증거가 바뀌면 append-only 검증이 실행을 중단한다.

## 10. 거래소 연결 계약

매 주문 전 다음을 최신 조회한다.

- wallet equity와 available balance
- quantity step, minimum quantity, minimum notional
- 현재 symbol notional/risk tier
- 실제 계정 commission rate
- 현재 leverage 설정과 허용 leverage
- 모든 pending order와 open position

API key는 주문·포지션·계좌조회에 필요한 최소 권한만 사용하고 출금 권한은 부여하지 않는다. REST timeout을 주문 실패로 단정하지 않고 stream과 주문 상태 조회로 재확인한다.

## 11. 구현 상태

구현됨:

- 장기 append-only champion–challenger 연구 상태기계
- causal market-data 계약과 검증 테스트
- BTC/ETH/SOL/XRP 공식 USD-M archive 수집·checksum 검증 workflow
- 정확한 고정손실 수량 계산
- 전역 pending/open 한 슬롯 상태
- 거래소 용량 fail-closed 검사
- 은행 미연결 수동 treasury 지시
- 거래 빈도 soft gate
- 분기 block-bootstrap 위험·계좌배분 optimizer

아직 구현·검증이 필요한 범위:

- Binance·Bybit private adapter의 실제 주문 제출
- user-data stream과 REST 영속 reconciliation
- 부분체결·queue·latency·order-book market impact 최종 모델
- 90일 이상 shadow와 100체결 이상 운영 증거
- 거래 빈도와 2024+ 이전성을 함께 만족하는 BTC/ETH/SOL/XRP 주전 전략

## 12. 현행 문서

- [실사용 중앙계좌 위험·오프라인 은행 계약](docs/LIVE_CAPITAL_AND_TREASURY_KO.md)
- [장기 연구·갱신·검증 사이클](docs/LONG_LIVED_RESEARCH_CYCLE_KO.md)
- [중앙계좌 위험 기준선](artifacts/capital_policy_2026_07_22/CENTRAL_ACCOUNT_RISK_BASELINE_KO.md)
- [EasyChart V0.3~V0.7 과거 연구 문서](artifacts/strategy_composition_2026_07_18/)

현재 최종 상태:

```text
LIVE_CHAMPION = CASH
DEPLOYMENT_RISK_FRACTION = 0
BANK_CONNECTOR = DISABLED
GLOBAL_PENDING_PLUS_OPEN = 1
RESEARCH_CONTINUES = TRUE
```
