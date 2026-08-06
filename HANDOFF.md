# fpgatest — debugging handoff

Paste this whole file as the opening message of a Claude Code session in this
repo. It describes the system, exactly where it is stuck, what has already been
ruled in or out, and which experiments to run in which order.

---

## What I need

`hw_server` on a remote Linux server will not attach to the Xilinx Virtual Cable
(XVC) daemon running on my Mac, so it reports zero JTAG targets and no bitstream
is ever programmed. Every other link in the chain is proven working. I need this
diagnosed and fixed, with a bias toward *evidence over guessing* — the tool
already has telemetry, so add more of it rather than changing things blindly.

---

## What the tool is

`fpgatest` programs and tests a Zynq-7000 FPGA board (Real Digital **Blackboard**,
`xc7z007sclg400-1`) that is plugged into my **Apple Silicon Mac**, using Vivado
and Vitis that live on a **remote university Linux server**. It is for validating
teaching-lab material (a Vitis HLS seven-segment display driver plus an AXI-Lite
calculator) on real hardware.

The board is on my desk. The tools and the licence are in Karlsruhe. Bridging
that gap is the whole point of the program.

### Why XVC rather than the obvious alternatives

- **USB/IP** forwards raw USB transfers, so every JTAG shift pays a full internet
  round trip. Tried previously in another lab; unusably slow.
- **A Linux VM on the Mac** running `hw_server` would work but means x86
  emulation plus USB passthrough on Apple Silicon — two fragile things stacked.
- **XVC** is what AMD designed for this. A userspace daemon exposes the JTAG
  cable over TCP and `hw_server` treats it as a local cable. Runs natively on
  Apple Silicon, no VM, no kernel extension.

### Architecture

```
Mac (this machine)                                remote server
------------------                                -------------
Blackboard --USB--> xvcd.py :LOCAL_PORT
                        ^
                        |  ssh -R REMOTE_PORT:127.0.0.1:LOCAL_PORT
                        +-------------- hw_server -s tcp::HWS_PORT
                                          -e "set auto-open-servers
                                              xilinx-xvc:127.0.0.1:REMOTE_PORT"
                                                        ^
UART <--USB-- (FT2232 channel B)                        |
                                                      xsdb
fpgatest ------------------- ssh -----------> remote_agent.py
                                                        |
                                                  vivado (detached)
```

Only JTAG traffic and a few kilobytes of JSON cross the network. The bitstream
never leaves the server that built it.

The Blackboard's FT2232H exposes **channel A as JTAG** (driven by `xvcd.py` via
libusb/pyftdi) and **channel B as a UART** (`/dev/cu.usbserial-*`), and both are
usable simultaneously.

### Files

| file | role |
|---|---|
| `fpgatest` | CLI. Config, SSH ControlMaster, dynamic reverse tunnel, build streaming, UART capture, run logs, reporting. |
| `xvcd.py` | XVC 1.0 server over FTDI MPSSE. The half that talks to real silicon. |
| `remote_agent.py` | Runs on the server. Artifact discovery + coherence, detached Vivado builds, launches `hw_server`, generates and runs the `xsdb` script. Pure stdlib; pushed over SSH on every run. |
| `fpgatest.toml` | My live config. `fpgatest.example.toml` is the annotated template. |
| `test_xvcd.py` `test_agent.py` `test_build.py` | Hardware-free tests, 145 checks, all passing (macOS and Linux). |
| `setup.sh` | Creates `.venv` (Homebrew Python is PEP 668 externally-managed). `fpgatest` re-executes itself into it. |

### Commands

```bash
./fpgatest doctor   # checks every link end to end
./fpgatest scan     # JTAG chain IDCODEs only, no server involved
./fpgatest run      # build if stale -> program -> register testbench -> visual check
python3 test_xvcd.py && python3 test_agent.py && python3 test_build.py
```

---

## What is already proven to work

Do not re-litigate any of this; it has been verified against real hardware.

1. **The cable and the JTAG chain.** `./fpgatest scan` returns
   `0x13723093 (xc7z007s)` and `0x4BA00477 (ARM DAP)`. This exercises the entire
   MPSSE path — command encoding, LSB-first bit ordering, the byte/bit/TMS
   command mix, TDO reassembly. macOS's own FTDI driver does **not** block
   libusb from claiming channel A; no `kextunload` is needed.
2. **The MPSSE implementation is bit-exact.** 400 randomised shifts at mixed TMS
   densities match a software TAP reference exactly.
3. **UART.** Captured on `/dev/cu.usbserial-8871000003061` at 115200.
4. **SSH and the reverse tunnel.** `doctor` opens a local listener, forwards it
   with `ssh -O forward -R 0:...`, and has the server connect back through
   `/dev/tcp` — real bytes cross in both directions.
5. **Remote toolchain.** `vivado`, `hw_server`, `xsdb`, `xsct` all resolve
   (Vivado 2022.2 + Vitis 2022.2).
6. **Artifact discovery and coherence.** Finds `seven_segment_wrapper.bit`
   *inside* the XSA, confirms it is byte-identical to the loose `impl_1` copy,
   parses the bitstream header (design name, part, build date).
7. **`hw_server` starts and `xsdb` connects to it.** `connect -url
   tcp:127.0.0.1:PORT` succeeds.

---

## The failure

```
program and test  (bit)
    [  ok] step connect
    [   0] info targets
    [fail] step targets  hw_server is connected but sees no JTAG targets
  ·     jtag total: 0.00 Mbit in 4 shifts, 0 kbit per shift

xvc conversation
  getinfo -> xvcServer_v1.0:262144
  closed: peer closed
  client left without shifting anything
  backed off vector_len to 32768

hw_server output
  ****** Xilinx hw_server v2022.2.0
  INFO: hw_server application started
  INFO: To connect to this hw_server instance use url: TCP:i80i9node1:41061
  Warning: Cannot create '3000:arm' GDB server: Address already in use
  Warning: Cannot create '3001:arm64' GDB server: Address already in use
  Warning: Cannot create '3002:microblaze' GDB server: Address already in use
  Warning: Cannot create '3003:microblaze64' GDB server: Address already in use
```

Read that `xvc conversation` block carefully — it is the whole case.

`hw_server` **did** reach the XVC daemon (one TCP connection). It sent exactly
one command, `getinfo:`. It received `xvcServer_v1.0:262144\n`. It then closed
the socket **without sending `settck:` and without a single `shift:`**. The
4 shifts in the telemetry are `fpgatest`'s own start-up chain scan, not
`hw_server`'s.

So the break is inside the XVC handshake, in the very first exchange.

The `Address already in use` warnings are for GDB ports 3000–3003 and mean
*another* `hw_server` is already running on this shared multi-user node. Our
instance still binds its own dynamic TCF port successfully, so this is probably
incidental — but it has not been ruled out.

---

## Hypotheses, ranked, with the evidence

### H1 — the advertised vector length is too large (most likely, untested)

`xvcd.py` advertised `262144` bytes. Xilinx's own reference `xvcServer.c`
advertises `2048`. A `hw_server` that sanity-checks or preallocates on this
value would reject it exactly as observed: read the reply, close the socket, say
nothing.

**Status: NOT YET TESTED.** A config edit meant to lower this to 2048 silently
failed to apply, so the run above still advertised 262144. `fpgatest.toml` now
correctly says `vector_bytes = 2048`.

**Test first. It is one run and it is free.**

### H2 — `hw_server` enumerates asynchronously and the query raced it

The `xsdb` script asked for `targets` immediately after `connect`. If
`hw_server` probes the XVC server at startup, closes, and only opens the real
cable later, a single immediate query would see nothing.

**Status: partly mitigated.** `remote_agent.py` now polls `targets` for 30 s and
emits `info waiting_for_targets N` each attempt. If `hw_server` reconnects during
that window the XVC trace will show a second connection. Weak evidence against:
`auto-open-servers` is documented as connecting at startup.

### H3 — protocol version

We answer `xvcServer_v1.0`. If `hw_server` 2022.2's `xilinx-xvc` transport
requires `xvcServer_v1.1` it would drop the connection right here. Cheap to test:
change the string, or answer v1.1 and support the v1.1 `getinfo` extensions.

### H4 — reply framing

We send `xvcServer_v1.0:262144\n`. Xilinx's reference sends the same shape with
a trailing newline, so this is unlikely, but the exact bytes have not been
compared against a working implementation.

### H5 — the pre-existing `hw_server` on the shared node interferes

Another instance owns ports 3000–3003. It should not affect a separate instance's
XVC cable, but it has not been excluded.

### RULED OUT

- The tunnel. `doctor` proves bytes flow both ways.
- `auto-open-servers` argument syntax. `hw_server` connected, so it parsed fine.
- The cable, the board, MPSSE, bit ordering. `scan` works on real silicon.
- `hw_server` argument parsing. Note that `-l info` is **invalid** in 2022.2 and,
  worse, `hw_server` abandons argument parsing at that point and silently ignores
  `-s`, so it listens on the default 3121. That bug is already fixed — do not
  reintroduce any `-l`/`-L` flags.

---

## Experiments, in order

1. **Run `./fpgatest run` as-is.** `vector_bytes` is now 2048 and the targets
   poll is in place. This tests H1 and H2 together; the `xvc conversation` trace
   distinguishes them (a second connection means H2 was live, more `getinfo`
   lines mean the back-off engaged).

2. **If it still closes after `getinfo`, bisect the value.** Try 2048, 1024,
   32768 by hand via `cable.vector_bytes` in `fpgatest.toml`. If *every* value
   fails, H1 is dead and the problem is the reply format or version (H3/H4).

3. **Get `hw_server` to say why.** It has TCF logging, but `-l info` is rejected.
   Find the accepted log-level tokens for 2022.2 (`hw_server -h`, or the TCF
   agent's `-L <file> -l <mode>` where mode is a bitmask or comma-separated
   names). Capture a log of a failing attach. This is the single highest-value
   piece of missing evidence.

4. **Reproduce by hand, minimally.** On the server:
   ```bash
   hw_server -s tcp::5555 -e "set auto-open-servers xilinx-xvc:127.0.0.1:<TUNNEL_PORT>"
   xsdb
   xsdb% connect -url tcp:127.0.0.1:5555
   xsdb% targets
   xsdb% help connect
   ```
   `help connect` matters: if `xsdb`'s `connect` accepts a `-xvc-url` option we
   can attach the cable explicitly and skip `auto-open-servers` entirely.

5. **Try the documented Vivado path as a cross-check.** `open_hw_target
   -xvc_url <host>:<port>` in the Vivado hardware manager is definitively
   documented for XVC. If that attaches where `hw_server -e` does not, the
   problem is `auto-open-servers`, not our daemon. This would also give a usable
   fallback for bitstream loading (though `ps7_init` and the register testbench
   still need `xsdb`).

6. **Compare against a known-good XVC server.** `xvcd` implementations exist that
   are known to work with `hw_server` (BerkeleyLab/XVC-FTDI-JTAG, tmbinc/xvcd).
   Diff their `getinfo` reply and connection handling against `xvcd.py`. Do not
   adopt one wholesale — the point is to find the one byte we get wrong.

---

## Constraints

- **Shared multi-user server**, no root. All ports must be dynamic; nothing may
  hard-code 3121 or 2542. Another `hw_server` is already running.
- **Apple Silicon Mac.** No Xilinx tools locally, ever. Homebrew Python is
  PEP 668 externally managed; dependencies live in `.venv` and `fpgatest`
  re-executes itself into it.
- The board is **JP2 = JTAG boot**. The BootROM runs no FSBL, so `FCLK_CLK0` is
  dead until `ps7_init`. A bitstream loaded without it programs cleanly and then
  does nothing — this is why `--mode bit` exists and why `--mode pl-only` is a
  trap for PS-clocked designs.
- Vivado 2022.2 + Vitis 2022.2. `xsdb`/`hw_server` come from Vitis.

---

## House rules

- **Prove it, do not assume it.** Every past bug here was a silent success:
  `doctor` reporting a pass because a malformed shell command produced no output;
  a run reporting `0 failed` because the `xsdb` script died before programming
  anything. When adding a check, make the failure mode loud.
- **Add telemetry before changing behaviour.** The `xvc conversation` trace is
  what turned this from "it hangs" into a one-line diagnosis. Extend it.
- **Keep the tests passing.** `test_xvcd.py`, `test_agent.py`, `test_build.py`
  run with no hardware and no server — they fake the FTDI, model the JTAG TAP,
  and put a fake `vivado` on `PATH`. Add a regression test for whatever this
  turns out to be.
- Comments explain *why*, especially where a non-obvious workaround exists.
  Several already document exactly this kind of trap; match that style.
- Do not reintroduce: `hw_server -l/-L` flags; `json.dumps` for shell quoting
  (use `shlex.quote`, and `rquote` for paths with a leading `~`); an
  `realpath`-based venv check (a venv's `bin/python3` is a symlink to the base
  interpreter, so it always compares equal).

---

## Success criteria

`./fpgatest run` gets past `targets`, shows JTAG traffic climbing in the progress
lines, loads the bitstream, runs `ps7_init`, executes the AXI-Lite register
testbench with the CPU halted, and asks me whether the seven-segment display
reads 9801.
