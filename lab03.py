"""Danh gia 3 agent design tren cung model, tool, data va safety harness.

Chay vi du:
    python lab03.py --design all --scenario all
    python lab03.py --design react --scenario chon-loc
    python lab03.py --approve no

Benchmark dung model that trong .env. Moi lan chay co the phat sinh API cost.
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from typing import Any, Literal

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from lib.harness import FlightHarness, HarnessGuardMiddleware
from lib.model_hybrid import create_hybrid_agent
from lib.model_pe import create_plan_execute_agent
from lib.model_react import create_react_agent
from lib.tools_datve import book_flight, search_flight_info


Design = Literal["react", "pe", "hybrid"]


@dataclass(frozen=True)
class Scenario:
    name: str
    request: str
    expected: Literal["book", "safe_stop"]
    lesson: str


SCENARIOS = {
    "co-ban": Scenario(
        name="co-ban",
        request=(
            "Trong database mô phỏng, tìm và đặt vé rẻ nhất còn chỗ từ Hà Nội "
            "đến Đà Nẵng ngày 2024-06-01. Không hỏi lại nếu dữ liệu đã đủ."
        ),
        expected="book",
        lesson="Loại vé rẻ nhưng full, chọn đúng chuyến available.",
    ),
    "chon-loc": Scenario(
        name="chon-loc",
        request=(
            "Trong database mô phỏng, đặt chuyến rẻ nhất còn chỗ từ TP.HCM đến "
            "Nha Trang ngày 2024-07-01. Phải so sánh tất cả kết quả search."
        ),
        expected="book",
        lesson="Vietjet rẻ nhất nhưng full; Bamboo là lựa chọn hợp lệ rẻ nhất.",
    ),
    "khong-co-ngay": Scenario(
        name="khong-co-ngay",
        request=(
            "Trong database mô phỏng, tìm vé Hà Nội đi Huế đúng ngày 2024-07-01. "
            "Không được tự đổi ngày và không được bịa kết quả. Nếu chưa chắc, có "
            "thể thử tìm lại; khi bế tắc phải dừng và nói rõ."
        ),
        expected="safe_stop",
        lesson="Không có đúng ngày; retry giống nhau sẽ bị harness phát hiện lặp.",
    ),
}


@dataclass
class Evaluation:
    design: str
    scenario: str
    expected: str
    outcome: str
    passed: bool
    tool_calls: int
    errors: int
    loop_detected: bool
    approval_requested: bool
    elapsed_seconds: float
    final_response: str
    exception: str = ""


def create_real_model() -> BaseChatModel:
    load_dotenv()
    model_name = os.getenv("OPENAI_MODEL")
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    if not model_name or not api_key:
        raise SystemExit("Can OPENAI_MODEL va OPENAI_API_KEY trong .env")

    kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": api_key,
        "temperature": 0,
    }
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)


def _last_message_text(result: dict[str, Any]) -> str:
    messages = result.get("messages", [])
    if not messages:
        return ""
    return str(getattr(messages[-1], "content", messages[-1]))


def _run_design(
    design: Design,
    scenario: Scenario,
    model: BaseChatModel,
    approve: bool,
) -> tuple[str, FlightHarness]:
    harness = FlightHarness(
        search_flight_info,
        book_flight,
        max_tool_calls=10,
        loop_window=6,
        repeat_limit=2,
    )
    tools = harness.langchain_tools()

    if design == "react":
        agent = create_react_agent(
            model,
            tools,
            middleware=[HarnessGuardMiddleware(harness)],
        )
        result = agent.invoke(
            {"messages": [{"role": "user", "content": scenario.request}]},
            {"recursion_limit": 30},
        )
        response = _last_message_text(result)

    elif design == "pe":
        agent = create_plan_execute_agent(model, tools, verbose=False)
        result = agent.invoke(
            {
                "user_request": scenario.request,
                "plan": [],
                "past_steps": [],
                "response": "",
            },
            {"recursion_limit": 30},
        )
        response = str(result.get("response", ""))

    else:
        agent = create_hybrid_agent(
            model,
            tools,
            verbose=False,
            middleware=[HarnessGuardMiddleware(harness)],
        )
        result = agent.invoke(
            {
                "user_request": scenario.request,
                "plan": [],
                "past_steps": [],
                "response": "",
            },
            {"recursion_limit": 40},
        )
        response = str(result.get("response", ""))

    # Benchmark mo phong user submit sau khi agent da dung tai approval gate.
    if harness.pending_booking is not None:
        submit_observation = harness.submit_booking(approve)
        response += f"\nUSER_SUBMIT={submit_observation}"
    return response, harness


def evaluate_one(
    design: Design,
    scenario: Scenario,
    model: BaseChatModel,
    approve: bool,
) -> Evaluation:
    started = time.perf_counter()
    response = ""
    exception = ""
    harness: FlightHarness | None = None
    try:
        response, harness = _run_design(design, scenario, model, approve)
    except Exception as exc:
        exception = f"{type(exc).__name__}: {exc}"

    elapsed = time.perf_counter() - started
    report = harness.report() if harness else {
        "history": [], "stop_reason": None, "pending_booking": None
    }
    history = report["history"]
    stop_reason = str(report.get("stop_reason") or "")
    loop_detected = stop_reason.startswith("loop_detected")
    approval_requested = any(
        isinstance(item.get("observation"), dict)
        and item["observation"].get("status") == "approval_required"
        for item in history
    )
    errors = sum(
        1
        for item in history
        if isinstance(item.get("observation"), dict)
        and item["observation"].get("status") in {"error", "stopped"}
    )

    booked = stop_reason == "booking_completed"
    if booked:
        outcome = "booked"
    elif loop_detected:
        outcome = "loop_stopped"
    elif exception:
        outcome = "exception"
    else:
        outcome = "safe_stop"

    passed = (
        scenario.expected == "book" and booked
    ) or (
        scenario.expected == "safe_stop" and not booked and not exception
    )
    return Evaluation(
        design=design,
        scenario=scenario.name,
        expected=scenario.expected,
        outcome=outcome,
        passed=passed,
        tool_calls=len(history),
        errors=errors,
        loop_detected=loop_detected,
        approval_requested=approval_requested,
        elapsed_seconds=elapsed,
        final_response=response,
        exception=exception,
    )


def print_report(results: list[Evaluation]) -> None:
    headers = (
        "DESIGN", "SCENARIO", "EXPECT", "OUTCOME", "PASS", "TOOLS", "ERR", "LOOP", "SEC"
    )
    print("\n" + " | ".join(headers))
    print("-" * 105)
    for item in results:
        print(
            f"{item.design:6} | {item.scenario:13} | {item.expected:9} | "
            f"{item.outcome:12} | {str(item.passed):5} | {item.tool_calls:5} | "
            f"{item.errors:3} | {str(item.loop_detected):5} | "
            f"{item.elapsed_seconds:5.1f}"
        )

    print("\nCHI TIET")
    for item in results:
        print(f"\n[{item.design}/{item.scenario}] {item.outcome}")
        if item.exception:
            print("EXCEPTION:", item.exception)
        print("RESPONSE:", item.final_response)


def main() -> None:
    parser = argparse.ArgumentParser(description="So sanh ReAct, P&E va Hybrid")
    parser.add_argument(
        "--design", choices=["all", "react", "pe", "hybrid"], default="all"
    )
    parser.add_argument(
        "--scenario", choices=["all", *SCENARIOS], default="all"
    )
    parser.add_argument(
        "--approve", choices=["yes", "no"], default="yes",
        help="Mo phong user submit tai approval gate",
    )
    args = parser.parse_args()

    designs: list[Design] = (
        ["react", "pe", "hybrid"] if args.design == "all" else [args.design]
    )
    scenarios = (
        list(SCENARIOS.values())
        if args.scenario == "all"
        else [SCENARIOS[args.scenario]]
    )
    model = create_real_model()
    results: list[Evaluation] = []
    for scenario in scenarios:
        print(f"\n=== SCENARIO {scenario.name}: {scenario.lesson} ===")
        for design in designs:
            print(f"Dang chay {design}...")
            results.append(
                evaluate_one(design, scenario, model, args.approve == "yes")
            )
    print_report(results)


if __name__ == "__main__":
    main()
