# SMC Trading System

EasyChart의 공식 영상·원본 자막을 의미 기준선으로 두고, SMC/ICT의 시장 구조·유동성·오더블록(OB)·FVG를 BTC·ETH에서 실제 운용 가능한 전략 코어로 재구성하는 프로젝트다.

## 최종 목표

목표는 승률, 거래 수, 손익비 중 하나만 최대화하는 것이 아니다.

```text
의미 있는 완료 거래 빈도
× 거래당 비용 포함 양의 평균 순R
× 통제된 손실·노출
= 장기적인 계좌 성장
```

큰 시간봉의 방향과 유동성 목적지에서 시작해, 낮은 시간봉의 displacement와 OB/FVG 실행 위치로 내려온다. 진입 전에 구조적으로 타당한 손절과 목표를 고정하고, 실제 진입가–손절가의 비용 포함 손실로 현재 공유 잔고의 3%를 위험에 노출한다. 같은 전략 코어를 historical, paper, live에서 공유하는 것이 최종 형태다.

## 연구 성공 계약

기본 반복 trial은 다음을 동시에 요구한다.

```text
연도 = 2022, 2023, 2024, 2025, 2026
연도별 무작위 score 기간 = 28일
trial당 포트폴리오 운용일 = 140일
symbols = BTCUSDT + ETHUSDT
최대 open position = BTC·ETH 전체 합계 1개
거래당 위험 = 실제 주문 생성 시점 현재 equity의 3%
수수료 + 손절 slippage 포함
완료 거래 수 > 포트폴리오 운용일 수
최종 equity >= 초기 equity의 5배
```

따라서 140일 trial은 미체결·취소·거절·미해결 주문을 제외한 완료 거래가 최소 141건이어야 한다. 141건이 모두 같은 비용 포함 순R을 낸다고 단순화하면 3% 복리로 5배에 필요한 거래당 결과는 약 `+0.38266R`이다. 이는 성과 예상치가 아니라 빈도와 기대값을 동시에 만족해야 하는 난이도 기준이다.

한 번 유리한 기간만 통과하거나, 반복 trial이 같은 날짜를 많이 재사용하거나, discovery 표본을 본 뒤 정책을 바꾼 결과는 독립적인 성공 증거로 취급하지 않는다.

## EasyChart 자료 우선 원칙

- 의미 규칙은 등록된 공식 EasyChart 한국어 원본 자막 corpus와 보존 프레임 감사를 우선한다.
- compilation이나 재게시 구간은 파일 수만큼 독립 근거로 중복 계산하지 않는다.
- 일반 SMC/ICT 자료는 EasyChart 자료가 자동화 경계를 닫지 못한 부분의 용어·메커니즘 참고에만 사용한다.
- 다른 GitHub 전략의 진입 정의나 수익 주장을 이 프로젝트의 전략 권위로 가져오지 않는다.
- source가 역할을 직접 지지하는지, 여러 장면을 연결한 논리적 합성인지, 안전한 자동화를 위한 공학 선택인지 구분한다.

등록 기준선은 [`registrations/easychart_caption_corpus_2026_07.json`](registrations/easychart_caption_corpus_2026_07.json)이다.

## 현재 hardened 전략 원칙

### 1. 유동성 목적지 소유권

terminal target은 사건 전에 존재한 미소비 pivot liquidity만 소유할 수 있다. OB와 FVG는 위치·displacement·실행 footprint·반응 장애물 역할을 가진다.

진입과 먼 pivot 사이에 더 가까운 활성 pivot, 반대 OB 또는 반대 FVG가 있으면 먼 목표를 건너뛰지 않고 거래를 거절한다. terminal pivot과 1 tick 이내의 구조는 별도 장애물이 아니라 같은 위치의 confluence로 본다.

### 2. HTF 문맥을 실행 정밀도보다 먼저 평가

H1과 M15 경계가 같은 FVG에 연결돼도 M15를 먼저 선택하지 않는다. 각 경계를 H1/H4 문맥과 displacement까지 독립적으로 평가한 뒤 자격을 얻은 후보만 하나의 장면으로 중복 제거한다.

### 3. 비용 포함 목표 공간

최소 target R은 단순 가격거리 R이 아니다. 목표 수수료를 차감한 순이익을, 진입 수수료·손절 수수료·불리한 손절 slippage까지 포함한 최대손실로 나눈 순R이다.

### 4. BTC·ETH 전역 포지션 하나

포지션이 없을 때는 여러 인과적으로 유효한 지정가가 대기할 수 있다. 가장 먼저 실제 체결되는 주문 하나만 슬롯을 얻고 나머지는 교차 취소한다. 포지션 보유 중 생긴 새 authority는 억제한다.

동시 fill 우선순위는 전략 버전 이름이 아니라 pivot 목적지, 비용 포함 target R, literal overlap, 실행 zone 폭, 결정론적 ID 순으로 정한다.

### 5. 노출 상한

손절이 지나치게 가까워 3% 위험 수량이 비현실적으로 커지는 것을 막기 위해 모든 전략 가족에 공통으로 명목 노출 `equity × 8` 상한을 적용한다. 이 값은 초기 연구 경계이며 공식 EasyChart 원문 규칙으로 주장하지 않는다.

## 현재 상태

현재는 `RESEARCH_ONLY`다.

- 보존 연구 선두: `V0.3 BREAK_RETEST + V0.5`
- hardened HTF 후보: H1/H4 문맥, material displacement, pivot-owned H1/H4 목적지, first-obstacle 경로를 요구
- hardened intraday 후보: 활성 M15 위치 안의 M5 pivot sweep·reclaim, prompt MSS-owning OB/FVG, 첫 회귀, pre-event pivot 목적지, first-obstacle 경로를 연결
- 반복 random trial, trial 중복 진단, discovery/holdout 분리, 비용 포함 순R, 전역 open-position 슬롯 구현
- 아직 반복 trial에서 5배·141건·낙폭 제한을 입증하지 못했으므로 paper/live 주문 권한 없음

자세한 최신 상태는 [`V08_RESEARCH_STATE.md`](V08_RESEARCH_STATE.md)를 따른다.

## 폴더 구조

```text
SMC-trading-system/
├─ src/ictbt/easychart_v0/          전략·위험·체결·포트폴리오 연구 코어
├─ tests/easychart_v0/              전략·체결·연구 규약 자동 테스트
├─ scripts/                         데이터 취득·버전 비교·random trial 실행
├─ artifacts/strategy_composition_2026_07_18/
│                                    정책·수식·연구 보고서
├─ results/easychart_*/             버전별 요약·진단·거래 원장
├─ data/easychart_captions/yt_dlp/  EasyChart 원본 자막
├─ registrations/                   자료 corpus와 SHA 등록
├─ V08_RESEARCH_STATE.md            hardened V0.8 최신 기준선
├─ MAIN_AGENT_STATE.md              V0.7 이하 보존 결과
├─ pyproject.toml                   패키지·CLI·테스트 설정
└─ README.md                        프로젝트 진입 안내
```

## 주요 문서

- [V0.8 hardened 연구 상태](V08_RESEARCH_STATE.md)
- [V0.8 강건 성장 프로토콜](artifacts/strategy_composition_2026_07_18/EASYCHART_V08_ROBUST_GROWTH_PROTOCOL_KO.md)
- [V0.7 이하 보존 상태](MAIN_AGENT_STATE.md)
- [전략 재구성 V2](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_STRATEGY_RECONSTRUCTION_V2.md)
- [V0.5 연구 선두 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_5_LIQUIDITY_DELIVERY_COMPARISON_KO.md)
- [V0.7 비교 보고서](artifacts/strategy_composition_2026_07_18/EASYCHART_SR_FLIP_FVG_V0_7_COMPARISON_KO.md)
- [OHLCV 판정 수식](artifacts/strategy_composition_2026_07_18/EASYCHART_OB_V0_OHLCV_FORMULA_CONTRACT.md)

## 설치와 자동 테스트

```powershell
python -m pip install -e ".[dev]"
python -m pytest tests\easychart_v0 -q
```

GitHub pull request의 `Research CI`는 전체 전략 테스트, Python compile 검사, hardened runner의 `--help` import 계약을 확인한다.

## 공식 5분봉 데이터 만들기

Binance USD-M futures public archive에서 BTCUSDT·ETHUSDT 5분봉을 내려받고 제공 checksum과 정확한 5분 연속성을 검증한다.

```powershell
python scripts\download_binance_um_5m.py ^
  --start 2021-11-01 ^
  --end 2026-07-20 ^
  --output-dir data\binance_um_5m
```

`--end`는 배타적 UTC 날짜다. 완전한 월은 monthly archive, 경계 월은 daily archive를 사용한다. OHLCV 데이터는 전략 의미의 권위가 아니라 causal replay의 관측 자료다.

## hardened 반복 random trial

### Discovery

```powershell
python scripts\run_easychart_v08_hardened_random_trials.py ^
  --data-dir data\binance_um_5m ^
  --trials 20 ^
  --seed 20260720 ^
  --risk-fraction 0.03 ^
  --phase discovery ^
  --output-dir results\easychart_v08_hardened_discovery
```

Discovery는 전략 후보의 비교와 실패 원인 분석에만 사용하며, 수치가 좋아도 paper/live 승격 권한을 만들지 않는다.

### 정책 고정 Holdout

```powershell
python scripts\run_easychart_v08_hardened_random_trials.py ^
  --data-dir data\binance_um_5m ^
  --trials 20 ^
  --seed 20260817 ^
  --risk-fraction 0.03 ^
  --phase holdout ^
  --frozen-policy-sha <검증할-commit-sha> ^
  --output-dir results\easychart_v08_hardened_holdout
```

Holdout을 확인한 뒤 전략·체결·비용·우선순위를 바꾸면 그 표본은 다시 discovery가 된다.

주요 결과물:

- `summary.json`: hard gate, arm 순위, growth feasibility, trial 날짜 중복, promotion 상태
- `trial_results.csv`: trial별 공유 잔고·빈도·낙폭·슬롯 진단
- `trade_ledger.csv`: 비용 포함 완료 거래 원장
- `build_diagnostics.csv`: 전략 family별 funnel과 거절 이유
- `trial_manifests/*.json`: seed와 연도별 score/data 경계

## paper/live 전환 조건

historical 성과만으로 실제 주문 권한을 주지 않는다. 정책 고정 holdout hard gate, 미해결 상태 0, 실시간 데이터 완결성, 주문 중복 방지, 원자적 cross-cancel, 재시작 상태 복구, paper 관찰을 별도로 통과해야 한다. 그전까지 모든 결과의 권한은 `RESEARCH_ONLY`다.

이 저장소의 결과는 전략 연구 자료이며 수익을 보장하지 않는다.
