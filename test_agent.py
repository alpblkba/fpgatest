"""
Hardware-free test of the discovery / coherence logic and the generated xsdb
script. Builds synthetic project trees on disk and checks that remote_agent
picks the right files and blocks the right mistakes.

Run: python3 test_agent.py
"""
import json, os, shutil, struct, sys, tempfile, time, zipfile
import remote_agent as ra

PASS = FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}  -- {detail}")


def make_bit(path, design="design_1_wrapper", part="7z007sclg400", body=b"\xaa" * 512):
    def kv(key, val):
        b = val.encode() + b"\x00"
        return key + struct.pack(">H", len(b)) + b
    blob = struct.pack(">H", 9) + bytes([0x0F, 0xF0, 0x0F, 0xF0, 0x0F, 0xF0, 0x0F, 0xF0, 0x00])
    blob += struct.pack(">H", 1) + b"a"
    blob = blob[:-3] + struct.pack(">H", 1) + b"a"  # keep header exactly 13 bytes + 'a'
    blob = (struct.pack(">H", 9)
            + bytes([0x0F, 0xF0, 0x0F, 0xF0, 0x0F, 0xF0, 0x0F, 0xF0, 0x00])
            + struct.pack(">H", 1) + b"a")
    blob += struct.pack(">H", len(design) + 1) + design.encode() + b"\x00"
    blob += kv(b"b", part) + kv(b"c", "2026/08/06") + kv(b"d", "19:04:11")
    blob += b"e" + struct.pack(">I", len(body)) + body
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(blob)
    return path


def make_project(root, bit_body=b"\xaa" * 512, xsa_body=None, with_bit_in_xsa=True,
                 stale_source=False):
    bit = make_bit(os.path.join(root, "lab2.runs", "impl_1", "design_1_wrapper.bit"),
                   body=bit_body)
    xsa = os.path.join(root, "lab2.xsa")
    with zipfile.ZipFile(xsa, "w") as zf:
        if with_bit_in_xsa:
            inner = make_bit(os.path.join(root, "_tmp.bit"),
                             body=xsa_body if xsa_body is not None else bit_body)
            zf.write(inner, "design_1_wrapper.bit")
            os.remove(inner)
        zf.writestr("design_1.hwh", "<hw/>")
        # Real XSAs always carry this, and ps7_init/ps7_post_config are only
        # defined by sourcing it -- nothing in xsdb provides them implicitly.
        zf.writestr("ps7_init.tcl", "proc ps7_init {} {}\nproc ps7_post_config {} {}\n")
    elf = os.path.join(root, "app", "Debug", "calculator.elf")
    os.makedirs(os.path.dirname(elf), exist_ok=True)
    open(elf, "wb").write(b"\x7fELF" + b"\x00" * 64)
    fsbl = os.path.join(root, "fsbl", "Debug", "fsbl.elf")
    os.makedirs(os.path.dirname(fsbl), exist_ok=True)
    open(fsbl, "wb").write(b"\x7fELF")
    src = os.path.join(root, "src", "top.v")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    open(src, "w").write("module top(); endmodule\n")
    past = time.time() - 3600
    os.utime(src, (past, past))
    if stale_source:
        future = time.time() + 600
        os.utime(src, (future, future))
    return bit, xsa, elf


print("bit header parsing:")
tmp = tempfile.mkdtemp()
b = make_bit(os.path.join(tmp, "x.bit"))
h = ra.bit_header(b)
check("design name", h.get("design") == "design_1_wrapper", h)
check("part", h.get("part") == "7z007sclg400", h)
check("build date/time", h.get("date") == "2026/08/06" and h.get("time") == "19:04:11", h)

print("\ncoherent project (impl bit == bit inside xsa):")
root = tempfile.mkdtemp()
bit, xsa, elf = make_project(root)
m = ra.discover(root, {})
check("discovery succeeded", m["ok"], m.get("error"))
check("coherent flag set", m["coherent"] is True, m.get("coherent"))
check("bitstream taken from the XSA", m["bit_source"].startswith("xsa:"), m["bit_source"])
check("application elf found", m["elf"] == elf, m["elf"])
check("fsbl.elf was not mistaken for the app", "fsbl" not in (m["elf"] or ""), m["elf"])
check("no warnings", not m["warnings"], m["warnings"])

print("\nMISMATCH: impl bit drifted from the XSA (the dangerous case):")
root = tempfile.mkdtemp()
make_project(root, bit_body=b"\x11" * 512, xsa_body=b"\x22" * 512)
m = ra.discover(root, {})
check("mismatch detected", m["coherent"] is False, m.get("coherent"))
check("warns about the drift",
      any("DIFFERS" in w for w in m["warnings"]), m["warnings"])
check("still uses the XSA copy so the ELF matches",
      m["bit_source"].startswith("xsa:"), m["bit_source"])

print("\nSTALE: a source file is newer than the bitstream:")
root = tempfile.mkdtemp()
make_project(root, stale_source=True)
m = ra.discover(root, {})
check("stale source listed", m["stale_sources"] == ["src/top.v"], m["stale_sources"])
check("warns about staleness",
      any("newer than the bitstream" in w for w in m["warnings"]), m["warnings"])

print("\nXSA exported without -include_bit:")
root = tempfile.mkdtemp()
make_project(root, with_bit_in_xsa=False)
m = ra.discover(root, {})
check("falls back to the impl bitstream", m["bit_source"] == "impl", m["bit_source"])
check("coherence reported as unverified", m["coherent"] is None, m["coherent"])
check("warns", any("no bitstream" in w for w in m["warnings"]), m["warnings"])

print("\nmissing project:")
m = ra.discover("/nonexistent/xyz", {})
check("clean error", not m["ok"] and "not found" in m["error"], m)

print("\ngenerated xsdb script:")
root = tempfile.mkdtemp()
make_project(root)
m = ra.discover(root, {})
spec = {
    "base": "0x43C00000",
    "load_elf": True,
    "settle_ms": 1500,
    "tests": [
        {"name": "multiply 99 x 99", "ops": [
            {"write": 0x10, "value": 99},
            {"write": 0x18, "value": 99},
            {"write": 0x20, "value": 2},
            {"delay_ms": 5},
            {"read": 0x28, "expect": 9801}]},
        {"name": "post check", "stage": "post_elf",
         "ops": [{"read": 0x28, "expect": 9801}]},
        {"name": "banner", "uart_expect": "ready"},
    ],
}
tcl = ra.build_tcl(m, spec, 41234)
for probe, why in [
    ("connect -url tcp:127.0.0.1:41234", "connects to the right hw_server port"),
    ("fpga -file", "programs the PL"),
    ("loadhw -hw", "loads the hardware handoff"),
    ("ps7_init ; ps7_post_config", "initialises the PS"),
    ("configparams force-mem-access 1", "enables host AXI access with the CPU halted"),
    ("mwr -force 0x43c00010 0x63", "writes op1 as an explicit hex literal (mwr parses values as hex)"),
    ("mwr -force 0x43c00020 0x2", "writes op_sel"),
    ("ft_expect {multiply 99 x 99} 0x43c00028 9801", "checks the result register"),
    ("dow {", "downloads the ELF"),
    ("con", "starts the application"),
]:
    check(why, probe in tcl, f"missing {probe!r}")
check("post_elf test lands after `dow`",
      tcl.index("dow {") < tcl.index("ft_expect {post check}"))
check("pre_elf test lands before `dow`",
      tcl.index("ft_expect {multiply 99 x 99}") < tcl.index("dow {"))
check("uart-only test emits no tcl", "banner" not in tcl)

# Regressions from the first run that got all the way to the board: the PL
# select filter matched the APU too, ps7_init was never defined because nothing
# sourced it, and every register access failed because the A9 was still running.
check("PL select cannot match the APU as well as the device",
      'jtag_device_ctx=~"jsn-*"' not in tcl)
check("ps7_init.tcl from the XSA is sourced before ps7_init is called",
      "source " in tcl and tcl.index("ps_source") < tcl.index("ps7_init ;"))
check("the CPU is halted before ps7_init touches PS registers",
      tcl.index("{stop}") < tcl.index("ps7_init ;"))
check("the CPU is halted before any register write",
      tcl.index("{stop}") < tcl.index("mwr -force"))
# `rst -system` cannot survive the XVC round trip: xsdb times out waiting for
# the DAP to come back and leaves it wedged until the board is power-cycled.
check("no system reset is issued over XVC", "rst -system" not in tcl)
# expr cannot concatenate a bareword with a variable; this form always raised
# `invalid bareword "0x"`, so every register read failed.
check("register reads do not use the invalid expr 0x-concatenation",
      "0x$raw" not in tcl)
# mrd -value answers in decimal; forcing a hex parse read 99 back as 0x99=153.
check("register reads are not force-parsed as hex",
      "scan $hex %x" not in tcl and "string is integer -strict $raw" in tcl)
check("a missing PS debug context stops the run instead of cascading",
      "exit 6" in tcl
      and tcl.index("ps_contexts") < tcl.index("mwr -force"))
check("an empty target list is caught and explained",
      "sees no JTAG targets" in tcl and "exit 5" in tcl)
check("the early exit still emits `done` so the CLI can tell it ended",
      tcl.index("sees no JTAG targets") < tcl.index("exit 5")
      and "ft done - - -" in tcl)

print("\n--- generated script ---")
print("\n".join(tcl.splitlines()[18:]))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
