# SMC Trading System

EasyChart의 실제 매매 논리를 중심으로 SMC/ICT의 시장 구조, 유동성, 오더블록(OB), FVG를 결합해 `BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `XRPUSDT`에서 운용 가능한 트레이딩 프로그램을 만드는 프로젝트다.

## 최종 목표

단순히 OB 발생 횟수를 세는 프로그램이 아니라, 큰 시간봉의 구조와 유동성 위치에서 시작해 낮은 시간봉의 실행 타점으로 연결되는 과정을 코드로 구현한다. 거래 기회를 억지로 만들지 않으면서 의미 있는 빈도와 비용 차감 후 양의 기대값을 함께 추구하고, 같은 전략·위험·주문 상태를 paper와 live에서 공유하는 것이 최종 목표다.

일평균 순수익 1~3%는 장기 도전 목표로 유지하지만 후보 선택, 하이퍼파라미터 탐색, 레버리지 조정의 목적함수로 사용하지 않는다. 전략 승격은 인과성, 비용 민감도, 낙폭, 기간 안정성, 수익 집중도와 실거래 재현 가능성으로 결정한다.

## 만들 결과물

- BTC·ETH·SOL·XRP 다중 시간봉 전략 엔진
- 위치·유동성 사건·OB/FVG·진입·손절·목표 판정기
- 거래소계좌와 수동 기록 은행계좌를 합친 총자산 기준 고정손실 수량 모듈
- 미체결 주문과 보유 포지션을 합친 전 종목 전역 한 슬롯 historical backtest
- 동일한 전략 코어를 사용하는 paper/live 실행 프로그램
- 은행 연결 없이 수동 입출금 지시만 생성하는 treasury 계층
- 데이터가 추가될 때 같은 인과성·워크포워드·승격 절차를 반복하는 장기 연구 사이클

## 폴더 구조

```text
SMC-trading-system/
├─ src/ictbt/easychart_v0/          전략·위험관리·체결·백테스트 코어
├─ tests/easychart_v0/              현재 전략 코어의 자동 테스트
├─ tests/research_cycle/            장기 연구·위험 선택 불변성 테스트
├─ scripts/                         버전별 비교와 연구 사이클 실행 스크립트
├─ configs/                         인과 데이터·워크포워드·자금배분 계약
├─ docs/                            장기 운용 설계 문서
├─ artifacts/strategy_composition_2026_07_18/
│                                    현행 전략 정책·수식·연구 보고서
├─ results/easychart_*/             버전별 요약·진단·거래 원장
├─ data/easychart_captions/yt_dlp/  EasyChart 연구에 사용한 영상 자막
├─ registrations/                   사용 자료 묶음과 등록 정보
├─ MAIN_AGENT_STATE.md              현재 전략·결과·다음 작업의 기준선
├─ pyproject.toml                   Python 패키지·CLI·테스트 설정
└─ README.md                        프로젝트 개요와 진입 안내
```

현재 작업을 파악할 때는 `README.md` 다음에 `MAIN_AGENT_STATE.md`를 읽고, 세부 규칙이나 결과가 필요할 때 `docs`, `artifacts`, `results`, `src` 순으로 확인하면 된다.

## 현재 상태

현재는 `RESEARCH_ONLY` 단계이며 live champion은 `CASH`다. 고정된 연구·인과성·체결·shadow gate를 모두 통과하기 전에는 실제 주문 위험률을 0으로 유지한다.

미체결 진입 주문과 보유 포지션을 합쳐 BTC·ETH·SOL·XRP 전체에서 최대 한 개만 허용한다. 수량은 거래소계좌와 수동 기록 은행계좌를 합친 총자산으로 계산하되, 거래소 용량이 부족하면 수량을 줄이지 않고 주문을 차단한다. 은행계좌는 연결하지 않는다.

장기 연구 사이클은 `CASH`를 정식 champion으로 인정한다. 어느 후보도 사전 등록 gate를 통과하지 못하면 거래하지 않으며, 새 판단은 계산 시점 이전으로 소급하지 않고 다음 deployment window부터만 적용한다.

현재 전략·결과·다음 작업은 [MAIN_AGENT_STATE.md](MAIN_AGENT_STATE.md)를 가장 먼저 따른다. 과거 문서와 충돌하면 사용자의 최신 결정, 현재 코드와 실행 결과, `MAIN_AGENT_STATE.md`, 정책·수식 문서, 과거 보고서 순으로 판단한다.

## 주요 문서

- [현재 상태 기준선](MAIN_AGENT_STATE.md)
- [장기 운용형 연구·갱신·검증 사이클](docs/LONG_LIVED_RESEARCH_CYCLE_KO.md)
- [실사용 중앙계좌 위험·오프라인 은행 계약](docs/LIVE_CAPITAL_AND_TREASURY_KO.md)
- [전략 정책 기준선](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_POLICY_DECISION_DRAFT.md)
- [OHLCV 판정 수식](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_OHLCV_FORMULA_CONTRACT.md)
- [전략 재구성 V2](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_STRATEGY_RECONSTRUCTION_V2.md)

## 설치와 확인

```powershell
python -m pip install -e .
python -m pytest tests\easychart_v0 tests\research_cycle -q
```

CLI 진입점은 다음과 같다.

```powershell
easychart-v0 --help
python -m ictbt.easychart_v0 --help
```

장기 연구 사이클은 후보 거래 원장과 운영 attestation을 연결한 뒤 실행한다.

```powershell
python scripts\smc_ict_research_cycle.py `
  --config configs\research_cycle.json `
  --registry configs\candidate_registry.json `
  --attestation artifacts\latest_operational_attestation.json `
  --state artifacts\research_state.json `
  --output-dir build\research-cycle
```

동결된 거래원장의 위험률과 자동매매계좌·은행계좌 비율은 별도 최적화한다.

```powershell
python scripts\optimize_central_account_risk.py `
  --ledger artifacts\frozen_candidate_ledger.csv `
  --policy configs\live_capital_policy.json `
  --output-dir build\capital-risk
```

이 저장소의 결과는 전략 연구 자료이며 수익을 보장하지 않는다.
