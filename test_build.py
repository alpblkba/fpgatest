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
