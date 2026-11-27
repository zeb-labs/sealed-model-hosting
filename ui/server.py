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
CUSTOMER = re.compile(r"[a-z][a-z0-9-]{1,30}")
ROLE = re.compile(r"arn:aws:iam::\d{12}:role/([\w+=,.@-]+/)*[\w+=,.@-]{1,64}")
VERSION = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}")
STACK = re.compile(r"smh-poc-oc[a-z0-9-]{0,12}")
SIDE_OF_ONE_CUSTOMER = {"up", "test", "finetune", "restart", "remove"}        # these act on ONE customer's stack: the page says which
PYTHON = os.path.join(ROOT, "sealed-model-poc", ".venv", "bin", "python")
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# action -> (side, label, what it does, minutes, costs money, needs a second click, inputs)
ACTIONS = {
    "setup":     ("builder", "Set up the builder's side", "Creates what is missing, sets up the agents, and the release agent prepares the build machine. Safe to run again.", 15, False, False, []),
    "ship":      ("builder", "Ship a model", "The release agent encrypts the model, builds and checks the sealed image. It is approved by itself only if all six checks pass; then the keys are tied to it and the customer gets access.", 60, True, True, ["model"]),
    "usage":     ("builder", "Read usage counts", "The test customer's usage records, as the builder may read them.", 1, False, False, []),
    "static":    ("builder", "Check the protections", "Ten read-only checks on the current release. Free.", 1, False, False, []),
    "suite":     ("builder", "Run the full attack suite", "All 33 tests on the live release: the read-only checks, one good run, and eight attacks that must each fail. It disables and re-enables the keys on the way, so no customer can be served while it runs.", 270, True, True, []),
    "customer_setup": ("customer", "Configure this AWS account", "Once per account, before the first bring-up: a small bucket for the agent's files, the builder's install package, then the customer agent and the remediation agent on AgentCore. Give the kit folder the builder sent.", 10, False, False, ["kit"]),
    "up":        ("customer", "Bring up the customer's side", "The customer agent creates the one stack, waits for the model, makes an API key and tests the endpoint.", 27, True, True, []),
    "finetune":  ("customer", "Fine-tune with a file", "Sends a .jsonl data file and a form. The adapter's name is printed at the end.", 4, False, False, ["data", "steps"]),
    "test":      ("customer", "Test the endpoint", "A stand-in app inside the network calls the endpoint.", 5, False, False, []),
    "restart":   ("customer", "Restart the sealed machine", "Stop, start, and wait for the model.", 15, False, True, []),
    "remove":    ("customer", "Remove the customer's side", "Deletes the customer's stack and its bucket.", 6, False, True, []),
    "status":    ("house", "Refresh status", "What is live, what is running, what has been spent.", 1, False, False, []),
    "off":       ("house", "Switch everything off", "Deletes the test machines and stops the build machine.", 5, False, True, []),
    "cutoff":    ("house", "Emergency: stop every customer", "Disables both keys for everyone. For one customer, use Pause on the Customers tab instead.", 1, False, True, []),
    "restore":   ("house", "End the emergency stop", "Enables both keys again.", 1, False, False, []),
    # Started from the tables on the Customers and Models tabs, one row at a time.
    "onboard_customer": ("row", "Onboard a customer", "", 4, False, False, ["name", "account"]),
    "kit":             ("row", "Write the install kit", "", 1, False, False, ["name"]),
    "customer_add":    ("row", "Add a customer", "", 2, False, False, ["name", "role"]),
    "customer_pause":  ("row", "Pause a customer", "", 2, False, True, ["name"]),
    "customer_resume": ("row", "Resume a customer", "", 2, False, False, ["name"]),
    "customer_remove": ("row", "Remove a customer", "", 2, False, True, ["name"]),
    "live":            ("row", "Make a release live", "", 10, False, True, ["version"]),
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
    elif action == "suite":
        args = ["suite", "all"]
    elif action == "onboard_customer":
        name, account = str(inputs.get("name") or "").strip(), str(inputs.get("account") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9]{1,10}", name) or not re.fullmatch(r"\d{12}", account):
            raise ValueError("a new customer needs a short name (2 to 11 lower-case letters or digits) and their 12-digit AWS account number")
        args = ["onboard-customer", name, account]
    elif action == "kit":
        if not CUSTOMER.fullmatch(str(inputs.get("name") or "")):
            raise ValueError("which customer?")
        args = ["kit", inputs["name"]]
    elif action == "customer_setup":
        kit = str(inputs.get("kit") or "").strip()
        if kit and not PLACE.fullmatch(kit):
            raise ValueError("the kit must be a folder on this machine")
        args = ["customer-setup"] + ([os.path.expanduser(kit)] if kit else [])
    elif action.startswith("customer_"):
        name, role = str(inputs.get("name") or "").strip(), str(inputs.get("role") or "").strip()
        if not CUSTOMER.fullmatch(name) or (action == "customer_add" and not ROLE.fullmatch(role)):
            raise ValueError("a customer needs a short lower-case name and, to be added, the full ARN of their machine role")
        args = ["customer", action.split("_")[1], name] + ([role] if action == "customer_add" else [])
    elif action == "live":
        version = str(inputs.get("version") or "")
        if not VERSION.fullmatch(version):
            raise ValueError("a version looks like 0.12.0")
        args = ["live", version]
    if action in SIDE_OF_ONE_CUSTOMER and inputs.get("stack"):
        if not STACK.fullmatch(str(inputs["stack"])):
            raise ValueError("a customer's stack is named smh-poc-oc or smh-poc-oc-<name>")
        env["NAME"] = str(inputs["stack"])
    return ["bash", "smh.sh"] + args, env


# Only these may start while something that uses the keys or the machines is running outside this page.
READ_ONLY = {"status", "usage"}
# Long work that may have been started from a terminal: the attack suite, a release, a bring-up, a setup.
OUTSIDE = r"assure\.sh (all|live|break|recheck)|smh\.sh (ship|up|setup|suite all|live|release)|deployer/run\.py"


def outside(own_pid=None):
    """What long job runs on this machine that this page did not start: (name, minutes it has run), or None."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,etime=,command="], capture_output=True, text=True).stdout
    rows = [line.split(None, 3) for line in out.splitlines() if len(line.split(None, 3)) == 4]
    ours = set()
    if own_pid:                                     # everything below the job this page started is not "outside"
        ours, grew = {str(own_pid)}, True
        while grew:
            more = {pid for pid, ppid, _, _ in rows if ppid in ours} - ours
            grew, ours = bool(more), ours | more
    for pid, _, etime, cmd in rows:
        m = re.search(OUTSIDE, cmd)
        if m and pid not in ours and "pgrep" not in cmd:
            parts = [int(x) for x in re.split(r"[-:]", etime)]
            minutes = sum(v * w for v, w in zip(reversed(parts), (0, 1, 60, 1440)))
            name = "the full attack suite" if "assure" in m.group(0) or "suite" in m.group(0) else "a run of  " + m.group(0).replace("\\", "")
            return name, minutes
    return None


class Jobs:
    """One job at a time; its output is kept in memory for the page to read."""

    def __init__(self):
        self.lock, self.job, self.status_text = threading.Lock(), None, ""

    def start(self, action, inputs, run=subprocess.Popen):
        argv, extra = command(action, inputs)
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise RuntimeError(f"\"{ACTIONS[self.job['action']][1]}\" is still running")
            busy = outside() if action not in READ_ONLY else None
            if busy:
                raise RuntimeError(f"Not now: {busy[0]} is running on this machine (started outside this page, {busy[1]} minutes ago). It uses the keys and the machines. Try again when it has finished.")
            job = {"id": secrets.token_hex(4), "action": action, "state": "running", "lines": [], "started": time.time(), "ended": None}
            self.job = job
        proc = job["proc"] = run(argv, cwd=ROOT, env=dict(os.environ, **extra), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
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


def table(what):
    """The builder's books as rows: customers or models. Read-only; it runs beside a job."""
    out = subprocess.run([PYTHON, "deployer/manage.py", what, "--json"], cwd=ROOT, capture_output=True, text=True, timeout=90)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[-300:] or "the list could not be read")
    return json.loads(out.stdout)


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
                page = f.read().replace("__TOKEN__", TOKEN).replace("__STACK__", os.environ.get("NAME", "smh-poc-oc") if STACK.fullmatch(os.environ.get("NAME", "smh-poc-oc")) else "smh-poc-oc").replace("__ACTIONS__", json.dumps(
                    {k: {"side": v[0], "label": v[1], "what": v[2], "minutes": v[3], "costs": v[4], "confirm": v[5], "inputs": v[6]} for k, v in ACTIONS.items()}))
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        if not self._allowed():
            return self._send(403, {"error": "not allowed"})
        if self.path.startswith("/api/job"):
            m = re.search(r"since=(\d+)", self.path)
            job = JOBS.job
            busy = outside(job["proc"].pid if job and job.get("proc") and job["state"] == "running" else None)
            return self._send(200, dict(JOBS.view(int(m.group(1)) if m else 0), status=status_fields(JOBS.status_text),
                                        outside={"name": busy[0], "minutes": busy[1]} if busy else None))
        if self.path in ("/api/customers", "/api/models"):
            try:
                return self._send(200, {"rows": table(self.path.split("/")[-1])})
            except Exception as error:
                return self._send(200, {"rows": [], "error": str(error)[:300]})
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
    assert command("static", {})[0] == ["bash", "smh.sh", "suite", "static"] and command("suite", {})[0] == ["bash", "smh.sh", "suite", "all"]
    assert re.search(OUTSIDE, "bash assure.sh all") and re.search(OUTSIDE, "bash smh.sh ship a/b") and not re.search(OUTSIDE, "bash smh.sh status")
    assert command("finetune", {"data": "/tmp/a.jsonl", "steps": "200"})[0][-2:] == ["/tmp/a.jsonl", "200"]
    assert command("customer_add", {"name": "acme", "role": "arn:aws:iam::111122223333:role/zl/acme-sealed-role"})[0][2:] == ["customer", "add", "acme", "arn:aws:iam::111122223333:role/zl/acme-sealed-role"]
    assert command("customer_pause", {"name": "acme"})[0][2:] == ["customer", "pause", "acme"] and command("live", {"version": "0.10.5"})[0][2:] == ["live", "0.10.5"]
    assert command("onboard_customer", {"name": "acme", "account": "111122223333"})[0][2:] == ["onboard-customer", "acme", "111122223333"]
    assert command("up", {"stack": "smh-poc-oc-acme"})[1] == {"NAME": "smh-poc-oc-acme"} and command("up", {})[1] == {} and command("customer_setup", {})[0][2:] == ["customer-setup"]
    for action, inputs in (("onboard_customer", {"name": "acme", "account": "12"}), ("onboard_customer", {"name": "Acme Corp", "account": "111122223333"}),
                           ("up", {"stack": "smh-poc-m1-key"}), ("remove", {"stack": "smh-poc-m1-builder"}), ("customer_setup", {"kit": "x; id"}),
                           ("customer_add", {"name": "acme", "role": "*"}), ("customer_pause", {"name": "acme; id"}), ("customer_remove", {}), ("live", {"version": "latest"}),
                           ("ship", {"model": "a/b; rm -rf ~"}), ("ship", {"model": "$(id)"}), ("finetune", {"data": "/tmp/a.txt"}),
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
