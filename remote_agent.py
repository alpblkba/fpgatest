#!/usr/bin/env python3
"""
remote_agent.py -- the half of fpgatest that lives on the CES server.

Pure standard library (no pip, no venv) so it can be dropped onto any Vitis
build host and just run. It is pushed automatically by the `fpgatest` CLI.

Two jobs:

  discover  Work out which .bit / .xsa / .elf in a project directory are the
            ones under test, and -- more importantly -- decide whether they
            actually belong together. A bitstream that does not match the
            hardware handoff the ELF was compiled against is the single most
            expensive failure mode in this workflow: everything programs
            cleanly and the board just misbehaves.

  run       Bring up hw_server pointed at the tunnelled XVC cable, then drive
            xsdb to program the PL, initialise the PS, run the register-level
            testbench and optionally load and start the application ELF.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

MARKER = "##FPGATEST##"
shlex_quote = shlex.quote

SOURCE_GLOBS = (
    "**/*.v", "**/*.sv", "**/*.vh", "**/*.svh", "**/*.vhd", "**/*.vhdl",
    "**/*.xci", "**/*.bd", "**/*.tcl", "**/*.c", "**/*.cpp", "**/*.h",
    "**/*.hpp", "**/*.xdc",
)

SKIP_DIRS = re.compile(
    r"(^|/)(\.git|\.Xil|\.jobs|ip_user_files|sim_\w+|\.metadata|_ide)(/|$)"
)


# --------------------------------------------------------------------- helpers

def sha256(path: str, limit: int | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            if limit and h.block_size > limit:
                break
    return h.hexdigest()


def newest(paths: list[str]) -> str | None:
    paths = [p for p in paths if os.path.isfile(p) and not SKIP_DIRS.search(p)]
    if not paths:
        return None
    return max(paths, key=lambda p: os.path.getmtime(p))


def rglob(root: str, pattern: str) -> list[str]:
    return glob.glob(os.path.join(root, pattern), recursive=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


def bit_header(path: str) -> dict:
    """Parse the .bit ASCII header: design name, part, build date and time."""
    out: dict = {}
    try:
        with open(path, "rb") as fh:
            blob = fh.read(4096)
        pos = 13  # skip the 13-byte magic preamble
        keys = {b"a": "design", b"b": "part", b"c": "date", b"d": "time"}
        while pos < len(blob):
            key = blob[pos:pos + 1]
            if key not in keys:
                break
            length = int.from_bytes(blob[pos + 1:pos + 3], "big")
            val = blob[pos + 3:pos + 3 + length].rstrip(b"\x00").decode("latin-1")
            out[keys[key]] = val
            pos += 3 + length
    except Exception as exc:
        out["error"] = str(exc)
    return out


# ------------------------------------------------------------------- discovery

def discover(project: str, hints: dict) -> dict:
    project = os.path.abspath(os.path.expanduser(project))
    if not os.path.isdir(project):
        return {"ok": False, "error": f"project directory not found: {project}"}

    warnings: list[str] = []
    notes: list[str] = []

    impl_bit = hints.get("bit") or newest(
        rglob(project, "**/*.runs/impl_*/*.bit") or rglob(project, "**/*.bit")
    )
    xsa = hints.get("xsa") or newest(rglob(project, "**/*.xsa"))

    elf = hints.get("elf")
    if not elf:
        candidates = rglob(project, "**/Debug/*.elf") + rglob(project, "**/Release/*.elf")
        candidates = [c for c in candidates
                      if "fsbl" not in os.path.basename(c).lower()]
        elf = newest(candidates)

    if not impl_bit and not xsa:
        return {"ok": False,
                "error": f"no .bit and no .xsa found anywhere under {project}"}

    # ---- coherence: prefer the bitstream that ships inside the XSA ----------
    #
    # The XSA is what Vitis compiled the ELF against (xparameters.h, address
    # map, ps7_init). If the loose impl bitstream has drifted from it, loading
    # the loose one gives you a board that runs the right software against the
    # wrong hardware. So we default to the XSA's own bitstream and shout when
    # the two disagree.
    chosen_bit = impl_bit
    bit_source = "impl"
    coherent: bool | None = None
    extracted = None
    ps7_init_tcl = None

    if xsa:
        try:
            with zipfile.ZipFile(xsa) as zf:
                # ps7_init/ps7_post_config are plain Tcl procs living in
                # ps7_init.tcl inside the XSA. Nothing defines them implicitly:
                # `loadhw` only sets the memory map (2022.2 has no -regs
                # option), so the script has to source this file by hand or the
                # PS is never initialised and FCLK_CLK0 never runs.
                for name in zf.namelist():
                    if os.path.basename(name) == "ps7_init.tcl":
                        tmpdir = os.path.join(tempfile.gettempdir(),
                                              "fpgatest-" + hashlib.sha1(
                                                  xsa.encode()).hexdigest()[:12])
                        os.makedirs(tmpdir, exist_ok=True)
                        ps7_init_tcl = os.path.join(tmpdir, "ps7_init.tcl")
                        with zf.open(name) as src, open(ps7_init_tcl, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        break

                members = [n for n in zf.namelist() if n.lower().endswith(".bit")]
                if members:
                    member = members[0]
                    tmpdir = os.path.join(tempfile.gettempdir(),
                                          "fpgatest-" + hashlib.sha1(
                                              xsa.encode()).hexdigest()[:12])
                    os.makedirs(tmpdir, exist_ok=True)
                    extracted = os.path.join(tmpdir, os.path.basename(member))
                    with zf.open(member) as src, open(extracted, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    chosen_bit, bit_source = extracted, f"xsa:{member}"
                    if impl_bit:
                        coherent = sha256(impl_bit) == sha256(extracted)
                        if coherent:
                            notes.append(
                                "impl bitstream is byte-identical to the one in the XSA")
                        else:
                            warnings.append(
                                "impl bitstream DIFFERS from the bitstream inside the "
                                "XSA -- the ELF was built against the XSA, so the XSA "
                                "copy is being used. Re-export the hardware if you "
                                "meant to test the impl run.")
                else:
                    warnings.append(
                        "XSA contains no bitstream (exported without -include_bit); "
                        "falling back to the impl .bit, coherence unverified")
        except zipfile.BadZipFile:
            warnings.append(f"{xsa} is not a readable XSA archive")

    if not chosen_bit:
        return {"ok": False, "error": "no usable bitstream found"}

    # ---- staleness: did anyone touch sources after the bitstream was built? --
    bit_mtime = os.path.getmtime(impl_bit or chosen_bit)
    stale: list[str] = []
    for pattern in SOURCE_GLOBS:
        for path in rglob(project, pattern):
            if SKIP_DIRS.search(path):
                continue
            try:
                if os.path.getmtime(path) > bit_mtime + 1:
                    stale.append(os.path.relpath(path, project))
            except OSError:
                pass
        if len(stale) > 25:
            break
    if stale:
        warnings.append(
            f"{len(stale)} source file(s) are newer than the bitstream, e.g. "
            + ", ".join(sorted(stale)[:4]))

    if elf and xsa and os.path.getmtime(elf) < os.path.getmtime(xsa) - 1:
        warnings.append(
            "the application ELF is older than the XSA -- rebuild the Vitis "
            "application against the current platform")

    manifest = {
        "ok": True,
        "project": project,
        "bit": chosen_bit,
        "bit_source": bit_source,
        "bit_header": bit_header(chosen_bit),
        "bit_sha256": sha256(chosen_bit)[:16],
        "impl_bit": impl_bit,
        "xsa": xsa,
        "ps7_init_tcl": ps7_init_tcl,
        "elf": elf,
        "coherent": coherent,
        "stale_sources": sorted(stale)[:25],
        "warnings": warnings,
        "notes": notes,
    }
    return manifest




# ------------------------------------------------------------------ build side
#
# Vivado runs are launched detached (own session and process group, output to a
# file) and their state lives in a per-project directory on the server. That is
# what makes a build survive a dropped SSH session or a Ctrl-C on the laptop:
# `fpgatest build` simply reattaches to a run that is already in flight.

STATE_ROOT = os.path.expanduser("~/.cache/fpgatest/builds")


def build_dir_for(project: str) -> str:
    project = os.path.abspath(os.path.expanduser(project))
    key = hashlib.sha1(project.encode()).hexdigest()[:12]
    name = os.path.basename(project.rstrip("/")) or "project"
    path = os.path.join(STATE_ROOT, f"{name}-{key}")
    os.makedirs(path, exist_ok=True)
    return path


def find_xpr(project: str) -> str:
    return newest(rglob(project, "**/*.xpr"))


BUILD_TCL = r"""
# generated by fpgatest -- staleness-aware project build
proc ftlog {args} { puts "%MARKER%|[join $args |]" ; flush stdout }
proc ftflat {msg} { return [string map [list | / \n " "] $msg] }

# Lean on Vivado's own bookkeeping rather than guessing from file timestamps:
# NEEDS_REFRESH is what the GUI's out-of-date indicator uses.
proc ft_needs_run {name} {
    if {[catch {get_runs $name} r]} { return 1 }
    if {[catch {get_property NEEDS_REFRESH $r} nr]} { return 1 }
    if {$nr} { return 1 }
    if {[get_property PROGRESS $r] ne "100%"} { return 1 }
    if {[string match -nocase "*error*" [get_property STATUS $r]]} { return 1 }
    return 0
}

proc ft_die {stage msg} {
    ftlog stage $stage fail [ftflat $msg]
    ftlog build fail - -
    exit 2
}

if {[catch {open_project %XPR%} err]} { ft_die open $err }
catch {update_compile_order -fileset sources_1}

set do_synth [ft_needs_run synth_1]
set do_impl  [expr {$do_synth || [ft_needs_run impl_1]}]

# A run can report 100% and still have had its bitstream deleted underneath it.
set bitdir [get_property DIRECTORY [get_runs impl_1]]
if {[llength [glob -nocomplain [file join $bitdir *.bit]]] == 0} { set do_impl 1 }
if {%FORCE%} { set do_synth 1 ; set do_impl 1 }

ftlog plan synth [expr {$do_synth ? "run" : "up-to-date"}] -
ftlog plan impl  [expr {$do_impl  ? "run" : "up-to-date"}] -

if {%DO_SIM%} {
    ftlog stage sim start -
    if {[catch {launch_simulation} err]} {
        ftlog stage sim fail [ftflat $err]
        if {%SIM_FATAL%} { ftlog build fail - - ; exit 2 }
    } else {
        catch {close_sim}
        ftlog stage sim ok -
    }
}

if {$do_synth} {
    ftlog stage synth start -
    catch {reset_run synth_1}
    if {[catch {launch_runs synth_1 -jobs %JOBS%} err]} { ft_die synth $err }
    wait_on_run synth_1
    if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {
        ft_die synth [get_property STATUS [get_runs synth_1]]
    }
    ftlog stage synth ok -
} else {
    ftlog stage synth skip up-to-date
}

if {$do_impl} {
    ftlog stage impl start -
    catch {reset_run impl_1}
    if {[catch {launch_runs impl_1 -to_step write_bitstream -jobs %JOBS%} err]} {
        ft_die impl $err
    }
    wait_on_run impl_1
    if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
        ft_die impl [get_property STATUS [get_runs impl_1]]
    }
    ftlog stage impl ok -
} else {
    ftlog stage impl skip up-to-date
}

set bits [lsort [glob -nocomplain [file join $bitdir *.bit]]]
if {[llength $bits] == 0} { ft_die impl "implementation finished but produced no bitstream" }
ftlog artifact bit [lindex $bits 0] -

if {%XSA% ne {}} {
    ftlog stage export_xsa start -
    if {[catch {write_hw_platform -fixed -include_bit -force %XSA%} err]} {
        ftlog stage export_xsa fail [ftflat $err]
    } else {
        ftlog stage export_xsa ok -
    }
}

ftlog build ok - -
exit 0
"""


def render_build_tcl(xpr: str, opts: dict) -> str:
    xsa = opts.get("export_xsa") or ""
    return (BUILD_TCL
            .replace("%MARKER%", MARKER)
            .replace("%XPR%", "{" + xpr + "}")
            .replace("%JOBS%", str(int(opts.get("jobs") or (os.cpu_count() or 4))))
            .replace("%FORCE%", "1" if opts.get("force") else "0")
            .replace("%DO_SIM%", "1" if opts.get("simulation") else "0")
            .replace("%SIM_FATAL%", "1" if opts.get("simulation_fatal") else "0")
            .replace("%XSA%", "{" + xsa + "}"))


def read_state(bdir: str):
    path = os.path.join(bdir, "state.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            state = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None

    rc_path = os.path.join(bdir, "exit_code")
    exit_code = None
    if os.path.isfile(rc_path):
        raw = open(rc_path).read().strip()
        if raw:
            try:
                exit_code = int(raw)
            except ValueError:
                exit_code = -1

    alive = False
    if exit_code is None:
        try:
            os.kill(state["pid"], 0)
            alive = True
        except OSError:
            alive = False

    state["running"] = alive
    state["exit_code"] = exit_code
    if exit_code is None and not alive:
        # Vanished without writing an exit code: killed, OOM, or the node
        # rebooted. Report it rather than polling forever.
        state["exit_code"] = -1
        state["died"] = True
    try:
        state["log_size"] = os.path.getsize(state["log"])
    except OSError:
        state["log_size"] = 0
    return state


def build_start(args) -> dict:
    project = os.path.abspath(os.path.expanduser(args.project))
    if not os.path.isdir(project):
        return {"ok": False, "error": f"project directory not found: {project}"}

    bdir = build_dir_for(project)
    existing = read_state(bdir)
    if existing and existing.get("running"):
        # Someone (possibly a previous, disconnected invocation) is already
        # building this project. Attach instead of starting a second Vivado.
        return {"ok": True, "attached": True, "state": existing, "build_dir": bdir}

    opts = json.loads(args.opts) if args.opts else {}

    if opts.get("tcl_script"):
        tcl_path = os.path.expanduser(opts["tcl_script"])
        if not os.path.isabs(tcl_path):
            tcl_path = os.path.join(project, tcl_path)
        if not os.path.isfile(tcl_path):
            return {"ok": False, "error": f"build script not found: {tcl_path}"}
        kind = "script"
    else:
        xpr = opts.get("xpr") or find_xpr(project)
        if not xpr:
            return {"ok": False,
                    "error": (f"no .xpr found under {project} and no "
                              "build.tcl_script configured -- cannot build")}
        tcl_path = os.path.join(bdir, "build.tcl")
        with open(tcl_path, "w") as fh:
            fh.write(render_build_tcl(xpr, opts))
        kind = f"project:{os.path.basename(xpr)}"

    log_path = os.path.join(bdir, "build.log")
    vlog_path = os.path.join(bdir, "vivado.log")
    rc_path = os.path.join(bdir, "exit_code")
    rc_tmp = rc_path + ".part"
    for old in (log_path, vlog_path, rc_path, rc_tmp):
        try:
            os.remove(old)
        except OSError:
            pass

    # The exit code is written from an EXIT trap and moved into place, so a
    # poller never sees a half-written file and a run that dies before vivado
    # (broken setup line, vanished project) still records an rc instead of
    # looking like it was killed.
    runner = os.path.join(bdir, "run.sh")
    with open(runner, "w") as fh:
        fh.write("#!/bin/bash\n")
        fh.write("_ft_rc=90\n")
        fh.write("_ft_finish() { printf '%%s\\n' \"$_ft_rc\" > %s && mv -f %s %s; }\n"
                 % (shlex_quote(rc_tmp), shlex_quote(rc_tmp), shlex_quote(rc_path)))
        fh.write("trap _ft_finish EXIT\n")
        if args.setup:
            fh.write(args.setup + "\n")
        fh.write("cd %s || exit 90\n" % shlex_quote(project))
        fh.write("vivado -mode batch -nojournal -notrace -log %s -source %s\n"
                 % (shlex_quote(vlog_path), shlex_quote(tcl_path)))
        fh.write("_ft_rc=$?\n")
    os.chmod(runner, 0o755)

    log_fh = open(log_path, "wb")
    try:
        # start_new_session already puts bash in its own session and process
        # group. Prefixing setsid(1) as well would make it fork and exit, so
        # the pid we record below would be dead within milliseconds and every
        # later poll -- each a fresh ssh invocation -- would call the build
        # vanished. (It is also not a macOS binary, so the tests could not run.)
        proc = subprocess.Popen(
            ["bash", runner],
            stdout=log_fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True, cwd=bdir)
    finally:
        log_fh.close()

    state = {
        "pid": proc.pid, "project": project, "kind": kind, "tcl": tcl_path,
        "log": log_path, "vivado_log": vlog_path, "started": time.time(),
        "started_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(os.path.join(bdir, "state.json"), "w") as fh:
        json.dump(state, fh)

    return {"ok": True, "attached": False, "build_dir": bdir,
            "state": dict(state, running=True, exit_code=None, log_size=0)}


def build_status(args) -> dict:
    bdir = build_dir_for(args.project)
    state = read_state(bdir)
    result = {"ok": True, "state": state, "build_dir": bdir,
              "chunk": "", "next_offset": args.offset or 0}
    if state and args.offset is not None:
        try:
            with open(state["log"], "rb") as fh:
                fh.seek(int(args.offset))
                raw = fh.read(int(args.max_bytes))
            # Byte offsets, not string lengths: lossy decoding would desync the
            # cursor and we would re-send or skip log content.
            result["next_offset"] = int(args.offset) + len(raw)
            result["chunk"] = raw.decode(errors="replace")
        except OSError:
            pass
    return result


def build_stop(args) -> dict:
    state = read_state(build_dir_for(args.project))
    if not state or not state.get("running"):
        return {"ok": True, "stopped": False}
    try:
        os.killpg(os.getpgid(state["pid"]), signal.SIGTERM)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "stopped": True}


def build_logs(args) -> dict:
    bdir = build_dir_for(args.project)
    logs = {}
    for name in ("build.log", "vivado.log", "build.tcl"):
        path = os.path.join(bdir, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                logs[name] = fh.read().decode(errors="replace")
    return {"ok": True, "logs": logs, "build_dir": bdir}



# ------------------------------------------------------------------ xsdb script

TCL_PREAMBLE = r"""
proc ft {args} { puts "%MARKER%|[join $args |]" ; flush stdout }

proc ft_read {addr} {
    set raw ""
    if {[catch {set raw [lindex [mrd -force -value $addr] 0]}]} { set raw "" }
    if {$raw eq ""} {
        # `mrd -value` has been seen returning an empty list rather than
        # raising. Falling straight through to expr then died with the
        # spectacularly unhelpful `invalid bareword "0x"`, which says nothing
        # about the read having failed. Fall back to the printed
        # "ADDR:   VALUE" form, and if that is empty too, say so plainly.
        set out ""
        catch {set out [mrd -force $addr]}
        set raw [lindex [split [string trim $out]] end]
        if {$raw eq "" || [string match "*:" $raw]} {
            error "mrd returned no value for $addr (raw output: [string trim $out])"
        }
    }
    # Two traps here, each of which produced a confidently wrong number.
    #
    # 1. Concatenating the bareword 0x with a variable inside expr is not valid
    #    Tcl -- it raises `invalid bareword "0x"` whatever the value is, so
    #    this read never once succeeded.
    # 2. `mrd -value` returns the value in DECIMAL ("99"), not hex. Parsing it
    #    as hex turned a perfectly correct 99 into 0x99 = 153 and failed the
    #    comparison while the hardware had been right all along.
    #
    # expr handles both a bare decimal and an explicit 0x literal, and
    # `string is integer` accepts exactly those two forms and nothing else.
    if {![string is integer -strict $raw]} {
        error "unparsable value [string map {| /} $raw] read from $addr"
    }
    return [expr {$raw}]
}

proc ft_expect {name addr want} {
    if {[catch {ft_read $addr} got]} {
        ft test $name error [string map {| /} $got]
        return
    }
    if {$got == $want} {
        ft test $name pass [format "0x%08X" $got]
    } else {
        ft test $name fail [format "want 0x%08X got 0x%08X" $want $got]
    }
}

proc ft_try {name body} {
    if {[catch {uplevel 1 $body} err]} {
        ft step $name fail [string map {| /} $err]
        return 0
    }
    return 1
}

proc ft_step {name body} {
    if {[catch {uplevel 1 $body} err]} {
        ft step $name fail [string map {| /} $err]
        return 0
    }
    ft step $name ok -
    return 1
}
"""


def tcl_quote(value: str) -> str:
    return "{" + str(value).replace("}", "").replace("{", "") + "}"


def build_tcl(manifest: dict, spec: dict, hws_port: int) -> str:
    """Emit the xsdb script.

    Three modes, narrowest first:

      pl-only  Nothing but `fpga -file`. The PS is left untouched, so this only
               makes sense if the PL is clocked by an on-board oscillator. On a
               Zynq block design the clock normally comes from FCLK_CLK0, which
               does not run until ps7_init has been executed -- a design
               programmed this way will look configured and do nothing.
      bit      Program the PL and initialise the PS (ps7_init) so FCLK and the
               PS-PL interfaces come alive, then run the register testbench
               with the CPU halted. No application is downloaded.
      full     As above, plus download and start the application ELF.
    """
    mode = spec.get("mode", "full")
    lines = [TCL_PREAMBLE.replace("%MARKER%", MARKER)]
    a = lines.append

    a(f"if {{![ft_step connect {{connect -url tcp:127.0.0.1:{hws_port}}}]}} {{ exit 3 }}")
    # hw_server may open the XVC cable and enumerate the chain asynchronously,
    # so a single immediate query can race it. Poll instead of assuming.
    a("""set ft_tgts {}
for {set ft_i 0} {$ft_i < 15} {incr ft_i} {
    if {[catch {targets -target-properties} ft_tgts]} { set ft_tgts {} }
    if {[llength $ft_tgts] > 0} { break }
    ft info waiting_for_targets $ft_i -
    after 2000
}
ft info targets [llength $ft_tgts]
if {[llength $ft_tgts] == 0} {
    ft step targets fail {hw_server is connected but sees no JTAG targets after 30s: the XVC cable never attached}
    ft done - - -
    exit 5
}""")

    # The old fallback filtered on jtag_device_ctx alone, which matches the APU
    # as well as the device and then dies with "more than one targets found".
    # Both fallbacks stay pinned to the device name so only the PL can match.
    select_pl = """ft_step select_pl {
        if {[catch {targets -set -nocase -filter {name =~ "xc7z*"}}]} {
            targets -set -filter {level==0 && name =~ "xc7z*"}
        }
    }"""
    bit = tcl_quote(manifest["bit"])

    if mode == "pl-only":
        a(select_pl)
        a(f"if {{![ft_step program_pl {{fpga -file {bit}}}]}} {{ exit 4 }}")
        a("catch {ft info done_state [fpga -state]}")
        a("ft done - - -")
        a("exit 0")
        return "\n".join(lines) + "\n"

    # Vitis' own debug launcher opens with `rst -system` here. Do NOT copy that
    # over XVC: the reset drops the DAP and xsdb's reconnect timeout is shorter
    # than a JTAG round trip to the far end of the tunnel, so it reports
    # "Timeout waiting DAP to reconnect after reset" and leaves the DAP wedged
    # -- after which the APU and A9 contexts stop being enumerated entirely and
    # only a board power cycle brings them back. Nothing here needs it: JP2 is
    # JTAG boot, so the BootROM has loaded nothing that would have to be reset.
    a(select_pl)
    a(f"if {{![ft_step program_pl {{fpga -file {bit}}}]}} {{ exit 4 }}")

    if manifest.get("xsa"):
        xsa = tcl_quote(manifest["xsa"])
        # If the PS contexts are missing the DAP is wedged, and every register
        # access below would fail against the PL device with a misleading
        # "Context does not support memory write". Stop here and say so
        # instead of emitting twenty errors that hide the real cause.
        a("""if {![ft_step select_apu {targets -set -nocase -filter {name =~ "APU*"}}]} {
    ft step ps_contexts fail {no APU/A9 debug context on the JTAG chain: the ARM DAP is not responding. Power-cycle the board and run again.}
    ft done - - -
    exit 6
}""")
        a(f"ft_step loadhw {{loadhw -hw {xsa} -mem-ranges [list {{0x40000000 0xbfffffff}}]}}")
        a("""if {![ft_step select_cpu {targets -set -nocase -filter {name =~ "*A9*#0"}}]} {
    ft step ps_contexts fail {APU is present but no Cortex-A9 core context: the DAP is only half awake. Power-cycle the board and run again.}
    ft done - - -
    exit 6
}""")
        # JP2 = JTAG boot means the BootROM leaves the A9 running with no FSBL.
        # ps7_init writes the PS configuration registers through the DAP, and
        # the DAP refuses register and memory access on a running core, so the
        # CPU has to be halted first -- this is the `stop` that Vitis' own
        # debug launcher issues at exactly this point.
        # Already-halted is the normal case in JTAG boot, not a failure.
        a('ft_step stop_cpu {if {[catch {stop} e] '
          '&& ![string match "*Already stopped*" $e]} { error $e }}')
        # Without ps7_init there is no FCLK, so the PL sits configured but
        # unclocked. This matters even when no application is being run.
        # ps7_init is not a built-in: it is a proc defined by ps7_init.tcl
        # inside the XSA, which discover() extracts for us.
        if manifest.get("ps7_init_tcl"):
            a(f"ft_step ps_source {{source {tcl_quote(manifest['ps7_init_tcl'])}}}")
            a("ft_step ps_init {ps7_init ; ps7_post_config}")
        else:
            a("ft info ps_init skipped no-ps7_init.tcl-in-xsa")
    else:
        a("ft info ps_init skipped no-xsa")

    # force-mem-access lets the host drive AXI while the CPU stays halted,
    # which is what makes the register testbench independent of any software.
    a("ft_step mem_access {configparams force-mem-access 1}")

    def emit_tests(stage: str) -> None:
        for test in spec.get("tests", []):
            if not test.get("ops"):
                continue
            if test.get("stage", "pre_elf") != stage:
                continue
            base = int(str(test.get("base", spec.get("base", "0x0"))), 0)
            name = tcl_quote(test.get("name", "unnamed"))
            a(f"ft testbegin {name} - -")
            for op in test["ops"]:
                if "write" in op:
                    addr = base + int(str(op["write"]), 0)
                    val = int(str(op["value"]), 0)
                    # Emit an explicit 0x literal so the written value cannot
                    # depend on how mwr parses a bare number.
                    a(f"ft_try {tcl_quote('write ' + hex(addr))} "
                      f"{{mwr -force {hex(addr)} {hex(val)}}}")
                elif "read" in op:
                    addr = base + int(str(op["read"]), 0)
                    want = int(str(op["expect"]), 0)
                    a(f"ft_expect {name} {hex(addr)} {want}")
                elif "delay_ms" in op:
                    a(f"after {int(op['delay_ms'])}")

    emit_tests("pre_elf")

    if mode == "full" and manifest.get("elf"):
        elf = tcl_quote(manifest["elf"])
        a(f"ft_step download_elf {{dow {elf}}}")
        a("ft_step run {con}")
        a(f"after {int(spec.get('settle_ms', 1500))}")
        emit_tests("post_elf")
    else:
        a(f"ft info elf skipped mode={mode}")

    a("ft done - - -")
    a("exit 0")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------- run

def bash(cmd: str, setup: str | None, **kw):
    prefix = f"{setup}\n" if setup else ""
    return subprocess.Popen(["bash", "-c", prefix + cmd], **kw)


def run(args) -> dict:
    manifest = json.loads(args.manifest) if args.manifest else discover(
        args.project, {})
    if not manifest.get("ok"):
        return {"ok": False, "error": manifest.get("error"), "manifest": manifest}
    spec = json.loads(args.spec) if args.spec else {}

    hws_port = free_port()
    events: list[dict] = []
    hw_log = tempfile.NamedTemporaryFile("w+", suffix=".hwserver.log", delete=False)

    # -l takes NAMED TOKENS, not severities -- `hw_server -h` lists them
    # (jtag, jtag2, protocol, context, discovery, ...). "-l info" is not one of
    # them, and on a bad token hw_server abandons argument parsing entirely, so
    # it silently ignores -s and listens on the default 3121 while reporting
    # that it started normally. jtag2 is what prints the jtagpoll trace showing
    # whether the XVC cable attached and enumerated.
    #
    # -p0 disables the 3000-3003 GDB ports. Nothing here uses them and on a
    # shared node they collide with any other instance, producing "Address
    # already in use" warnings that look like a cause and are not.
    #
    # stdbuf keeps hw_server's stdout unbuffered. Without it the banner and any
    # XVC complaint sit in a pipe buffer and are lost when we terminate it --
    # which is precisely the output needed to debug a failed attach.
    hw_cmd = (
        f'{"stdbuf -o0 -e0 " if shutil.which("stdbuf") else ""}'
        f'hw_server -s tcp::{hws_port} -p0 -L- -ljtag,jtag2 '
        f'-e "set auto-open-servers xilinx-xvc:127.0.0.1:{args.xvc_port}"'
    )
    # start_new_session puts hw_server in its own process group so the whole
    # tree can be signalled at once. bash does not exec hw_server here -- the
    # `setup` prologue forces it to fork -- so killing just the Popen child
    # leaves an orphaned hw_server holding its ports forever. That leaked one
    # instance per run until the node had fifteen of them.
    hw = bash(hw_cmd, args.setup, stdout=hw_log, stderr=subprocess.STDOUT,
              start_new_session=True)

    try:
        if not wait_for_port(hws_port, timeout=45):
            # hw_server announces the port it actually bound to. If it differs
            # from what we asked for, believe the log rather than giving up.
            text = open(hw_log.name).read()
            match = re.search(r"use url:\s*TCP:[^\s:]*:(\d+)", text)
            if match and wait_for_port(int(match.group(1)), timeout=10):
                hws_port = int(match.group(1))
            else:
                return {"ok": False,
                        "error": (f"hw_server did not open port {hws_port} "
                                  "within 45s"),
                        "hw_server_log": text[-4000:]}

        tcl = build_tcl(manifest, spec, hws_port)
        script = tempfile.NamedTemporaryFile("w", suffix=".tcl", delete=False)
        script.write(tcl)
        script.close()

        proc = bash(f"xsdb {script.name}", args.setup,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        raw_lines: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            raw_lines.append(line)
            if line.startswith(MARKER):
                parts = line[len(MARKER):].lstrip("|").split("|")
                parts += ["-"] * (4 - len(parts))
                kind, name, status, detail = parts[:4]
                events.append({"kind": kind, "name": name,
                               "status": status, "detail": detail})
                if not args.quiet:
                    detail_txt = "" if detail == "-" else f"  {detail}"
                    print(f"    [{status:>4}] {kind} {name}{detail_txt}",
                          file=sys.stderr, flush=True)
        proc.wait(timeout=args.timeout)

        hw_log.seek(0)
        return {
            "ok": True,
            "manifest": manifest,
            "tcl": tcl if args.emit_tcl else None,
            "events": events,
            "xsdb_exit": proc.returncode,
            "xsdb_log": "\n".join(raw_lines)[-20000:],
            "hw_server_log": open(hw_log.name).read()[-20000:],
        }
    finally:
        try:
            os.killpg(os.getpgid(hw.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            hw.terminate()
        try:
            hw.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(hw.pid), signal.SIGKILL)


# ------------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description="fpgatest server-side agent")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover")
    d.add_argument("--project", required=True)
    d.add_argument("--bit"), d.add_argument("--xsa"), d.add_argument("--elf")

    for name, needs_opts in (("build-start", True), ("build-status", False),
                             ("build-stop", False), ("build-logs", False)):
        b = sub.add_parser(name)
        b.add_argument("--project", required=True)
        b.add_argument("--setup")
        if needs_opts:
            b.add_argument("--opts", help="JSON build options")
        if name == "build-status":
            b.add_argument("--offset", type=int)
            b.add_argument("--max-bytes", type=int, default=1 << 20)

    r = sub.add_parser("run")
    r.add_argument("--project", required=True)
    r.add_argument("--xvc-port", type=int, required=True)
    r.add_argument("--manifest", help="JSON manifest from a previous discover")
    r.add_argument("--spec", help="JSON test specification")
    r.add_argument("--setup", help="shell snippet sourced before Xilinx tools")
    r.add_argument("--timeout", type=float, default=900)
    r.add_argument("--emit-tcl", action="store_true")
    r.add_argument("--quiet", action="store_true")

    args = ap.parse_args()

    if args.cmd == "discover":
        hints = {k: getattr(args, k) for k in ("bit", "xsa", "elf")
                 if getattr(args, k, None)}
        result = discover(args.project, hints)
    elif args.cmd == "build-start":
        result = build_start(args)
    elif args.cmd == "build-status":
        result = build_status(args)
    elif args.cmd == "build-stop":
        result = build_stop(args)
    elif args.cmd == "build-logs":
        result = build_logs(args)
    else:
        result = run(args)

    print("##FPGATEST-RESULT##" + json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
