"""An agent on AWS AgentCore Runtime whose model is the SEALED model.

This is how a customer uses the sealed model through AgentCore. It runs in the customer's own account and
network. A person (or another program) sends it a prompt through AgentCore; it thinks with the sealed model,
may call a tool, and answers. It never sees the model's weights: it only calls the sealed machine's endpoint,
like any app in the customer's network.

AgentCore Runtime's contract: answer GET /ping and POST /invocations on port 8080. Standard library only, so the
deployment package is this one file and there is nothing to build.

Settings (environment):
  SMH_ENDPOINT     https://<the customer's endpoint>       (the sealed machine, through its load balancer)
  SMH_CERT_SHA256  the fingerprint of the sealed machine's certificate, from its own log. The certificate is
                   self-signed, so instead of trusting a public authority this agent accepts exactly this one
                   certificate and no other (pinning). shortcut: it changes when the sealed machine restarts;
                   run  bash smh.sh agent-up  again, or give the endpoint a certificate from a public authority.
  SMH_MODEL        the model's name on the endpoint ("demo", or the name of a fine-tuned adapter)
  SMH_KEY_BUCKET   the customer's bucket; the API key is read from gate/api-keys.txt in it (the customer's own file)
  SMH_API_KEY      used only when the bucket cannot be read from here

Self-check: python3 consume/main.py --test
"""
import ast
import hashlib
import json
import operator
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENDPOINT = os.environ.get("SMH_ENDPOINT", "").rstrip("/")
PIN = os.environ.get("SMH_CERT_SHA256", "").replace(":", "").lower()
MODEL = os.environ.get("SMH_MODEL", "demo")
MAX_TURNS = 4
SYSTEM = ("You are a helpful assistant. Answer briefly. For arithmetic use the calculator tool, and for the date or "
          "time use the clock tool; do not guess those.")

# ---------------------------------------------------------------- the tools the agent may use (harmless on purpose)

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos}


def calculator(expression):
    """Arithmetic only: numbers and + - * / ** % ( ). Nothing else is evaluated."""
    def walk(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 64 or abs(left) > 1e6):
                raise ValueError("power too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.operand))
        raise ValueError("only numbers and + - * / ** % ( ) are allowed")
    if not isinstance(expression, str) or len(expression) > 200:
        raise ValueError("give a short arithmetic expression")
    return str(walk(ast.parse(expression, mode="eval").body))


def clock():
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())


TOOLS = {"calculator": lambda a: calculator(a.get("expression")), "clock": lambda a: clock()}
TOOL_SPECS = [
    {"type": "function", "function": {"name": "calculator", "description": "Work out an arithmetic expression exactly.",
                                      "parameters": {"type": "object", "properties": {"expression": {"type": "string", "description": "for example 17*23"}}, "required": ["expression"]}}},
    {"type": "function", "function": {"name": "clock", "description": "The current date and time (UTC).", "parameters": {"type": "object", "properties": {}}}},
]

# ---------------------------------------------------------------- talking to the sealed model, one certificate only


class Refused(Exception):
    pass


def pinned_context(pin):
    """TLS that accepts exactly one certificate, by fingerprint. No public authority is involved."""
    if len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin):
        raise Refused("this agent has no certificate fingerprint for the sealed machine; run  bash smh.sh agent-up  again")
    context = ssl.create_default_context()
    context.check_hostname = False              # the name is not what we trust; the fingerprint below is
    context.verify_mode = ssl.CERT_NONE
    return context


def check_pin(sock, pin):
    seen = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
    if seen != pin:
        raise Refused("the endpoint's certificate is not the sealed machine's (fingerprint differs). Nothing was sent. "
                      "If the sealed machine was restarted, run  bash smh.sh agent-up  again")


def post(path, body, key, pin=None, endpoint=None, timeout=120):
    """POST JSON to the sealed endpoint. The certificate is checked BEFORE the request, with the key in it, is sent."""
    import http.client
    pin, endpoint = pin or PIN, endpoint or ENDPOINT
    host = endpoint.split("://", 1)[-1].split("/", 1)[0]
    conn = http.client.HTTPSConnection(host, 443, context=pinned_context(pin), timeout=10)     # a machine that is gone is noticed in seconds
    try:
        conn.connect()
        check_pin(conn.sock, pin)
        conn.sock.settimeout(timeout)                   # the answer itself may take longer
        conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json", "Authorization": "Bearer " + key})
        response = conn.getresponse()
        text = response.read().decode("utf-8", "replace")
        return response.status, (json.loads(text) if text.strip().startswith("{") else {"raw": text[:300]})
    finally:
        conn.close()


def api_key():
    bucket, why = os.environ.get("SMH_KEY_BUCKET"), "no bucket named"
    if bucket:
        try:                                            # the customer's own key file; the agent's role may read this one object
            import boto3
            text = boto3.client("s3").get_object(Bucket=bucket, Key="gate/api-keys.txt")["Body"].read().decode()
            keys = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
            if keys:
                return keys[0]
            why = "the key file is empty"
        except Exception as error:                      # only the kind of error is passed on, never its text
            why = "reading gate/api-keys.txt failed: " + (getattr(error, "response", {}).get("Error", {}).get("Code") or type(error).__name__)
    key = os.environ.get("SMH_API_KEY", "")
    if not key:
        raise Refused(f"this agent has no API key for the sealed endpoint ({why}). Make one with  bash smh.sh api-key")
    return key


def answer(prompt, ask=post, key=None):
    """The agent loop: ask the sealed model; if it wants a tool, run it and ask again. Returns (text, trace)."""
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
        raise Refused("send a prompt of 1 to 8000 characters")
    key = key or api_key()
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    trace, tools_on = [], True
    for _ in range(MAX_TURNS):
        body = {"model": MODEL, "messages": messages, "max_tokens": 400, "temperature": 0}
        if tools_on:
            body.update(tools=TOOL_SPECS, tool_choice="auto")
        status, data = ask("/v1/chat/completions", body, key)
        if status == 400 and tools_on:                  # a model server without tool calling: carry on as plain chat
            tools_on = False
            trace.append("the model server does not offer tool calling; answering without tools")
            continue
        if status != 200:
            raise Refused(f"the sealed endpoint answered {status}" + (": no valid API key" if status == 401 else ""))
        message = data["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            return (message.get("content") or "").strip(), trace
        messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        for call in calls[:4]:
            name = call.get("function", {}).get("name")
            try:
                result = TOOLS[name](json.loads(call["function"].get("arguments") or "{}"))
            except Exception as error:                  # a tool that fails is information for the model
                result = f"error: {str(error)[:120]}"
            trace.append(f"tool {name} -> {result}")
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": str(result)})
    return "I could not finish within the allowed number of steps.", trace


# ---------------------------------------------------------------- AgentCore Runtime's contract

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"status": "Healthy", "time_of_last_update": int(time.time())}) if self.path == "/ping" else self._send(404, {})

    def do_POST(self):
        if self.path != "/invocations":
            return self._send(404, {})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 20000)) or b"{}")
            text, trace = answer(body.get("prompt"))
            self._send(200, {"result": text, "model": MODEL, "served_by": "the sealed model, through an agent on AgentCore Runtime", "steps": trace})
        except Refused as error:
            self._send(200, {"error": str(error)})
        except Exception as error:
            print("FAULT", repr(error)[:300], flush=True)
            self._send(200, {"error": f"the agent could not reach the sealed model ({type(error).__name__}). Is the sealed machine running, and does this customer still have access?"})


def self_test():
    assert calculator("17*23") == "391" and calculator("(2+3)**2 % 7") == "4"
    for bad in ("__import__('os').system('id')", "open('/etc/passwd')", "9**9**9", "a+1", "1;2"):
        try:
            calculator(bad)
            raise AssertionError(bad)
        except (ValueError, SyntaxError):
            pass
    # a model that asks for the calculator, then answers with its result
    script = [(200, {"choices": [{"message": {"content": "", "tool_calls": [{"id": "c1", "function": {"name": "calculator", "arguments": "{\"expression\": \"17*23\"}"}}]}}]}),
              (200, {"choices": [{"message": {"content": "17 times 23 is 391."}}]})]
    sent = []
    text, trace = answer("What is 17 times 23?", ask=lambda p, b, k: (sent.append(b), script.pop(0))[1], key="k")
    assert text == "17 times 23 is 391." and trace == ["tool calculator -> 391"] and sent[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "391"}
    # a model server without tool calling: plain chat still works
    script = [(400, {}), (200, {"choices": [{"message": {"content": "hello"}}]})]
    text, trace = answer("hi", ask=lambda p, b, k: script.pop(0), key="k")
    assert text == "hello" and "without tools" in trace[0]
    for status in (401, 503):
        try:
            answer("hi", ask=lambda p, b, k: (status, {}), key="k")
            raise AssertionError(status)
        except Refused:
            pass
    for pin in ("", "zz", "ab" * 31):
        try:
            pinned_context(pin)
            raise AssertionError(pin)
        except Refused:
            pass

    class Sock:
        def getpeercert(self, binary_form):
            return b"the sealed machine's certificate"
    check_pin(Sock(), hashlib.sha256(b"the sealed machine's certificate").hexdigest())
    try:
        check_pin(Sock(), "0" * 64)
        raise AssertionError("a wrong certificate was accepted")
    except Refused:
        pass
    print("ok")


if __name__ == "__main__":
    if "--test" in sys.argv:
        self_test()
        sys.exit(0)
    print(f"agent for the sealed model {MODEL!r} at {ENDPOINT or '(no endpoint set)'}; listening on 8080", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
