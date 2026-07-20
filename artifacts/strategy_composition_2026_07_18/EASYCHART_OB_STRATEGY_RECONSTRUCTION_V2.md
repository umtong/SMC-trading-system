# EasyChart OB 중심 전략 재구성 V2 — A1+B1 단일 confluence 개편

작성일: 2026-07-18 KST  
개편일: 2026-07-20 KST  
상태: **V0.3~V0.7 장면 구현·historical 비교 완료 — 연구 선두 유지, V0.6·V0.7 제외**  
paper/live/order/fill/execution/trade/risk/promotion authority: 모두 `false`

## 1. 이번 재구성의 결론

이번 개편의 핵심은 OB를 고립된 패턴이 아니라 **한 유동성에서 다음 유동성으로 이동하는 delivery 과정의 실행 도구**로 두는 것이다. 여러 EasyChart 장면의 호환되는 역할을 연결한 `LOGICAL_SYNTHESIS`이며, 화자가 한 번에 말한 단일 원규칙으로 주장하지 않는다.

```text
완료된 상위 구조에서 기존 A1 위치와 최초 목표 후보를 먼저 확인
→ 그 위치 안에서 같은 방향 새 5분 또는 15분 B1 OB 완료
→ 두 요소를 하나의 A1_B1_CONFLUENCE 장면으로 결합
→ 15m+5m 중첩을 우선해 진입 구역 고정
→ 새 OB 완료 뒤 중첩 구역 첫 재방문 지정가 한 개 실행
→ 형성 구조 반대편 wick 바깥 1 tick의 최초 손절가 적용
→ 하나의 최초 목표가와 1.4R/1R 규칙, 독립 거래량 신호로 익절
```

초기 구현의 진입 장면은 `A1_B1_CONFLUENCE` 하나였다. 기존 A1 OB는 위치를, 같은 방향 새 5분 또는 15분 B1 OB는 확인을 제공하며 어느 쪽도 단독 주문을 만들지 않는다. 유효한 `15m+5m` 중첩이 있으면 `1H+15m`보다 우선하며, 선택된 5분 또는 15분의 확정 pivot은 최초 목표 후보에도 포함한다. 이후 V0.3~V0.7을 별도 장면 가족으로 구현해 비교했으며, 현재 활성 주전은 아직 없고 연구 선두는 `V0.3 BREAK_RETEST + V0.5` 한 슬롯 조합이다.

프로그램의 포지션 계약은 `USER_INVARIANT + ENGINEERING_V0`다. 종목 수와 관계없이 시스템 전체에서 하나의 최초 진입, 하나의 pending intent 또는 하나의 open position만 허용하며, 진입 뒤 포지션 수량을 늘리는 상태나 주문은 존재하지 않는다. 어떤 종목에서 pending 또는 open 상태가 생기면 그 상태가 끝날 때까지 다른 종목의 신규 진입도 막는다. 이는 EasyChart의 모든 실행 행동을 복제한다는 주장이 아니라 사용자가 요구한 V0 범위다.

### 1.1 최종 목표와 작업 우선순위

이 문서의 목표는 일반적인 ICT 자동매매기를 만드는 것이 아니다. **EasyChart가 설명한 통합 투자 방식을 OB 중심으로 재구성하여, 여러 시장 환경을 포괄했을 때 비용 차감 후 양의 기대값과 통제 가능한 손실 위험을 추구하는 실제 운용 프로그램을 만드는 것**이다. 원 ICT 자료, 다른 교육 영상, 공개 GitHub 저장소는 EasyChart 전략을 정의하거나 교체할 권위를 갖지 않는다.

여러 시장 환경에서 작동한다는 것은 모든 환경에서 억지로 거래한다는 뜻이 아니다. EasyChart의 구조·위치·현금 원칙에 따라 불리하거나 불명확한 환경을 `NO_TRADE`로 거르는 것도 기대값을 만드는 전략 행동이다.

작업 우선순위는 다음과 같다.

1. EasyChart의 실제 선택·진입·무효화·목표·관리 논리를 실행 가능한 전략으로 만든다.
2. 대표적인 서로 다른 시장 환경에서 기대값을 훼손하는 장면을 찾고, EasyChart 근거 안에서 전략을 개선한다.
3. 진입 전 손실 한도와 수량 계산, stop 확대 금지, no-trade로 계좌 생존성을 확보한다.
4. 백테스트는 개선이 실제인지 판단할 수 있는 최소 신뢰 수준으로만 사용한다.
5. 충분한 후보가 생기면 동일한 실시간 런타임을 paper로 운용하고, 별도 승인 뒤 같은 런타임을 live로 전환한다.

검증 깊이는 이론적으로 가능한 최대가 아니라 **현재 결정을 신뢰할 수 있게 만드는 최소 수준**으로 제한한다. 검증 게이트, 감사 문서, 테스트 수 자체는 최종 산출물이 아니다.

### 1.2 V0.6 B+ 구현 뒤 수정된 판단

이 문서에서 제안했던 다중 시간봉 OB 중첩을 더 구체화하여 다음 B+ 장면을 실제 코드와 84종목일 비교로 확인했다.

```text
M15 OB 마지막 방향봉의 직접 M15 구조 돌파
→ 같은 방향 H1 또는 M5 OB 몸통 중첩
→ 구역 이탈 뒤 첫 회귀 지정가
```

같은 83개 장면에서 M15 anchor 형성극단 손절과 보호 M15 swing 손절을 비교했다. 실제 거래는 두 arm 모두 5건이었고 각각 `-2.438R`, `-2.528R`이었다. 기존 연구 선두에 더하면 거래는 13건에서 18건으로 늘지만 `+2.049R`이 각각 `-0.389R`, `-0.479R`로 바뀌었다.

따라서 이 B+ 장면은 활성 confluence에 추가하지 않는다. 이번 결과가 뜻하는 바는 `OB 중첩을 사용하지 않는다`가 아니라, **중첩과 구조 돌파만으로는 어떤 유동성 상황의 OB인지 충분히 구분되지 않았다**는 것이다. 다음 빈도 보완 장면은 위치·유동성 제거·displacement·OB/FVG·목표를 하나의 사건으로 연결해야 한다. 상세 수식과 실제 다섯 거래는 [V0.6 B+ 구현 및 손절 비교](EASYCHART_OB_V0_6_OWNED_M15_OVERLAP_STOP_COMPARISON_KO.md)를 따른다.

### 1.3 V0.7 SR Flip+FVG 구현 뒤 수정된 판단

V0.6에서 부족했던 유동성 문맥을 보완하기 위해 다음 장면을 별도 `SR_FLIP_FVG` 가족으로 구현했다.

```text
사건 전에 확정된 H1 또는 M15 경계
→ M15 FVG 중앙 B봉이 경계를 방향성 있게 돌파
→ C봉이 돌파 상태를 유지해 수용 확인
→ 경계가 FVG 안에 있을 때 한 장면으로 연결
→ 경계 첫 회귀 지정가 또는 실제 다음 M5 시가
```

BTC·ETH 여섯 구간을 한 잔고·한 포지션 슬롯으로 연결한 84종목일·70포트폴리오일 결과에서 지정가 단독은 19건 `-1.598R`, 다음 시가 단독은 31건 `-1.090R`이었다. 다음 시가를 연구 선두와 합치면 거래는 13건에서 44건으로 늘었지만 수익률은 `+6.14%`에서 `+2.37%`로, 최대낙폭은 `3.00%`에서 `7.09%`로 나빠졌다. 완료된 선두 거래가 밀린 것이 아니라 음의 V0.7 손익이 그대로 추가된 결과다.

다음 시가 arm은 승률이 80.65%였어도 평균 승리 `+0.196R`, 평균 손실 약 `-1R`이었다. 특히 목표별 합계가 pivot `+0.934R`, 반대 OB `+0.121R`, 반대 FVG `-2.144R`로 갈렸다. 따라서 V0.7을 활성 조합에 넣지 않는다. 다음 설계는 모든 가까운 반대 FVG를 같은 terminal target으로 보지 않고 독립 유동성 목적지 소유 조건을 구체화하며, H1·H4 문맥이 실제 선택을 가르도록 해야 한다. 상세 내용은 [V0.7 구현 및 전역 비교](EASYCHART_SR_FLIP_FVG_V0_7_COMPARISON_KO.md)를 따른다.

## 2. 증거 범위와 수용 방식

### 2.1 자료 custody

- 의미 기준선은 등록된 `EASYCHART_ACTIVE_18_2026_07`의 18개 한국어 자막이다. 현재 파일 18개는 등록 SHA-256과 모두 일치한다.
- registration에 기록된 known exact-fragment 재수록 edge는 독립 근거로 중복 계산하지 않았다. 이 목록이 모든 의미상 재수록을 발견했다는 주장은 하지 않으며, `GGF`, `xlYu`, `Zoz` 같은 compilation도 파일 수 자체로 독립성을 부여하지 않는다.
- 사용자가 제시한 `gcrJXbmNWFY`, `dHZNSbF32eA`는 별도 보충 source다. 두 자료는 OB 단독화의 공백을 잘 보여 주지만 EasyChart 원전이나 성과 증거는 아니다.
- 다른 ICT 자동매매 저장소와 일반 백테스트 엔진은 pending order, 취소, 상태 복구 같은 **공학 구현 가능성**만 참고한다. 그 저장소의 OB 정의, 진입 순서, 대기 봉 수, 수익 주장을 EasyChart 규칙으로 가져오지 않는다.
- 자막은 단어 검색 결과가 아니라 관련 전후 문맥을 다시 읽었다. 선택 경계·시간 순서가 중요한 장면은 보존된 프레임 감사와 대조했다.

### 2.2 문장 분류

이 문서의 모든 규칙은 다음 네 종류 중 하나로 취급한다.

| 표지 | 의미 |
|---|---|
| `SOURCE_DIRECT` | EasyChart 자막·프레임에서 해당 역할이나 행동을 직접 확인 |
| `LOGICAL_SYNTHESIS` | 여러 독립 장면의 호환되는 역할을 연결한 전략 논리 |
| `ENGINEERING_V0` | 자동화·재현성·안전을 위해 우리가 의도적으로 고른 구현 선택 |
| `UNRESOLVED` | 주문을 바꿀 수 있으나 source만으로 하나로 닫히지 않은 부분 |

`세력이 주문을 회수한다`, `개인 참여자가 꼬인다`와 같은 내부 행위자 설명은 사실로 측정할 수 없다. 그러나 관찰 가능한 유동성 집중, sweep, 반응, displacement를 설명하는 합리적 메커니즘 가설이라면 버리지 않는다. 프로그램 predicate로는 관찰 가능한 가격·완료봉·구조 상태만 사용한다.

반익절 뒤 손실이 없다는 설명은 `부분 체결 뒤 잔량 stop을 본절로 이동`한 장면으로 이해한다. V0에서는 이를 모든 거래에 확대하지 않고, 실제 목표 거리가 `1.4R 이상`인 거래가 `1R` 반익절에 실제 체결됐을 때만 적용한다.

### 2.3 시간·의사결정 비용

시간과 구현 비용은 명시적인 프로젝트 제약이다. 모든 작업은 시작 전에 다음 네 항목을 적는다.

```text
decision_changed   이 작업 결과가 바꿀 구체적 결정
minimum_evidence   그 결정을 내리기에 충분한 최소 증거
stop_condition     추가 작업의 정보 가치가 낮아지는 중단 기준
program_output     최종 프로그램에 추가되거나 바뀌는 산출물
```

결과가 달라도 다음 행동이 바뀌지 않는 테스트, 이미 닫힌 의미의 반복 감사, 모든 표본의 수동 차트화, 필요 없는 전기간·전조합 검사, 호가 단위 정밀화는 수행하지 않는다. 한 결정을 닫기에 충분하면 다음 전략 제작 단계로 이동한다.

### 2.4 개발 범위 원칙

운영자와 개발 에이전트가 합의된 전략을 정상적으로 구현하고 운용한다는 전제를 둔다. 이 전제를 감시하기 위한 준수 검사기, 전용 감사 로그, 방지 게이트와 추가 테스트는 만들지 않는다. 해당 체계는 거래 논리나 기대값을 개선하지 않으면서 구현 시간과 의사결정 비용을 늘릴 수 있기 때문이다.

완료봉과 다음 실행 시점은 실제 주문이 활성화되는 시계를 정의하는 전략 규칙으로만 사용한다. 개발 자원은 EasyChart 기반 selector·진입·반익절·전량익절·수량·실시간 런타임과 시장환경별 기대값 개선에 우선 투입한다.

## 3. 자막이 직접 지지하는 공통 문법

### 3.1 구조와 방향

- `xlYu 01:51:48–01:52:34`: 포지션을 먼저 정하고 구조를 찾지 않는다. 구조를 먼저 찾고, 아무것도 없거나 양방향 구조가 함께 보이면 현금 상태를 유지한다. `SOURCE_DIRECT`
- `V3 00:48–01:47`: 월·주·일봉은 큰 추세, 12H·4H·1H는 중간 추세·패턴·지지저항, 15m·5m·1m은 실제 진입에 사용한다. `SOURCE_DIRECT`
- `xlYu 01:17:28–01:18:17`: 낮은 시간봉의 작은 반대 OB가 있어도 더 큰 구조가 우세한 한 장면이 있다. 모든 반대 OB가 자동 veto는 아니다. `SOURCE_DIRECT`
- `xlYu 02:12:31–02:13:55`: 관점이 무효화되면 작도를 지우고 상위부터 다시 분석해 기존 방향 편향을 끊는다. `SOURCE_DIRECT`

따라서 방향은 `OB가 bullish/bearish인가` 또는 `현재 가격이 주요 고점·저점에 닿았는가`만으로 정하지 않는다. 구조와 최초 목표 후보를 먼저 확인하고, 위치 도달 뒤 반전 사건인지 돌파 지속 사건인지 구별한 다음 그 사건과 같은 방향의 displacement·OB를 실행 근거로 사용한다. exact sweep·reclaim·break·acceptance 수식은 `ENGINEERING_V0`로 닫는다.

### 3.2 의미 있는 위치

- `HReT 02:41–04:24`: 주요 저점, 깨진 이전 지지, 손절이 모일 가격을 먼저 표시한다. 유동성 위치 하나만으로 진입하지 않고 그곳의 OB를 두 번째 근거로 본다.
- `F3 04:39–04:58`: OB 구역으로 돌아올 때 거래하며, 유동성 흡수나 다른 구조 위치와 겹치면 더 의미 있게 본다.
- `GGF 01:47–02:34`: 중요 고점·저점 또는 FVG 내부의 OB를 우선한다.
- `V3 04:14–05:38`: 월봉 OB, 주봉 FVG, 일봉 OB 중첩을 중요한 지지 위치로 사용한다.
- `V3 06:42–07:27`: 추세선 돌파·리테스트 과정의 15분 이중장악형 OB를 거래 위치로 사용한다.
- `HiH 10:22–10:41`, `12:18–12:37`: OB 하나만으로 부족하며 FVG·추세선 등 독립 역할의 근거가 겹치는 장면을 설명한다.

반대로 `xlYu 01:51:02–01:51:45`는 하락 뒤 횡보에서 새 bullish OB가 생겨 앞선 bearish 효과가 상쇄되는 한 장면을 설명한다. exact 횡보 detector는 source가 주지 않는다. V0는 기존 OB가 현재 delivery 안에서 진입 위치 역할을 갖고, 그 안에서 같은 방향 새 LTF OB가 확인될 때만 confluence를 만든다.

### 3.3 OB 형태와 진입 시계

EasyChart 자막에는 최소 두 형태가 있다.

```text
SIMPLE_2C
  반대색 P의 몸통을 E의 몸통이 감싸고 E가 방향을 정함

DOUBLE_3C
  C2가 C1을 감싸고 C3가 다시 C2를 감쌈
  C1과 C3는 최종 방향의 같은 색, C2는 반대색
  가운데 C2 몸통이 OB zone
  stop extreme은 C1–C3 전체에서 방향 반대쪽의 가장 먼 wick
  최초 stop은 그 extreme보다 LONG 1 tick 아래, SHORT 1 tick 위
```

`SIMPLE_2C` 근거는 `F3 02:30–02:41`이다. `DOUBLE_3C`의 두 번 장악 관계는 `HiH 03:54–04:10`, `V3 06:55–07:10` 자막에서 확인되고, `Cx 08:26–08:50`에는 하락형 반대 사례가 있다. `V3 07:00`과 `Cx` 장면의 실제 표시 구역은 가운데 `C2` body와 일치한다. `F3 05:04–05:10`과 두 사례의 stop 설명은 3봉 전체 방향 반대쪽 wick extreme을 구조 무효화 기준으로 쓰는 것을 지지한다. 실제 주문 stop을 그 바깥 `1 tick`으로 두는 것과 non-doji·body 동가 포함·`close(C3)` 뒤 활성은 `ENGINEERING_V0` 경계다.

최초 진입 시계도 하나가 아니다.

- 기존 OB 또는 새 OB zone으로 나중에 되돌아와 진입: `xlYu 02:03–02:14`, `V3 14:12–15:42`, `F3 04:39–04:59`, `-Tp 05:42–09:06`.
- 새 15분 OB의 마감이 최초 진입 window를 여는 장면: `cZq 03:13–04:14`, 복기 `12:17–12:30`; exact 첫 실행·체결 시계는 닫히지 않았다.
- `V3 06:42–07:27`은 추세선 retest와 15분 이중장악형을 진입 근거로 쓰지만, 완성 뒤 별도 body-zone 재방문인지는 자막만으로 닫히지 않아 `ENTRY_CLOCK_UNRESOLVED`다.
- 5분 bearish OB를 최초 숏 위치로 사용: `Cx 08:26–09:05`, 복기 `13:13–13:23`.
- OB 접촉 뒤 지지 반응을 더 보고 진입: `UmEP 08:08–08:30`, 복기 `11:17–11:38`; 확인봉 predicate는 미결이다.
- 진행 중 1분 OB를 예상해 진입한 반례: `SMt 10:45–10:55`.

따라서 `완료봉만`, `첫 되돌림만`, `strict-cross 봉을 포함한 인접 2봉만`은 EasyChart 전체의 원규칙이 아니다. 특히 기존 초안의 `P/E 또는 다음 한 봉` 창은 HReT·F3의 한 도해와 맞지만 source-wide 규칙이 아니라 좁은 공학 leaf다.

자동 프로그램 V0에서는 주문 신호의 활성 시점을 통일하기 위해 `COMPLETED_BAR_ONLY=true`를 유지한다. 이는 `ENGINEERING_V0`이며, EasyChart가 항상 완료봉만 사용했다는 주장이 아니다.

### 3.4 관리 역할

- 직전 파동 고점·저점 또는 유동성을 목표로 사용: `F3 05:42–06:14`, `-Tp 07:51–08:06`, `V3 12:21–12:27`, `Cx 09:16–10:00`.
- 목표에서 일부를 실제 실현한 뒤 잔량을 본절로 이동하는 장면이 반복됨: 위 구간들. 다만 F3는 목표에서 전량 종료도 선택지로 제시한다.
- `UmEP 07:36–08:06`: 거래량을 동반한 매물대 돌파와 FVG 형성을 되돌림 롱의 지속 근거로 사용한다.
- `Cx 07:07–07:59`, `cZq 08:13–08:27`: 수익 진행 뒤 거래량 급증을 보고 예정한 더 먼 목표보다 먼저 전량익절한다.
- `SMt 08:19–08:56`: 사전에 정한 이전 저점 목표가 갱신됐지만 지정가가 체결되지 않자 거래량과 급반등 가능성을 함께 보고 시장가로 전량익절한다.

첫 V0는 이 자료를 가장 단순한 실행 규칙으로 닫는다. 진입 전에 가장 가까운 유효 구조 하나를 최초 목표로 정하고, 목표 거리가 실제 체결가 기준 `1.4R 이상`일 때만 `1R`에서 50%를 반익절한 뒤 잔량을 최초 목표까지 보유한다. 반대 5분·15분 OB의 별도 조기 종료 규칙은 만들지 않는다. 거래량은 돌파 지속 분기가 아니라 아래의 비용 포함 수익 상태 `RVOL` 전량익절에만 사용한다.

## 4. 공통 상태 모델

### 4.1 구조·유동성 사건·방향과 시장 상태

```text
DIRECTION_STATE =
  LONG_ALLOWED
  SHORT_ALLOWED
  NEUTRAL
  CONFLICT

MARKET_STATE =
  DIRECTIONAL
  RANGE_CONFLICT
  TRANSITION
  UNRESOLVED

LIQUIDITY_EVENT_STATE =
  APPROACHING
  TOUCHED_AMBIGUOUS
  SWEPT_RECLAIMED
  BROKEN_PENDING_RETEST
  BROKEN_ACCEPTED
```

- `NEUTRAL`, `CONFLICT`, `RANGE_CONFLICT`, `TRANSITION`, `UNRESOLVED`에서는 진입하지 않는다. `TOUCHED_AMBIGUOUS`와 `BROKEN_PENDING_RETEST`도 아직 완료된 B1 사건이 아니므로 주문하지 않는다. `SWEPT_RECLAIMED` 또는 `BROKEN_ACCEPTED`가 같은 방향 새 LTF OB 형성에 포함될 때만 B1 후보가 되며, 그 결합도 A1 위치 없이는 주문하지 않는다.
- 주요 고점·저점 위치 자체에는 방향을 배정하지 않는다. `SWEPT_RECLAIMED`와 `BROKEN_ACCEPTED`는 새 확인 OB와 결합돼야 하며, 사건과 OB 중 어느 하나도 단독 진입 신호가 아니다.
- 기존 A1 위치는 현재 상위 구조·delivery와 유효한 최초 목표 안에서 진입 역할을 가져야 하고, 그 안에서 같은 방향 새 LTF OB가 완료되어야 주문이 생긴다.
- 작은 반대 OB는 무조건 방향을 뒤집지 않는다. 반대 객체의 시간봉, 역할, 진입에서 목표까지의 위치를 함께 본다.
- 같은 우선순위의 상·하방 구조가 활성 위치에서 충돌하면 억지 점수로 승자를 만들지 않는다.

현재 자동 direction selector는 `현재 delivery의 기존 A1 위치 + 완료된 1H 유동성 사건과 같은 방향 새 5m 또는 15m OB의 B1 결합 + 유효한 initial_target`으로 닫는다. 4H·1H의 동급 또는 상위 반대 구조는 가장 가까운 목표 후보 또는 진입 경로 장벽으로 처리한다. 그 proximal 가격이 비용 포함 양의 순익을 만들지 못하면 `CONFLICT`로 두어 진입하지 않는다. 15m·5m의 작은 반대 OB 하나만으로 방향을 뒤집거나 자동 veto하지 않는다. exact 중첩과 허용 시계는 `EASYCHART_OB_V0_OHLCV_FORMULA_CONTRACT.md`로 고정됐다.

| 상태 층 | source가 직접 지지하는 범위 | 현재 프로그램 지위 |
|---|---|---|
| direction·market state | 구조 먼저, 양방향·무구조면 현금 | enum과 event-direction 결합은 `LOGICAL_SYNTHESIS + USER_DECISION`; exact OHLCV 수식은 `ENGINEERING_V0_FROZEN` |
| liquidity node·event | 주요 고저점, sweep·반응, 돌파·리테스트 개별 사례 | 1H 5봉 pivot과 sweep/reclaim·break/acceptance 중 하나가 B1에 필수지만, 사건만의 주문 권위는 없음 |
| OB form | 2봉·3봉 관계 | exact OHLC 동가·doji·zone·formation extreme은 `ENGINEERING_V0_FROZEN` |
| OB lifecycle·role | touch·break·flip·entry·profit exit의 개별 장면 | 상태 전이와 역할 배정은 `LOGICAL_SYNTHESIS + ENGINEERING_V0` |
| A1+B1 단일 confluence와 intent | 기존 위치와 새 LTF 확인을 함께 보는 장면 | 결합 taxonomy는 synthesis, 단일 intent는 `USER_INVARIANT + ENGINEERING_V0` |

### 4.2 유동성·delivery·POI 객체

```text
LOCATION_TYPE =
  PREEXISTING_OB_CLUSTER
  MAJOR_HIGH_LOW_LIQUIDITY
  HTF_OB_OR_FVG_OVERLAP
  SUPPORT_RESISTANCE_OR_FLIP
  TRENDLINE_OR_CHANNEL_RETEST
  MAJOR_PATTERN_BOUNDARY

DELIVERY_INTENT =
  source_event
  trade_side
  displacement_leg
  entry_poi
  invalidation
  initial_target

POI_ROLE =
  ENTRY_ORIGIN
  RETEST_ENTRY
  SUPPORT_OR_RESISTANCE
  BARRIER
  TARGET
```

`OB-B`의 최소 논리 조건은 `독립 liquidity node → 완료 event → 같은 방향 displacement와 새 실행 OB → initial_target`이다. `OB-A`에서는 기존 OB cluster가 현재 delivery 안에서 `ENTRY_ORIGIN` 또는 `RETEST_ENTRY` 역할을 가질 때만 entry authority가 된다. 같은 displacement에서 함께 생긴 OB와 FVG는 하나의 결합 구조로 연결하되 각 객체의 역할은 따로 기록한다.

첫 자동 구현에서 추세선·채널을 무시한다는 뜻은 아니다. 다만 anchor 선택과 재작도 규칙이 닫히기 전에는 수동 해석을 코드 predicate로 가장하지 않는다. 위치 type은 보존하되 해당 selector가 닫히기 전까지 `UNRESOLVED`로 거절한다.

### 4.3 OB 객체

```text
OB_FORM =
  SIMPLE_2C
  DOUBLE_3C

OB_LIFECYCLE =
  FORMING
  ACTIVE
  TOUCHED
  BROKEN
  FLIPPED
  INVALIDATED

OB_ROLE =
  ENTRY_ORIGIN
  RETEST_ENTRY
  SUPPORT_OR_RESISTANCE
  BARRIER
  TARGET
```

- V0에서 `FORMING`은 관찰만 하고 주문 권위를 갖지 않는다.
- `BROKEN → FLIPPED`는 EasyChart 자막에 존재하지만 exact close/wick·재접촉 predicate는 별도 공학 선택이 필요하다(`xlYu 00:44:16–00:47:24`).
- `TOUCHED`가 곧 무효는 아니다. `xlYu 02:02:14–02:02:25`에는 생성 직후 한 번 접촉한 12H OB를 최근 형성됐다는 이유로 여전히 유효하게 보는 장면이 있어 lifetime first-touch만 허용하는 보편 source 규칙은 성립하지 않는다.
- 한 OB가 어떤 scene에서 entry인지 exit인지 역할을 상태 모델에서 먼저 정한다.

5분봉은 하나의 역할로 뭉치지 않는다.

```text
5M_ENTRY_ROLE =
  UNUSED
  INITIAL_LOCATION
  INITIAL_TRIGGER

5M_TARGET_ROLE =
  UNUSED
  CONFIRMED_PIVOT_CANDIDATE
```

### 4.4 백테스트·paper·live 계층 경계

세 계층은 다음처럼 구분한다.

```text
BACKTEST
  과거 OHLCV를 순차 재생하는 전략 연구 도구
  EasyChart 규칙의 기대값과 실패 환경을 조사

PAPER
  실시간 데이터와 공통 운용 런타임을 사용하는 가상자금 실행 모드

LIVE
  PAPER와 같은 전략·risk·주문·복구 런타임을 사용하는 실제자금 실행 모드
```

백테스트에는 다음을 구현하지 않는다. 이는 후순위가 아니라 **영구 범위 제외**다.

- L2/L3 호가장 재구성
- 주문 대기열 순번 또는 체결 확률 추정
- 호가 기반 부분체결 시뮬레이션
- 밀리초 단위 지연과 시장충격 모델

백테스트는 필요할 때 더 낮은 시간봉의 OHLCV를 사용할 수 있지만, EasyChart 시간봉 역할이나 같은 봉 사건 순서를 판단하는 데 실제 결정 가치가 있을 때만 사용한다. 체결은 사전에 고정한 단순 전량 체결·미체결 규칙으로 처리하고 수수료, 단순 spread/slippage, gap, 동시 사건의 불리한 경로는 보존한다.

paper와 live는 별도 전략 프로그램으로 만들지 않는다. 실시간 시세 수신, 전략 판단, 진입 전 risk 계산, 주문 상태, 취소, stop/target, 재시작 복구, ledger는 동일해야 한다. 실행 계좌·자격증명·실제 자금 사용 여부만 adapter와 권한으로 분리한다. 모드 전환은 미체결 주문과 보유 포지션이 없는 안전 상태에서만 허용한다.

현재 `paper/live/order/fill authority=false`는 아직 해당 런타임을 실행하거나 승인하지 않았다는 현재 상태이지, paper와 live를 서로 다른 제품으로 만들겠다는 뜻이 아니다.

## 5. 최초 진입 장면 `A1_B1_CONFLUENCE`

### 5.1 기존 A1 위치

기존 A1 OB는 현재 상위 구조와 delivery 안에서 `ENTRY_ORIGIN` 또는 `RETEST_ENTRY` 역할을 가진다. `xlYu 00:01:07–02:14`의 완성된 상위 OB 중첩과 `V3 14:12–15:42`, `17:13–18:43`의 1H·15m 되돌림 장면은 이미 존재하는 OB가 진입 위치를 제공한다는 점을 지지한다. 그러나 V0에서는 위치만 선택됐다는 이유로 즉시 지정가를 만들지 않는다.

### 5.2 새 B1 확인 OB

기존 위치 안에서 같은 방향 `5m` 또는 `15m` `SIMPLE_2C`·`DOUBLE_3C` OB가 새로 완료되어야 한다. `cZq 03:13–04:14`, `urzm 07:16–07:38`은 완료 확인 뒤 진입 window가 열린다는 점을, `Cx 08:26–09:05`는 5분 bearish 이중장악 OB가 위치·side·stop 역할을 한다는 점을 지지한다. 새 OB도 단독 시장가 신호가 아니라 기존 위치를 확인하고 좁히는 구성요소다.

### 5.3 결합과 실행

```text
완료된 상위 구조와 진행 방향의 유효 initial_target
→ 현재 delivery 안의 기존 A1 위치 선택
→ 그 위치 안에서 같은 방향 새 5m 또는 15m B1 OB 완료
→ 두 body zone의 교집합 폭 >= 1 tick
→ A1_B1_CONFLUENCE 한 장면으로 고정
→ 새 OB 완료 뒤 중첩 구역 첫 재방문 지정가
→ 최초 진입
```

- 유효한 `1H+15m`과 `15m+5m` 중첩이 함께 존재하면 더 좁은 `15m+5m` 교집합을 우선한다.
- LONG 지정가는 선택 교집합 상단, SHORT 지정가는 하단이다. 새 OB 완료 다음 합법 시점부터 첫 재방문만 체결 기회로 사용한다.
- 최초 stop은 선택된 기존 위치 OB와 새 확인 OB의 모든 형성봉 중 방향 반대쪽 가장 먼 wick보다 LONG은 `1 tick` 아래, SHORT은 `1 tick` 위다.
- 기존 위치와 새 OB의 방향이 다르거나 교집합 폭이 `1 tick` 미만이면 `DIRECTION_CONFLICT / NO_TRADE`다.
- strict sweep·reclaim인 `SWEEP_RECLAIM` 또는 break·첫 retest acceptance인 `BREAK_ACCEPTANCE` 중 하나가 새 확인 OB에 반드시 연결된다. 사건 이름은 주문을 추가하거나 진입 시계를 바꾸지 않지만 B1 성립에는 필수다.

## 6. 공통 no-trade와 충돌 처리

다음은 첫 프로그램의 공통 거절 상태다.

| 거절 상태 | 의미 | 근거 성격 |
|---|---|---|
| `NO_DIRECTION` | 기존 A1 위치와 새 LTF OB가 같은 방향을 만들지 못함 | source 역할 직접, exact gate는 공학 |
| `NO_NEW_LTF_CONFIRMATION` | 기존 위치는 있으나 같은 방향 새 5분·15분 OB가 아직 완료되지 않음 | `LOGICAL_SYNTHESIS + USER_DECISION` |
| `NO_VALID_INTERSECTION` | 기존 위치와 새 OB의 body zone 교집합 폭이 1 tick 미만 | `ENGINEERING_V0` |
| `DIRECTION_CONFLICT` | 기존 위치와 새 LTF OB 방향이 충돌 | source 역할 직접, exact 우선순위는 공학 |
| `RANGE_OB_NOISE` | 횡보에서 상·하방 OB 효과가 상쇄되어 방향 구조가 불명 | 한 source 장면 + exact detector는 공학 |
| `ISOLATED_OB` | delivery 역할의 기존 위치 또는 새 LTF 확인 OB 중 하나만 존재 | `LOGICAL_SYNTHESIS + ENGINEERING_V0` |
| `INCOMPLETE_CONFIRMATION` | 새 LTF 확인 OB가 아직 완료되지 않음 | `ENGINEERING_V0` |
| `OPPOSING_ZONE_BLOCKS_PATH` | 가장 가까운 동급·상위 반대 zone이 비용 포함 양의 최초 목표가가 될 수 없음 | 고정 `ENGINEERING_V0` |
| `TARGET_SPACE_UNRESOLVED` | 진행 방향에서 가장 가까운 최초 목표 후보를 하나로 고를 수 없음 | source 경고 + exact 수치는 공학 |
| `MISSED_ENTRY_NO_CHASE` | 동결한 entry mode의 가격을 주지 않고 진행 | 특정 source 역할 + 범용 gate는 공학 |
| `UNRESOLVED_GEOMETRY` | trendline·flip·중첩 경계를 재현할 수 없음 | 안전 거절 |

반대 HTF OB는 먼저 가장 가까운 최초 목표 후보로 평가한다.

```text
진입 zone과 겹치거나 비용 포함 양의 목표가가 될 수 없음 → BLOCK
진행 방향 앞의 유효한 기존 zone                          → TARGET 후보
진입 뒤 새로 완료된 반대 OB                              → 관찰만 하고 V0 주문은 변경하지 않음
더 작은 반대 구조이고 큰 구조가 우세함                   → subordinate 후보
```

정확한 거리와 시간봉 우선순위는 수식 계약으로 고정했다. 기존 `15m·1H·4H` 반대 zone은 최초 목표 후보가 될 수 있고, 가장 가까운 후보가 비용 포함 양의 순익을 만들지 못하면 no-trade다. 진입 뒤 새 5분·15분 반대 OB는 V0의 stop이나 목표를 변경하지 않는다.
같은 가격 사건의 중복 권위는 주문 전에 막는다.

```text
scene_id
anchor_location_id
confirmation_ob_id
entry_authority = A1_B1_CONFLUENCE
entry_mode = LIMIT_RETURN
intent_created_at
pending_intent_id
```

- 현재 delivery에서 진입 역할이 확인된 기존 A1 위치와 `1H 유동성 사건 + 같은 방향 새 LTF OB` B1이 함께 하나의 entry authority다.
- 같은 `scene_id + anchor_location_id + confirmation_ob_id`에는 confluence 지정가 intent 하나만 허용한다.
- 종목 전체를 합쳐 pending intent 또는 open position을 최대 하나만 허용한다.
- position이 열려 있는 동안 새 intent는 주문 권위 없이 관찰만 한다. `CLOSED` 뒤 다음 완료봉의 새 `scene_id`부터 다시 arm할 수 있으며 같은 사건의 즉시 반전 주문은 만들지 않는다.
- `OPEN_CENSORED`는 해당 bounded replay에서 계속 open으로 남아 같은 symbol의 후속 intent를 차단한다.

이 routing의 단일 intent·단일 position, confluence scene ownership과 `scene_id` 재무장 시계는 수식 계약으로 고정됐다.

### 6.1 지정가 pending 계약

지정가를 사용하는 entry arm은 다음 상태를 명시적으로 가진다.

```text
PENDING_ENTRY
  → FILLED
  → CANCELLED_BY_INVALIDATION
  → CANCELLED_BY_ENTRY_WINDOW_END
  → REJECTED
```

- 미체결 entry는 거래가 아니며 승·패로 계산하지 않고 fill-rate 분모에 남긴다.
- 지정가가 체결되지 않았다는 이유만으로 시장가 추격 주문을 만들지 않는다. 새 EasyChart setup이 성립하면 새 `scene_id`와 새 intent로 시작한다.
- 구조 무효화나 해당 entry window 종료가 발생하면 즉시 취소한다. 고정 봉 수 만료는 사용하지 않는다.
- pending 취소는 이미 열린 position의 시간 강제청산과 다르다.
- 진입 주문은 새 확인 OB 완료 뒤 중첩 구역 재방문 지정가 하나뿐이다. 완료 직후 시장가 진입이나 별도 진입 경로는 만들지 않는다.

백테스트에서는 OHLCV에 기반한 단순 전량 체결 또는 미체결만 사용한다. 부분체결 상태는 백테스트 결과를 정밀화하기 위해 모사하지 않는다. paper/live 공통 주문관리기는 같은 상태 인터페이스로 실제 또는 paper executor의 주문 응답을 처리해야 한다.

## 7. stop·target·관리

### 7.1 최초 stop

- 선택된 기존 A1 위치 OB와 새 LTF B1 확인 OB의 모든 형성 캔들을 하나의 stop 구조로 사용한다.
- LONG은 그 전체의 가장 낮은 wick보다 `1 tick` 아래, SHORT은 가장 높은 wick보다 `1 tick` 위를 `initial_stop`으로 고정한다.
- 진입 뒤 반익절 체결 전에는 stop을 바꾸지 않고, 불리한 방향으로 넓히지 않는다.

### 7.2 진입 전 위험 예산과 주문 수량

entry와 stop이 확정되지 않으면 주문 수량을 만들지 않는다. 공통 메커니즘은 다음과 같다.

```text
risk_budget = current_equity × user_risk_fraction
risk_per_unit = abs(entry_price - stop_price) × contract_value_adjustment
raw_quantity = risk_budget / risk_per_unit
order_quantity = exchange-valid rounded quantity
```

- `user_risk_fraction`, 계좌 최대 노출, 최대 허용 drawdown은 사용자의 risk 설정이다.
- 현재 사용자 기본값은 거래당 현재 전략 equity의 `3%`이며, 일일 실현손실 중단은 `OFF`다. 두 값은 진입 메커니즘과 분리된 사용자 설정으로 변경할 수 있다.
- 선택 기능인 일일 손실 제한을 켤 경우 기본 한도는 KST 날짜 시작 equity의 `1%`다. 비용 포함 실현 순손익이 한도에 도달하면 새 주문만 중단하고 기존 포지션의 최초 stop과 목표는 유지한다. 남은 일일 예산이 더 작으면 주문 위험을 그 예산까지 축소한다.
- entry·stop을 선택하는 공식과 stop 확대 금지는 strategy invariant다.
- 수수료·단순 slippage buffer, 계약 단위, leverage, 최소 주문금액과 수량 step을 적용한 뒤 예상 최대손실이 risk budget을 넘으면 수량을 줄인다.
- 유효 수량이 0이거나 거래소 최소 주문을 만족하지 못하면 `NO_TRADE_INVALID_SIZE`로 거절한다.
- position이 열린 뒤 수량을 늘리거나 손실을 만회하기 위한 추가 주문은 허용하지 않는다.

정확한 거래소 수량 공식은 product/execution 단계에서 닫지만, **진입가와 손절가의 차이로 수량을 정한다는 메커니즘은 진입 전 불변식**이다.

### 7.3 단일 최초 목표가

진입 방향 앞쪽에서 다음 후보를 만든다.

1. 선택한 `A1_B1_CONFLUENCE` 전체 형성 구조의 진행 방향 impulse extreme. LONG은 선택 구조 모든 형성봉의 최고가, SHORT은 모든 형성봉의 최저가다.
2. 장면을 소유한 5분 또는 15분의 확정 5봉 pivot
3. 확정 1H·4H 5봉 pivot
4. 반대 15분·1H·4H body-engulf OB body zone
5. 반대 15분·1H·4H FVG zone

겹치는 zone은 합치고 point가 zone 안에 있으면 그 zone에 흡수한다. LONG은 진입가 위의 가장 낮은 proximal 후보, SHORT은 진입가 아래의 가장 높은 proximal 후보 하나만 `initial_target`으로 선택한다. point는 실제 high/low, zone은 LONG에서 하단·SHORT에서 상단을 주문가격으로 사용한다.

가장 가까운 후보가 비용 포함 양의 순익을 만들지 못하면 더 먼 후보로 건너뛰지 않고 거래하지 않는다. 유효한 최초 목표가가 정해지면 별도의 최소 진입 손익비 gate는 두지 않는다. 매물대는 표준 차트 표시로 유지하고, 데이터 제공자가 수치 zone을 직접 제공할 때만 후보 adapter로 연결한다.

### 7.4 `1.4R` 익절 분기

```text
R = abs(entry_fill_price - initial_stop)
target_R = 진입 방향의 abs(initial_target - entry_fill_price) / R

IF target_R >= 1.4
  EXIT initial quantity 50% at exactly 1.0R
  AFTER actual partial fill, MOVE remainder stop to actual entry_fill_price
  EXIT remaining 50% at the original initial_target

ELSE
  NO partial order
  EXIT current quantity 100% at the original initial_target
```

정확히 `1.4R`은 반익절 경로에 포함한다. 반익절은 거래당 한 번뿐이며 다른 반익절 조건은 없다. `initial_target`은 진입 뒤 새 구조에 따라 늘리거나 줄이지 않으며, 새 반대 5분·15분 OB/FVG도 stop 변경이나 구조 청산 권위를 갖지 않는다.

- **수익 상태 거래량 전량익절:** 5분 또는 15분 완료봉에서 직전 20봉 volume 중앙값 대비 `RVOL >= 2.0`이고, LONG은 완료봉 종가가 실제 진입 체결가 이상, SHORT은 이하이며, 수수료·단순 slippage까지 뺀 잔여 포지션 순손익이 양수이면 다음 실행 가능 open에서 현재 보유량 전부를 시장성 익절한다.
- **실행 재확인:** 다음 실행 가능 가격에서도 진입가 방향 조건과 비용 포함 순이익을 만족할 때만 실행한다. gap으로 조건이 사라지면 거래량 청산만 취소하고 원래 `initial_stop`·`initial_target` 경로를 유지한다.
- **사용하지 않는 결합:** 최초 목표 접촉, 거부봉, 반대 5분·15분 OB를 거래량 청산의 선행조건으로 요구하지 않는다. 거래량 급증을 장벽 돌파·유지와 결합하는 별도 지속 분기도 첫 V0에 두지 않는다.

백테스트에서는 목표 가격 사건과 예정 position fraction만 기록하고 호가 기반 부분체결을 모사하지 않는다. 진입가 stop 이동은 OHLCV 체결 규칙상 반익절이 성립한 뒤에만 활성화한다. 수량 반올림은 주문 전 position-size 계산에 포함하고, reduce-only/OCO, 주문 교체, 실제 부분체결과 취소 경쟁은 전략 백테스트가 아니라 paper/live 공통 주문관리기의 책임으로 둔다.

사용자 정정에 따라 TP·SL 또는 구조 종료 없이 24시간이 지났다는 이유로 강제 종료하지 않는다. 데이터 끝에서도 이익이나 손실로 임의 확정하지 않고 `OPEN_CENSORED`로 둔다.

## 8. 초기 V0 OHLCV 구현 계약

초기 구현 범위 `A1_B1_CONFLUENCE`의 exact 수식과 주문 시계는 `EASYCHART_OB_V0_OHLCV_FORMULA_CONTRACT.md`로 고정했다. 이후 장면 가족은 각 버전 코드와 비교 보고서의 별도 수식을 따른다.

1. **데이터·시계:** UTC 완료봉, 5m/15m 실행, 1H/4H context, 필요한 feature가 모두 준비되면 `DATA_READY`.
2. **B1 사건:** strict 1H 5봉 pivot의 1 tick sweep·같은 봉 reclaim 또는 1 tick close break·첫 retest acceptance 중 하나와 같은 방향 LTF OB가 결합돼야 하며, 사건이나 OB 단독 진입 권위는 없음.
3. **OB 객체:** inclusive body engulf, non-doji, SIMPLE_2C·DOUBLE_3C zone과 전체 formation extreme.
4. **scene ownership:** 기존 A1 위치와 `새 1H 유동성 사건 + 같은 방향 LTF OB` B1을 하나의 confluence로 결합하고, 유효한 `15m+5m` 중첩을 우선.
5. **entry:** 새 확인 OB 완료 뒤 중첩 구역 첫 재방문 지정가 한 개, 고정 TTL·시장가 재추격 없음.
6. **target:** impulse extreme, 장면 소유 5m/15m pivot, 1H/4H pivot, 반대 15m/1H/4H OB·FVG 중 가장 가까운 유효 후보 하나를 최초 목표로 고정.
7. **관리:** 실제 `target_R >= 1.4`이면 1R에서 50% 반익절 후 잔량 stop을 실제 진입가로 이동하고, 잔량은 최초 목표까지 보유. 그 미만이면 최초 목표에서 전량익절. 별도로 비용 포함 수익 상태 `RVOL >= 2.0`이면 전량익절.
8. **사건 순서:** 실제 제공되는 하위 OHLCV로 먼저 닿은 사건을 적용하고, 최저 봉에서도 겹치면 stop 우선.
9. **router:** 관찰 후보는 슬롯을 차지하지 않고 실제 confluence pending 또는 open position만 시스템 전체 최대 하나.

고정 실행 순서는 다음과 같다.

```text
완료 데이터 확정
→ 상위 구조·기존 A1 위치·최초 목표 후보 확인
→ 기존 위치 안에서 1H 유동성 사건과 같은 방향 새 5m 또는 15m OB가 결합된 B1 완료 확인
→ A1_B1_CONFLUENCE와 `15m+5m` 우선 진입 구역 확정
→ 반대 zone·목표 공간·방향 충돌 gate
→ 전체 confluence 구조로 entry·stop·target 계산
→ risk budget과 entry–stop 거리로 주문 수량 계산
→ 재방문 지정가 ORDER_INTENT 하나 생성
→ 새 OB 완료 다음 합법 시점부터 주문 활성
→ 체결
→ `1.4R/1.0R` 고정 익절 경로와 수익 상태 거래량 전량익절 실행
→ 종료 또는 OPEN_CENSORED
```

기존 A1 위치나 새 LTF OB가 각각 후보로 관찰되는 동안에는 시스템 주문 슬롯을 막지 않는다. 두 요소의 유효한 교집합이 확정되어 `CONFLUENCE_PENDING` 지정가가 생성될 때만 슬롯을 차지한다.

현재 고정값은 다음과 같다.

| 항목 | V0 | 이유 |
|---|---|---|
| 초기 구현 leaf | `A1_B1_CONFLUENCE` | 기존 위치와 새 LTF 확인을 한 장면으로 결합 |
| 방향 충돌 | 가장 가까운 구조 장애물이 비용 포함 양의 최초 목표가가 될 수 없으면 no-trade | 가까운 장애물을 건너뛰는 억지 목표 선택 방지 |
| 완료 시계 | 모든 program confirmation은 완료봉 마감 뒤 활성 | 확인과 주문 시점 통일 |
| scene ownership | 기존 위치와 새 LTF OB의 조합에 confluence 하나 부여 | 한 장면에서 주문 하나만 생성 |
| 중첩 접촉 | 새 OB 완료 뒤 선택 교집합의 첫 재방문 | 형성 완료와 지정가 체결을 분리 |
| 15m+5m 우선 | `1H+15m`과 함께 존재하면 더 좁은 `15m+5m` 사용 | 실제 타점 시간봉으로 진입 구역 세분화 |
| confluence stop | 선택된 모든 위치·확인 OB 형성봉의 반대쪽 가장 먼 wick 바깥 1 tick | 전체 구조를 하나의 risk 단위로 취급 |
| 관리 | `target_R >= 1.4`이면 1R에서 50%→진입가 stop→최초 목표 잔량익절; 미만이면 최초 목표 전량익절; 수익 상태 `RVOL >= 2.0`은 독립 전량익절 | 하나의 목표와 두 가지 명확한 관리 경로로 단순화 |
| 5m | 기존 위치를 확인하고 15m 구역을 세분화하며 pivot 목표 후보에 사용 | EasyChart의 실제 실행 시간봉 역할 반영 |

## 9. 불변식과 사용자화 경계

### 9.1 유지할 프로그램 불변식

- 각 판단은 완료봉과 해당 상태의 주문 활성 시점을 사용한다.
- source의 직접 의미, 논리적 종합, 공학 선택, 미결 상태를 서로 바꿔 쓰지 않는다.
- 시스템 전체에는 하나의 최초 진입, 하나의 pending intent 또는 하나의 open position만 허용한다. 진입 뒤 수량을 늘리는 주문 상태와 추가 매수는 없으며, 어느 종목에 pending/open 상태가 있으면 다른 종목의 신규 진입도 차단한다.
- 최초 stop은 진입 전에 고정하고 진입 뒤 불리한 방향으로 넓히지 않는다.
- 진입 주문 수량은 주문 전에 현재 equity의 risk budget과 entry–stop 거리로 계산한다.
- TP·SL·구조 종료가 없다는 이유로 24시간 또는 데이터 끝에서 강제 청산하지 않는다.
- 증거 gate 전에는 paper/live/order/fill/execution/risk/promotion 권위를 열지 않는다.

완료봉만 사용하는 것은 source 보편 규칙이 아니라 V0 프로그램 불변식이다. 진행 중 패턴까지 모사하려면 별도 실시간 arm과 새로운 오류·실행 위험 검증이 필요하며, V0 설정 하나로 켜고 끄지 않는다.

### 9.2 버전으로 고정할 전략 장면

다음은 사용자가 거래 중 임의 조정하는 설정이 아니라, 서로 다른 전략 가설로 사전에 이름 붙이고 고정할 항목이다.

- 기존 A1 위치와 새 LTF B1 확인 OB를 묶는 `A1_B1_CONFLUENCE` ownership.
- 새 확인 OB 완료 뒤 선택 교집합 첫 재방문 지정가로 고정한 단일 entry clock.
- 허용하는 `SIMPLE_2C`·`DOUBLE_3C` form.
- direction conflict와 opposing-zone 정책.
- confluence zone geometry와 전체 형성 구조 hard stop.
- 최근접 최초 목표 selector와 `1.4R/1.0R/50%` 관리 분기.
- 5분·15분 확인 역할, `15m+5m` 우선 중첩, 선택 pivot의 목표 후보 역할.
- 상대 거래량 비교식, 진입가 방향 조건, 비용 포함 양의 순손익을 결합한 독립 전량익절 관리.

유동성 사건 종류나 geometry를 바꾸는 실험은 이름 붙은 전략 버전으로 분리하되, 같은 장면에서 주문을 여러 개 만들지 않는다.

### 9.3 후속 단계에서 사용자화할 수 있는 것

- 해당 strategy version에서 이미 동결·검증된 symbol 중 감시 화면과 알림·시각화 표시 방식. 새 거래 symbol을 추가하면 새 버전과 새 증거가 필요하다.
- 공통 실시간 런타임의 `PAPER` 또는 승인된 `LIVE` 실행 모드와 UI 승인 방식.
- 거래당 위험 비율, 계좌·포트폴리오 최대 노출, 최대 허용 drawdown 같은 계좌 risk 한도.

사용자가 실제 Bybit 계좌에 적용할 자금 성장 단계는 프로그램이 달성 금액을 자동 추적·승격하는 기능으로 만들지 않는다. OB 의미, entry·stop·target 공식과 stop 확대 금지는 UI에서 임의 변경할 사용자 설정이 아니다. 지정가 취소 조건처럼 체결률과 기대값을 바꾸는 값은 단순 개인 취향 값으로 바꾸지 않고 이름 붙은 strategy/execution version에서 고정한다.

## 10. 효율적인 다음 단계

현재까지 V0.3~V0.7을 구현했다. 연구 선두는 `V0.3 BREAK_RETEST + V0.5`이며, V0.6과 V0.7은 빈도를 늘렸지만 기대값을 훼손해 제외했다. 다음 단계는 기존 장면을 조금씩 느슨하게 만드는 작업이 아니라 V0.7이 드러낸 손익 원인을 새 장면 정의에 반영하는 것이다.

1. **완료:** 단일 최초 목표, `1.4R/1R` 관리, 수익 상태 거래량 전량익절, 현재 equity 기반 3% 위험 수량, 전역 한 포지션 계약을 historical replay에 연결했다.
2. **완료:** V0.3 BREAK_RETEST와 V0.5의 양수 소표본 조합을 연구 선두로 정했다.
3. **완료:** V0.6 M15-owned OB 중첩 장면과 두 손절 arm을 구현했고, 손절 선택이 아니라 장면 자체의 기대값 문제임을 확인했다.
4. **완료:** V0.7 SR Flip+FVG 장면에서 첫 회귀 지정가와 실제 다음 M5 시가를 구현하고, BTC·ETH 전역 단일 잔고·단일 슬롯으로 비교했다.
5. **현재:** V0.7 목표 종류별 손익을 바탕으로 pivot·OB·FVG가 terminal target 권위를 갖는 조건을 구체화한다. 특히 FVG는 단순 근접 구역과 독립 유동성 목적지를 구별한다.
6. **다음:** H1·H4가 큰 틀을 제공하고 M15가 위치·사건, M5가 직접 타점을 제공하도록 역할이 실제 후보 선택을 가르는 새 장면 하나를 확정한다.
7. **그다음:** 새 장면을 별도 가족으로 구현해 연구 선두와 결합했을 때 거래 빈도와 비용 차감 기대값이 함께 개선되는지 본다.
8. 의미 있는 양수 조합이 만들어지면 BTC·ETH 통합 실시간 한-slot runtime을 만들고 `PAPER`로 운용한다. 같은 전략·위험·주문 상태기를 별도 승인 뒤 `LIVE`로 전환한다.

추가 작업은 최종 전략 결정이나 실행 프로그램에 영향을 줄 때 수행한다. 빈도가 부족하면 표본 기간만 늘리기 전에 위치 selector, 사건 정의, entry clock, 목표 권위 중 무엇이 기회를 막거나 손익을 압축하는지 먼저 본다.

## 11. 현재 판정

- EasyChart OB 중심 전략: OB·FVG를 단독 신호가 아니라 위치·유동성 사건·displacement·목표를 연결하는 도구로 사용한다.
- 현재 활성 주전: 미확정. 연구 선두는 `V0.3 BREAK_RETEST + V0.5` 한 슬롯 조합이다.
- V0.6 판정: M15-owned OB 중첩 장면은 음의 기대값이므로 제외한다.
- V0.7 판정: SR Flip+FVG의 두 진입 arm은 빈도를 늘렸지만 음의 기대값이므로 제외한다.
- 5분·15분 역할: 15분은 위치와 사건, 5분은 직접 타점을 맡되 새 장면에서 이 역할이 실제 후보 선택을 가르도록 보완한다.
- 거래량 관리: 완료 5분·15분 `RVOL >= 2.0`, 진입가 방향 조건, 비용 포함 양의 순손익을 만족하면 독립적으로 전량익절하는 첫 V0 규칙으로 고정됨.
- 프로그램 목표: 일반 ICT가 아니라 EasyChart 기반 OB 중심 전략의 기대값 개선으로 고정됨.
- 백테스트 범위: OHLCV 전용이며 호가장·대기열·호가 기반 부분체결은 영구 제외됨.
- paper/live 목표 구조: 별도 전략이 아니라 동일한 실시간 런타임의 실행 모드로 고정됨.
- 진입 전 risk-based position sizing 메커니즘: 추가됨; 현재 사용자 기본값은 거래당 `3%`, 일일 손실 제한 `OFF`로 확정·구현됨. 거래소별 계약 단위와 주문 제한은 paper/live adapter에서 이어서 적용한다.
- 시간 원칙: 결정을 바꾸는 최소 작업만 수행하고 충분한 증거 뒤 추가 엄밀성 작업을 중단함.
- 공통 no-trade와 관리 역할: exact OHLCV event·수익 상태 relative-volume 전량익절·동시 사건 순서까지 수식 계약으로 고정됨.
- 추가 경제·실행 권위: 없음.
- 최신 전역 결과: 연구 선두 13건 `+2.049R`·`+6.14%`, V0.7 다음 시가 단독 31건 `-1.090R`·`-3.55%`, 결합 44건 `+0.959R`·`+2.37%`다.
- 현재 다음 결정: terminal target의 독립 유동성 소유 조건과 H1/H4 문맥 역할을 구체화한 다음 장면을 정한다.
