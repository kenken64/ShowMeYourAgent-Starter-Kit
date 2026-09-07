# -*- coding: utf-8 -*-
"""test_llm_gateway_langgraph_native.py

Native-tool-calling counterpart to test_llm_gateway_langgraph.py.

test_llm_gateway_langgraph.py works around the gateway's (previously) broken
tool-calling support by having the model emit a JSON blob in plain text,
parsed with a regex. This script instead uses the *real*, idiomatic LangGraph
pattern — `llm.bind_tools(tools)` + the prebuilt `ToolNode` / `tools_condition`
— the same pattern that sits as unused "dead code" in test_llm_gateway.py
because it never worked against the old, broken gateway.

It targets the LOCAL ollama-proxy-bedrock instance (http://localhost:11434),
which has been verified (see ../ollama-proxy-bedrock/test_tool_calling.py) to
correctly translate Ollama-style `tools` into Bedrock's native toolConfig.
The public gateway (https://api.softwaresystems.app, from LLM_GATEWAY_URL in
.env) is running a stale build without that fix, so this script intentionally
does NOT read LLM_GATEWAY_URL from .env — point LOCAL_GATEWAY_URL below at
wherever your local/redeployed proxy lives once it's updated.
"""

import os
from typing import Annotated, Any, Dict, List, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import AnyMessage
from langchain_core.tools import tool
from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

load_dotenv()

LOCAL_GATEWAY_URL = os.getenv("LOCAL_GATEWAY_URL", "http://localhost:11434")
LLM_GATEWAY_API_KEY = os.getenv("LLM_GATEWAY_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "global.anthropic.claude-sonnet-4-5-20250929-v1:0")

SYSTEM = """You are a travel cost estimation agent.

Output format:
1) Total cost (with assumptions)
"""


@tool
def estimate_trip_cost(
    destination: str,
    days: int,
    travelers: int,
    comfort: str = "mid",
) -> Dict[str, Any]:
    """
    Estimate a rough trip budget (SGD) using simple heuristics.
    comfort: budget | mid | premium
    Returns a breakdown and total estimate in SGD.
    """
    if days <= 0 or travelers <= 0:
        raise ValueError("days and travelers must be > 0")

    comfort = comfort.lower().strip()
    if comfort not in {"budget", "mid", "premium"}:
        raise ValueError("comfort must be one of: budget, mid, premium")

    lodging_pppd = {"budget": 60, "mid": 140, "premium": 300}[comfort]
    food_pppd = {"budget": 30, "mid": 60, "premium": 120}[comfort]
    local_transport_pppd = {"budget": 10, "mid": 20, "premium": 50}[comfort]
    activities_pppd = {"budget": 20, "mid": 50, "premium": 120}[comfort]

    lodging = lodging_pppd * travelers * days
    food = food_pppd * travelers * days
    transport = local_transport_pppd * travelers * days
    activities = activities_pppd * travelers * days

    subtotal = lodging + food + transport + activities
    contingency = round(subtotal * 0.12)
    total = subtotal + contingency

    return {
        "destination": destination,
        "days": days,
        "travelers": travelers,
        "comfort": comfort,
        "currency": "SGD",
        "breakdown": {
            "lodging": lodging,
            "food": food,
            "local_transport": transport,
            "activities": activities,
            "contingency": contingency,
        },
        "total_estimate": total,
        "note": "Heuristic estimate excludes international flights/insurance/visa fees.",
    }


tools = [estimate_trip_cost]

llm = ChatOllama(
    model=LLM_MODEL,
    base_url=LOCAL_GATEWAY_URL,
    temperature=0.0,
    num_predict=300,
    client_kwargs={"headers": {"X-API-Key": LLM_GATEWAY_API_KEY}} if LLM_GATEWAY_API_KEY else {},
)
llm_with_tools = llm.bind_tools(tools)


class State(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]


def chatbot(state: State):
    return {"messages": [llm_with_tools.invoke(state["messages"])]}


builder = StateGraph(State)
builder.add_node("chatbot", chatbot)
builder.add_node("tools", ToolNode(tools))

builder.set_entry_point("chatbot")
builder.add_conditional_edges("chatbot", tools_condition)  # -> "tools" or END
builder.add_edge("tools", "chatbot")

graph = builder.compile()


def run_agent(user_msg: str, max_iterations: int = 4) -> str:
    initial_state: State = {
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_msg},
        ]
    }
    final_state = graph.invoke(
        initial_state,
        config={"recursion_limit": 2 * max_iterations + 1},
    )
    for msg in final_state["messages"]:
        role = getattr(msg, "type", getattr(msg, "role", "?"))
        tool_calls = getattr(msg, "tool_calls", None)
        print(f">>> [{role}] {msg.content!r}"
              + (f"  tool_calls={tool_calls}" if tool_calls else ""))
    return final_state["messages"][-1].content


if __name__ == "__main__":
    print(f"Target: {LOCAL_GATEWAY_URL}  (model={LLM_MODEL})")
    print("-" * 72)
    msg = "Plan a 2-day Tokyo trip for 2 adults. Mid comfort. how much will be the cost"
    answer = run_agent(msg)
    print("-" * 72)
    print("FINAL ANSWER:")
    print(answer)
    if "1210" in answer or "1,210" in answer:
        print("\nPASS: final answer contains the tool-derived value SGD 1,210.")
    else:
        print("\nCHECK: final answer does not obviously contain SGD 1,210.")
