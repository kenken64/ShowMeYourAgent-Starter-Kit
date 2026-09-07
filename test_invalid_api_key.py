# -*- coding: utf-8 -*-
"""test_invalid_api_key.py

Security regression test: confirms the AWS LLM Gateway (https://api.softwaresystems.app)
rejects requests carrying a wrong/invalid X-API-Key instead of silently
falling back to a shared secret.

Relevant context: an earlier version of the ollama-proxy-bedrock proxy code
baked in a real gateway API key as a default fallback (since removed --
see that repo's "Remove hardcoded gateway API key fallback" commit). This
test exercises the deployed gateway directly to verify a bad key is
actually rejected, not silently accepted.

A PASS here means the gateway returned an error status (4xx) for the bad
key. A FAIL means it returned 200 -- i.e. the bad key was accepted, which
would indicate the fallback-key vulnerability (or an equivalent one) is
still present on whatever is currently deployed.
"""

import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()

LLM_GATEWAY_URL = os.getenv("LLM_GATEWAY_URL")
LLM_MODEL = os.getenv("LLM_MODEL", "global.anthropic.claude-sonnet-4-5-20250929-v1:0")

if not LLM_GATEWAY_URL:
    raise EnvironmentError("Missing LLM_GATEWAY_URL in .env")

CHAT_URL = f"{LLM_GATEWAY_URL.rstrip('/')}/api/chat"
WRONG_API_KEY = "this-is-a-deliberately-wrong-api-key-000000"

payload = {
    "model": LLM_MODEL,
    "stream": False,
    "messages": [{"role": "user", "content": "Say hello in one word."}],
    "options": {"num_predict": 20},
}


def main() -> int:
    print(f"POST {CHAT_URL}")
    print(f"X-API-Key: {WRONG_API_KEY!r} (intentionally invalid)")
    print("-" * 72)

    resp = requests.post(
        CHAT_URL,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": WRONG_API_KEY,
        },
        json=payload,
        timeout=60,
    )

    print("HTTP status:", resp.status_code)
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    print(json.dumps(body, indent=2) if isinstance(body, dict) else body)
    print("-" * 72)

    if resp.status_code == 200:
        print("FAIL: gateway returned HTTP 200 for an invalid API key -- "
              "the wrong key was accepted (possible fallback-key vulnerability).")
        return 1

    if 400 <= resp.status_code < 500:
        print(f"PASS: gateway correctly rejected the invalid API key (HTTP {resp.status_code}).")
        return 0

    print(f"CHECK: unexpected status {resp.status_code} -- neither a clean "
          f"accept (200) nor a clean client-error rejection (4xx). Inspect manually.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
