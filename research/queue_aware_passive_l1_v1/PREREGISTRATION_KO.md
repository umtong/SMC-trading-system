# Queue-Aware Passive L1 V1 사전등록

상태: `PRE_OUTCOME_LOCK / RESEARCH_ONLY / NO_ORDER_AUTHORITY`

## 가설

시장가 방향예측의 작은 엣지는 taker 비용에 소멸했다. 새로운 경로는 방향예측을 미세조정하지 않고, 실제 BBO에 post-only로 대기하되 체결 후 음의 drift가 낮을 것으로 추정되는 상태만 선택한다. 단순 `가격 접촉=체결`을 금지하고, 표시된 선행 대기열의 두 배와 소량의 자체 수량을 실제 반대 aggressor 체결량이 소진한 이후에만 체결을 인정한다.

## 결과독립 날짜

공식 Binance Vision daily `bookTicker` 보존 시작 이후의 날짜를 후보 손익과 무관하게 고정한다.

- train: `2023-06-27`
- selection: `2023-08-30`
- validation: `2023-10-25`
- 조건부 one-shot test: `2023-12-28`

BTCUSDT·ETHUSDT를 함께 사용하며 pending order와 open position을 합쳐 전역 한 슬롯이다. Test 원자료는 selection과 validation이 모든 게이트를 동시에 통과할 때만 다운로드·평가한다.

## 정보·주문 시계

- 초 `s` feature는 `[s,s+1)`의 완료된 aggTrades와 `s+1초` 이전 마지막 BBO만 사용한다.
- 신호 known_at은 `s+1초`이다.
- 100ms 주문 지연 뒤 그 시점 이전 마지막 BBO가 500ms 이내에 갱신됐을 때만 해당 best bid/ask에 post-only 주문을 둔다.
- 선행 대기열은 주문 시점 표시수량의 `2배`로 고정한다. `3배`는 선택에 쓰지 않는 스트레스다.
- Bid 매수는 실제 seller-aggressor 체결가가 주문가 이하인 수량, ask 매도는 buyer-aggressor 체결가가 주문가 이상인 수량만 대기열 소진에 사용한다. 취소로 대기열을 줄이지 않는다.
- TTL은 3·5·10초, 체결 뒤 taker 청산 horizon은 10·30·60초다.
- 자체 수량은 표시수량의 1%와 1,000 USDT 중 작은 값이다. 청산 BBO 표시수량의 10%를 넘으면 해당 체결은 무효다.
- 실제 bid/ask를 사용하고, 별도 비용은 maker 2bp + taker 5.5bp + impact 1.5bp를 포함한 9bp, 스트레스 13·17bp다.

## 독립 전략군

1. `ABSORPTION`: 공격 flow와 반대 방향의 L1·microprice 지지가 가격진행을 흡수.
2. `PULLBACK_CONTINUATION`: 30초 flow·가격추세와 같은 방향의 book 지지 속 1초 반대 flow pullback.
3. `FLOW_FLIP`: 30초 flow와 반대 방향으로 1초 flow가 전환되고 book·microprice가 새 방향을 지지.
4. `MICROPRICE_CONTINUATION`: microprice·L1 imbalance·5초 aggressor flow가 같은 방향.

각 전략군 score의 0.99·0.995·0.999 분위 임계값은 완료된 train 날짜만으로 계산하고 이후 날짜에 그대로 적용한다.

## 선택 게이트

Selection과 validation 각각 canonical queue 2배에서:

- 주문 시도 100건 이상, 체결 50건 이상
- 9bp와 13bp 로그성장 양수
- 9bp PF 1.10 이상
- 상위 10승리 비중 40% 이하
- 상위 10승리 제거 후 양수
- MDD 10% 이하

추가로 queue 3배·13bp에서 체결 30건 이상과 양의 로그성장을 요구한다. 통과 후보 중 selection·validation의 queue2/queue3 13bp 성장 최솟값이 가장 큰 하나만 test를 한 번 연다.

Test가 통과해도 다중일·다중레짐 확장, 부분체결·취소·실제 계정 수수료·용량, paper/live 공통 OMS와 독립 재현 전에는 승격하지 않는다.
