# Lớp 
"""Harness doc lap nam giua agent va cac tool co side effect.

Agent chi *de xuat* tool call. Harness quyet dinh co cho thuc thi hay khong,
ghi observation, phat hien lap, gioi han ngan sach va yeu cau human approval
truoc khi goi ``book_flight``.
"""

from __future__ import annotations

import json
from collections import Counter, deque
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool


class FlightHarness:
    """Policy layer doc lap voi model va LangGraph.

    Flow an toan:
        search -> observation -> model chon flight -> book proposal
        -> approval_required -> user submit -> harness goi book tool that
    """

    def __init__(
        self,
        search_tool: Callable[..., Any],
        book_tool: Callable[..., Any],
        *,
        max_tool_calls: int = 12,
        loop_window: int = 6,
        repeat_limit: int = 2,
    ) -> None:
        self._search_tool = search_tool
        self._book_tool = book_tool
        self.max_tool_calls = max_tool_calls
        self.repeat_limit = repeat_limit
        self._recent: deque[str] = deque(maxlen=loop_window)
        self.history: list[dict[str, Any]] = []
        self.last_search_results: list[dict[str, Any]] = []
        self.pending_booking: dict[str, Any] | None = None
        self.stop_reason: str | None = None

    @staticmethod
    def _fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
        return json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _record(
        self, tool_name: str, arguments: dict[str, Any], observation: Any
    ) -> Any:
        self.history.append(
            {
                "tool": tool_name,
                "arguments": dict(arguments),
                "observation": observation,
            }
        )
        return observation

    def _guard(self, tool_name: str, arguments: dict[str, Any]) -> dict | None:
        if self.stop_reason:
            return {"status": "stopped", "reason": self.stop_reason}

        if len(self.history) >= self.max_tool_calls:
            self.stop_reason = f"tool_budget_exceeded:{self.max_tool_calls}"
            return {"status": "stopped", "reason": self.stop_reason}

        fingerprint = self._fingerprint(tool_name, arguments)
        count = Counter(self._recent)[fingerprint] + 1
        if count >= self.repeat_limit:
            self.stop_reason = (
                f"loop_detected:{tool_name} repeated {count} times "
                f"inside last {self._recent.maxlen} calls"
            )
            return {"status": "stopped", "reason": self.stop_reason}

        self._recent.append(fingerprint)
        return None

    def search_flight_info(
        self,
        departure: str,
        arrival: str,
        date: str,
        hour: str | None = None,
    ) -> Any:
        arguments = {
            "departure": departure,
            "arrival": arrival,
            "date": date,
            "hour": hour,
        }
        blocked = self._guard("search_flight_info", arguments)
        if blocked:
            return self._record("search_flight_info", arguments, blocked)

        result = self._search_tool(**arguments)
        self.last_search_results = result if isinstance(result, list) else []
        observation = {
            "status": "ok",
            "count": len(self.last_search_results),
            "results": self.last_search_results,
        }
        return self._record("search_flight_info", arguments, observation)

    def propose_booking(
        self,
        airline: str,
        departure: str,
        arrival: str,
        date: str,
        hour: str,
    ) -> dict[str, Any]:
        """Khong dat ve; chi tao pending booking de cho user submit."""

        arguments = {
            "airline": airline,
            "departure": departure,
            "arrival": arrival,
            "date": date,
            "hour": hour,
        }

        if self.pending_booking == arguments:
            return self._record(
                "book_flight",
                arguments,
                {
                    "status": "approval_required",
                    "pending_booking": arguments,
                    "message": "Dang cho user submit; khong goi book_flight lan nua.",
                },
            )

        blocked = self._guard("book_flight", arguments)
        if blocked:
            return self._record("book_flight", arguments, blocked)

        matched = next(
            (
                flight
                for flight in self.last_search_results
                if str(flight.get("Airline", "")).casefold() == airline.casefold()
                and str(flight.get("Departure", "")).casefold() == departure.casefold()
                and str(flight.get("Arrival", "")).casefold() == arrival.casefold()
                and flight.get("Date") == date
                and flight.get("Hour") == hour
                and flight.get("State") == "available"
            ),
            None,
        )
        if matched is None:
            return self._record(
                "book_flight",
                arguments,
                {
                    "status": "error",
                    "error": "flight_not_verified",
                    "hint": "Phai search va chon dung chuyen available trong observation.",
                },
            )

        self.pending_booking = arguments
        return self._record(
            "book_flight",
            arguments,
            {
                "status": "approval_required",
                "pending_booking": arguments,
                "matched_flight": matched,
                "message": "Can user submit approve/reject truoc khi dat ve that.",
            },
        )

    def submit_booking(self, approved: bool) -> dict[str, Any]:
        """Diem duy nhat duoc phep goi side-effect book tool that."""

        if self.pending_booking is None:
            return {"status": "error", "error": "no_pending_booking"}

        arguments = self.pending_booking
        self.pending_booking = None
        if not approved:
            self.stop_reason = "user_rejected_booking"
            return self._record(
                "user_submit", arguments, {"status": "rejected", "booking": arguments}
            )

        result = self._book_tool(**arguments)
        if isinstance(result, dict) and result.get("status") == "success":
            self.stop_reason = "booking_completed"
        return self._record("user_submit", arguments, result)

    def langchain_tools(self) -> list[StructuredTool]:
        """Tool proxy cho agent; moi call deu di qua harness policy."""

        return [
            StructuredTool.from_function(
                self.search_flight_info,
                name="search_flight_info",
                description=(
                    "Tim chuyen bay. hour la tuy chon. Ket qua duoc harness ghi lai "
                    "de xac minh booking sau nay."
                ),
            ),
            StructuredTool.from_function(
                self.propose_booking,
                name="book_flight",
                description=(
                    "De xuat dat chuyen da search. Tool KHONG dat ngay; no tra "
                    "approval_required va cho user submit."
                ),
            ),
        ]

    def report(self) -> dict[str, Any]:
        return {
            "stop_reason": self.stop_reason,
            "pending_booking": self.pending_booking,
            "tool_call_count": len(self.history),
            "history": self.history,
        }


class HarnessGuardMiddleware(AgentMiddleware):
    """Chan agent goi them tool khi harness dang cho nguoi hoac da dung."""

    def __init__(self, harness: FlightHarness) -> None:
        super().__init__()
        self.harness = harness

    @hook_config(can_jump_to=["end"])
    def after_model(self, state, runtime):
        message = state["messages"][-1]
        if not getattr(message, "tool_calls", None):
            return None

        if self.harness.pending_booking is not None:
            pending = json.dumps(
                self.harness.pending_booking, ensure_ascii=False, default=str
            )
            return {
                "jump_to": "end",
                "messages": [
                    AIMessage(
                        content=(
                            "Can nguoi dung xac nhan truoc khi dat ve. "
                            f"Pending booking: {pending}"
                        )
                    )
                ],
            }

        if self.harness.stop_reason is not None:
            return {
                "jump_to": "end",
                "messages": [
                    AIMessage(content=f"Harness da dung: {self.harness.stop_reason}")
                ],
            }
        return None
