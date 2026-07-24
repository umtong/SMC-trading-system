#!/usr/bin/env python3
"""Validate the repository-side project control plane without dependencies."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = [
    "project/PROJECT_INSTRUCTIONS.md",
    "project/OPERATING_SYSTEM.md",
    "project/EVALUATION_CONTRACT.md",
    "project/DRIVE_CONTROL_PLANE.md",
    "project/prompts/COORDINATOR.md",
    "project/prompts/RESEARCH_LANE.md",
    "project/schemas/champion.schema.json",
    "project/schemas/run_report.schema.json",
]

INSTRUCTION_MARKERS = [
    "## 최상위 목표",
    "## 처음부터 충족해야 하는 기본 조건",
    "## 종료 조건과 연속 실행",
    "## 탐색과 전환",
    "## 위험과 성과 판단",
    "## 병렬 채팅과 상태 기록",
]

FORBIDDEN_INSTRUCTIONS = [
    "GPT Pro로 행동",
    "Pro Extended",
    "이 규칙을 만든 이유",
    "사용자가 이 말을 한 이유",
]


def main() -> int:
    errors: list[str] = []
    for rel in REQUIRED:
        if not (ROOT / rel).is_file():
            errors.append(f"missing: {rel}")

    for rel in ["project/schemas/champion.schema.json", "project/schemas/run_report.schema.json"]:
        path = ROOT / rel
        if path.exists():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                errors.append(f"invalid JSON {rel}: {exc}")

    instructions = ROOT / "project/PROJECT_INSTRUCTIONS.md"
    if instructions.exists():
        text = instructions.read_text(encoding="utf-8")
        for marker in INSTRUCTION_MARKERS:
            if marker not in text:
                errors.append(f"PROJECT_INSTRUCTIONS missing section: {marker}")
        for phrase in FORBIDDEN_INSTRUCTIONS:
            if phrase in text:
                errors.append(f"PROJECT_INSTRUCTIONS contains non-runtime phrase: {phrase}")
        if "오직 두 가지" not in text or "시간제한" not in text or "목표" not in text:
            errors.append("two-condition stop rule is not explicit")
        if "Google Drive" not in text or "GitHub" not in text:
            errors.append("dual control-plane contract is missing")

    if errors:
        print("CONTROL PLANE VALIDATION FAILED")
        for error in errors:
            print(f"- {error}")
        return 1

    print("CONTROL PLANE VALIDATION OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
