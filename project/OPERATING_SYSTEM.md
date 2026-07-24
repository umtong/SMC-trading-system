# SMC/ICT 2호기 — 병렬 연구 운영체계

## 1. 정본 분리

- **Google Drive:** 현재 revision, epoch, Champion, 작업 보드, 결정, 평가 계약, 실행 보고서와 milestone snapshot의 라이브 제어면
- **GitHub:** 프로젝트 규칙, 전략 코드, 데이터 manifest, 실행·평가 스크립트, immutable 결과와 장기 변경 이력
- **프로젝트 채팅:** 추론·연구·구현·감사 실행 공간. 채팅 기억은 보조 문맥이며 정본이 아니다.

Drive의 5TB 용량은 원시 자막, 대규모 결과, 로그와 snapshot 보관에 활용할 수 있다. 대형 원시 파일은 Drive에 보관하고 GitHub에는 manifest, checksum, 생성 절차와 요약 결과만 커밋한다. 공개 저장소에는 Drive 비공개 URL, 자격증명, 개인 정보와 비공개 데이터 내용을 넣지 않는다.

## 2. 병렬 구조

```text
00_COORDINATOR
├─ 10_ALPHA_A
├─ 11_ALPHA_B
├─ 20_EXECUTION_COST
├─ 30_PORTFOLIO
└─ 40_RED_TEAM
```

채팅 수는 고정하지 않는다. 수익 원리나 작업 산출물이 실질적으로 다를 때만 lane을 추가하고, 중복 역할은 통합한다.

## 3. 총괄 채팅

총괄만 Drive의 공통 상태, Champion, 작업 보드와 결정 기록을 수정한다.

1. 최신 state revision, Champion, 평가 계약과 완료 보고서를 읽는다.
2. 목표 격차와 핵심 병목을 분해한다.
3. 서로 겹치지 않는 작업을 lane별로 배정한다.
4. 각 task에 `epoch_id`, `lane_id`, `task_id`, `base_revision`, 성공·실패 조건을 부여한다.
5. 완료 보고서와 GitHub PR을 비교한다.
6. 데이터·비용·체결·평가 조건 차이, 중복과 충돌을 검사한다.
7. 검증된 결과만 공통 상태와 Champion에 병합한다.
8. revision을 증가시키고 다음 epoch를 배정한다.

## 4. 연구 lane

각 lane은 시작 시 최신 공통 상태, Champion, 평가 계약, 자신의 작업 보드 행과 관련 GitHub 코드를 읽는다.

코드·문서·실험 증거를 변경할 때는 다음을 수행한다.

1. `agent/<epoch>-<lane>-<task>` 형식의 브랜치를 만든다.
2. 배정 범위 안에서 파일을 생성·수정한다.
3. 대체되거나 잘못된 파일을 삭제해야 할 때는 근거와 영향 범위를 보고서와 commit에 남긴다.
4. 관련 테스트·검증을 실행한다.
5. 논리적인 단위로 commit한다.
6. main 대상 draft PR을 만든다.
7. 고유 Drive 실행 보고서에 branch·commit·PR·산출물 링크를 기록한다.

연구 lane은 다른 lane branch, 공통 Drive 상태, Champion을 직접 수정하지 않는다. main 직접 commit, force push와 검증되지 않은 자동 병합을 금지한다.

## 5. 실행 보고서

파일명:

```text
RUN__<epoch_id>__<lane_id>__<task_id>__<YYYYMMDD-HHMM-KST>
```

필수 내용:

- 식별자와 `base_revision`
- 목표, 범위와 가정
- 사용한 코드·데이터·비용·체결 조건
- 실제 수행 작업과 핵심 결과
- 유효성·민감도·수익 집중도
- 유지·수정·보류·폐기·무효 판정
- Champion에 미치는 영향
- 실패와 남은 병목
- GitHub branch·commit·PR와 Drive 산출물
- 다음 정확한 작업

## 6. 충돌과 stale result

- 같은 task를 여러 lane에 배정하지 않는다. 독립 재현이면 별도 task ID를 쓴다.
- 연구 lane은 고유 보고서와 자신의 브랜치만 쓴다.
- `base_revision`이 오래됐다고 자동 폐기하지 않고, 그 사이 바뀐 가정과 충돌을 확인한다.
- 비교 조건이 다르면 정규화하거나 차이를 명시한다.
- Champion 변경 전 기존 Champion의 재현 조건과 증거를 보존한다.
- 동시에 열린 PR이 같은 파일을 수정하면 총괄이 우선순위·재베이스·통합 PR을 결정한다.

## 7. 동기화 주기

- **매 실행:** lane의 Drive run report
- **코드·증거 변경 시:** GitHub branch, commit, draft PR
- **매 epoch 병합:** Drive 상태·Champion·작업 보드·결정 갱신
- **중요 milestone:** Drive snapshot을 생성하고 GitHub에 공개 가능한 상태 요약·manifest·commit SHA를 보존

## 8. 실패 복구

특정 도구나 데이터가 막히면 연구를 종료하지 않고 다른 고정보가치 작업으로 전환한다. Drive 쓰기가 불가능하면 완전한 상태 패치 파일을 생성하고 기존 revision을 명시한다. GitHub 쓰기가 불가능하면 patch와 파일 목록·검증 결과를 Drive 보고서에 남겨 다음 실행에서 즉시 적용한다.
