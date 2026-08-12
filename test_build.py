"""
Hardware-free test of the build orchestration: detached execution, log
streaming by byte offset, reattach, graceful failure, and the three
programming modes.

A fake `vivado` on PATH stands in for the real one, so the process lifecycle
(setsid, exit code file, polling) is exercised for real.

Run: python3 test_build.py
"""
import argparse, json, os, shutil, stat, subprocess, sys, tempfile, time
import remote_agent as ra

HERE = os.path.dirname(os.path.abspath(__file__))
# Importing ./fpgatest below runs its module-level _reexec_into_venv(), which
# would os.execv this whole test process into .venv with no CLI arguments.
# The re-exec tests at the bottom pop this back out of the child's env.
os.environ["FPGATEST_NO_REEXEC"] = "1"

PASS = FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}  -- {detail}")


def fake_vivado(bindir, body):
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, "vivado")
    with open(path, "w") as fh:
        fh.write("#!/bin/bash\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def ns(**kw):
    return argparse.Namespace(**kw)


def agent(*argv):
    """Invoke the agent as a one-shot process, exactly the way ssh does.

    Calling build_start/build_status in-process cannot see a runner that lost
    its pid: the corpse stays an unreaped child of this interpreter, so
    os.kill(pid, 0) keeps succeeding. Over ssh the agent exits after every
    subcommand and the kernel reaps it, which is what exposes a dead pid.
    """
    out = subprocess.run([sys.executable, os.path.join(HERE, "remote_agent.py"),
                          *argv], cwd=HERE, capture_output=True, text=True)
    for line in out.stdout.splitlines():
        if line.startswith("##FPGATEST-RESULT##"):
            return json.loads(line[len("##FPGATEST-RESULT##"):])
    raise AssertionError(f"no result line from agent {argv}:\n"
                         f"{out.stdout}\n{out.stderr}")


def agent_state(project):
    return agent("build-status", "--project", project) .get("state") or {}


def poll(project, timeout=30):
    """Stream the build log exactly the way the CLI does, by byte offset."""
    offset, text = 0, ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = ra.build_status(ns(project=project, offset=offset, max_bytes=1 << 20))
        text += st["chunk"]
        offset = st["next_offset"]
        state = st["state"] or {}
        if state.get("exit_code") is not None and not state.get("running"):
            st = ra.build_status(ns(project=project, offset=offset, max_bytes=1 << 20))
            text += st["chunk"]
            return state, text
        time.sleep(0.2)
    return {"timeout": True}, text


print("build tcl generation:")
tcl = ra.render_build_tcl("/home/alp/labs/lab2/lab2.xpr",
                          {"jobs": 8, "export_xsa": "/home/alp/labs/lab2/lab2.xsa"})
check("opens the project", "open_project {/home/alp/labs/lab2/lab2.xpr}" in tcl)
check("uses Vivado's own staleness flag", "NEEDS_REFRESH" in tcl)
check("checks the bitstream really exists",
      "glob -nocomplain [file join $bitdir *.bit]" in tcl)
check("passes the job count", "-jobs 8" in tcl)
check("stops implementation at write_bitstream", "-to_step write_bitstream" in tcl)
check("exports the XSA with the bitstream inside",
      "write_hw_platform -fixed -include_bit -force {/home/alp/labs/lab2/lab2.xsa}" in tcl)
check("simulation off by default", "if {0} {\n    ftlog stage sim start" in tcl)
tcl_force = ra.render_build_tcl("/x/p.xpr", {"force": True, "simulation": True})
check("force overrides the staleness decision",
      "if {1} { set do_synth 1 ; set do_impl 1 }" in tcl_force)
check("simulation can be switched on", "if {1} {\n    ftlog stage sim start" in tcl_force)
check("no leftover placeholders", "%" not in tcl.replace("100%", ""))

print("\nsuccessful detached build:")
proj = tempfile.mkdtemp(); open(os.path.join(proj, "p.xpr"), "w").write("")
bindir = fake_vivado(tempfile.mkdtemp(), r'''
echo "##FPGATEST##|plan|synth|run|-"
echo "##FPGATEST##|stage|synth|start|-"
sleep 0.4
echo "##FPGATEST##|stage|synth|ok|-"
echo "##FPGATEST##|stage|impl|start|-"
echo "ERROR: this line should reach the local log"
sleep 0.4
echo "##FPGATEST##|stage|impl|ok|-"
echo "##FPGATEST##|build|ok|-|-"
exit 0''')
setup = f"export PATH={bindir}:$PATH"
res = ra.build_start(ns(project=proj, setup=setup, opts=json.dumps({})))
check("build started", res["ok"] and not res["attached"], res)
check("pid recorded", isinstance(res["state"]["pid"], int), res["state"])

state, log = poll(proj)
check("build finished", state.get("exit_code") == 0, state)
check("vivado detached from the ssh session (own process group)",
      state["pid"] != os.getpid())
check("marker lines streamed home", log.count("##FPGATEST##") == 6, log)
check("plain vivado output streamed too", "ERROR: this line" in log)
check("offsets did not duplicate content", log.count("stage|synth|ok") == 1)

logs = ra.build_logs(ns(project=proj))["logs"]
check("build.log retrievable", "build.log" in logs)
check("generated tcl retained for inspection", "build.tcl" in logs)

print("\nreattach to a build already in flight:")
proj2 = tempfile.mkdtemp(); open(os.path.join(proj2, "p.xpr"), "w").write("")
bindir2 = fake_vivado(tempfile.mkdtemp(), 'sleep 3; exit 0')
first = ra.build_start(ns(project=proj2, setup=f"export PATH={bindir2}:$PATH",
                          opts=json.dumps({})))
second = ra.build_start(ns(project=proj2, setup=f"export PATH={bindir2}:$PATH",
                           opts=json.dumps({})))
check("second invocation attaches instead of starting a second vivado",
      second["attached"] is True, second)
check("same pid", second["state"]["pid"] == first["state"]["pid"])
stopped = ra.build_stop(ns(project=proj2))
check("build can be stopped", stopped["ok"] and stopped["stopped"], stopped)

print("\nfailing build exits gracefully:")
proj3 = tempfile.mkdtemp(); open(os.path.join(proj3, "p.xpr"), "w").write("")
bindir3 = fake_vivado(tempfile.mkdtemp(), r'''
echo "##FPGATEST##|stage|synth|start|-"
echo "ERROR: [Synth 8-403] syntax error near 'endmodul'"
echo "##FPGATEST##|stage|synth|fail|synth_design failed"
echo "##FPGATEST##|build|fail|-|-"
exit 2''')
ra.build_start(ns(project=proj3, setup=f"export PATH={bindir3}:$PATH",
                  opts=json.dumps({})))
state, log = poll(proj3)
check("nonzero exit propagated", state.get("exit_code") == 2, state)
check("not marked as running", state.get("running") is False)
check("failure reason captured", "syntax error" in log)

print("\nbuild started by one agent process, polled by the next:")
# The regression. build_start used to run ["setsid", "bash", runner] on top of
# start_new_session=True. bash was already a process-group leader, so setsid(1)
# took its fork-and-exit path and the pid recorded in state.json belonged to a
# wrapper that was gone milliseconds later. Every poll is a fresh ssh
# invocation, so os.kill(pid, 0) failed and a healthy -- often already
# finished and successful -- build was reported as "vanished without an
# exit code (killed, out of memory, or the node went down)".
proj4 = tempfile.mkdtemp(); open(os.path.join(proj4, "p.xpr"), "w").write("")
bindir4 = fake_vivado(tempfile.mkdtemp(), 'sleep 5; exit 0')
started = agent("build-start", "--project", proj4,
                "--setup", f"export PATH={bindir4}:$PATH", "--opts", "{}")
check("build started by a throwaway agent process", started["ok"], started)
mid = agent_state(proj4)
check("a running build is still alive to the next poller",
      mid.get("running") is True and not mid.get("died"), mid)
agent("build-stop", "--project", proj4)

# The lab2 case: everything up to date, so vivado is gone in seconds. The exit
# code has to arrive whole -- a truncate-then-write would let a poll land on an
# empty file and read it as failure.
proj5 = tempfile.mkdtemp(); open(os.path.join(proj5, "p.xpr"), "w").write("")
bindir5 = fake_vivado(tempfile.mkdtemp(), r'''
echo "##FPGATEST##|stage|synth|skip|up-to-date"
echo "##FPGATEST##|stage|impl|skip|up-to-date"
echo "##FPGATEST##|build|ok|-|-"
exit 0''')
agent("build-start", "--project", proj5,
      "--setup", f"export PATH={bindir5}:$PATH", "--opts", "{}")
fast = {}
for _ in range(100):
    fast = agent_state(proj5)
    if fast.get("exit_code") is not None and not fast.get("running"):
        break
    time.sleep(0.1)
check("an up-to-date build reports ok, not vanished",
      fast.get("exit_code") == 0 and not fast.get("died"), fast)

# cd failing used to skip the `echo $? >` line altogether, leaving no exit code
# at all -- indistinguishable from an OOM kill.
proj6 = tempfile.mkdtemp(); open(os.path.join(proj6, "p.xpr"), "w").write("")
agent("build-start", "--project", proj6, "--setup", f"rm -rf {proj6}",
      "--opts", "{}")
gone = {}
for _ in range(100):
    gone = agent_state(proj6)
    if gone.get("exit_code") is not None and not gone.get("running"):
        break
    time.sleep(0.1)
check("a runner that dies before vivado still records an exit code",
      gone.get("exit_code") == 90 and not gone.get("died"), gone)

print("\nmissing build inputs:")
empty = tempfile.mkdtemp()
res = ra.build_start(ns(project=empty, setup="", opts=json.dumps({})))
check("clear error when there is no .xpr and no script",
      not res["ok"] and "no .xpr" in res["error"], res)
res = ra.build_start(ns(project="/nope/nope", setup="", opts=json.dumps({})))
check("clear error for a missing project", not res["ok"], res)

print("\nprogramming modes:")
manifest = {"ok": True, "bit": "/p/design.bit", "xsa": "/p/design.xsa",
            "ps7_init_tcl": "/p/ps7_init.tcl",
            "elf": "/p/app.elf", "bit_header": {}}
spec = {"base": "0x43C00000", "tests": [
    {"name": "t", "ops": [{"write": 0x10, "value": 1}, {"read": 0x28, "expect": 1}]}]}

pl = ra.build_tcl(manifest, dict(spec, mode="pl-only"), 3121)
check("pl-only programs the PL", "fpga -file {/p/design.bit}" in pl)
check("pl-only does not touch the PS", "ps7_init" not in pl and "rst -system" not in pl)
check("pl-only does not load the ELF", "dow " not in pl)

bit = ra.build_tcl(manifest, dict(spec, mode="bit"), 3121)
check("bit mode programs the PL", "fpga -file {/p/design.bit}" in bit)
check("bit mode runs ps7_init so FCLK is alive", "ps7_init ; ps7_post_config" in bit)
check("bit mode runs the register testbench", "ft_expect {t} 0x43c00028 1" in bit)
check("bit mode does not load the ELF", "dow {" not in bit)
check("bit mode says why the ELF was skipped", "ft info elf skipped mode=bit" in bit)

full = ra.build_tcl(manifest, dict(spec, mode="full"), 3121)
check("full mode loads and runs the ELF",
      "dow {/p/app.elf}" in full and "ft_step run {con}" in full)

noxsa = ra.build_tcl(dict(manifest, xsa=None), dict(spec, mode="bit"), 3121)
check("no XSA -> ps7_init skipped and flagged",
      "ft info ps_init skipped no-xsa" in noxsa and "ps7_init" not in noxsa)

print("\nlocal run-log directory:")
import importlib.util, importlib.machinery

_spec = importlib.util.spec_from_loader(
    "ftcli", importlib.machinery.SourceFileLoader("ftcli", "./fpgatest"))
cli = importlib.util.module_from_spec(_spec)
sys.modules["ftcli"] = cli          # dataclasses need the module registered
_spec.loader.exec_module(cli)

base = tempfile.mkdtemp()
made = []
for i in range(5):
    rl = cli.RunLog(base, "/home/alp/labs/lab2", keep=3)
    rl.write("build.log", f"run {i}")
    rl.write_json("manifest.json", {"i": i})
    made.append(rl.dir)
    time.sleep(1.02)                # directory names are per-second

kept = sorted(d for d in os.listdir(base) if d[0].isdigit())
check("rotation keeps only the newest runs", len(kept) == 3, kept)
check("the survivors are the newest ones",
      kept == sorted(os.path.basename(d) for d in made[-3:]), kept)
check("`latest` points at the most recent run",
      os.path.realpath(os.path.join(base, "latest")) == os.path.realpath(made[-1]))
check("directory name carries a timestamp and the project name",
      os.path.basename(made[-1])[:8].isdigit() and "lab2" in os.path.basename(made[-1]),
      os.path.basename(made[-1]))
check("artifacts land in the run directory",
      os.path.isfile(os.path.join(made[-1], "manifest.json")))
rl = cli.RunLog(base, "/home/alp/labs/lab2", keep=3)
rl.write("absent.log", None)
check("writing missing content is a no-op, not a crash",
      not os.path.exists(os.path.join(rl.dir, "absent.log")))
check("elapsed times read sensibly",
      (cli.fmt_elapsed(7), cli.fmt_elapsed(75), cli.fmt_elapsed(3725))
      == ("0:07", "1:15", "1:02:05"),
      (cli.fmt_elapsed(7), cli.fmt_elapsed(75), cli.fmt_elapsed(3725)))

print("\nuart metric extraction:")
# Build a capture without touching a serial port.
cap = cli.UartCapture.__new__(cli.UartCapture)
cap.buf = bytearray(
    b"Function Unaccelerated software ran for 431.250000 ms\n"
    b"Function Hardware accelerated ran for 88.500000 ms\n"
    b"SW result: 962122000\nHW result: 962122000\nRESULT: PASS\n")
check("captures a float from a named group",
      cap.extract(r"Unaccelerated software ran for ([0-9.]+) ms", 0) == "431.250000")
check("captures the second measurement independently",
      cap.extract(r"Hardware accelerated ran for ([0-9.]+) ms", 0) == "88.500000")
check("returns None when the pattern never appears",
      cap.extract(r"Nonexistent ([0-9]+)", 0) is None)
check("a pattern with no group returns the whole match",
      cap.extract(r"RESULT: PASS", 0) == "RESULT: PASS")

# The buffer is never cleared, so a repeated measurement must resolve to the
# most recent one rather than a stale line from an earlier boot.
cap.buf += b"Function Hardware accelerated ran for 12.000000 ms\n"
check("the last match wins, not the first",
      cap.extract(r"Hardware accelerated ran for ([0-9.]+) ms", 0) == "12.000000")

print("\nmetric comparisons:")
m = {"sw_ms": 431.25, "hw_ms": 88.5}
good, detail = cli.evaluate_comparison(
    {"metric": "sw_ms", "over": "hw_ms", "min_ratio": 2.0}, m)
check("a speedup that clears the bar passes", good, detail)
check("the detail reports the ratio", "4.87x" in detail, detail)
good, detail = cli.evaluate_comparison(
    {"metric": "sw_ms", "over": "hw_ms", "min_ratio": 10.0}, m)
check("a speedup below the bar fails", not good, detail)
check("the failure says what was wanted", "at least 10.00x" in detail, detail)
good, detail = cli.evaluate_comparison(
    {"metric": "sw_ms", "over": "hw_ms", "max_ratio": 2.0}, m)
check("max_ratio is enforced too", not good, detail)
good, detail = cli.evaluate_comparison(
    {"metric": "sw_ms", "over": "typo_ms", "min_ratio": 2.0}, m)
check("a misspelled metric fails instead of passing vacuously", not good, detail)
check("...and names the metrics that were captured",
      "typo_ms" in detail and "hw_ms" in detail, detail)
good, detail = cli.evaluate_comparison(
    {"metric": "sw_ms", "over": "zero", "min_ratio": 2.0}, {**m, "zero": 0.0})
check("dividing by a zero metric fails rather than raising", not good, detail)

print("\npreflight environment checks:")
ok_, d = cli.evaluate_preflight({"command": "true"}, 0, "")
check("a command that succeeds passes", ok_, d)
ok_, d = cli.evaluate_preflight({"command": "test -f /boot/x"}, 1, "")
check("a command that fails is reported with its status", not ok_ and "exit 1" in d, d)
ok_, d = cli.evaluate_preflight(
    {"command": "uname -r", "expect": r"5\.15\.36-xilinx"}, 0, "5.15.36-xilinx-v2022.2")
check("an expected pattern matches", ok_, d)
ok_, d = cli.evaluate_preflight(
    {"command": "uname -r", "expect": r"5\.15\.36-xilinx"}, 0, "6.1.0-generic")
check("a wrong environment is caught", not ok_ and "does not match" in d, d)
ok_, d = cli.evaluate_preflight(
    {"command": "dmesg", "reject": "zocl.*failed"}, 0, "zocl: probe failed")
check("a forbidden pattern fails", not ok_, d)
# A pattern found in the output of a command that itself failed proves nothing.
ok_, d = cli.evaluate_preflight(
    {"command": "cat /etc/x", "expect": "ok"}, 1, "ok")
check("a matching pattern from a failed command does not pass", not ok_, d)
ok_, d = cli.evaluate_preflight(
    {"command": "grep -q x /f", "exit_code": 1}, 1, "")
check("a non-zero status can be the expected one", ok_, d)

print("\nimplementation report parsing:")
_rep = tempfile.mkdtemp()
_util = os.path.join(_rep, "top_utilization_placed.rpt")
with open(_util, "w") as fh:
    fh.write("""
1. Slice Logic
--------------

+----------------------------+------+-------+------------+-----------+-------+
|          Site Type         | Used | Fixed | Prohibited | Available | Util% |
+----------------------------+------+-------+------------+-----------+-------+
| Slice LUTs                 | 8321 |     0 |          0 |     14400 | 57.78 |
|   LUT as Logic             | 7900 |     0 |          0 |     14400 | 54.86 |
|   LUT as Memory            |  421 |     0 |          0 |      6000 |  7.02 |
| Slice Registers            | 9002 |     0 |          0 |     28800 | 31.26 |
+----------------------------+------+-------+------------+-----------+-------+

+-------------------+------+-------+------------+-----------+-------+
|     Site Type     | Used | Fixed | Prohibited | Available | Util% |
+-------------------+------+-------+------------+-----------+-------+
| Block RAM Tile    |   28 |     0 |          0 |        50 | 56.00 |
| DSPs              |   40 |     0 |          0 |        66 | 60.61 |
+-------------------+------+-------+------------+-----------+-------+
""")
u = ra.parse_utilization(_util)
check("LUT totals are read", u["lut"] == {"used": 8321, "available": 14400, "pct": 57.78}, u.get("lut"))
check("an indented sub-row is not mistaken for the total",
      u["lut"]["used"] == 8321, u.get("lut"))
check("registers, BRAM and DSP are read",
      (u["ff"]["used"], u["bram"]["used"], u["dsp"]["used"]) == (9002, 28, 40), u)
check("percentages survive", u["dsp"]["pct"] == 60.61, u.get("dsp"))

_tim = os.path.join(_rep, "top_timing_summary_routed.rpt")
with open(_tim, "w") as fh:
    fh.write("""
Design Timing Summary
---------------------

    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints
    -------      -------  ---------------------  -------------------
      1.234        0.000                      0                 9876
""")
check("WNS is read from the timing summary",
      ra.parse_timing(_tim) == {"wns_ns": 1.234}, ra.parse_timing(_tim))
with open(_tim, "w") as fh:
    fh.write("    WNS(ns)\n    -------\n     -0.512\n")
check("a negative WNS is read as negative",
      ra.parse_timing(_tim) == {"wns_ns": -0.512}, ra.parse_timing(_tim))
check("a report with no WNS block yields nothing rather than guessing",
      ra.parse_timing(_util) == {}, ra.parse_timing(_util))

print("\nresource budgets:")
_r = {"utilization": u, "timing": {"wns_ns": 1.234}}
res = cli.evaluate_resources({"utilization": {"lut_pct_max": 80}}, _r)
check("a budget that is met passes", res == [(True, "LUT 57.78% within 80%")], res)
res = cli.evaluate_resources({"utilization": {"lut_pct_max": 50}}, _r)
check("a budget that is exceeded fails", not res[0][0], res)
res = cli.evaluate_resources({"utilization": {"dsp_max": 40}}, _r)
check("absolute counts are compared, not just percentages", res[0][0], res)
res = cli.evaluate_resources({"timing": {"wns_min": 0.0}}, _r)
check("positive slack meets a zero floor", res[0][0], res)
res = cli.evaluate_resources({"timing": {"wns_min": 0.0}},
                             {"utilization": u, "timing": {"wns_ns": -0.1}})
check("negative slack fails the floor", not res[0][0], res)
res = cli.evaluate_resources({"utilization": {"lut_pct_max": 80}}, {})
check("a budget with no reports fails instead of passing vacuously",
      not res[0][0] and "no LUT figure" in res[0][1], res)
res = cli.evaluate_resources({"utilization": {"nonsense_max": 1}}, _r)
check("an unknown budget key is rejected", not res[0][0], res)
res = cli.evaluate_resources({"utilization": {}}, _r)
check("an empty budget is an error, not a pass", not res[0][0], res)

print("\nlinux console backend:")


class FakeBoard:
    """A Linux console that echoes, answers, and reports an exit status.

    Writes land in the capture buffer synchronously, which is exactly what a
    real board does asynchronously -- close enough to exercise the protocol.
    """
    PROMPT = "root@blkboard:~# "

    def __init__(self, cap, needs_login=False, responses=None):
        self.cap = cap
        self.responses = responses or {}
        self.state = "login" if needs_login else "shell"
        self.commands = []

    def flush(self):
        pass

    def _emit(self, s):
        self.cap.buf += s.encode()

    def write(self, data):
        line = data.decode()
        if self.state == "login":
            if line.strip() == "":
                self._emit("\r\nblkboard login: ")
            else:
                self.state = "password"
                self._emit(line.strip() + "\r\nPassword: ")
            return len(data)
        if self.state == "password":
            self.state = "shell"
            self._emit("\r\n" + self.PROMPT)
            return len(data)
        cmd = line.strip()
        if not cmd:
            self._emit("\r\n" + self.PROMPT)
            return len(data)
        self._emit(cmd + "\r\n")                       # terminal echo
        real = cmd.split("; echo " + cli.LinuxConsole.SENTINEL)[0]
        self.commands.append(real)
        rc, out = self.responses.get(real, (0, ""))
        if out:
            self._emit(out + "\r\n")
        self._emit(f"{cli.LinuxConsole.SENTINEL}{rc}\r\n{self.PROMPT}")
        return len(data)


def make_console(needs_login=False, responses=None):
    cap = cli.UartCapture.__new__(cli.UartCapture)
    cap.buf = bytearray()
    cap._ser = FakeBoard(cap, needs_login, responses)
    con = cli.LinuxConsole(cap, prompt=r"root@[^ ]*# ",
                           login="root", password="root")
    return cap, con


cap, con = make_console(responses={"echo hello": (0, "hello")})
con.connect(timeout=5)
rc, out = con.run("echo hello")
check("runs a command and captures its output", (rc, out) == (0, "hello"), (rc, out))
check("the echoed command is not mistaken for output", "echo hello;" not in out, out)

cap, con = make_console(responses={"false": (1, "")})
con.connect(timeout=5)
rc, out = con.run("false")
check("exit status 1 is propagated", rc == 1, rc)

cap, con = make_console(responses={"cat /nope": (1, "No such file or directory")})
con.connect(timeout=5)
rc, out = con.run("cat /nope")
check("stderr-style output is captured with the failing status",
      rc == 1 and "No such file" in out, (rc, out))

# A board sitting at a login prompt must be driven through it, and the banner
# must not be answered twice -- searching the whole buffer each time used to
# re-match the login line forever.
cap, con = make_console(needs_login=True, responses={"uname -m": (0, "armv7l")})
con.connect(timeout=5)
rc, out = con.run("uname -m")
check("logs in when the board asks", (rc, out) == (0, "armv7l"), (rc, out))
check("the login name is sent exactly once",
      cap.text().count("root\r\nPassword:") == 1, cap.text()[:200])

# Multi-line output, and output that itself contains something prompt-shaped.
cap, con = make_console(responses={"ip addr": (0, "1: lo\r\n2: eth0\r\n    inet 192.168.1.50/24")})
con.connect(timeout=5)
rc, out = con.run("ip addr")
check("multi-line output survives intact",
      "eth0" in out and "192.168.1.50/24" in out, out)

cap, con = make_console(responses={"tricky": (0, "root@blkboard:~# not a prompt")})
con.connect(timeout=5)
rc, out = con.run("tricky")
check("a prompt-shaped string inside output does not truncate it",
      "not a prompt" in out and rc == 0, (rc, out))

# No board at all: connect has to give up loudly rather than hang.
cap = cli.UartCapture.__new__(cli.UartCapture)
cap.buf = bytearray()


class DeadSerial:
    def write(self, data): return len(data)
    def flush(self): pass


cap._ser = DeadSerial()
con = cli.LinuxConsole(cap, prompt=r"root@[^ ]*# ")
t0 = time.time()
try:
    con.connect(timeout=2)
    check("a silent console raises instead of hanging", False, "no error raised")
except cli.ConsoleError as exc:
    check("a silent console raises instead of hanging", "no shell prompt" in str(exc), str(exc))
check("...and gives up near the deadline", time.time() - t0 < 8, time.time() - t0)

print("\nu-boot console:")


class FakeUBoot:
    """Counts down, then offers a prompt once a key arrives."""
    PROMPT = "Zynq> "

    def __init__(self, cap, keys_needed=3, listing=None):
        self.cap = cap
        self.keys = keys_needed
        self.listing = listing or {}
        self.at_prompt = False

    def flush(self):
        pass

    def write(self, data):
        if not self.at_prompt:
            self.keys -= 1
            if self.keys <= 0:
                self.at_prompt = True
                self.cap.buf += b"\r\n" + self.PROMPT.encode()
            else:
                self.cap.buf += b"\rHit any key to stop autoboot:  2 "
            return len(data)
        cmd = data.decode().strip()
        out = self.listing.get(cmd, "")
        self.cap.buf += (cmd + "\r\n" + (out + "\r\n" if out else "")
                         + self.PROMPT).encode()
        return len(data)


_listing = ("            BOOT.BIN\n"
            "            image.ub\n"
            "            boot.scr\n"
            "            binary_container_1.xclbin\n"
            "            matmul\n"
            "5 file(s), 0 dir(s)")
cap = cli.UartCapture.__new__(cli.UartCapture)
cap.buf = bytearray()
cap._ser = FakeUBoot(cap, keys_needed=3, listing={"fatls mmc 0:1 /": _listing})
boot = cli.UBootConsole(cap)
check("autoboot is interrupted by repeated keys", boot.interrupt(timeout=5))
out = boot.command("fatls mmc 0:1 /")
check("the FAT partition is listed", "binary_container_1.xclbin" in out, out)
check("the echoed command is not part of the listing",
      not out.startswith("fatls"), out[:40])
check("the host application is on the card", "matmul" in out, out)

# A board that boots straight through must not look like a u-boot prompt.
cap2 = cli.UartCapture.__new__(cli.UartCapture)
cap2.buf = bytearray()


class SilentBoot:
    def write(self, data):
        cap2.buf += b"\r\nStarting kernel ...\r\n"
        return len(data)

    def flush(self):
        pass


cap2._ser = SilentBoot()
check("no false u-boot prompt when the board boots through",
      not cli.UBootConsole(cap2).interrupt(timeout=2))

print("\nconsole file transfer:")
import base64 as _b64, hashlib as _hl, re as _re


class FakeFsBoard(FakeBoard):
    """FakeBoard plus just enough shell to receive a base64 transfer."""

    def __init__(self, cap, has_sha=True):
        super().__init__(cap)
        self.files = {}
        self.has_sha = has_sha
        self.chunks = 0

    def _shell(self, cmd):
        # shlex.quote leaves shell-safe paths unquoted, so match either form.
        c = cmd.replace("'", "")
        if c.startswith("command -v sha256sum"):
            return (0, "") if self.has_sha else (1, "")
        if c.startswith("command -v md5sum"):
            return (0, "")
        m = _re.match(r"(sha256sum|md5sum) (\S+) 2>/dev/null$", c)
        if m:
            data = self.files.get(m.group(2))
            if data is None:
                return (1, "")
            h = (_hl.sha256 if m.group(1) == "sha256sum" else _hl.md5)(data)
            return (0, f"{h.hexdigest()}  {m.group(2)}")
        m = _re.match(r"mkdir -p \S+ && : > (\S+)$", c)
        if m:
            self.files[m.group(1)] = b""
            return (0, "")
        m = _re.match(r"printf %s ([A-Za-z0-9+/=]*) >> (\S+)$", c)
        if m:
            self.chunks += 1
            self.files[m.group(2)] = self.files.get(m.group(2), b"") + m.group(1).encode()
            return (0, "")
        m = _re.match(r"base64 -d (\S+) > (\S+) && rm -f \S+$", c)
        if m:
            self.files[m.group(2)] = _b64.b64decode(self.files[m.group(1)])
            del self.files[m.group(1)]
            return (0, "")
        return (0, "")

    def write(self, data):
        line = data.decode().strip()
        if self.state == "shell" and line:
            real = line.split("; echo " + cli.LinuxConsole.SENTINEL)[0]
            rc, out = self._shell(real)
            self._emit(line.split("\n")[0] + "\r\n")
            if out:
                self._emit(out + "\r\n")
            self._emit(f"{cli.LinuxConsole.SENTINEL}{rc}\r\n{self.PROMPT}")
            return len(data)
        return super().write(data)


def make_fs_console(has_sha=True):
    cap = cli.UartCapture.__new__(cli.UartCapture)
    cap.buf = bytearray()
    board = FakeFsBoard(cap, has_sha)
    cap._ser = board
    con = cli.LinuxConsole(cap, prompt=r"root@[^ ]*# ")
    con.connect(timeout=5)
    return board, con


_src = os.path.join(tempfile.mkdtemp(), "app.elf")
_blob = bytes(range(256)) * 40                      # 10 KB, not base64-friendly
open(_src, "wb").write(_blob)

board, con = make_fs_console()
check("a file is transferred and verified",
      con.put_file(_src, "/home/root/app.elf", chunk=1024) == "sent")
check("the bytes arrive intact", board.files["/home/root/app.elf"] == _blob)
check("it really was chunked, not one blind write", board.chunks > 1, board.chunks)
check("the base64 staging file is cleaned up",
      "/home/root/app.elf.b64" not in board.files, list(board.files))
check("an unchanged file is not re-sent",
      con.put_file(_src, "/home/root/app.elf", chunk=1024) == "unchanged")

# Corruption must be caught: the console has no error detection of its own.
board.files["/home/root/app.elf"] = _blob[:-1] + b"\x00"
try:
    con.put_file(_src, "/home/root/app.elf", chunk=1024)
    board.files["/home/root/app.elf"] = _blob   # transfer repaired it
    check("a corrupted remote copy is re-sent and ends up correct", True)
except cli.ConsoleError as exc:
    check("a corrupted remote copy is re-sent and ends up correct", False, str(exc))

board, con = make_fs_console(has_sha=False)
check("falls back to md5sum when sha256sum is missing",
      con.put_file(_src, "/home/root/a.elf", chunk=4096) == "sent"
      and con.hasher()[0] == "md5sum", con.hasher()[0])

print("\nremote command quoting:")
import importlib.util as _iu, importlib.machinery as _im
_s = _iu.spec_from_loader("ftcli0", _im.SourceFileLoader("ftcli0", "./fpgatest"))
_c = _iu.module_from_spec(_s); sys.modules["ftcli0"] = _c; _s.loader.exec_module(_c)

import subprocess as _sp

def through_bash(quoted, home="/home/alp"):
    env = dict(os.environ, HOME=home)
    return _sp.run(["bash", "-c", f"printf %s {quoted}"], env=env,
                   capture_output=True, text=True).stdout

check("a leading ~ still expands on the remote side",
      through_bash(_c.rquote("~/vivado/lab2")) == "/home/alp/vivado/lab2",
      through_bash(_c.rquote("~/vivado/lab2")))
check("a path with spaces survives intact",
      through_bash(_c.rquote("/home/a b/lab 2")) == "/home/a b/lab 2",
      through_bash(_c.rquote("/home/a b/lab 2")))
check("shell metacharacters cannot escape the argument",
      through_bash(_c.rquote("~/x; touch /tmp/pwned")) == "/home/alp/x; touch /tmp/pwned",
      through_bash(_c.rquote("~/x; touch /tmp/pwned")))

# The old code used json.dumps for shell arguments. That looks like quoting but
# a double-quoted "a\nb" reaches bash as a literal backslash-n, so a multi-line
# remote.setup silently became one broken line.
import shlex, subprocess
multiline = ("source /Software/xilinx/2022.2/Vivado/2022.2/settings64.sh\n"
             "source /Software/xilinx/2022.2/Vitis/2022.2/settings64.sh")
via_json = subprocess.run(["bash", "-c", f"printf %s {json.dumps(multiline)}"],
                          capture_output=True, text=True).stdout
via_shlex = subprocess.run(["bash", "-c", f"printf %s {shlex.quote(multiline)}"],
                           capture_output=True, text=True).stdout
check("json.dumps really does mangle a multi-line setup (the old bug)",
      via_json != multiline)
check("shlex.quote round-trips it through bash intact", via_shlex == multiline)

print("\ndoctor's remote probe:")
multi = ("source /Software/xilinx/2022.2/Vivado/2022.2/settings64.sh\n"
         "if [ -f /nonexistent/settings64.sh ]; then source /nonexistent/settings64.sh; fi\n"
         "true\n")
tool_probe = ('for t in vivado hw_server xsdb xsct; do '
              'printf "%s -> %s\\n" "$t" "$(command -v $t || echo MISSING)"; done')

# The old join. Kept as a test so the false-green cannot come back.
bad_cmd = "; ".join([multi, tool_probe])
r = _sp.run(["bash", "-c", bad_cmd], capture_output=True, text=True)
check("joining a multi-line setup with '; ' really is a syntax error (the old bug)",
      r.returncode != 0 or not r.stdout.strip(), r.stdout[:200])

good_cmd = "\n".join([multi, tool_probe])
r = _sp.run(["bash", "-c", good_cmd], capture_output=True, text=True)
out = r.stdout.strip().splitlines()
check("joining with newlines runs the probe", len(out) == 4, r.stdout[:300])
check("a missing tool is reported, not silently skipped",
      all("MISSING" in l for l in out), out)
check("every probed tool is named", 
      [l.split(" -> ")[0] for l in out] == ["vivado", "hw_server", "xsdb", "xsct"], out)

print("\nvirtualenv re-exec:")
import subprocess, venv as venvmod

sandbox = tempfile.mkdtemp()
for f in ("fpgatest", "xvcd.py", "remote_agent.py"):
    shutil.copy(f, sandbox)
venvmod.create(os.path.join(sandbox, ".venv"), with_pip=False)
site = [os.path.join(sandbox, ".venv", "lib", d, "site-packages")
        for d in os.listdir(os.path.join(sandbox, ".venv", "lib"))][0]
os.makedirs(os.path.join(site, "pyftdi"), exist_ok=True)
# A stand-in for the real pyftdi that reports which interpreter loaded it.
with open(os.path.join(site, "pyftdi", "__init__.py"), "w") as fh:
    fh.write("import sys\nprint('RAN_UNDER=' + sys.executable)\n")
with open(os.path.join(site, "pyftdi", "ftdi.py"), "w") as fh:
    fh.write("class Ftdi:\n"
             "    def open_mpsse_from_url(self, *a, **k):\n"
             "        raise RuntimeError('no cable')\n")

env = dict(os.environ); env.pop("FPGATEST_NO_REEXEC", None)
out = subprocess.run([sys.executable, "./fpgatest", "scan"], cwd=sandbox, env=env,
                     capture_output=True, text=True, timeout=60)
blob = out.stdout + out.stderr
venv_python = os.path.join(sandbox, ".venv", "bin")
check("re-executed into .venv when the dependency is missing",
      "RAN_UNDER=" in blob and venv_python in blob.split("RAN_UNDER=")[1][:200],
      blob[:400])
# A virtualenv's bin/python3 is a symlink to the base interpreter, so an
# realpath-based "am I already inside?" test silently never fires. Guard it.
check("did not loop forever re-executing", blob.count("RAN_UNDER=") == 1, blob[:400])

out2 = subprocess.run([os.path.join(sandbox, ".venv", "bin", "python3"),
                       "./fpgatest", "scan"], cwd=sandbox, env=env,
                      capture_output=True, text=True, timeout=60)
check("no re-exec when already inside the venv",
      (out2.stdout + out2.stderr).count("RAN_UNDER=") == 1)

bare = tempfile.mkdtemp()          # scripts present, but nobody ran setup.sh
for f in ("fpgatest", "xvcd.py", "remote_agent.py"):
    shutil.copy(f, bare)
out3 = subprocess.run([sys.executable, "./fpgatest", "scan"], cwd=bare,
                      env=env, capture_output=True, text=True, timeout=60)
check("missing .venv is a clear message, not a crash loop",
      out3.returncode != 0 and "setup.sh" in (out3.stdout + out3.stderr),
      (out3.stdout + out3.stderr)[-300:])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
