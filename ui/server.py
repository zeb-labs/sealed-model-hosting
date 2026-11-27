"""The control page: a small local web app over smh.sh, for people who do not want a terminal.

    python3 ui/server.py            then open the address it prints (http://127.0.0.1:8765)
    bash smh.sh ui                  the same

It adds no new power. Every button runs one command of smh.sh from a FIXED list, with the AWS login this laptop
already has; the agents on AgentCore do the work as before. Standard library only, nothing to install.

What keeps it safe:
- It listens on this machine only (127.0.0.1). Another address needs SMH_UI_HOST and is refused unless a login
  sits in front of it (see "Hosting it" in docs/user-guide.md).
- A random token is made at start and built into the page. A request without it is refused, so another web
  site open in your browser cannot press these buttons.
- No input reaches a shell. Commands are started as argument lists, and each input is checked against a pattern.
- One job at a time. Starting a second one is refused while the first runs.

Self-check: python3 ui/server.py --test
"""
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = secrets.token_urlsafe(24)
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}/[A-Za-z0-9][A-Za-z0-9._-]{0,90}")
PLACE = re.compile(r"(s3://[a-z0-9.-]{3,63}/[A-Za-z0-9._/-]{1,200}|[/~][^\0\n;|&$`<>\"']{1,300})")     # a folder or an s3:// location
DATA = re.compile(r"[/~][^\0\n;|&$`<>\"']{1,300}\.jsonl")
STEPS = re.compile(r"[1-9][0-9]{0,3}")
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# action -> (side, label, what it does, minutes, costs money, needs a second click, inputs)
ACTIONS = {
    "setup":     ("builder", "Set up the builder's side", "Creates what is missing, sets up the agents, and the release agent prepares the build machine. Safe to run again.", 15, False, False, []),
    "ship":      ("builder", "Ship a model", "The release agent encrypts the model, builds and checks the sealed image. It is approved by itself only if all six checks pass; then the keys are tied to it and the customer gets access.", 60, True, True, ["model"]),
    "onboard":   ("builder", "Give the customer access", "Lets the customer's machine role ask for the keys. The measurements are still required.", 2, False, False, []),
    "offboard":  ("builder", "Take the customer's access away", "Their sealed machine stops within about four minutes.", 2, False, True, []),
    "usage":     ("builder", "Read usage counts", "The customer's usage records, as the builder may read them.", 1, False, False, []),
    "static":    ("builder", "Check the protections", "Ten read-only checks on the current release. Free.", 1, False, False, []),
    "up":        ("customer", "Bring up the customer's side", "The customer agent creates the one stack, waits for the model, makes an API key and tests the endpoint.", 27, True, True, []),
    "finetune":  ("customer", "Fine-tune with a file", "Sends a .jsonl data file and a form. The adapter's name is printed at the end.", 4, False, False, ["data", "steps"]),
    "test":      ("customer", "Test the endpoint", "A stand-in app inside the network calls the endpoint.", 5, False, False, []),
    "restart":   ("customer", "Restart the sealed machine", "Stop, start, and wait for the model.", 15, False, True, []),
    "remove":    ("customer", "Remove the customer's side", "Deletes the customer's stack and its bucket.", 6, False, True, []),
    "status":    ("house", "Refresh status", "What is live, what is running, what has been spent.", 1, False, False, []),
    "off":       ("house", "Switch everything off", "Deletes the test machines and stops the build machine.", 5, False, True, []),
    "cutoff":    ("house", "Stop every customer", "Disables both keys. Every sealed machine stops answering within about two minutes.", 1, False, True, []),
    "restore":   ("house", "Allow customers again", "Enables both keys.", 1, False, False, []),
}


def command(action, inputs):
    """The argument list and extra environment for one action. Raises ValueError for anything not allowed."""
    if action not in ACTIONS:
        raise ValueError("no such action")
    env, args = {}, [action]
    if action == "ship":
        model = str(inputs.get("model") or "Qwen/Qwen2.5-1.5B-Instruct").strip()
        if not (MODEL.fullmatch(model) or PLACE.fullmatch(model)):
            raise ValueError("the model must be owner/name, a folder, or an s3:// location")
        args, env = ["ship", os.path.expanduser(model)], {"APPROVE": "yes"}
    elif action == "finetune":
        data, steps = str(inputs.get("data") or "").strip(), str(inputs.get("steps") or "150").strip()
        if not DATA.fullmatch(data) or not STEPS.fullmatch(steps):
            raise ValueError("the data file must be a full path ending in .jsonl, and steps a number")
        args = ["finetune", os.path.expanduser(data), steps]
    elif action == "static":
        args = ["suite", "static"]
    return ["bash", "smh.sh"] + args, env


class Jobs:
    """One job at a time; its output is kept in memory for the page to read."""

    def __init__(self):
        self.lock, self.job, self.status_text = threading.Lock(), None, ""

    def start(self, action, inputs, run=subprocess.Popen):
        argv, extra = command(action, inputs)
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise RuntimeError(f"\"{ACTIONS[self.job['action']][1]}\" is still running")
            job = {"id": secrets.token_hex(4), "action": action, "state": "running", "lines": [], "started": time.time(), "ended": None}
            self.job = job
        proc = run(argv, cwd=ROOT, env=dict(os.environ, **extra), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        threading.Thread(target=self._follow, args=(job, proc), daemon=True).start()
        return job["id"]

    def _follow(self, job, proc):
        for line in proc.stdout:
            line = ANSI.sub("", line.rstrip("\n"))
            if line.strip():
                job["lines"].append(line[:600])
        code = proc.wait()
        if job["action"] == "status" and code == 0:
            self.status_text = "\n".join(job["lines"])
        job["ended"], job["state"] = time.time(), "done" if code == 0 else "failed"

    def view(self, since=0):
        job = self.job
        if not job:
            return {"state": "idle", "lines": [], "next": 0}
        return {"id": job["id"], "action": job["action"], "label": ACTIONS[job["action"]][1], "state": job["state"],
                "seconds": int((job["ended"] or time.time()) - job["started"]), "lines": job["lines"][since:], "next": len(job["lines"]),
                "minutes": ACTIONS[job["action"]][3]}


JOBS = Jobs()


def status_fields(text):
    """`smh.sh status` prints  name : value  lines; the page shows them as tiles."""
    return {m.group(1).strip(): m.group(2).strip() for m in re.finditer(r"^\s+([a-z][a-z -]{2,20}?)\s*:\s*(.*)$", text, re.M)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ALLOWED_HOSTS and secrets.compare_digest(self.headers.get("X-SMH-Token") or "", TOKEN)

    def do_GET(self):
        if self.path == "/":
            with open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8") as f:
                page = f.read().replace("__TOKEN__", TOKEN).replace("__ACTIONS__", json.dumps(
                    {k: {"side": v[0], "label": v[1], "what": v[2], "minutes": v[3], "costs": v[4], "confirm": v[5], "inputs": v[6]} for k, v in ACTIONS.items()}))
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        if not self._allowed():
            return self._send(403, {"error": "not allowed"})
        if self.path.startswith("/api/job"):
            m = re.search(r"since=(\d+)", self.path)
            return self._send(200, dict(JOBS.view(int(m.group(1)) if m else 0), status=status_fields(JOBS.status_text)))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed() or self.path != "/api/run":
            return self._send(403, {"error": "not allowed"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4000)) or b"{}")
            return self._send(200, {"id": JOBS.start(str(body.get("action")), body.get("inputs") or {})})
        except (ValueError, RuntimeError) as error:
            return self._send(400, {"error": str(error)})


def self_test():
    assert command("ship", {})[0][-1] == "Qwen/Qwen2.5-1.5B-Instruct" and command("ship", {})[1] == {"APPROVE": "yes"}
    assert command("ship", {"model": "s3://my-bucket/models/a"})[0][-1] == "s3://my-bucket/models/a"
    assert command("static", {})[0] == ["bash", "smh.sh", "suite", "static"]
    assert command("finetune", {"data": "/tmp/a.jsonl", "steps": "200"})[0][-2:] == ["/tmp/a.jsonl", "200"]
    for action, inputs in (("ship", {"model": "a/b; rm -rf ~"}), ("ship", {"model": "$(id)"}), ("finetune", {"data": "/tmp/a.txt"}),
                           ("finetune", {"data": "/tmp/a.jsonl", "steps": "1; id"}), ("approve", {}), ("rm", {}), ("demo", {})):
        try:
            command(action, inputs)
            raise AssertionError((action, inputs))
        except ValueError:
            pass
    jobs = Jobs()
    first = jobs.start("status", {}, run=lambda argv, **k: subprocess.Popen([sys.executable, "-c", "import time; print('  current release : 0.1.0 (ami-1)'); time.sleep(0.4)"], **{x: k[x] for x in ("stdout", "stderr", "text")}))
    try:
        jobs.start("status", {})
        raise AssertionError("a second job started")
    except RuntimeError:
        pass
    while jobs.view()["state"] == "running":
        time.sleep(0.05)
    assert jobs.view()["id"] == first and jobs.view()["state"] == "done" and status_fields(jobs.status_text) == {"current release": "0.1.0 (ami-1)"}
    print("ok")


HOST = os.environ.get("SMH_UI_HOST", "127.0.0.1")
ALLOWED_HOSTS = {"127.0.0.1", "localhost"} | set(filter(None, os.environ.get("SMH_UI_NAMES", "").split(",")))

if __name__ == "__main__":
    if "--test" in sys.argv:
        self_test()
        sys.exit(0)
    if HOST != "127.0.0.1" and os.environ.get("SMH_UI_BEHIND_LOGIN") != "yes":
        sys.exit("Refusing to listen beyond this machine: this page can start GPU machines. Put a login in front of it and set SMH_UI_BEHIND_LOGIN=yes (docs/user-guide.md, \"Hosting it\").")
    port = int(os.environ.get("SMH_UI_PORT", "8765"))
    server = ThreadingHTTPServer((HOST, port), Handler)
    print(f"Sealed model hosting, control page: http://{HOST}:{port}   (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
