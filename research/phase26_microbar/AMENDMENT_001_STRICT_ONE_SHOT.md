# Phase26-A001 — 정확한 첫 BBO 및 단일 확인구간

상태: `PRE_OUTCOME_LOCK / RESEARCH_ONLY / NO_ORDER_AUTHORITY`

기록 시점: 2026-07-24 UTC. 기존 또는 수정 후보의 손익, 개발·검증·확인 결과를 열기 전.

## 사전 감사에서 발견한 문제

1. 초기 실행기는 확인 시점 다음 초의 **마지막** BBO를 진입·청산에 사용했다. 이는 해당 초가 끝나기 전에는 알 수 없고 실제 시장가 체결가도 아니다.
2. 모든 정책에 대해 마지막 30% 확인구간 손익을 계산하고 정렬의 보조 기준으로 사용했다. 따라서 확인구간이 one-shot holdout이 아니었다.
3. 7,290개 조합은 짧은 사건 표본에 비해 과도했다.
4. horizon보다 짧은 cooldown으로 포지션이 겹칠 수 있었다.
5. Hugging Face 복제본 revision이 고정되지 않았다.

## 고정 수정

- 데이터 revision: `1d41abbecffb7a098a8faf7d86b6481a091d6561`.
- feature는 완료된 초의 마지막 실제 BBO·depth만 사용한다.
- 결정 경계 이후 최초 실제 BBO를 진입에 사용하고, 실제 진입 timestamp+horizon 이후 최초 실제 BBO를 청산에 사용한다. 둘 다 2초를 넘으면 신호를 폐기한다.
- long은 ask 진입→bid 청산, short는 bid 진입→ask 청산이며 실제 spread를 직접 포함한다.
- 후보를 192개로 제한한다: family 2 × liquidation-z 2 × confirmation 2 × depth 2 × flow 2 × reclaim 2 × horizon 3.
- 개발 40%와 검증 30%에서만 후보를 평가한다. 개발·검증 모두 통과한 최상위 한 후보가 있을 때만 마지막 30% 확인구간을 한 번 연다.
- 확인구간 값은 후보 정렬·선택에 절대 사용하지 않는다.
- 10·15·20bp 비용은 기본 신호 집합에 숫자 비용만 추가하여 재생한다.
- 전체 사건에 전역 포지션 슬롯 1개를 적용하고 이전 거래가 끝나기 전 사건은 건너뛴다.
- 분할 경계를 넘겨 끝나는 거래는 앞 구간에서 제외한다.

## 안전 상태

- candidate PnL observed before amendment: `false`
- confirmation opened: `false`
- orders submitted: `false`
- paper/live enabled: `false`
