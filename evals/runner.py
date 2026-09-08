"""对正在运行的 FitMind API 执行可重复的 Agent 场景评测。"""

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events, name = [], None
    for line in body.splitlines():
        if line.startswith("event: "):
            name = line[7:].strip()
        elif line.startswith("data: ") and name:
            events.append((name, json.loads(line[6:])))
    return events


def grade(case: dict, events: list[tuple[str, dict]]) -> tuple[bool, list[str]]:
    cards = [data.get("type") for name, data in events if name == "card"]
    tools = [data.get("label") for name, data in events if name == "tool_start"]
    answer = "".join(data.get("text", "") for name, data in events if name == "text_delta")
    failures = []
    if case.get("expected_card") and case["expected_card"] not in cards:
        failures.append(f"缺少卡片 {case['expected_card']}，实际 {cards}")
    if case.get("expected_tool") and case["expected_tool"] not in tools:
        failures.append(f"缺少工具行为 {case['expected_tool']}，实际 {tools}")
    for term in case.get("forbidden_terms", []):
        if term in answer:
            failures.append(f"出现禁用表达：{term}")
    return not failures, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("FITMIND_API", "http://localhost:8000/api/v1"))
    parser.add_argument("--email", default=os.getenv("FITMIND_EVAL_EMAIL", "demo@fitmind.cn"))
    parser.add_argument("--password", default=os.getenv("FITMIND_EVAL_PASSWORD", "demo123456"))
    parser.add_argument("--output", default="evals/reports/latest.json")
    args = parser.parse_args()
    cases = json.loads((Path(__file__).parent / "cases/core.json").read_text())
    client = httpx.Client(base_url=args.base_url, timeout=120)
    token = client.post("/auth/login", json={"email": args.email, "password": args.password}).raise_for_status().json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    results = []
    for case in cases:
        conv = client.post("/conversations", headers=headers).raise_for_status().json()["id"]
        events = []
        for prompt in case.get("steps", [case.get("input")]):
            response = client.post(f"/conversations/{conv}/messages", headers=headers, json={"text": prompt, "client_message_id": uuid.uuid4().hex})
            response.raise_for_status()
            events.extend(parse_sse(response.text))
        passed, failures = grade(case, events)
        results.append({"id": case["id"], "passed": passed, "failures": failures})
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "passed": sum(r["passed"] for r in results), "total": len(results), "cases": results}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
