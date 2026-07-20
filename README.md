# SMC Trading System

EasyChart의 실제 매매 논리를 중심으로 SMC/ICT의 시장 구조, 유동성, 오더블록(OB), FVG를 결합해 BTC·ETH에서 운용 가능한 트레이딩 프로그램을 만드는 프로젝트다.

## 최종 목표

단순히 OB 발생 횟수를 세는 프로그램이 아니라, 큰 시간봉의 구조와 유동성 위치에서 시작해 낮은 시간봉의 실행 타점으로 연결되는 과정을 코드로 구현한다. 거래 기회를 억지로 만들지 않으면서 의미 있는 빈도와 비용 차감 후 양의 기대값을 함께 추구하고, 같은 전략·위험·주문 상태를 paper와 live에서 공유하는 것이 최종 목표다.

현재 연구 성공 조건은 다음을 동시에 요구한다.

```text
2022~2026 각 연도 무작위 28일 = trial당 140 포트폴리오 운용일
BTC·ETH 전역 공유 잔고와 pending/open 최대 한 슬롯
거래당 현재 equity의 3% 현금위험
수수료·단순 slippage 포함
완료 거래 수 > 포트폴리오 운용일 수
초기 equity 대비 최소 5배
```

140일 trial에서는 미체결·취소·거절을 제외한 완료 거래가 최소 141건이어야 한다. 한 번의 유리한 기간만 통과하는 결과는 성공으로 인정하지 않고, 같은 고정 전략을 반복해서 바뀌는 window manifest에 적용한다.

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
├─ src/ictbt/easychart_v0/          전략·위험관리·체결·포트폴리오 연구 코어
├─ tests/easychart_v0/              현재 전략 코어의 자동 테스트
├─ scripts/                         데이터 취득·버전 비교·랜덤 trial 실행
├─ artifacts/strategy_composition_2026_07_18/
│                                    현행 전략 정책·수식·연구 보고서
├─ results/easychart_*/             버전별 요약·진단·거래 원장
├─ data/easychart_captions/yt_dlp/  EasyChart 연구에 사용한 영상 자막
├─ registrations/                   사용 자료 묶음과 등록 정보
├─ V08_RESEARCH_STATE.md            V0.8 draft의 최신 결정·게이트·미입증 항목
├─ MAIN_AGENT_STATE.md              V0.7 이하 보존 전략·결과 기준선
├─ pyproject.toml                   Python 패키지·CLI·테스트 설정
└─ README.md                        프로젝트 개요와 진입 안내
```

V0.8 draft를 파악할 때는 `README.md` 다음에 `V08_RESEARCH_STATE.md`, `MAIN_AGENT_STATE.md` 순으로 읽는다. 세부 규칙이나 결과가 필요할 때 `artifacts`, `results`, `src`를 확인한다.

## 현재 상태

현재는 `RESEARCH_ONLY` 단계다. 보존 연구 선두는 `V0.3 BREAK_RETEST + V0.5` 한 슬롯 조합이다. V0.8 draft에서는 두 새 가족을 비교한다.

- HTF SR flip: H1/H4 문맥, material displacement, H1/H4 pivot liquidity 목표를 모두 요구한다.
- intraday internal liquidity delivery: 활성 M15 OB 안의 M5 pivot sweep·reclaim, prompt MSS-owning OB/FVG, 첫 회귀, pre-event pivot 목표를 연결한다.

V0.8은 아직 반복 random trial에서 5배·141건 이상을 입증하지 못했으므로 paper/live 주문 권한은 없다.

V0.8 branch에서 내용이 충돌하면 사용자의 최신 결정, 현재 코드와 실제 실행 결과, [V0.8 연구 상태 기준선](V08_RESEARCH_STATE.md), [V0.7 이하 보존 상태](MAIN_AGENT_STATE.md), 정책·수식·과거 보고서 순으로 판단한다.

## 주요 문서

- [V0.8 연구 상태 기준선](V08_RESEARCH_STATE.md)
- [V0.7 이하 보존 상태](MAIN_AGENT_STATE.md)
- [V0.8 강건 성장 프로토콜](artifacts/strategy_composition_2026_07_18/EASYCHART_V08_ROBUST_GROWTH_PROTOCOL_KO.md)
- [전략 정책 기준선](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_POLICY_DECISION_DRAFT.md)
- [OHLCV 판정 수식](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_OHLCV_FORMULA_CONTRACT.md)
- [전략 재구성 V2](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_STRATEGY_RECONSTRUCTION_V2.md)
- [V0.5 연구 선두 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_5_LIQUIDITY_DELIVERY_COMPARISON_KO.md)
- [V0.7 비교 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_SR_FLIP_FVG_V0_7_COMPARISON_KO.md)

## 설치와 테스트

```powershell
python -m pip install -e ".[dev]"
python -m pytest tests\easychart_v0 -q
```

GitHub pull request에는 `Research CI` 전체 테스트가 실행된다.

## 공식 5분봉 데이터 만들기

Binance USD-M futures public archive에서 BTCUSDT·ETHUSDT 5분봉을 내려받고 checksum과 연속성을 확인한다.

```powershell
python scripts\download_binance_um_5m.py ^
  --start 2021-11-01 ^
  --end 2026-07-20 ^
  --output-dir data\binance_um_5m
```

`--end`는 배타적 UTC 날짜다. 완전한 월은 monthly archive, 경계 월은 daily archive를 사용한다.

## 반복 random trial 실행

```powershell
python scripts\run_easychart_v08_random_trials.py ^
  --data-dir data\binance_um_5m ^
  --trials 20 ^
  --seed 20260720 ^
  --risk-fraction 0.03 ^
  --output-dir results\easychart_v08_random_trials
```

결과물:

- `summary.json`: 5배·빈도·낙폭·평균 순R gate와 arm 순위
- `trial_results.csv`: trial별 전역 잔고 결과
- `trade_ledger.csv`: 비용 포함 완료 거래 원장
- `build_diagnostics.csv`: 전략 family별 funnel

CLI 진입점은 다음과 같다.

```powershell
easychart-v0 --help
python -m ictbt.easychart_v0 --help
```

이 저장소의 결과는 전략 연구 자료이며 수익을 보장하지 않는다.
