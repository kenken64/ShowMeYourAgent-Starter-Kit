#!/usr/bin/env python3
"""Weather demo client — full Ollama tool-calling loop against the proxy.

Flow:
  1. Send the user's question + the get_weather tool schema to the proxy
  2. The model answers with a tool call: get_weather({"city": ...})
  3. THIS script executes the tool — calls the real Open-Meteo API
     (free, no API key needed) after geocoding the city name
  4. Send the real weather data back as a role:"tool" message
  5. The model writes the final natural-language answer

Credentials come from .env (or real environment variables):
  LLM_GATEWAY_URL      e.g. http://your-llm-gateway:11434
  LLM_GATEWAY_API_KEY  gateway API key (sent as X-API-Key header)
  LLM_MODEL            model id or proxy alias (e.g. sonnet4.5)

Stdlib only — no pip installs needed. Requires internet access for Open-Meteo.

Usage:
  python3 weather_demo.py "What's the weather in Singapore?"
  python3 weather_demo.py                    # default question
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def load_dotenv(path=".env"):
    """Minimal .env loader — real env vars take precedence."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    except FileNotFoundError:
        pass


load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

LLM_GATEWAY_URL = os.getenv("LLM_GATEWAY_URL")
LLM_GATEWAY_API_KEY = os.getenv("LLM_GATEWAY_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL")

if not all([LLM_GATEWAY_URL, LLM_GATEWAY_API_KEY, LLM_MODEL]):
    raise EnvironmentError(
        "Missing required env vars. Please create a .env file with:\n"
        "  LLM_GATEWAY_URL, LLM_GATEWAY_API_KEY, LLM_MODEL\n"
        "See README.md for details."
    )

# ---------------------------------------------------------------------------
# Tool schema (Ollama / OpenAI format)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "City name, e.g. Singapore",
                    }
                },
                "required": ["city"],
            },
        },
    }
]

# WMO weather interpretation codes -> short description
WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    61: "slight rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "slight snow", 73: "snow", 75: "heavy snow",
    77: "snow grains",
    80: "slight rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


def http_get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "weather-demo/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Tool implementation — the part the CLIENT executes (not the proxy)
# ---------------------------------------------------------------------------

def get_weather(city: str) -> dict:
    """Real weather lookup: geocode city -> Open-Meteo current conditions."""
    geo = http_get_json(
        "https://geocoding-api.open-meteo.com/v1/search?"
        + urllib.parse.urlencode({"name": city, "count": 1, "language": "en"})
    )
    results = geo.get("results") or []
    if not results:
        return {"error": f"City not found: {city}"}

    place = results[0]
    wx = http_get_json(
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode({
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                       "weather_code,wind_speed_10m",
        })
    )
    cur = wx.get("current", {})
    return {
        "city": place.get("name", city),
        "country": place.get("country", ""),
        "temperature_c": cur.get("temperature_2m"),
        "feels_like_c": cur.get("apparent_temperature"),
        "humidity_pct": cur.get("relative_humidity_2m"),
        "wind_kmh": cur.get("wind_speed_10m"),
        "condition": WMO_CODES.get(cur.get("weather_code"), "unknown"),
    }


# ---------------------------------------------------------------------------
# Proxy call
# ---------------------------------------------------------------------------

def chat(messages, tools=None, num_predict=512) -> dict:
    """POST /api/chat to the Ollama proxy (non-streaming)."""
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": num_predict},
    }
    if tools:
        payload["tools"] = tools

    req = urllib.request.Request(
        LLM_GATEWAY_URL.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": LLM_GATEWAY_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"Proxy error HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    question = " ".join(sys.argv[1:]) or "What's the weather in Singapore right now?"

    print(f"Proxy : {LLM_GATEWAY_URL}")
    print(f"Model : {LLM_MODEL}")
    print(f"You   : {question}")
    print("-" * 64)

    messages: list[dict] = [{"role": "user", "content": question}]

    # Step 1: ask — model should respond with a tool call
    resp = chat(messages, tools=TOOLS)
    msg = resp.get("message", {})
    tool_calls = msg.get("tool_calls") or []

    if not tool_calls:
        # Model answered directly without using the tool
        print(f"Assistant (no tool call): {msg.get('content', '')}")
        return 0

    # Step 2: record the assistant turn (content + tool_calls) in history
    messages.append({
        "role": "assistant",
        "content": msg.get("content", ""),
        "tool_calls": tool_calls,
    })

    # Step 3: execute each tool call locally, append role:"tool" results
    for call in tool_calls:
        fn = call.get("function", {})
        name = fn.get("name")
        args = fn.get("arguments") or {}
        print(f"Tool call: {name}({json.dumps(args)})")

        if name == "get_weather":
            result = get_weather(**args)
        else:
            result = {"error": f"unknown tool: {name}"}

        print(f"Tool result: {json.dumps(result)}")
        messages.append({"role": "tool", "content": json.dumps(result)})

    print("-" * 64)

    # Step 4: send results back — model writes the final answer
    resp = chat(messages, tools=TOOLS)
    answer = resp.get("message", {}).get("content", "")
    print(f"Assistant: {answer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
