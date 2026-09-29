"""Plan-and-Execute agent voi mot tool call cho moi buoc ke hoach.

Day la P&E "thuan": planner tao plan, executor chi thuc hien *mot* tool call
cho buoc hien tai, sau do replanner doc observation de giu/sua plan. Executor
khong tu lap ReAct ben trong mot buoc; diem nay phan biet no voi hybrid.
"""

from __future__ import annotations

import json
import operator
import re
from collections.abc import Sequence
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field


class Plan(BaseModel):
    steps: list[str] = Field(description="Cac buoc ro rang, co thu tu va co the thuc thi")


class ToolInstruction(BaseModel):
    tool_name: str = Field(description="Ten tool can goi cho buoc hien tai")
    arguments: dict[str, Any] = Field(description="Tham so dung theo schema cua tool")


class ReplanDecision(BaseModel):
    action: Literal["continue", "finish"]
    remaining_steps: list[str] = Field(default_factory=list)
    response: str | None = None
    reason: str = Field(description="Vi sao giu, sua ke hoach, hoac ket thuc")


class PlanExecuteState(TypedDict):
    user_request: str
    plan: list[str]
    past_steps: Annotated[list[tuple[str, str]], operator.add]
    response: str


def _tool_descriptions(tools: Sequence[BaseTool]) -> str:
    return "\n".join(
        f"- {tool.name}: {tool.description}; schema={tool.args}" for tool in tools
    )


def _invoke_structured(
    model: BaseChatModel,
    schema: type[BaseModel],
    messages: list,
    label: str,
) -> BaseModel:
    """Lay JSON co schema tu ca model ho tro va khong ho tro tool calling.

    Mot so OpenAI-compatible endpoint bo qua ``function_calling`` va tra text
    thuong, lam ``with_structured_output`` tra ``None``. Cach nay yeu cau JSON
    truc tiep, sau do de Pydantic validate ket qua.
    """

    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    response = model.invoke(
        [
            SystemMessage(
                content=(
                    "Chi tra ve DUY NHAT mot JSON object hop le, khong markdown, "
                    f"dung JSON Schema sau: {schema_json}"
                )
            ),
            *messages,
        ]
    )
    content = response.content
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    text = str(content).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    candidate = match.group(0) if match else text
    try:
        return schema.model_validate_json(candidate)
    except Exception as exc:
        raise RuntimeError(
            f"{label} khong tra JSON dung schema. Raw response={text!r}"
        ) from exc


def create_plan_execute_agent(
    model: BaseChatModel, tools: Sequence[BaseTool], *, verbose: bool = False
):
    """Tao va compile graph planner -> executor -> replanner.

    Model backend can ho tro ``with_structured_output``. Voi nhieu provider,
    tinh nang nay duoc xay tren structured tool calling / ``bind_tools``.
    """

    tools = list(tools)
    tools_by_name = {tool.name: tool for tool in tools}
    descriptions = _tool_descriptions(tools)

    def planner(state: PlanExecuteState) -> dict[str, Any]:
        if verbose:
            print("[PLANNER] Nhan yeu cau va tao plan ban dau")
        plan = _invoke_structured(
            model,
            Plan,
            [
                SystemMessage(
                    content=(
                        "Day la bai mo phong tren database tinh nam 2024; khong tu choi "
                        "vi ngay trong du lieu da qua. Lap ke hoach dat ve co thu tu. "
                        "Tim kiem truoc, chi dat ve sau "
                        "khi observation xac nhan chuyen bay ton tai va con cho."
                    )
                ),
                HumanMessage(content=state["user_request"]),
            ],
            "planner",
        )
        if verbose:
            print("[PLAN]", plan.steps)
        return {"plan": plan.steps}

    def executor(state: PlanExecuteState) -> dict[str, Any]:
        if not state["plan"]:
            return {"past_steps": []}

        step = state["plan"][0]
        if verbose:
            print("[EXECUTOR] Buoc hien tai:", step)
        instruction = _invoke_structured(
            model,
            ToolInstruction,
            [
                SystemMessage(
                    content=(
                        "Day la bai mo phong tren database tinh nam 2024. "
                        "Chon dung mot tool de thuc hien buoc hien tai. Khong lap them "
                        "ke hoach va khong bia tham so.\nCac tool:\n" + descriptions
                    )
                ),
                HumanMessage(
                    content=(
                        f"Yeu cau: {state['user_request']}\n"
                        f"Buoc hien tai: {step}\n"
                        f"Ket qua cac buoc truoc: {state.get('past_steps', [])}"
                    )
                ),
            ],
            "executor",
        )

        tool = tools_by_name.get(instruction.tool_name)
        if tool is None:
            observation = {
                "status": "error",
                "error": "unknown_tool",
                "requested_tool": instruction.tool_name,
                "valid_tools": sorted(tools_by_name),
            }
        else:
            try:
                if verbose:
                    print(f"[ACTION] {instruction.tool_name}({ascii(instruction.arguments)})")
                observation = tool.invoke(instruction.arguments)
            except Exception as exc:  # bien loi tool thanh observation cho replanner
                observation = {
                    "status": "error",
                    "error": type(exc).__name__,
                    "message": str(exc),
                }

        observation_text = json.dumps(observation, ensure_ascii=False, default=str)
        if verbose:
            print("[OBSERVATION]", ascii(observation_text))
        return {"past_steps": [(step, observation_text)]}

    def replanner(state: PlanExecuteState) -> dict[str, Any]:
        if verbose:
            print("[REPLANNER] Kiem tra observation va cap nhat plan")
        decision = _invoke_structured(
            model,
            ReplanDecision,
            [
                SystemMessage(
                    content=(
                        "Day la bai mo phong tren database tinh nam 2024. "
                        "Ban la replanner. Doc observation moi nhat. Neu success thi bo "
                        "buoc da xong. Neu khong co chuyen phu hop, tao buoc tim cac lua "
                        "chon gan nhat (gio/ngay/hang khac) nhung khong tu dat neu vuot "
                        "yeu cau nguoi dung. Neu tool error, dung hint de sua plan. "
                        "Neu observation co status=approval_required thi finish ngay "
                        "va hoi nguoi dung xac nhan; khong goi book_flight lan nua. "
                        "Chi finish khi yeu cau da hoan thanh hoac can nguoi dung quyet dinh."
                    )
                ),
                HumanMessage(
                    content=(
                        f"Yeu cau: {state['user_request']}\n"
                        f"Plan hien tai: {state['plan']}\n"
                        f"Lich su step/observation: {state.get('past_steps', [])}"
                    )
                ),
            ],
            "replanner",
        )
        if decision.action == "finish":
            if verbose:
                print("[REPLANNER -> END]", decision.reason)
            return {"plan": [], "response": decision.response or decision.reason}
        if verbose:
            print("[REPLAN]", decision.remaining_steps, "|", decision.reason)
        return {"plan": decision.remaining_steps, "response": ""}

    def route_after_replan(state: PlanExecuteState) -> str:
        return "finish" if state.get("response") else "continue"

    graph = StateGraph(PlanExecuteState)
    graph.add_node("planner", planner)
    graph.add_node("executor", executor)
    graph.add_node("replanner", replanner)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "executor")
    graph.add_edge("executor", "replanner")
    graph.add_conditional_edges(
        "replanner",
        route_after_replan,
        {"continue": "executor", "finish": END},
    )
    return graph.compile()


# ---------------------------------------------------------------------------
# Chay model that: python -m lib.model_pe
def _main() -> None:
    import os

    from dotenv import load_dotenv
    from langchain_openai import ChatOpenAI

    from harness import FlightHarness
    from tools_datve import book_flight, search_flight_info

    load_dotenv()
    model_name = os.getenv("OPENAI_MODEL")
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    if not model_name or not api_key:
        raise SystemExit("Can OPENAI_MODEL va OPENAI_API_KEY trong .env")

    model_kwargs = {"model": model_name, "api_key": api_key, "temperature": 0}
    if base_url:
        model_kwargs["base_url"] = base_url
    model = ChatOpenAI(**model_kwargs)
    harness = FlightHarness(search_flight_info, book_flight)
    tools = harness.langchain_tools()
    agent = create_plan_execute_agent(model, tools, verbose=True)

    request = (
        "Trong bài mô phỏng dùng database tĩnh, tìm rồi đặt vé Hà Nội đi "
        "Đà Nẵng ngày 2024-06-01."
    )
    print("FLOW: START -> PLANNER -> EXECUTOR -> REPLANNER -> ... -> END")
    print("USER:", request)
    result = agent.invoke(
        {
            "user_request": request,
            "plan": [],
            "past_steps": [],
            "response": "",
        },
        {"recursion_limit": 20},
    )
    print("\nFINAL:", ascii(result["response"]))
    print("PAST STEPS:", ascii(result.get("past_steps", [])))
    print("HARNESS:", ascii(harness.report()))
    if harness.pending_booking is not None:
        answer = input("\nXac nhan dat ve? [y/N]: ").strip().casefold()
        observation = harness.submit_booking(answer in {"y", "yes", "co", "có"})
        print("[USER SUBMIT OBSERVATION]", ascii(observation))


if __name__ == "__main__":
    _main()
