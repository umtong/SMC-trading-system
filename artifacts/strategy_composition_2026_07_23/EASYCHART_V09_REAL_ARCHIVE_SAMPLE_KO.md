# EasyChart V0.9 실제 Binance 원본 통합 검증

검증일: 2026-07-23 KST  
상태: 통과  
범위: 데이터 계약 검증 전용, 전략 성과 아님

## 검증 대상

- 종목: `BTCUSDT` USD-M perpetual
- UTC 일자: `2024-03-03`
- aggregate trades: 공식 daily archive
- mark price: 공식 daily 1m archive
- funding rate: 공식 monthly archive
- 각 ZIP은 대응 `.CHECKSUM`의 SHA-256과 일치해야 통과

## 결과

| 항목 | 결과 |
|---|---:|
| aggregate trade 행 | 980,946 |
| underlying trade 합계 | 2,976,570 |
| 생성된 1분 flow 봉 | 1,440 |
| 1분 mark-price 봉 | 1,440 |
| 당일 funding 정산 | 3 |
| aggregate trade id 시작 | 2,044,024,289 |
| aggregate trade id 종료 | 2,045,005,234 |
| 일 총 quote volume | 12,480,530,306.73098 USDT |
| 일 signed quote delta | +70,369,551.36715996 USDT |

1분 flow와 mark-price는 UTC 하루의 1,440분을 빠짐없이 채웠다. aggregate trade id는 중복·역전 없이 증가했고, funding archive도 같은 정규화 코드로 세 정산을 읽었다.

## 원본 해시

| 자료 | SHA-256 |
|---|---|
| `BTCUSDT-aggTrades-2024-03-03.zip` | `63096b6af6f56ec87b6e6d79a6ee227880ffa34050044e952f80c9f3471e7726` |
| `BTCUSDT-1m-2024-03-03.zip` mark price | `f52209d034273a7a6f0f3d317eda5b2aefb366e751daefaf104e877454698aae` |
| `BTCUSDT-fundingRate-2024-03.zip` | `711dcaf2a341aedfd06447b4117b540f60adb0784c5e64a313c718e4cf092bc3` |

## 판정

- 합성 fixture만 통과한 상태가 아니라 실제 Binance archive의 열 구조와 header 형식을 통과했다.
- buyer-maker 부호 변환, 1분 flow 집계, mark-price 연속성, funding 정규화가 한 실행에서 함께 검증됐다.
- 이 결과는 데이터·파서 계약을 승인할 뿐, V0.9 전략의 기대값이나 실거래 승격을 의미하지 않는다.
- 이후 대규모 연구는 outcome-blind 장면 manifest가 요구하는 UTC 날짜만 daily aggregate-trade archive로 받는다.
