# -*- coding: utf-8 -*-
"""test_llm_gateway_langgraph.py

LangGraph-driven version of test_llm_gateway.py.

The AWS LLM Gateway does not implement Ollama's native tool-calling protocol
(the `tools` field in /api/chat is silently ignored), so the model is
prompted to emit a JSON tool request as plain text instead. Unlike
test_llm_gateway.py — which drives that loop with a plain `while` loop —
this script encodes the same request/tool/respond cycle as an actual
LangGraph StateGraph: a conditional edge inspects the model's text output
for a JSON tool call, routes to a tool node when one is found, and loops
back to the model with the tool result until a final answer is produced.
"""

import json
import re
import time
from typing import Annotated, Any, Dict, List, TypedDict

import os
from dotenv import load_dotenv
from langchain_core.messages import AnyMessage
from langchain_core.tools import tool
from langchain_ollama import ChatOllama
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

"""## 1) Load environment variables (see README.md)"""

load_dotenv()

"""## 2) Prompts"""

SYSTEM = """You are a travel cost estimation agent.

Output format:
1) Total cost (with assumptions)
"""

# Tool-calling instructions sent as part of the user message (not system prompt).
# Useful when the gateway overrides or ignores the system prompt.
TOOL_CALL_INSTRUCTIONS = """
When the user asks for a cost estimate, you MUST first reply with ONLY this JSON and nothing else:
{"tool": "estimate_trip_cost", "args": {"destination": "<str>", "days": <int>, "travelers": <int>, "comfort": "<budget|mid|premium>"}}
Do not invent cost figures. After you receive the tool result, present it to the user.
"""

"""## 3) Tool - estimate trip cost"""

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

    # Very rough per-person-per-day estimates (SGD) excluding flights
    lodging_pppd = {"budget": 60, "mid": 140, "premium": 300}[comfort]
    food_pppd = {"budget": 30, "mid": 60, "premium": 120}[comfort]
    local_transport_pppd = {"budget": 10, "mid": 20, "premium": 50}[comfort]
    activities_pppd = {"budget": 20, "mid": 50, "premium": 120}[comfort]

    lodging = lodging_pppd * travelers * days
    food = food_pppd * travelers * days
    transport = local_transport_pppd * travelers * days
    activities = activities_pppd * travelers * days

    subtotal = lodging + food + transport + activities
    contingency = round(subtotal * 0.12)  # 12% buffer
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
tools_by_name = {t.name: t for t in tools}

"""## 4) LLM gateway configuration - loaded from .env (see README.md)"""

LLM_GATEWAY_URL = os.getenv("LLM_GATEWAY_URL")
LLM_GATEWAY_API_KEY = os.getenv("LLM_GATEWAY_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL")

if not all([LLM_GATEWAY_URL, LLM_GATEWAY_API_KEY, LLM_MODEL]):
    raise EnvironmentError(
        "Missing required env vars. Please create a .env file with:\n"
        "  LLM_GATEWAY_URL, LLM_GATEWAY_API_KEY, LLM_MODEL\n"
        "See README.md for details."
    )

llm = ChatOllama(
    model=LLM_MODEL,
    base_url=LLM_GATEWAY_URL,
    temperature=0.4,
    num_predict=2000,
    client_kwargs={
        "headers": {
            "X-API-Key": LLM_GATEWAY_API_KEY
        }
    }
)


def invoke_with_retry(messages, max_retries=5, backoff=3):
    """Retry llm.invoke on transient gateway errors (ALB rate-limits rapid successive calls)."""
    for attempt in range(1, max_retries + 1):
        try:
            return llm.invoke(messages)
        except Exception as e:
            if attempt == max_retries:
                raise
            wait = backoff * attempt
            print(f">>> Attempt {attempt} failed ({type(e).__name__}), retrying in {wait}s...")
            time.sleep(wait)


def extract_tool_call(text: str):
    """Return {"tool": ..., "args": {...}} if the model emitted a tool request."""
    match = re.search(r'\{.*"tool".*\}', text, re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if payload.get("tool") and isinstance(payload.get("args"), dict):
        return payload
    return None


"""## 5) Build the LangGraph agent loop

Because the gateway ignores native tool-calling, the "tools_condition" /
ToolNode prebuilts (which key off `message.tool_calls`) don't apply here.
Routing and tool execution are done manually instead, based on the JSON
the model puts in its text response.
"""

class State(TypedDict):
    # add_messages makes state["messages"] append-only and compatible with tool loops
    messages: Annotated[List[AnyMessage], add_messages]


def chatbot(state: State):
    reply = invoke_with_retry(state["messages"])
    return {"messages": [reply]}


def route_after_chatbot(state: State) -> str:
    last_msg = state["messages"][-1]
    tool_req = extract_tool_call(last_msg.content)
    return "tools" if tool_req else END


def call_tool(state: State):
    last_msg = state["messages"][-1]
    tool_req = extract_tool_call(last_msg.content)
    print(f">>> Tool request: {tool_req}")

    tool_fn = tools_by_name[tool_req["tool"]]
    result = tool_fn.invoke(tool_req["args"])
    print(f">>> Tool result: {result}")

    return {
        "messages": [
            {
                "role": "user",
                "content": f"Tool result: {json.dumps(result)}\nNow present the estimate to the user.",
            }
        ]
    }


builder = StateGraph(State)
builder.add_node("chatbot", chatbot)
builder.add_node("tools", call_tool)

builder.add_edge(START, "chatbot")
builder.add_conditional_edges("chatbot", route_after_chatbot, {"tools": "tools", END: END})
builder.add_edge("tools", "chatbot")  # tool result goes back to model for the next step

graph = builder.compile()

"""## 6) Run"""

def pretty_print(state: Dict[str, Any]):
    last_msg = state["messages"][-1]

    if isinstance(last_msg.content, list):
        text = "".join(
            block["text"]
            for block in last_msg.content
            if block.get("type") == "text"
        )
    else:
        text = last_msg.content

    print(text)


def run_agent(user_msg: str, max_iterations: int = 4) -> str:
    initial_state: State = {
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": TOOL_CALL_INSTRUCTIONS + "\n" + user_msg},
        ]
    }
    # Each manual-loop "iteration" is a chatbot -> tools -> chatbot round trip,
    # i.e. 2 graph steps, plus 1 for the initial chatbot call.
    try:
        final_state = graph.invoke(
            initial_state,
            config={"recursion_limit": 2 * max_iterations + 1},
        )
    except GraphRecursionError:
        return "Max tool-call iterations reached."
    return final_state["messages"][-1].content


if __name__ == "__main__":
    msg = "Plan a 2-day Tokyo trip for 2 adults. Mid comfort. how much will be the cost"
    print(run_agent(msg))
