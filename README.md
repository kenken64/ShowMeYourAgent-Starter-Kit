# LLM Gateway — Travel Cost Estimation Agent

A LangChain/LangGraph test script that connects to an **AWS-hosted LLM Gateway** (Ollama-compatible API fronting Claude Sonnet 4.5, behind an AWS Application Load Balancer) and demonstrates **tool calling** with a travel cost estimation agent.

This project is used to **test the AWS API key provided LLM Gateway** — verifying connectivity, authentication via `X-API-Key` header, and end-to-end tool-calling behavior through the AWS ALB endpoint.

## What it does

1. Connects to the **AWS LLM Gateway** via `ChatOllama` (Ollama-compatible `/api/chat` endpoint behind an AWS ALB)
2. Authenticates using the provided **AWS API key** (sent as `X-API-Key` header)
3. Sends a user query: *"Plan a 2-day Tokyo trip for 2 adults. Mid comfort. how much will be the cost"*
4. The model emits a JSON tool request (the gateway has no native tool-call support, so a JSON protocol is used)
5. The script parses the JSON, **actually invokes** the `estimate_trip_cost` tool, and feeds the result back
6. The model presents the final answer driven by the real tool output
7. Prints the auto-generated OpenAI-compatible tool schema

### Example output

```
>>> Tool request: {'tool': 'estimate_trip_cost', 'args': {'destination': 'Tokyo', 'days': 2, 'travelers': 2, 'comfort': 'mid'}}
>>> Tool result: {'destination': 'Tokyo', ..., 'total_estimate': 1210, ...}

# 2-Day Tokyo Trip Cost Estimate for 2 Adults (Mid Comfort)
## Total Estimated Cost: SGD $1,210
...
```

## Setup

### 1. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install langgraph langchain-openai python-dotenv langchain_ollama ipython
```

### 2. Create a `.env` file

Copy the template below into a file named `.env` in the project root. Replace the placeholder values with the **AWS-provided** credentials:

```env
# AWS LLM Gateway configuration
LLM_GATEWAY_URL=http://llm-wrapper-alb-2110380302.ap-southeast-1.elb.amazonaws.com
LLM_GATEWAY_API_KEY=your-aws-provided-api-key-here
LLM_MODEL=global.anthropic.claude-sonnet-4-5-20250929-v1:0
```

| Variable | Description |
|---|---|
| `LLM_GATEWAY_URL` | AWS ALB endpoint of the Ollama-compatible LLM gateway |
| `LLM_GATEWAY_API_KEY` | AWS-provided API key, sent as `X-API-Key` header for authentication |
| `LLM_MODEL` | Model identifier available on the gateway |

> **Note:** All three variables are **required** — the script will raise an error if any are missing.

### 3. Run

```bash
.venv/bin/python test_llm_gateway.py
```

## Project structure

```
.
├── test_llm_gateway.py   # Main script — agent loop + tool calling
├── .env                  # Gateway URL, API key, model (not committed)
└── .venv/                # Python virtual environment
```

## How tool calling works here

The gateway does **not** implement Ollama's native tool-calling protocol (the `tools` field in `/api/chat` is silently ignored). To work around this:

1. **System prompt** instructs the model to reply with only a JSON tool request when a cost estimate is needed:
   ```json
   {"tool": "estimate_trip_cost", "args": {"destination": "Tokyo", "days": 2, "travelers": 2, "comfort": "mid"}}
   ```
2. **`extract_tool_call()`** parses the JSON from the model's text response
3. **`run_agent()`** executes the tool via `estimate_trip_cost.invoke(args)` and feeds the result back to the model
4. The model then presents the final answer using the **real** tool data

The LangGraph `StateGraph` + `ToolNode` setup is still present in the script (lines 140–191) for reference, but the actual run uses the manual loop since native tool calls never arrive from this gateway.

## The `estimate_trip_cost` tool

A heuristic trip budget estimator (SGD). Per-person-per-day rates:

| Category | Budget | Mid | Premium |
|---|---|---|---|
| Lodging | 60 | 140 | 300 |
| Food | 30 | 60 | 120 |
| Local transport | 10 | 20 | 50 |
| Activities | 20 | 50 | 120 |

Plus a 12% contingency buffer. Excludes international flights, insurance, and visa fees.

## Known issues & notes

- **AWS ALB rate limiting:** The gateway's Application Load Balancer may return `403 Forbidden` on rapid successive requests. The script includes `invoke_with_retry()` with linear backoff (3s, 6s, 9s…) to handle this.
- **Duplicate `SYSTEM` prompt:** The first `SYSTEM` definition (lines 42–53) is dead code — the second definition (lines 55–68) overwrites it. Kept for reference from the original Colab notebook.
- **Graph diagram:** `draw_mermaid_png()` renders as an IPython `Image` object — visible in Jupyter/Colab but not in terminal output.
