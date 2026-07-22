# BTC·ETH·SOL·XRP USD-M 데이터 감사

생성일: `2026-07-22 UTC/KST`  
상태: `CHECKSUM_VERIFIED / APPEND_ONLY_SNAPSHOT / GAP_AWARE`

## 범위

- 시장: Binance USDⓈ-M public archive
- 종목: BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT
- 데이터: 5분 trade kline, mark-price kline, premium-index kline, historical funding
- 월별 범위: 2020-01 ~ 2026-06
- 일별 추가 범위: 2026-07-01 ~ 2026-07-21
- 정규화 데이터셋: 16개
- 휴대용 ZIP SHA-256: `158a6dade5713138eceed7c072c4fa2fc7c2760f9f7a754424ee75326a66b35b`

모든 원본 ZIP은 공개된 `.CHECKSUM`과 대조했다. 정규화 CSV별 SHA-256과 원본 archive 목록은 `portable_manifest.json`에 보존한다.

## trade-kline 연속성

| 종목 | 행 수 | 시작 | 종료 | 불규칙 간격 |
|---|---:|---|---|---:|
| BTCUSDT | 689,472 | 2020-01-01 00:00 UTC | 2026-07-21 23:55 UTC | 0 |
| ETHUSDT | 689,472 | 2020-01-01 00:00 UTC | 2026-07-21 23:55 UTC | 0 |
| SOLUSDT | 613,932 | 2020-09-14 07:00 UTC | 2026-07-21 23:55 UTC | 2 |
| XRPUSDT | 686,492 | 2020-01-06 08:20 UTC | 2026-07-21 23:55 UTC | 2 |

SOL과 XRP의 공통 trade-kline 공백:

1. `2022-02-25 23:55 UTC → 2022-03-01 00:00 UTC`
2. `2022-03-31 23:55 UTC → 2022-04-03 00:00 UTC`

이 구간은 보간하지 않는다. 공백을 포함하는 달력일은 완전 운용일에서 제외하고, 사건 형성·pending·보유 경로가 공백을 가로지르면 해당 episode를 무효화한다.

## 기타 상태

- SOL의 2020-01~2020-08 archive 부재는 상장 전 구간이며 `missing_archives`에 명시되어 있다.
- mark-price와 premium-index에는 소수의 불규칙 간격이 있다. 전략은 각 특징의 원천 시각 연속성을 검사하고 공백 이후 warm-up을 다시 요구해야 한다.
- funding snapshot은 월별 확정 archive 기준 2026-06-30까지다. 2026-07 funding은 다음 월별 archive 확정 뒤 append한다.
- 날짜 추가나 archive revision은 새 snapshot으로 기록하며 과거 의사결정 증거를 조용히 덮어쓰지 않는다.
