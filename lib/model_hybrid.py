"""Hybrid agent: Plan-and-Execute o ngoai, ReAct executor o ben trong.

Replanner chi la mot node cua hybrid. Phan lam kien truc nay thanh "hybrid" la
moi buoc cap cao duoc giao cho mot ReAct agent co the goi tool nhieu vong.
"""

from __future__ import annotations

import json
import operator
import re
from collections.abc import Sequence
from typing import Annotated, Any, Literal, TypedDict

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field


class HybridPlan(BaseModel):
    steps: list[str] = Field(description="Cac muc tieu cap cao theo dung thu tu")


class HybridReplanDecision(BaseModel):
    action: Literal["continue", "finish"]
    remaining_steps: list[str] = Field(default_factory=list)
    response: str | None = None
    reason: str


class HybridState(TypedDict):
    user_request: str
    plan: list[str]
    past_steps: Annotated[list[tuple[str, str]], operator.add]
    response: str


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    return content if isinstance(content, str) else str(content)


def _invoke_structured(
    model: BaseChatModel,
    schema: type[BaseModel],
    messages: list,
    label: str,
) -> BaseModel:
    """Yeu cau JSON truc tiep cho OpenAI-compatible endpoint.

    Cach nay khong phu thuoc endpoint co ho tro structured function calling
    hay khong; Pydantic van la lop kiem tra output cuoi cung.
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


def create_hybrid_agent(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    *,
    verbose: bool = False,
    middleware: Sequence[Any] = (),
):
    """Tao graph planner -> ReAct executor -> replanner.

    Mot buoc plan co the tao nhieu vong model/tool trong ReAct sub-agent. Sau
    khi sub-agent dung, replanner doc ket qua de ket thuc hoac sua plan.
    """

    tools = list(tools)
    tool_descriptions = "\n".join(
        f"- {tool.name}: {tool.description}; schema={tool.args}" for tool in tools
    )
    react_executor = create_agent(
        model=model,
        tools=tools,
        middleware=list(middleware),
        system_prompt=(
            "Day la bai mo phong tren database tinh nam 2024; khong tu choi vi "
            "ngay trong du lieu da qua. Ban la ReAct executor cho mot buoc trong "
            "ke hoach dat ve. Goi tool, "
            "doc observation va tu sua cach lam trong pham vi buoc duoc giao. "
            "search_flight_info co the bo trong hour; khong hoi gio neu nguoi dung "
            "chua neu gio. Chi dung tool trong danh sach sau:\n"
            + tool_descriptions
            + "\n"
            "Khong bia du lieu. Neu can thay doi muc tieu cap cao hoac can nguoi "
            "dung chon, hay dung va bao ro."
        ),
    )

    def planner(state: HybridState) -> dict[str, Any]:
        if verbose:
            print("[PLANNER] Tao muc tieu cap cao")
        plan = _invoke_structured(
            model,
            HybridPlan,
            [
                SystemMessage(
                    content=(
                        "Day la bai mo phong tren database tinh nam 2024; khong tu "
                        "choi vi ngay trong du lieu da qua. Lap cac muc tieu cap cao "
                        "cho yeu cau dat ve. Moi muc tieu "
                        "se duoc mot ReAct agent thuc hien. Chi tao muc tieu co the "
                        "thuc hien bang cac tool that; khong tao buoc mo giao dien, "
                        "truy cap database, luu ma hay nhap thong tin neu tool khong "
                        "ho tro. search_flight_info cho phep bo trong hour.\nCac tool:\n"
                        + tool_descriptions
                    )
                ),
                HumanMessage(content=state["user_request"]),
            ],
            "hybrid planner",
        )
        if verbose:
            print("[PLAN]", plan.steps)
        return {"plan": plan.steps}

    def react_execute(state: HybridState) -> dict[str, Any]:
        if not state["plan"]:
            return {"past_steps": []}

        step = state["plan"][0]
        if verbose:
            print("[REACT EXECUTOR] Nhan muc tieu:", step)
        result = react_executor.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Yeu cau goc: {state['user_request']}\n"
                            f"Chi thuc hien buoc hien tai: {step}\n"
                            f"Ket qua truoc do: {state.get('past_steps', [])}"
                        ),
                    }
                ]
            }
        )
        messages = result["messages"]
        final_message = messages[-1]
        tool_observations = [
            _message_text(message) for message in messages if message.type == "tool"
        ]
        observation = json.dumps(
            {
                "tool_observations": tool_observations,
                "final_answer": _message_text(final_message),
            },
            ensure_ascii=False,
        )
        if verbose:
            for message in messages:
                tool_calls = getattr(message, "tool_calls", None)
                if tool_calls:
                    print("[REACT ACTION]", ascii(tool_calls))
                if message.type == "tool":
                    print("[REACT OBSERVATION]", ascii(_message_text(message)))
            print("[EXECUTOR RESULT]", ascii(observation))
        return {"past_steps": [(step, observation)]}

    def replanner(state: HybridState) -> dict[str, Any]:
        if verbose:
            print("[REPLANNER] Danh gia ket qua cua ca ReAct sub-agent")
        decision = _invoke_structured(
            model,
            HybridReplanDecision,
            [
                SystemMessage(
                    content=(
                        "Day la bai mo phong tren database tinh nam 2024. Kiem tra "
                        "ket qua cua ReAct executor. Neu buoc thanh cong, bo "
                        "buoc do va tiep tuc. Neu observation cho thay khong co chuyen "
                        "phu hop, lap ke hoach tim lua chon gan nhat nhu gio/ngay/hang "
                        "khac. Khong tu thay doi rang buoc quan trong cua nguoi dung; "
                        "neu can thi finish bang mot cau hoi xin xac nhan. Neu executor "
                        "dang hoi hoac cho thong tin tu nguoi dung thi BAT BUOC action="
                        "finish ngay, khong tao step cho doi/hoi lai. Neu raw tool "
                        "observation co status=approval_required cung phai finish ngay."
                    )
                ),
                HumanMessage(
                    content=(
                        f"Yeu cau: {state['user_request']}\n"
                        f"Plan hien tai: {state['plan']}\n"
                        f"Ket qua executor: {state.get('past_steps', [])}"
                    )
                ),
            ],
            "hybrid replanner",
        )
        if decision.action == "finish":
            if verbose:
                print("[REPLANNER -> END]", decision.reason)
            return {"plan": [], "response": decision.response or decision.reason}
        if verbose:
            print("[REPLAN]", decision.remaining_steps, "|", decision.reason)
        return {"plan": decision.remaining_steps, "response": ""}

    def route_after_replan(state: HybridState) -> str:
        return "finish" if state.get("response") else "continue"

    graph = StateGraph(HybridState)
    graph.add_node("planner", planner)
    graph.add_node("react_executor", react_execute)
    graph.add_node("replanner", replanner)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "react_executor")
    graph.add_edge("react_executor", "replanner")
    graph.add_conditional_edges(
        "replanner",
        route_after_replan,
        {"continue": "react_executor", "finish": END},
    )
    return graph.compile()


# ---------------------------------------------------------------------------
# Chay model that: python -m lib.model_hybrid
def _main() -> None:
    import os

    from dotenv import load_dotenv
    from langchain_openai import ChatOpenAI

    from harness import FlightHarness, HarnessGuardMiddleware
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
    agent = create_hybrid_agent(
        model,
        tools,
        verbose=True,
        middleware=[HarnessGuardMiddleware(harness)],
    )

    request = (
        "Trong bài mô phỏng dùng database tĩnh, tìm rồi đặt vé Hà Nội đi "
        "Đà Nẵng ngày 2024-06-01."
    )
    print("FLOW: START -> PLANNER -> [REACT MODEL <-> TOOL] -> REPLANNER -> END")
    print("USER:", request)
    result = agent.invoke(
        {
            "user_request": request,
            "plan": [],
            "past_steps": [],
            "response": "",
        },
        {"recursion_limit": 30},
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
