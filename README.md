# SMC Trading System

EasyChart의 실제 매매 논리를 중심으로 SMC/ICT의 시장 구조, 유동성, 오더블록(OB), FVG를 결합해 BTC·ETH에서 운용 가능한 트레이딩 프로그램을 만드는 프로젝트다.

## 최종 목표

단순히 OB 발생 횟수를 세는 프로그램이 아니라, 큰 시간봉의 구조와 유동성 위치에서 시작해 낮은 시간봉의 실행 타점으로 연결되는 과정을 코드로 구현한다. 거래 기회를 억지로 만들지 않으면서 의미 있는 빈도와 비용 차감 후 양의 기대값을 함께 추구하고, 같은 전략·위험·주문 상태를 paper와 live에서 공유하는 것이 최종 목표다.

## 만들 결과물

- BTC·ETH 다중 시간봉 OHLCV 전략 엔진
- 위치·유동성 사건·OB/FVG·진입·손절·목표 판정기
- 현재 잔고와 진입가–손절가 거리로 수량을 계산하는 위험관리 모듈
- 전역 단일 포지션 historical backtest
- 동일한 전략 코어를 사용하는 paper/live 실행 프로그램
- 차트, 주문 상태, 위험 설정과 거래 결과를 확인하는 사용자 인터페이스

## 폴더 구조

```text
SMC-trading-system/
├─ src/ictbt/easychart_v0/          전략·위험관리·체결·백테스트 코어
├─ tests/easychart_v0/              현재 전략 코어의 자동 테스트
├─ scripts/                         버전별 비교와 결과 분석 실행 스크립트
├─ artifacts/strategy_composition_2026_07_18/
│                                    현행 전략 정책·수식·연구 보고서
├─ results/easychart_*/             버전별 요약·진단·거래 원장
├─ data/easychart_captions/yt_dlp/  EasyChart 연구에 사용한 영상 자막
├─ registrations/                   사용 자료 묶음과 등록 정보
├─ MAIN_AGENT_STATE.md              현재 전략·결과·다음 작업의 기준선
├─ pyproject.toml                   Python 패키지·CLI·테스트 설정
└─ README.md                        프로젝트 개요와 진입 안내
```

현재 작업을 파악할 때는 `README.md` 다음에 `MAIN_AGENT_STATE.md`를 읽고, 세부 규칙이나 결과가 필요할 때 `artifacts`, `results`, `src` 순으로 확인하면 된다.

## 현재 상태

현재는 `RESEARCH_ONLY` 단계다. 연구 선두는 `V0.3 BREAK_RETEST + V0.5` 한 슬롯 조합이며, V0.6과 V0.7은 거래 빈도를 늘렸지만 기대값을 훼손해 현재 조합에서 제외했다. paper와 live 주문 권한은 아직 없다.

현재 전략·결과·다음 작업은 [MAIN_AGENT_STATE.md](MAIN_AGENT_STATE.md)를 가장 먼저 따른다. 과거 문서와 충돌하면 사용자의 최신 결정, 현재 코드와 실행 결과, `MAIN_AGENT_STATE.md`, 정책·수식 문서, 과거 보고서 순으로 판단한다.

## 주요 문서

- [현재 상태 기준선](MAIN_AGENT_STATE.md)
- [전략 정책 기준선](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_POLICY_DECISION_DRAFT.md)
- [OHLCV 판정 수식](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_OHLCV_FORMULA_CONTRACT.md)
- [전략 재구성 V2](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_STRATEGY_RECONSTRUCTION_V2.md)
- [V0.5 연구 선두 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_5_LIQUIDITY_DELIVERY_COMPARISON_KO.md)
- [V0.7 최신 비교 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_SR_FLIP_FVG_V0_7_COMPARISON_KO.md)

## 설치와 확인

```powershell
python -m pip install -e .
python -m pytest tests\easychart_v0 -q
```

CLI 진입점은 다음과 같다.

```powershell
easychart-v0 --help
python -m ictbt.easychart_v0 --help
```

이 저장소의 결과는 전략 연구 자료이며 수익을 보장하지 않는다.
