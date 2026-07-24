# 연구 lane 실행 프롬프트

Google Drive의 최신 공통 상태, Champion, Evaluation Contract와 Task Board에서 lane_id `[LANE_ID]`에 배정된 task를 읽고 즉시 수행하라. `epoch_id`, `task_id`, `base_revision`을 확인하라.

코드·문서·실험 증거를 변경하면 `agent/<epoch>-<lane>-<task>` 브랜치를 만들고, 배정 범위 안에서 필요한 파일 생성·수정·삭제를 수행하라. 검증 후 논리적인 commit과 main 대상 draft PR을 생성하라. 다른 lane branch와 공통 Drive 문서는 수정하지 마라.

목표 달성 또는 시간제한까지 진행 중 작업 완료, 결과 해석과 다음 고정보가치 작업을 반복하라. 종료 시 고유 Run Report를 Drive에 생성하고 데이터·코드·비용 조건, 핵심 결과, 유효성, 판정, 실패·병목, GitHub branch·commit·PR와 다음 정확한 작업을 기록하라.
