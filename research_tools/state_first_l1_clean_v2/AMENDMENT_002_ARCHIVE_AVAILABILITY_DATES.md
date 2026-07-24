# V3-A002 — 공식 bookTicker 보존기간에 따른 날짜 이동

상태: `PRE_OUTCOME_LOCK / RESEARCH_ONLY / NO_ORDER_AUTHORITY`

기록 시점: 2026-07-24 UTC. 후보 손익, selection, validation, one-shot test 결과는 아직 열지 않았다.

## 확인된 전송 사실

원래 train 날짜 `2023-01-03`의 Binance Vision USD-M `bookTicker` 아카이브가 HTTP 404로 존재하지 않아 패널 생성이 불가능했다. 이는 전략·수익 결과가 아니라 원천자료 가용성 실패다. 별도 공식 스키마 감사에서 `2023-06-27` BTCUSDT daily `bookTicker` ZIP과 인접 CHECKSUM의 일치가 이미 확인됐다.

## 고정 수정

분기별 역할과 순서를 유지하면서 네 날짜를 결과 관측 전에 다음과 같이 이동한다.

- train: `2023-06-27`
- selection: `2023-08-30`
- validation: `2023-11-09`
- conditional one-shot test: `2023-12-28`

BTCUSDT와 ETHUSDT는 같은 날짜를 사용한다. 어느 종목이든 필요한 공식 `bookTicker` 또는 `aggTrades` 아카이브가 없으면 그 날짜를 다른 날짜로 다시 최적화하지 않고 fail-closed한다.

## 변경하지 않는 항목

- 한 초가 완전히 끝난 뒤 신호 확정
- 250 ms 추가 지연 후 최초 실제 BBO 진입
- 최초 실제 BBO 기반 3/10/30/60초 청산
- 실제 bid/ask spread 포함
- 전역 BTC·ETH 한 슬롯
- 모델·규칙·horizon·분위수 격자
- train 날짜 예측분포로만 임계값 산출
- 12/18/24 bp에서 동일 승인 신호목록 재생
- 모든 selection·validation 게이트와 conditional one-shot test 규칙

## 안전 상태

- candidate PnL observed before amendment: `false`
- unavailable archive failure observed: `true`
- test opened: `false`
- paper/live enabled: `false`
- order authority: `none`
