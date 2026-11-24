"""Turn the release agent's working notes into a few plain progress lines.

    bash deployer/run.sh "..." | python3 deployer/progress.py

The full notes are still written to the transcript file by run.py. This
filter prints one line when something changes: a machine's state, a step
starting or finishing, the final status. Self-check: python3 deployer/progress.py --test
"""

from __future__ import annotations

import json
import re
import sys
import time

STEPS = {"lock": "Encrypting the model and its container (about 25 minutes)",
         "build": "Building and signing the sealed image (about 15 minutes)",
         "register": "Registering the image (about 10 minutes)",
         "check": "Checking the image",
         "package": "Writing the customer's install package",
         "pin": "Tying both keys to the image"}
MACHINES = {"helper": "GPU helper machine", "builder": "Build machine"}
FINAL = {"NEEDS_APPROVAL": "Prepared: every step is done and the release is ready to approve.",
         "DONE": "Done.",
         "STUCK": "The agent stopped early."}
_STATUS = re.compile(r'\{\\?"version\\?".*\\?"steps\\?"')


def lines_for(line: str, seen: dict) -> list:
    """The plain lines this raw line adds, given what was already said (`seen` is updated)."""
    out = []
    m = re.search(r"FINAL STATUS: (\w+)", line)
    if m:
        return [FINAL.get(m.group(1), "Ended: " + m.group(1))]
    if "[tool result]" not in line or not _STATUS.search(line):
        return out
    try:
        status = json.loads(json.loads(line.split("[tool result]", 1)[1].strip())[0]["text"])
    except (ValueError, KeyError, IndexError, TypeError):
        return out
    for name, state in (status.get("machines") or {}).items():
        if seen.get(name) != state and name in MACHINES:
            seen[name] = state
            if state.startswith("failed") and seen.get("lock") == "done":
                continue                      # the helper has done its job and is being removed
            if state == "failed:capacity":
                out.append(f"{MACHINES[name]}: AWS has no free machine of this type; trying another")
            elif state not in ("stopped", "absent"):
                out.append(f"{MACHINES[name]}: {state}")
    for step in status.get("steps") or []:
        name, state = step.get("step"), step.get("state")
        if name in STEPS and seen.get(name) != state:
            seen[name] = state
            if state == "running":
                out.append(STEPS[name])
            elif state == "done":
                out.append(f"  finished: {STEPS[name].split(' (')[0].lower()}" + ("" if step.get("ok", True) else "  (WITH AN ERROR)"))
    return out


def _test() -> None:
    def raw(d):
        return "  [tool result] " + json.dumps([{"text": json.dumps(d)}])
    seen: dict = {}
    s = {"version": "0.1.0", "machines": {"builder": "stopped", "helper": "starting"}, "steps": [{"step": "lock", "state": "not_started"}]}
    assert lines_for(raw(s), seen) == ["GPU helper machine: starting"]
    assert lines_for(raw(s), seen) == []                                  # nothing new, nothing said
    s["machines"]["helper"] = "failed:capacity"
    assert "no free machine" in lines_for(raw(s), seen)[0]
    s["machines"]["helper"] = "online"; s["steps"][0]["state"] = "running"
    assert lines_for(raw(s), seen) == ["GPU helper machine: online", STEPS["lock"]]
    s["steps"][0].update(state="done", ok=True)
    assert lines_for(raw(s), seen) == ["  finished: encrypting the model and its container"]
    assert lines_for("FINAL STATUS: NEEDS_APPROVAL   (session x)", seen) == [FINAL["NEEDS_APPROVAL"]]
    s["machines"]["helper"] = "failed:other"
    assert lines_for(raw(s), seen) == []                                  # not an alarm once its step is done
    assert lines_for("  [tool result] not json", seen) == [] and lines_for("The helper is starting.", seen) == []
    print("ok")


MINUTES = {"lock": 25, "build": 20, "register": 10, "check": 3, "package": 2, "pin": 5}   # measured on five releases
ORDER = ["lock", "build", "register", "check", "package", "pin"]
SHORT = {"lock": "encrypting the model", "build": "building the image", "register": "registering the image",
         "check": "checking the image", "package": "writing the package", "pin": "tying the keys"}
WIDTH = 78      # keep the status line short enough for a narrow window


def status_line(seen: dict, started: dict, now: float) -> str:
    """One line that says what is happening, for how long, and when the person is needed."""
    current = next((n for n in ORDER if seen.get(n) == "running"), None)
    if current is None:
        left = sum(MINUTES[n] for n in ORDER if seen.get(n) != "done")
        return f"waiting for a machine | about {left} min to go | nothing to type"
    spent = int((now - started.get(current, now)) / 60)
    left = max(MINUTES[current] - spent, 1) + sum(MINUTES[n] for n in ORDER[ORDER.index(current) + 1:])
    turn = time.strftime("%H:%M", time.localtime(now + left * 60))
    return f"{SHORT[current]}, min {spent + 1} of ~{MINUTES[current]} | done about {turn} | nothing to type"


if __name__ == "__main__":
    if sys.argv[1:] == ["--test"]:
        _test()
        t = {"lock": "running"}
        assert "min 3 of ~25" in status_line(t, {"lock": 1000.0}, 1000.0 + 150) and "nothing to type" in status_line(t, {"lock": 0.0}, 0.0)
        assert "waiting for a machine" in status_line({}, {}, 0.0) and "about 65 min" in status_line({}, {}, 0.0)
        assert all(len(status_line({n: "running"}, {n: 0.0}, 60.0 * 99)) + 2 <= WIDTH for n in ORDER)
        raise SystemExit(0)
    import threading

    said: dict = {}
    started: dict = {}
    lock = threading.Lock()
    done = threading.Event()

    def ticker() -> None:                   # one fresh line a minute, so the screen never looks frozen
        while not done.wait(60):
            with lock:
                print(f"  {time.strftime('%H:%M')}  " + status_line(said, started, time.time()), flush=True)

    threading.Thread(target=ticker, daemon=True).start()
    for raw_line in sys.stdin:
        with lock:
            before = dict(said)
            texts = lines_for(raw_line, said)
            for name in ORDER:
                if said.get(name) == "running" and before.get(name) != "running":
                    started[name] = time.time()
            for text in texts:
                print(f"  {time.strftime('%H:%M')}  {text}", flush=True)
    done.set()
