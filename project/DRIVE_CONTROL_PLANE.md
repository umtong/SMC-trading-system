# Google Drive 라이브 제어면

권장 폴더 구조:

```text
SMC_ICT_2_LIVE/
├─ 00_CONTROL_PLANE/
│  ├─ 00_PROJECT_STATE
│  ├─ 01_CHAMPION
│  ├─ 02_TASK_BOARD
│  ├─ 03_DECISION_LOG
│  ├─ 04_EVALUATION_CONTRACT
│  └─ 05_SYSTEM_MAP
├─ 10_RUN_REPORTS/
├─ 20_MILESTONE_SNAPSHOTS/
└─ 90_ARCHIVE/
```

## 작성 권한

- 총괄 채팅: `00_CONTROL_PLANE` 공통 문서 수정
- 연구 lane: `10_RUN_REPORTS`에 고유 보고서 생성
- milestone 병합: 총괄이 `20_MILESTONE_SNAPSHOTS`에 snapshot 생성
- 과거·대체 자료: `90_ARCHIVE`

## 작업 보드 필드

`epoch_id`, `lane_id`, `task_id`, `base_revision`, `status`, `priority`, `objective`, `scope`, `inputs`, `success_condition`, `failure_condition`, `report_link`, `github_branch`, `github_commit`, `pull_request`, `dependencies`, `assigned_at`, `completed_at`

권장 상태:

`QUEUED | ASSIGNED | RUNNING | REPORTED | MERGED | BLOCKED | CANCELLED`

## 저장 원칙

Drive의 큰 용량은 원시 자막, 대규모 결과, 로그와 snapshot에 사용한다. 파일명에는 날짜·실험 ID·데이터 범위를 포함하고, 중요한 원시 자료에는 checksum과 출처를 함께 보존한다. 활성 제어면은 간결하게 유지하고, 오래된 자료는 archive로 이동한다.
