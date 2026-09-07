#!/usr/bin/env python3
"""
OpenAI-compatible <-> Ollama-native /api/chat translation proxy.

Hermes (and the wider OpenAI ecosystem) speaks the OpenAI Chat Completions
protocol (POST /v1/chat/completions). Some gateways — notably this AWS
Bedrock-backed Ollama gateway — expose ONLY Ollama's native protocol
(POST /api/chat). There is no /v1/chat/completions endpoint.

This proxy sits between Hermes and that gateway:
  * Hermes -> proxy : OpenAI /v1/chat/completions  (tools + streaming)
  * proxy -> gateway: Ollama native /api/chat       (tools + streaming)

It exposes itself to Hermes as a standard OpenAI-compatible "custom"
endpoint so Hermes requires no code changes.

Usage:
    GATEWAY_URL=https://api.softwaresystems.app GATEWAY_API_KEY=... \\
    GATEWAY_API_KEY_FALLBACK=... python3 proxy.py [--port 11500]

GATEWAY_API_KEY_FALLBACK is optional: when the primary key returns a
token-quota-exceeded (429) response, requests are retried with the fallback
credential before the error is surfaced to the client.
"""

import argparse
import html as htmlmod
import json
import os
import re
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import httpx

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://13.213.1.174:11434")
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "")
# Secondary credential used when the primary key hits a quota-exceeded (429)
# response from the gateway. Leave empty to disable failover.
GATEWAY_API_KEY_FALLBACK = os.environ.get("GATEWAY_API_KEY_FALLBACK", "")
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "600"))
# The gateway honours Ollama's num_predict strictly (verified: num_predict=16
# stops after exactly 16 tokens). With the default options the gateway uses a
# tiny ~200-token cap, which truncates tool-call XML mid-</invoke>. Raise it so
# long file-writes complete.
_NUM_PREDICT = 16384

_client = httpx.Client(
    timeout=httpx.Timeout(15.0, read=UPSTREAM_TIMEOUT, write=60.0, pool=15.0),
    follow_redirects=True,
)


def _ollama_headers(api_key=None):
    h = {"Content-Type": "application/json"}
    key = api_key or GATEWAY_API_KEY
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _is_quota_error(status_code, text):
    """True when the upstream response looks like an exhausted-token-quota error."""
    if status_code == 429:
        return True
    low = (text or "").lower()
    return any(m in low for m in ("token quota exceeded", "quota exceeded", "insufficient quota", "too many requests"))


def translate_messages(messages):
    """Translate OpenAI message list to Ollama /api/chat message list."""
    out = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if role == "tool":
            # Ollama doesn't have a 'tool' role; fold tool results back into
            # a user message with the tool name/id for context.
            tool_call_id = m.get("tool_call_id", "")
            name = m.get("name", "tool")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "") for p in content if p.get("type") == "text"
                )
            out.append({
                "role": "user",
                "content": f"[tool_result id={tool_call_id} name={name}]\n{content}",
            })
            continue
        if role == "assistant":
            tool_calls = m.get("tool_calls")
            if tool_calls:
                # The Bedrock-backed gateway rejects assistant messages whose
                # text block is missing/empty (500 "text content blocks must
                # be non-empty" / "Internal server error"). Always send a
                # non-empty text block alongside the tool_calls.
                txt = m.get("content")
                if isinstance(txt, list):
                    txt = "\n".join(
                        p.get("text", "") for p in txt if p.get("type") == "text"
                    )
                txt = (txt or "").strip()
                if not txt:
                    txt = " "
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except Exception:
                        args = {"raw": fn.get("arguments", "")}
                    out.append({
                        "role": "assistant",
                        "content": txt,
                        "tool_calls": [{
                            "function": {
                                "name": fn.get("name", ""),
                                "arguments": args,
                            },
                        }],
                    })
            # If content present and no tool_calls, keep simple text assistant.
            if content is not None and content != "":
                if isinstance(content, list):
                    content = "\n".join(
                        p.get("text", "") for p in content if p.get("type") == "text"
                    )
                out.append({"role": "assistant", "content": content})
            continue
        # user / system
        if isinstance(content, list):
            content = "\n".join(
                p.get("text", "") for p in content if p.get("type") == "text"
            )
        out.append({"role": role, "content": content or ""})
    return out


def translate_tools(tools):
    """Translate OpenAI tool schemas to Ollama tool format."""
    result = []
    for t in tools or []:
        if t.get("type") == "function":
            fn = t.get("function", {})
            result.append({
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                },
            })
        elif "function" in t:
            # already ollama-ish
            result.append(t)
    return result


def _coerce_arg_value(raw):
    """Best-effort conversion of an XML text value into a JSON value."""
    v = (raw or "").strip()
    if v == "":
        return ""
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none"):
        return None
    try:
        return json.loads(v)
    except Exception:
        return htmlmod.unescape(v)


_BLOCK_RE = re.compile(r"<([A-Za-z_][\w-]*)\b[^>]*>(.*?)</\1\s*>", re.S)
_PARAM_RE = _BLOCK_RE
_INVOKE_RE = re.compile(
    r"<invoke\b[^>]*\bname\s*=\s*(['\"])([^'\"]*?)\1[^>]*>(.*?)</invoke\s*>", re.S
)
_INVOKE_PARAM_RE = re.compile(
    r"<parameter\b[^>]*\bname\s*=\s*(['\"])([^'\"]*?)\1[^>]*>(.*?)</parameter\s*>", re.S
)
_FC_WRAP_RE = re.compile(r"<function_calls\b[^>]*>.*?</function_calls\s*>", re.S)


def _extract_invoke_calls(content, known):
    """Extract Claude-Code ``<invoke name=\"tool\"><parameter name=\"k\">v``
    calls (optionally wrapped in <function_calls>). Returns
    (clean_text, openai_tool_calls)."""
    calls = []
    parts = []
    last = 0
    for m in _INVOKE_RE.finditer(content):
        name = m.group(2)
        body = m.group(3)
        if known and name not in known:
            continue
        params = {}
        for pm in _INVOKE_PARAM_RE.finditer(body):
            params[pm.group(2)] = _coerce_arg_value(pm.group(3))
        parts.append(content[last:m.start()])
        last = m.end()
        calls.append({
            "id": f"call_{int(time.time()*1000)}_{len(calls)}",
            "index": 0,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(params),
            },
        })
    parts.append(content[last:])
    if not calls:
        return content, []
    return "".join(parts), calls


def parse_claude_xml_tool_calls(content, known_tools=None):
    """Extract Claude-Code-style XML tool invocations from assistant text.

    Claude routed through Ollama-native Bedrock gateways commonly emits tool
    invocations as literal XML markup inside ``content`` instead of a
    structured ``tool_calls`` array. Two formats are handled:

    Block format::

        <write_file>
        <path>/tmp/x</path>
        <content>hi</content>
        </write_file>

    Claude-Code invoke format (usually wrapped in <function_calls>)::

        <function_calls>
        <invoke name="write_file">
        <parameter name="path">/tmp/x</parameter>
        <parameter name="content">hi</parameter>
        </invoke>
        </function_calls>

    Hermes (OpenAI protocol) cannot execute those as tools, so we translate
    them into OpenAI-style ``tool_calls`` and strip the markup from the
    visible text. Only blocks/invokes whose tag-name is in ``known_tools`` are
    converted (unknown blocks stay in the text verbatim so nothing is lost).

    Returns ``(clean_content, tool_calls)`` where each tool_call is already
    OpenAI-shaped: ``{"id", "index", "type": "function", "function":
    {"name", "arguments"}}`` with ``arguments`` a JSON string.
    """
    if not content or "<" not in content:
        return content, []
    known = set(known_tools or [])
    calls = []
    kept = []
    last = 0
    for m in _BLOCK_RE.finditer(content):
        name = m.group(1)
        body = m.group(2)
        if known and name not in known:
            continue  # unknown tag — leave the block in the text
        params = {}
        for pm in _PARAM_RE.finditer(body):
            params[pm.group(1)] = _coerce_arg_value(pm.group(2))
        if not params and body.strip():
            continue  # unparseable block — leave it in the text
        kept.append(content[last:m.start()])
        last = m.end()
        calls.append({
            "id": f"call_{int(time.time()*1000)}_{len(calls)}",
            "index": 0,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(params),
            },
        })
    kept.append(content[last:])
    if calls:
        return "".join(kept).rstrip(), calls
    # No block-format calls — try Claude-Code <invoke> format.
    clean, invoke_calls = _extract_invoke_calls(content, known)
    if invoke_calls:
        # Drop any leftover <function_calls>...</function_calls> wrapper text.
        clean = _FC_WRAP_RE.sub("", clean)
        return clean.rstrip(), invoke_calls
    return content, []


def _known_tool_names(tools):
    """Extract tool names from an Ollama-format tools list."""
    out = []
    for t in tools or []:
        if isinstance(t, dict):
            fn = t.get("function", {}) if isinstance(t.get("function"), dict) else {}
            if fn.get("name"):
                out.append(fn["name"])
    return out


def build_ollama_payload(body):
    payload = {
        "model": body.get("model", "sonnet4.5:latest"),
        "messages": translate_messages(body.get("messages", [])),
        "stream": bool(body.get("stream", False)),
    }
    if body.get("temperature") is not None:
        payload["options"] = {"temperature": body["temperature"]}
    tools = translate_tools(body.get("tools"))
    if tools:
        payload["tools"] = tools
    return payload


def to_openai_finish(reason):
    return {
        "stop": "stop",
        "length": "length",
        "tool_calls": "tool_calls",
    }.get(reason or "", "stop")


def artifacts_to_openai(content, tool_calls, finish_reason, model):
    """Ollama message -> OpenAI-style response chunk (stream)."""
    choices = []
    delta = {}
    if content:
        delta["content"] = content
    if tool_calls:
        delta["tool_calls"] = tool_calls
    choices.append({
        "index": 0,
        "delta": delta,
        "finish_reason": finish_reason,
    })
    return {
        "id": "chatcmpl-ollama",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": choices,
    }


def run_conversation(agent, tools, model):
    """
    Single forward pass against the Ollama native /api/chat. Returns the
    assistant message and the upstream done_reason.

    NOTE: tool-calling is driven by the CLIENT (Hermes). Each Hermes request
    is one forward pass. When Hermes receives tool_calls back, it executes the
    tools and issues a NEW request (with preceding 'tool' role messages folded
    into user messages by translate_messages). So no internal loop is needed.
    """
    payload = {
        "model": model,
        "messages": agent,
        "stream": False,
        "options": {"num_predict": _NUM_PREDICT},
    }
    if tools:
        payload["tools"] = tools
    print(f"[proxy] -> POST /api/chat model={model} nmsg={len(agent)} ntools={len(tools)}", flush=True)
    resp = _client.post(
        f"{GATEWAY_URL}/api/chat",
        headers=_ollama_headers(),
        content=json.dumps(payload),
    )
    if resp.status_code != 200 and GATEWAY_API_KEY_FALLBACK and _is_quota_error(resp.status_code, resp.text):
        print(f"[proxy] -> primary key quota exceeded ({resp.status_code}), retrying with fallback key", flush=True)
        resp = _client.post(
            f"{GATEWAY_URL}/api/chat",
            headers=_ollama_headers(GATEWAY_API_KEY_FALLBACK),
            content=json.dumps(payload),
        )
    print(f"[proxy] <- {resp.status_code} body={resp.text[:300]!r}", flush=True)
    if resp.status_code != 200:
        raise RuntimeError(f"upstream error {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    msg = data.get("message", {})
    if not msg.get("tool_calls"):
        # This gateway's Claude rarely emits structured tool_calls; it writes
        # them as Claude-Code XML markup in content. Translate those blocks.
        clean, xml_calls = parse_claude_xml_tool_calls(
            msg.get("content") or "", _known_tool_names(tools)
        )
        print(f"[proxy] NONSTREAM parsed: content={len(clean)} chars tool_calls={len(xml_calls)} names={[t['function']['name'] for t in xml_calls]}", flush=True)
        if xml_calls:
            msg["content"] = clean
            msg["tool_calls"] = [
                {"index": 0, "function": c["function"]} for c in xml_calls
            ]
    return msg, data.get("done_reason", "stop")


def handle_nonstream(body):
    """Handle a non-streaming chat completion request."""
    model = body.get("model", "sonnet4.5:latest")
    messages = translate_messages(body.get("messages", []))
    tools = translate_tools(body.get("tools"))
    try:
        msg, done_reason = run_conversation(messages, tools, model)
    except RuntimeError as exc:
        return {"error": {"message": str(exc), "type": "upstream_error"}}, 502
    except Exception as exc:
        return {"error": {"message": f"proxy error: {exc}", "type": "proxy_error"}}, 502

    content = msg.get("content") or ""
    tool_calls = []
    for tc in msg.get("tool_calls", []) or []:
        fn = tc.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            args_str = args
        else:
            try:
                args_str = json.dumps(args)
            except Exception:
                args_str = json.dumps({})
        tool_calls.append({
            "id": f"call_{int(time.time()*1000)}_{len(tool_calls)}",
            "type": "function",
            "function": {"name": fn.get("name", ""), "arguments": args_str},
        })

    choice = {
        "index": 0,
        "message": {"role": "assistant", "content": content},
        "finish_reason": to_openai_finish(done_reason),
    }
    if tool_calls:
        choice["message"]["tool_calls"] = tool_calls
        choice["finish_reason"] = "tool_calls"

    response = {
        "id": "chatcmpl-ollama",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
    return response, 200


def handle_stream(body):
    """Handle a streaming chat completion request (SSE)."""
    model = body.get("model", "sonnet4.5:latest")

    def generate():
        import json as _json
        # Open the send-role chunk (mimic OpenAI streaming role deltas)
        # Then stream from the upstream /api/chat.
        payload = {
            "model": model,
            "messages": translate_messages(body.get("messages", [])),
            "stream": True,
            "options": {"num_predict": _NUM_PREDICT},
        }
        tools = translate_tools(body.get("tools"))
        if tools:
            payload["tools"] = tools
        upstream = _client.post(
            f"{GATEWAY_URL}/api/chat",
            headers=_ollama_headers(),
            content=json.dumps(payload),
        )
        if upstream.status_code != 200 and GATEWAY_API_KEY_FALLBACK and _is_quota_error(upstream.status_code, upstream.text):
            print(f"[proxy] -> streaming: primary key quota exceeded ({upstream.status_code}), retrying with fallback key", flush=True)
            upstream = _client.post(
                f"{GATEWAY_URL}/api/chat",
                headers=_ollama_headers(GATEWAY_API_KEY_FALLBACK),
                content=json.dumps(payload),
            )
        if upstream.status_code != 200:
            yield f"data: {_json.dumps({'error': {'message': upstream.text[:300]}})}\n\n"
            yield "data: [DONE]\n\n"
            return

        def sse(d):
            return f"data: {_json.dumps(d)}\n\n"

        # Ollama-native /api/chat streams plain newline-delimited JSON (one
        # object per line). Some Ollama-flavoured gateways prefix each line
        # with "data:" SSE-style instead. Accept both; never drop a line.
        # Buffer the whole turn so Claude-XML tool invocations (which this
        # gateway emits as plain text) can be translated before emitting.
        content_parts = []
        native_tool_calls = []
        known_names = _known_tool_names(tools)
        for raw in upstream.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            if line.startswith("data:"):
                line = line[5:].strip()
            try:
                chunk = _json.loads(line)
            except Exception:
                continue
            msg = chunk.get("message", {})
            done = chunk.get("done", False)
            c = msg.get("content") or ""
            if c:
                content_parts.append(c)
            if msg.get("tool_calls"):
                native_tool_calls.extend(msg.get("tool_calls") or [])
            if done:
                break

        content = "".join(content_parts)
        print(f"[proxy] STREAM raw content ({len(content)} chars): {content[:600]!r}", flush=True)
        tool_calls = []
        for tc in native_tool_calls:
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                args_str = args
            else:
                try:
                    args_str = _json.dumps(args)
                except Exception:
                    args_str = "{}"
            tool_calls.append({
                "index": 0,
                "id": f"call_{int(time.time()*1000)}_{len(tool_calls)}",
                "type": "function",
                "function": {"name": fn.get("name", ""), "arguments": args_str},
            })
        if not tool_calls:
            # No structured tool calls from the gateway — look for Claude-XML
            # tool markup embedded in the emitted text and convert it.
            clean, xml_calls = parse_claude_xml_tool_calls(content, known_names)
            if xml_calls:
                content = clean
                tool_calls = xml_calls

        if content:
            yield sse(artifacts_to_openai(content, [], None, model))
        if tool_calls:
            print(f"[proxy] STREAM emitted tool_calls={len(tool_calls)} names={[t['function']['name'] for t in tool_calls]}", flush=True)
            yield sse(artifacts_to_openai("", tool_calls, None, model))
            yield sse(artifacts_to_openai("", [], "tool_calls", model))
        else:
            print(f"[proxy] STREAM emitted content only ({len(content)} chars, no tool_calls)", flush=True)
            yield sse(artifacts_to_openai("", [], "stop", model))
        yield "data: [DONE]\n\n"

    return generate(), 200


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[proxy] %s - %s\n" % (self.address_string(), fmt % args))

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.rstrip("/") in ("/v1/models", "/models"):
            # Expose the gateway's model list, OpenAI-style.
            try:
                r = _client.get(f"{GATEWAY_URL}/api/tags", headers=_ollama_headers())
                models = [
                    {"id": m["name"], "object": "model", "created": 0, "owned_by": "ollama-gateway"}
                    for m in r.json().get("models", [])
                ]
            except Exception:
                models = []
            self._send(200, json.dumps({"object": "list", "data": models}))
            return
        self._send(404, json.dumps({"error": {"message": "not found"}}))

    def do_POST(self):
        path = urlparse(self.path).path
        if path.rstrip("/") != "/v1/chat/completions":
            self._send(404, json.dumps({"error": {"message": "not found"}}))
            return
        body = self._read_json()
        stream = bool(body.get("stream", False))
        print(f"[proxy] HERMES-> req model={body.get('model')!r} stream={stream} roles={[m.get('role') for m in body.get('messages', [])]} nmsgs={len(body.get('messages', []))} tools={len(body.get('tools') or [])}", flush=True)
        try:
            if stream:
                gen, status = handle_stream(body)
            else:
                result, status = handle_nonstream(body)
                print(f"[proxy] HERMES<- {status} {json.dumps(result)[:200]}", flush=True)
                self._send(status, json.dumps(result))
                return
        except Exception as exc:
            print(f"[proxy] HERMES<- 502 {exc}", flush=True)
            self._send(502, json.dumps({"error": {"message": f"proxy error: {exc}"}}))
            return

        # Streaming SSE response
        print(f"[proxy] HERMES<- 200 (stream)", flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for chunk in gen:
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self.wfile.write(f"data: {json.dumps({'error': {'message': str(exc)}})}\n\n".encode())
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11500)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[proxy] listening on http://{args.host}:{args.port}")
    print(f"[proxy] upstream = {GATEWAY_URL}")
    print(f"[proxy] api_key  = {'set' if GATEWAY_API_KEY else 'NOT SET'}")
    print(f"[proxy] fallback = {'set' if GATEWAY_API_KEY_FALLBACK else 'NOT SET'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
