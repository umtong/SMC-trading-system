# State-First L1 V3 스트리밍 기술 수정

상태: `PRE_OUTCOME_LOCK / RESEARCH_ONLY / NO_ORDER_AUTHORITY`

기준 사전등록은 `research_tools/state_first_l1_clean_v2/PREREGISTRATION_KO.md`와 `AMENDMENT_001_TRAIN_ONLY_THRESHOLD.md`이다. 날짜, 종목, 특징 목록, 후보, 학습·선택·검증·조건부 테스트 순서, 250ms 지연, 실제 BBO 진입·청산, 비용, 게이트와 전역 한 슬롯은 변경하지 않는다.

V2 실행은 후보 손익을 계산하기 전에 일별 `bookTicker` 전체를 DataFrame으로 결합하는 단계에서 중단됐다. V3는 결과를 보지 않은 상태에서 데이터 운송과 메모리 구조만 다음처럼 수정한다.

- `bookTicker`를 25만 행 단위로 읽고 초별 마지막 BBO와 업데이트 수만 보존한다.
- BBO 업데이트가 없는 초도 직전 실제 BBO를 그대로 유지해 완전한 1초 상태시계를 만든다. BBO의 원래 `event_time`은 보존해 quote age를 계산한다.
- `aggTrades`는 각 청크에서 초별로 집계한 뒤 다시 결합하므로 원시 일일 체결을 전부 메모리에 보관하지 않는다.
- 진입 목표시각과 청산 목표시각은 각각 별도의 순차 패스로 원본 BBO에서 `target 이상 최초 실제 update`를 찾는다.
- 2초 이내 BBO가 없는 신호는 사전등록대로 폐기하며, 폐기 수와 비율을 manifest에 기록한다.
- 거래가 없는 초의 `trade_age_ms`는 마지막 실제 체결시각을 이어받아 계산한다.
- 모든 원본 ZIP은 인접 공식 CHECKSUM과 SHA-256이 일치해야 한다.

잠금 러너:

- uncompressed runner SHA-256: `384dc2dd9752024837c1bb95152973415996f4856a035eb8474c3ceec7272103`
- gzip SHA-256: `09b08b1c5bbdd7ee204fb177844a6b6bc32e8d0eee87628f2a13bf28afae81f2`
- base64 text SHA-256: `5348e8e7f207915f60de02cb1167809e2173e0759c794c01653b4dc7757df4c2`

후보 PnL은 이 수정 전에 관측되지 않았다. Paper/Live와 주문 권한은 계속 비활성이다.
