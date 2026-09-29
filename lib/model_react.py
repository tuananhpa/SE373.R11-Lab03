"""ReAct agent: model chon tool, doc observation, roi quyet dinh buoc tiep theo.

Module nay khong tu tao provider model. Ham ``create_react_agent`` nhan mot
``BaseChatModel`` da duoc cau hinh tu ben ngoai, vi the co the dung cung OpenAI,
Anthropic, model gia, hoac bat ky backend nao ho tro ``bind_tools``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool


REACT_SYSTEM_PROMPT = """Ban la agent dat ve may bay theo kieu ReAct.

Quy tac:
- Luon goi search_flight_info truoc khi dat ve.
- Chi goi book_flight voi mot chuyen bay co trong observation cua tool tim kiem.
- Neu tool bao loi, doc message va hint cua tool de chon hanh dong tiep theo.
- Khong tu bia gia, gio bay, tinh trang cho hay ket qua dat ve.
- Book flight la hanh dong co side effect; chi thuc hien khi yeu cau nguoi dung da ro.
- Khi da co du ket qua, ngung goi tool va tra loi ngan gon.
"""


def create_react_agent(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    *,
    system_prompt: str = REACT_SYSTEM_PROMPT,
    middleware: Sequence[Any] = (),
):
    """Tao ReAct graph dung san cua LangChain.

    Flow noi bo:
        model -> (co tool_calls?) -> tools -> ToolMessage -> model
                              `----> khong -> END

    ``model`` phai ho tro ``bind_tools``. ``create_agent`` tra ve mot compiled
    LangGraph, nen ket qua co cac ham ``invoke``, ``ainvoke`` va ``stream``.
    """

    return create_agent(
        model=model,
        tools=list(tools),
        system_prompt=system_prompt,
        middleware=list(middleware),
    )


# ---------------------------------------------------------------------------
# Chay model that: python -m lib.model_react
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

    model_kwargs = {"model": model_name, "api_key": api_key, "temperature": 0.2}
    if base_url:
        model_kwargs["base_url"] = base_url
    model = ChatOpenAI(**model_kwargs)
    harness = FlightHarness(search_flight_info, book_flight)
    tools = harness.langchain_tools()
    agent = create_react_agent(
        model, tools, middleware=[HarnessGuardMiddleware(harness)]
    )

    request = "Tìm rồi đặt vé Hà Nội đi Đà Nẵng ngày 2024-06-01 với giá rẻ nhất."
    print("FLOW: START -> MODEL <-> TOOL -> END")
    print("USER:", request)
    final_message = None
    for update in agent.stream(
        {"messages": [{"role": "user", "content": request}]},
        stream_mode="updates",
    ):
        node, data = next(iter(update.items()))
        print(f"\n[NODE {node.upper()}]")
        if data is None:
            print("(khong co state update)")
            continue
        if not isinstance(data, dict):
            print(ascii(data))
            continue
        for message in data.get("messages", []):
            print(ascii(message))
            final_message = message
    if final_message is not None:
        print("\nFINAL:", ascii(final_message.content))
    print("\nHARNESS:", ascii(harness.report()))
    if harness.pending_booking is not None:
        answer = input("\nXac nhan dat ve? [y/N]: ").strip().casefold()
        observation = harness.submit_booking(answer in {"y", "yes", "co", "có"})
        print("[USER SUBMIT OBSERVATION]", ascii(observation))


if __name__ == "__main__":
    _main()
