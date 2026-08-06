# fpgatest

One command: build on the CES server if it is out of date, then program and
test the Blackboard plugged into your Mac.

```
$ fpgatest run
config: ./fpgatest.toml
  ·     logs: ~/.fpgatest/logs/20260806-201455-lab2

local cable
  ok    0x4BA00477 (ARM DAP (Cortex-A9))
  ok    0x03723093 (xc7z007s)

remote: hiwi-node
  ok    reverse tunnel: hiwi-node:38311 -> localhost:52104
  ok    agent synced to ~/.cache/fpgatest/remote_agent.py

build
  ·     needs build: 3 source file(s) newer than the bitstream
  ·     vivado launched detached on hiwi-node (pid 41822)
  ·     safe to Ctrl-C: the build keeps going and `fpgatest build` reattaches
  ok    synth: run
  ok    impl: run
  >     synth running…
  ok    synth  (4:12)
  >     impl running…
  ok    impl  (9:38)
  ·     bit -> /home/alp/labs/lab2/lab2.runs/impl_1/design_1_wrapper.bit
  ok    build complete in 9:41

artifacts under test
  ·     bit    lab2.xsa!design_1_wrapper.bit
  ok    bitstream matches the hardware handoff the ELF was built against

program and test  (bit)
  ok    program_pl
  ok    ps_init
  --- multiply 99 x 99
  ok    multiply 99 x 99  0x00002649

PASS  2 passed, 0 failed
```

## Does the SD card need anything?

No. Set **JP2 to JTAG** and leave the card out entirely — it is never touched.
JTAG configuration writes straight into the PL; there is no boot image, no
FAT32 partition, no FSBL.

There is one consequence worth knowing, and it is the reason `--mode` exists.
In JTAG boot mode the BootROM does not run an FSBL, so the PS comes up
uninitialised — including **FCLK_CLK0**. On a Zynq block design that is the
clock feeding your PL. A bitstream loaded with nothing else done will report
DONE and then sit there doing absolutely nothing, which looks exactly like a
broken design.

So `--mode bit` (the default when there is no application ELF) programs the PL
**and** runs `ps7_init` from the XSA to bring FCLK and the PS-PL interfaces up.
No ELF, no application — just a live PL. `--mode pl-only` is the truly minimal
`fpga -file` and is only correct if your design is clocked by an on-board
oscillator rather than FCLK.

## Why it is built this way

The board is on your desk; the tools and the licence are in Karlsruhe. There are
three ways to bridge that gap and only one of them is pleasant.

**USB/IP** forwards raw USB transfers, so every JTAG shift pays a full internet
round trip. This is why the FPGA lab was so painfully slow.

**A Linux VM on the Mac** running `hw_server` works, but on Apple Silicon it
means x86 emulation plus USB passthrough — two fragile things stacked.

**XVC (Xilinx Virtual Cable)** is the one AMD actually designed for this. A tiny
daemon exposes the JTAG cable over TCP; `hw_server` treats it as a local cable.
The daemon is pure userspace, so it runs natively on Apple Silicon with no VM.
`xvcd.py` here is that daemon: MPSSE on top of `pyftdi`, no compilation.

The one thing that makes or breaks XVC latency is `cable.vector_bytes`. It is
the maximum shift size we advertise in the XVC handshake, and `hw_server` chunks
its traffic to fit. At the default 256 KiB a full 7z007s bitstream download is a
few dozen round trips instead of several thousand.

```
Mac                                          hiwi-node
---                                          ---------
Blackboard --USB--> xvcd :52104
                      ^
                      |  ssh -R 38311:127.0.0.1:52104
                      +-------------- hw_server -e "...xilinx-xvc:127.0.0.1:38311"
                                                ^
UART <--USB-- (FT2232 ch. B)                    |
                                              xsdb
fpgatest ------------------ ssh ----------> remote_agent.py
                                                |
                                          vivado (detached)
```

Nothing but JTAG traffic and a few kilobytes of JSON crosses the network. The
bitstream never leaves the machine that built it.

## Building

`fpgatest run` builds first if it has to, and nothing more than it has to.

Staleness is decided by **Vivado itself** — `NEEDS_REFRESH` and `PROGRESS` on
`synth_1` and `impl_1`, the same properties behind the out-of-date indicator in
the GUI — not by comparing file timestamps and hoping. An up-to-date project is
never re-synthesised. Implementation is additionally forced if the bitstream
has gone missing from a run that still claims to be at 100%.

Vivado runs **detached** on the server: its own session, its own process group,
output to a file. Losing the SSH connection does not kill it. Neither does
Ctrl-C here — you get told so, and `fpgatest build` reattaches to the run
already in flight rather than starting a second Vivado on the same project.

If synthesis or implementation fails, the build stops there, the reason is
printed, and the exit code is non-zero. Nothing is programmed. The whole
`vivado.log` is pulled back to this machine before the run directory is closed,
because the next build on the server will overwrite it.

```bash
fpgatest build              # build only, if out of date
fpgatest build --force      # rebuild regardless
fpgatest build -v           # echo Vivado's own progress lines
fpgatest run --no-build     # never build; fail if stale
fpgatest run --build        # rebuild even if it looks current
```

Two flows are supported. If a `.xpr` is found, run management is used as above.
If you set `build.tcl_script`, that script is run instead — for the non-project
Tcl flow — and staleness falls back to the source-timestamp heuristic.

Simulation is off by default (`build.simulation`). Turn it on to run
`launch_simulation` before synthesis; `build.simulation_fatal` decides whether a
failure there stops the build.

## Logs

Every run gets its own timestamped directory on **this** machine:

```
~/.fpgatest/logs/20260806-201455-lab2/
    build.log        streamed live from the server while Vivado ran
    vivado.log       fetched at the end, before it can be overwritten
    build.tcl        exactly what was executed
    manifest.json    which .bit/.xsa/.elf were chosen, and why
    program.tcl      the generated xsdb script
    xsdb.log         everything xsdb printed
    hw_server.log
    uart.log         everything the board said
    run.json         the whole result, machine-readable
~/.fpgatest/logs/latest -> the newest one
```

Old runs are pruned to `log.keep` (default 50).

## The part that actually catches bugs

Programming almost always "succeeds". The failure that costs an afternoon is a
bitstream that does not match the hardware handoff the application was compiled
against — right software, wrong hardware, silent misbehaviour. So before
anything is loaded, `discover`:

1. Extracts the bitstream **from inside the XSA** and prefers it, because the
   XSA is what Vitis generated `xparameters.h` and `ps7_init` from. This makes a
   mismatch structurally impossible rather than merely detected.
2. Hashes the loose `impl_1/*.bit` and compares. A difference means a re-export
   you forgot, or a sync script quietly reverting your work.
3. Flags any source file newer than the bitstream.
4. Flags an application ELF older than the XSA.

Any of these triggers a build. If one survives the build, the run stops rather
than programming something you did not mean to test. `--force` overrides.

## Install

On the Mac:

```bash
./setup.sh                               # libusb + .venv + dependencies
ln -s "$PWD/fpgatest" /usr/local/bin/fpgatest
```

Homebrew's Python is externally managed (PEP 668), so `pip3 install` into it is
refused. `setup.sh` puts the dependencies in a `.venv` next to the script and
`fpgatest` re-executes itself into that virtualenv on startup — there is
nothing to activate, `./fpgatest` just works.

Nothing to install on the server. The agent is stdlib-only Python and gets
pushed over the existing SSH connection on every run.

```bash
fpgatest doctor     # checks every link: cable, JTAG chain, UART, ssh, tools
fpgatest scan       # just the JTAG chain
fpgatest run        # the whole pipeline
```

Start with `doctor`. It is the only command that tells you *which* link is
broken rather than that something is.

`remote.setup` must source `settings64.sh` for a Vivado that has both `vivado`
and `xsdb`/`hw_server` on the path.

### macOS and the FTDI driver

macOS binds its own driver to FTDI interfaces, which can stop `libusb` from
claiming channel A. If `doctor` reports a claim or resource error on the JTAG
probe, unload it:

```bash
sudo kextunload -b com.apple.driver.AppleUSBFTDI
```

This also removes `/dev/cu.usbserial-*`, so set `uart.enabled = false` when you
do. Re-plug the board afterwards. Try `doctor` first — depending on your macOS
version and the board's FTDI descriptor this may not be needed at all.

## Writing tests

Tests live in `fpgatest.toml`. Two kinds, two phases.

**Register tests** run with the CPU halted immediately after `ps7_init`, with
`force-mem-access` on, so the host drives AXI directly. The result depends only
on your PL — no application, no `xil_printf`, nothing to go wrong in software.
This is the phase that actually tests the Vitis HLS block, and it works in
`--mode bit` with no ELF anywhere in sight.

```toml
[hw]
base = "0x43C00000"

[[test]]
name = "multiply 99 x 99"
ops = [
  { write = 0x10, value = 99 },
  { write = 0x18, value = 99 },
  { write = 0x20, value = 2 },
  { delay_ms = 5 },
  { read = 0x28, expect = 9801 },
]
```

`stage = "post_elf"` runs a register test after the application has started
instead.

**UART tests** are regexes matched against everything the board prints on
FT2232 channel B. `uart_reject` is the inverse — the run fails if the pattern
ever appears.

```toml
[[test]]
name        = "boot banner"
uart_expect = "Calculator ready"
timeout_s   = 15

[[test]]
name        = "no processor exception"
uart_reject = "Data Abort|Prefetch Abort|Undefined Instruction"
```

Useful flags: `--only "multiply 99 x 99"`, `--mode pl-only|bit|full`,
`--emit-tcl` to print the generated `xsdb` script, `--show-uart`.

## What is verified and what is not

Three test files run with no hardware and no server attached. All 133 checks
pass.

- `test_xvcd.py` drives a software model of a JTAG TAP through a fake FTDI that
  interprets our MPSSE opcodes, then checks `scan_chain` recovers the exact
  IDCODEs. Bit ordering, the byte/bit/TMS command mix and TDO reassembly are
  covered at awkward shift lengths (1, 7, 9, 17, 31, 33, 100 bits).
- `test_agent.py` builds synthetic project trees and checks discovery picks the
  right files, catches a drifted bitstream, catches stale sources, ignores
  `fsbl.elf`, and emits a correctly ordered `xsdb` script.
- `test_build.py` puts a fake `vivado` on `PATH` and exercises the real process
  lifecycle: detached launch, log streaming by byte offset without duplication
  or gaps, reattaching to a running build, stopping one, non-zero exit
  propagation, and the three programming modes. Plus run-log rotation.

What has **not** been exercised is real silicon. The FTDI pin mapping is the
standard Digilent one (AD0 TCK, AD1 TDI, AD2 TDO, AD3 TMS) but is worth
confirming with `fpgatest scan` as the very first thing you do. If the chain
scan returns the two expected IDCODEs, every layer below `hw_server` is proven
correct — and if `scan` works but `hw_server` cannot see the target, the problem
is the tunnel or the `auto-open-servers` argument, not the cable.

## Files

| | |
|---|---|
| `fpgatest` | CLI: config, SSH control master, reverse tunnel, build streaming, UART, run logs |
| `xvcd.py` | XVC 1.0 server over FTDI MPSSE; also runnable standalone |
| `remote_agent.py` | Server side: artifact discovery, coherence, detached Vivado, `hw_server` + `xsdb` |
| `fpgatest.example.toml` | Annotated configuration and example testbench |
| `setup.sh` | One-time virtualenv bootstrap |
| `test_xvcd.py` `test_agent.py` `test_build.py` | Hardware-free tests |
