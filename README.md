# fpgatest

Test a Xilinx FPGA board attached to your machine, using Vivado and Vitis installed on another one.

One command builds the design remotely if it is out of date, gets it onto the board — over JTAG, or onto a board that boots itself from SD — and runs your tests against the hardware.

```
$ fpgatest run

local cable
  ok    0x13723093 (xc7z007s)
  ok    0x4BA00477 (ARM DAP (Cortex-A9))
  ok    UART capture on /dev/cu.usbserial-XXXXXXXX @ 115200

remote: user@buildhost
  ok    reverse tunnel: buildhost:41097 -> localhost:52883
  ok    agent synced to ~/.cache/fpgatest/remote_agent.py

resources
  ·     LUT      2942 / 14400    20.43%
  ·     FF       2844 / 28800     9.88%
  ·     DSP        56 / 66       84.85%
  ok    WNS +8.263 ns

program and test  (bit)
  ·     jtag 12.13 Mbit in 1733 shifts (7 kbit each), 2196 kbit/s
  ok    program_pl
  ok    ps_init

Five pragma builds, one seed -- the checksums must not differ
  --- baseline
  ok    baseline  0x00006328
        this variant, after place and route:
        LUT      323 / 14400    2.24%
        DSP       16 / 66      24.24%
        latency 11 cycles  (0.110 us)   [HLS estimate: the fabric has no cycle counter]
  --- PIPELINE
  ok    PIPELINE  0x00006328
        this variant, after place and route:
        LUT      104 / 14400    0.72%
        DSP        4 / 66       6.06%
        latency 21 cycles  (0.210 us)   [HLS estimate: the fabric has no cycle counter]

PASS  11 passed, 0 failed
```

Only JTAG traffic and a few kilobytes of JSON cross the network. The bitstream never leaves the machine that built it.

![fpgatest demo](demo/fpgatest.mp4)

## Transport

FPGA toolchains are large, licensed and Linux-only, and the board is rarely attached to the machine that has them. Two obvious ways to bridge that gap disappoint: USB/IP forwards raw USB transfers, so every JTAG shift pays a network round trip; a Linux VM with USB passthrough works but adds x86 emulation on ARM hosts.

XVC (Xilinx Virtual Cable) is the mechanism AMD provides for this. A userspace daemon exposes the JTAG cable over TCP and `hw_server` treats it as a local cable. `xvcd.py` is that daemon — MPSSE over `pyftdi`, pure Python, no kernel driver and nothing to compile.

```
your machine                                 build machine
------------                                 -------------
board --USB--> xvcd.py :52104
                 ^
                 |  ssh -R 41097:127.0.0.1:52104
                 +-------------- hw_server -e "...xilinx-xvc:127.0.0.1:41097"
                                           ^
UART <--USB-- (FTDI ch. B)                 |
                                          xsdb
fpgatest ------------- ssh -----------> remote_agent.py
                                           |
                                     vivado (detached)
```

## Requirements

**Cable.** Any FTDI MPSSE JTAG interface (FT2232H and relatives). The default pin map is the usual one: `AD0` TCK, `AD1` TDI, `AD2` TDO, `AD3` TMS. If a second channel is exposed as a UART, it is captured concurrently.

**Device.** Anything `hw_server` can program. The PS-initialisation step is Zynq-7000 specific — it looks for `ps7_init.tcl` inside the XSA — so MPSoC and Versal are not supported by the `jtag` target as it stands. The `linux` target does not care: the board configures its own fabric.

**Build machine.** SSH access, and a Vivado whose `settings64.sh` puts `vivado`, `xsdb` and `hw_server` on `PATH`. `xsdb` and `hw_server` ship with Vitis, not with Vivado alone.

**Local machine.** Python 3.11+ and `libusb`. No Xilinx tool runs locally.

## Installation

```bash
./setup.sh
ln -s "$PWD/fpgatest" /usr/local/bin/fpgatest
```

`setup.sh` creates a `.venv` and `fpgatest` re-executes itself into it, so there is nothing to activate. This matters where the system Python is externally managed (PEP 668) and `pip install` is refused.

Nothing is installed on the build machine. The agent is stdlib-only Python, pushed over the existing SSH connection on every run.

## Commands

```bash
fpgatest doctor     # check every link: cable, chain, UART, ssh, remote tools
fpgatest scan       # read the JTAG chain IDCODEs, nothing else involved
fpgatest sdcard     # read the board's SD card through its own console
fpgatest build      # build only, if out of date
fpgatest run        # build if stale, program, test
```

Start with `doctor`. It is the only command that reports *which* link is broken rather than that something is.

```bash
fpgatest run --no-build        # never build; fail if stale
fpgatest run --build           # rebuild even if it looks current
fpgatest run --force           # program even if sources are newer
fpgatest run --mode pl-only|bit|full
fpgatest run --only "name"     # a single test, matched exactly
fpgatest run --no-confirm      # skip interactive prompts
fpgatest run --emit-tcl        # print the generated xsdb script
fpgatest run --show-uart
fpgatest run --port /dev/cu.x  # serial console (linux target)
fpgatest run --command "..."   # override target.command (linux target)
fpgatest build --force
```

`sdcard` is for the case where the board boots from SD and will not come up. It
catches u-boot before it hands off and lists both partitions (`fatls`,
`ext4ls`), which works even when Linux does not — and "the card was written
wrongly" is the most likely reason it does not. If the board is already at a
Linux prompt it logs in and lists `/boot` instead.

## Configuration

`fpgatest.toml` is looked up in the working directory, then `~/.config/fpgatest/`. `fpgatest.example.toml` is an annotated template.

| section | key | meaning |
|---|---|---|
| `remote` | `host` | SSH destination. Aliases from `~/.ssh/config`, `ProxyJump` and friends all work |
| | `project` | remote directory searched for `.xpr` / `.bit` / `.xsa` / `.elf` |
| | `setup` | shell fragment sourced before every remote command |
| | `python` | interpreter used for the agent (default `python3`) |
| `build` | `enabled` | build automatically when artifacts are stale |
| | `xpr` | `""` autodetects the newest `.xpr` |
| | `tcl_script` | non-project flow: run this Tcl instead of managing runs |
| | `jobs` | `0` uses every core |
| | `simulation` | run `launch_simulation` before synthesis |
| | `simulation_fatal` | stop the build if simulation fails |
| | `export_xsa` | re-export the XSA to this path after implementation |
| | `poll_s` | build status poll interval |
| `cable` | `url` | pyftdi URL, e.g. `ftdi://ftdi:2232h/1` |
| | `frequency_hz` | TCK frequency |
| | `vector_bytes` | max shift advertised in the XVC handshake |
| `uart` | `enabled`, `baud`, `port`, `settle_s` | `port = ""` autodetects |
| `hw` | `base` | default base address for register tests |
| `hls` | `report` | Vitis HLS C-synthesis report, or a directory holding one |
| `target` | `kind` | `jtag` (default) or `linux` — see [Targets](#targets) |
| | `prompt`, `login`, `password` | serial console credentials, `linux` only |
| | `command`, `run_timeout_s` | what to run on the board, `linux` only |
| | `boot_timeout_s` | how long to wait for a shell |
| `run` | `mode` | `auto`, `pl-only`, `bit` or `full` |
| | `settle_ms` | pause after starting an application |
| | `timeout_s` | overall programming timeout |
| `log` | `dir`, `keep` | one timestamped directory per run |

Three list sections order the run: `[[setup]]` prepares the board, `[[preflight]]`
verifies it, `[[deploy]]` copies files onto it. All three are `linux`-only;
`[[test]]` applies to both targets.

### Minimal

Enough to program a board and run register tests.

```toml
[remote]
host    = "user@buildhost"
project = "~/projects/blinky"
setup   = "source /opt/Xilinx/Vitis/2022.2/settings64.sh"

[hw]
base = "0x40000000"

[run]
mode = "bit"
```

### Project flow, re-exporting the XSA after every build

```toml
[remote]
host    = "builder"
project = "~/projects/accel"
setup   = """
source /opt/Xilinx/Vivado/2022.2/settings64.sh
source /opt/Xilinx/Vitis/2022.2/settings64.sh
"""

[build]
enabled    = true
xpr        = ""                        # newest .xpr under the project
jobs       = 8
export_xsa = "~/projects/accel/accel.xsa"
```

### Non-project Tcl flow

When the build is driven by a script rather than a `.xpr`. Staleness then falls back to comparing source timestamps against the bitstream.

```toml
[build]
enabled    = true
tcl_script = "scripts/build.tcl"
```

### Simulation as a build gate

```toml
[build]
simulation       = true
simulation_fatal = true                # a failing testbench stops the build
```

### Headless, no UART

The second FTDI channel is not always wired to anything, and on some hosts claiming it interferes with the JTAG channel.

```toml
[uart]
enabled = false
```

### A slow or marginal cable

Long ribbon cables, level shifters and unpowered hubs all show up as an unreliable chain scan. Lower the clock first.

```toml
[cable]
url          = "ftdi://ftdi:2232h/1"
frequency_hz = 5_000_000
vector_bytes = 2048
```

`vector_bytes` is the maximum shift size advertised during the XVC handshake; larger values mean fewer network round trips per bitstream. 2048 is what Xilinx's own reference server advertises. Raise it and watch the `kbit per shift` figure in the progress lines — if the cable still attaches and the number climbs, keep it.

### Selecting a specific cable

With more than one FTDI device attached, address one explicitly. `python3 -m pyftdi.ftdi` lists what is connected.

```toml
[cable]
url = "ftdi://ftdi:2232h:FT4XYZ12/1"   # serial number, channel 1 (A)
```

### Running an application ELF

```toml
[run]
mode      = "full"                     # program, ps7_init, download ELF, run it
settle_ms = 2000                       # wait this long before post_elf tests
timeout_s = 900
```

## Targets

What separates the two targets is not the toolchain that produced the artifacts.
It is who configures the fabric and who runs the code.

| `target.kind` | configures the PL | runs the code |
|---|---|---|
| `jtag` (default) | fpgatest, over JTAG | the host, register by register, CPU halted |
| `linux` | the board itself — u-boot, or XRT from an xclbin | the board, driven over its serial console |

A `linux` run touches no JTAG at all. By the time it starts, the PL is already
configured; the console is the entire interface. It goes: wait for a shell, run
`[[setup]]`, check `[[preflight]]`, copy `[[deploy]]` files, run
`target.command`, then evaluate `[[test]]` against what was printed.

```toml
[target]
kind           = "linux"
prompt         = "root@[^ ]*[#$] "
login          = "root"
boot_timeout_s = 180
command        = "/home/root/matmul"

[[setup]]
name    = "mount the FAT boot partition at /boot"
command = "mountpoint -q /boot || mount -t vfat /dev/mmcblk0p1 /boot"

[[preflight]]
name    = "kernel is the one the platform was built for"
command = "uname -r"
expect  = "5\\.15\\.36-xilinx"

[[preflight]]
name    = "accelerator is where the application looks for it"
command = "test -f /boot/binary_container_1.xclbin"

[[deploy]]
local  = "~/.cache/fpgatest/artifacts/matmul"
remote = "/home/root/matmul"
chmod  = "755"
```

Setup acts, preflight verifies, tests measure. Keeping the acting separate means
a check can never quietly repair the thing it is reporting on, so a wrong kernel
or a missing accelerator is reported as a broken environment rather than as a
pile of failed measurements. Setup commands must be idempotent — a run is
allowed to start from an already-prepared board.

Preflight checks accept `expect` / `reject` regexes and an `exit_code`. A
pattern that matches in the output of a command that itself failed does not
pass: it proves nothing about the environment.

Deployment is over the console, in acknowledged base64 chunks, because a serial
line has no flow control and streaming megabytes blind overruns the UART FIFO
with no indication. Each file is hashed first and skipped when the board already
has it, so leaving a large artifact in `[[deploy]]` costs nothing after the
first run.

## Boot mode

On a board configured to boot from JTAG there is no boot image and no FSBL, so the BootROM leaves the PS uninitialised — including `FCLK_CLK0`, which on a Zynq block design clocks the PL. A bitstream loaded with nothing else done reports DONE and then does nothing, which is indistinguishable from a broken design.

`mode` selects how much is done beyond configuration:

| mode | behaviour |
|---|---|
| `pl-only` | `fpga -file` and nothing else. Correct only when the PL is clocked by an on-board oscillator |
| `bit` | program the PL and run `ps7_init`, bringing up FCLK and the PS-PL interfaces. No application |
| `full` | as `bit`, then download and start an application ELF |
| `auto` | `full` when an application ELF is found, otherwise `bit` |

Register tests are skipped in `pl-only`, since nothing is there to drive AXI.

## Register map

Base address and offsets are both recorded in the XSA, which is an ordinary zip file. Read them rather than guessing.

```bash
python3 -c "
import zipfile, sys
z = zipfile.ZipFile(sys.argv[1])
print('\n'.join(z.namelist()))
" mydesign.xsa
```

For IP generated by Vitis HLS, the driver header is authoritative about every offset and every field width:

```bash
python3 -c "
import zipfile, sys
z = zipfile.ZipFile(sys.argv[1])
for n in z.namelist():
    if n.endswith('_hw.h'):
        print(z.read(n).decode())
" mydesign.xsa
```

```
0x10 : Data signal of op1      bit 6~0  (Read/Write)
0x18 : Data signal of op2      bit 6~0  (Read/Write)
0x20 : Data signal of op_sel   bit 1~0  (Read/Write)
```

Two things there are easy to miss. `bit 6~0` means a 7-bit field, so anything above 127 is truncated on write. And if the map stops before your return value, that value is not on the bus at all — reading its expected address yields 0, which means "nothing is mapped here", not "the computation is wrong".

The base address is in the `.hwh` in the same archive, under `BASEVALUE`. Getting it wrong is silent: writes to unmapped addresses are accepted without complaint and nothing ever changes.

## Tests

Register tests run with the CPU halted immediately after PS initialisation and `force-mem-access` enabled, so the host drives AXI directly. No application is involved, which means a failure implicates the PL and nothing else.

| operation | meaning |
|---|---|
| `{ write = <offset>, value = <n> }` | write `n` to `base + offset` |
| `{ read = <offset>, expect = <n> }` | read back and compare |
| `{ delay_ms = <n> }` | pause |

| test key | meaning |
|---|---|
| `name` | required; also what `--only` matches |
| `ops` | register operations, in order |
| `base` | override `[hw].base` for this test |
| `stage` | `pre_elf` (default) or `post_elf` |
| `confirm` | ask the operator a question instead of reading registers |
| `uart_expect` / `uart_reject` | regex matched against captured UART |
| `uart_extract` + `metric` | capture a number out of the UART and name it |
| `compare` | assert a ratio between two named metrics |
| `utilization` / `timing` | resource and slack budgets from the build reports |
| `hls` | print this cell's own resources and latency under the result |
| `note` | free text printed under the result |
| `banner` | a heading; asserts nothing, counts for nothing |
| `timeout_s` | how long to wait for a UART match |

Values are plain decimal: `value = 99` is ninety-nine. `fpgatest` emits explicit hex literals internally, because `xsdb`'s `mwr` reads bare numbers as hex while `mrd` answers in decimal.

### Writing and reading back

```toml
[[test]]
name = "control register is writable"
ops = [
  { write = 0x00, value = 1 },
  { read  = 0x00, expect = 1 },
]
```

### Several registers at once

```toml
[[test]]
name = "operands survive each other"
ops = [
  { write = 0x10, value = 99 },
  { write = 0x18, value = 42 },
  { write = 0x20, value = 2 },
  { delay_ms = 5 },
  { read = 0x10, expect = 99 },
  { read = 0x18, expect = 42 },
  { read = 0x20, expect = 2 },
]
```

### A computation with a readable result

```toml
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

Write this only if `0x28` exists in your register map. If it does not, the read returns 0 and the test proves nothing.

### Field widths and boundaries

```toml
[[test]]
name = "7-bit field accepts its maximum"
ops = [{ write = 0x10, value = 127 }, { read = 0x10, expect = 127 }]

[[test]]
name = "one past the top truncates"
ops = [{ write = 0x10, value = 128 }, { read = 0x10, expect = 0 }]

[[test]]
name = "2-bit field holds 3"
ops = [{ write = 0x20, value = 3 }, { read = 0x20, expect = 3 }]

[[test]]
name = "reserved bits read back as zero"
ops = [{ write = 0x20, value = 255 }, { read = 0x20, expect = 3 }]
```

### Walking a bit pattern

Catches a swapped or stuck data line, which fixed test values often miss.

```toml
[[test]]
name = "walking ones through the data register"
ops = [
  { write = 0x10, value = 1 },  { read = 0x10, expect = 1 },
  { write = 0x10, value = 2 },  { read = 0x10, expect = 2 },
  { write = 0x10, value = 4 },  { read = 0x10, expect = 4 },
  { write = 0x10, value = 8 },  { read = 0x10, expect = 8 },
  { write = 0x10, value = 16 }, { read = 0x10, expect = 16 },
  { write = 0x10, value = 32 }, { read = 0x10, expect = 32 },
  { write = 0x10, value = 64 }, { read = 0x10, expect = 64 },
]
```

### Reset and read-only registers

```toml
[[test]]
name = "registers come up zeroed"
ops = [
  { read = 0x10, expect = 0 },
  { read = 0x18, expect = 0 },
  { read = 0x20, expect = 0 },
]

[[test]]
name = "IP identification register"
ops = [{ read = 0x0C, expect = 0x1234ABCD }]

[[test]]
name = "a read-only register ignores writes"
ops = [
  { write = 0x0C, value = 0 },
  { read  = 0x0C, expect = 0x1234ABCD },
]
```

### Latency

Separate the write from the read to find out how long a result actually takes.

```toml
[[test]]
name = "result is ready within 1 ms"
ops = [
  { write = 0x10, value = 50 },
  { write = 0x18, value = 50 },
  { write = 0x20, value = 2 },
  { delay_ms = 1 },
  { read = 0x28, expect = 2500 },
]

[[test]]
name = "result is stable 500 ms later"
ops = [
  { delay_ms = 500 },
  { read = 0x28, expect = 2500 },
]
```

### Watching a sequence at human speed

Writes normally land within microseconds of each other, so anything they drive appears to change in a single step. Delays stretch the sequence out. This is the practical way to work out an undocumented mode encoding: put the peripheral in a mode where each input is visible on its own, then change one register at a time and watch.

```toml
[[test]]
name = "watch each operand arrive"
ops = [
  { write = 0x20, value = 0 },
  { write = 0x10, value = 0 },
  { write = 0x18, value = 0 },
  { delay_ms = 2000 },
  { write = 0x10, value = 99 },
  { delay_ms = 2500 },
  { write = 0x18, value = 99 },
  { delay_ms = 2500 },
  { write = 0x20, value = 2 },
  { delay_ms = 2500 },
  { read = 0x10, expect = 99 },
]
```

### More than one peripheral

```toml
[[test]]
name = "GPIO block responds"
base = "0x41200000"
ops  = [{ write = 0x00, value = 170 }, { read = 0x00, expect = 170 }]

[[test]]
name = "timer block responds"
base = "0x42800000"
ops  = [{ read = 0x00, expect = 0 }]
```

### Before and after the application runs

```toml
[[test]]
name  = "host seeds the operands"
ops   = [{ write = 0x10, value = 7 }, { write = 0x18, value = 6 }]

[[test]]
name  = "application consumed them"
stage = "post_elf"
ops   = [{ read = 0x28, expect = 42 }]
```

### Operator confirmation

The only way to test what happens downstream of a readable register.

```toml
[[test]]
name    = "display reads 9801"
confirm = "Does the display read 9801?"

[[test]]
name    = "display is steady"
confirm = "Is the display steady rather than flickering?"

[[test]]
name    = "LED pattern matches"
confirm = "Are LEDs 1, 3, 5 and 7 lit and no others?"

[[test]]
name    = "button is wired through"
confirm = "Press BTN0 -- does the rightmost digit change?"
```

Pick values whose failure mode is unambiguous. If a wiring fault mirrors seven-segment digits, 9801 becomes 6801 and 25 becomes 52; a register read-back passes in both cases and only the operator catches it.

Prompts need a terminal. `--no-confirm` skips them and records them as skipped.

### UART assertions

Regexes matched against everything captured from the FTDI's second channel.

```toml
[[test]]
name        = "boot banner"
uart_expect = "Application ready"
timeout_s   = 15

[[test]]
name        = "self-test passes"
uart_expect = "POST: 12/12 ok"
timeout_s   = 30

[[test]]
name        = "no processor exception"
uart_reject = "Data Abort|Prefetch Abort|Undefined Instruction"
timeout_s   = 5

[[test]]
name        = "nothing complains"
uart_reject = "(?i)error|fail|assert"
```

### Resources and latency, per variant

Point `[hls].report` at the C-synthesis report and give a test an `hls` key
naming a cell. The figures are printed under that test's result, in the same
shape as the whole-design `resources` block so the two read against each other.

```toml
[hls]
report = "~/vitis/lab3/dense_axi_prj/solution1/syn/report/dense_axi_csynth.rpt"

[[test]]
banner = "Five pragma builds, one seed -- the checksums must not differ"

[[test]]
name = "ARRAY_PARTITION alone -- must match baseline"
ops  = [ { write = 0x18, value = 3 }, { write = 0x00, value = 1 },
         { delay_ms = 5 }, { read = 0x20, expect = 0x00006328 } ]
hls  = "LOOP_52_1"
```

```
  ok    ARRAY_PARTITION alone -- must match baseline  0x00006328
        this variant, after place and route:
        LUT      384 / 14400    2.67%
        DSP       16 / 66      24.24%
        latency 11 cycles  (0.110 us)   [HLS estimate: the fabric has no cycle counter]
```

**Resources are measured, latency is not**, and the output says so rather than
letting the two blur together.

LUT/FF/BRAM/DSP come from `report_utilization -hierarchical` on the routed
checkpoint — what the device actually holds, per cell, after place and route.
Vivado does not emit that breakdown during a normal implementation run, so the
agent produces it from the checkpoint and caches it under
`~/.cache/fpgatest/reports/`, regenerating only when the checkpoint is newer.
The first run pays a couple of minutes; later runs are free.

Latency cannot be measured this way. Nothing in the fabric counts its own
cycles, so unless the design carries a counter, the cycle figure is the
synthesis estimate and is labelled as one. To make it real, expose a counter
that runs between `ap_start` and `ap_done` as an AXI register and read it with
an ordinary `{ read = ... }`.

The gap between the two is worth seeing. On one 4×4 dense layer, C synthesis
estimated 8 DSP and 1335 LUT for a variant that place-and-route actually built
with **16 DSP and 323 LUT** — twice the DSPs and a quarter of the LUTs. An
estimate is not a measurement, and a report that mixes them silently is worse
than either.

Cell names in the report come from source line numbers
(`..._Pipeline_VITIS_LOOP_52_1` is the loop on line 52), so `hls` takes any
substring. A name matching nothing, or matching several cells, prints that
loudly instead of leaving a blank where a number should be.

### Measuring an accelerator against what it replaced

`uart_extract` pulls a number out of whatever the board printed; `compare`
asserts a relationship between two of them. Useful when the design times itself
and prints the result — an accelerator is only interesting relative to the
software it replaced.

```toml
[[test]]
name         = "software baseline"
uart_extract = "Unaccelerated software ran for ([0-9.]+) ms"
metric       = "sw_ms"

[[test]]
name         = "hardware accelerated"
uart_extract = "Accelerated software ran for ([0-9.]+) ms"
metric       = "hw_ms"

[[test]]
name    = "acceleration actually helps"
compare = { metric = "sw_ms", over = "hw_ms", min_ratio = 2.0 }
```

`max`/`min` bound a single metric; `min_ratio`/`max_ratio` bound the quotient.
The last match in the capture buffer wins, so a measurement printed again by a
later run is not confused with a stale one from an earlier boot. Naming a metric
that was never captured fails loudly — a comparison with nothing to compare is a
configuration error, not a pass.

### Resource budgets

```toml
[[test]]
name        = "fits the device with room to spare"
utilization = { lut_pct_max = 80, dsp_max = 60 }
timing      = { wns_min = 0.0 }
```

Checked against the implementation reports. A budget naming a resource the
reports do not contain fails rather than passing quietly, because that almost
always means the reports were never generated.

### Running one test

`--only` matches the name exactly. A name that matches nothing is refused and the available names are listed, rather than quietly running zero tests.

```bash
fpgatest run --only "walking ones through the data register"
```

## Build

Vivado runs detached on the build machine, in its own session and process group with output to a file. A dropped SSH connection does not kill it, and neither does Ctrl-C here — `fpgatest build` reattaches to a run already in flight instead of starting a second Vivado on the same project.

For the project flow, staleness is decided by Vivado's own `NEEDS_REFRESH` and `PROGRESS` properties on `synth_1` and `impl_1` — the same ones behind the out-of-date indicator in the GUI. An up-to-date project is never re-synthesised. Implementation is forced anyway if the bitstream has gone missing from a run still claiming to be complete.

If synthesis or implementation fails the build stops, the reason is printed, the exit status is non-zero and nothing is programmed. The whole `vivado.log` is copied back before the run directory is closed, because the next build overwrites it.

## Artifact coherence

The expensive failure is a bitstream that does not match the hardware handoff the application was compiled against: right software, wrong hardware, silent misbehaviour. Before anything is loaded, `fpgatest`

1. extracts the bitstream from inside the XSA and prefers it, since the XSA is what generated `xparameters.h` and `ps7_init` — this makes a mismatch structurally impossible rather than merely detected;
2. hashes the loose `impl_1/*.bit` and compares the two;
3. flags source files newer than the bitstream;
4. flags an application ELF older than the XSA.

Any of these triggers a build. If one survives the build, the run stops rather than programming something you did not mean to test. `--force` overrides.

## Logs

Each run writes a timestamped directory on the local machine.

```
~/.fpgatest/logs/<timestamp>-<project>/
    build.log        streamed live while Vivado ran
    vivado.log       copied back before it could be overwritten
    build.tcl        exactly what was executed
    manifest.json    which .bit/.xsa/.elf were chosen, and why
    program.tcl      the generated xsdb script
    xsdb.log         everything xsdb printed
    hw_server.log    including the jtagpoll trace
    uart.log         everything the board said
    reports.json     utilization, timing and per-cell figures
    metrics.json     numbers extracted from the UART
    run.json         the whole result, machine-readable
~/.fpgatest/logs/latest -> the most recent run
```

Pruned to `log.keep`, default 50.

## Troubleshooting

Most of these present as a silent success — something reports OK while achieving nothing.

| Symptom | Cause |
|---|---|
| Everything reports OK, hardware never changes | Wrong base address. Writes to unmapped addresses are accepted without complaint |
| A register reads 0 whatever you write | Nothing is mapped at that offset. Check the driver header; the value may not be exposed on the bus |
| Read value is off by an odd factor | `mrd` answers in decimal, `mwr` reads bare numbers as hex. 99 comes back as `0x99` = 153 |
| `invalid command name "ps7_init"` | `loadhw` does not define it; `ps7_init.tcl` inside the XSA has to be sourced. 2022.2 has no `loadhw -regs` |
| `Cannot write memory if not stopped` | Halt the CPU with `stop` before touching PS registers or AXI |
| `MMU section translation fault` on every access | `SCTLR.M` survives `stop`, so a core that ran anything earlier translates every physical address. `fpgatest` issues `rst -processor` before halting, which clears it without dropping the DAP |
| `N source file(s) newer than the bitstream` after a run that worked | `loadhw` unpacks the XSA into the project directory, so a successful run leaves `ps7_init.*` and `drivers/` looking freshly edited and blocks the next one. Excluded from the staleness scan since; `--force` is the escape on older versions |
| CPU debug targets disappear from `targets` | A system reset over XVC times out and leaves the DAP wedged. Only a board power cycle recovers it, which is why `fpgatest` never issues `rst -system` |
| `FTDI read timeout -- cable wedged?` | The FTDI default 16 ms latency timer starves short transactions; `xvcd.py` lowers it to 1 ms |
| `hw_server` reports zero JTAG targets | Verify the tunnel with `doctor` first. `hw_server -l` takes named tokens (`jtag`, `jtag2`, `protocol`), never a severity — an invalid one makes it abandon argument parsing, ignore `-s` and listen on the default port |
| Ports 3000–3003 already in use | Leftover `hw_server` processes from earlier runs |
| PL programs but the design does nothing | JTAG boot leaves FCLK dead. Use `bit`, not `pl-only` |
| Chain scan is intermittent | Lower `cable.frequency_hz` |
| libusb cannot claim the interface | A kernel or system FTDI driver holds it. `doctor` reports this and suggests the platform-specific unbind |

## Test suites

Three suites run with no hardware and no build machine.

```bash
python3 test_xvcd.py     # MPSSE and XVC bit plumbing
python3 test_agent.py    # discovery, coherence, generated xsdb script
python3 test_build.py    # detached build lifecycle
```

`test_xvcd.py` drives a software JTAG TAP model through a fake FTDI that interprets the MPSSE opcodes, checks `scan_chain` recovers exact IDCODEs at awkward shift lengths (1, 7, 9, 17, 31, 33, 100 bits), verifies 400 randomised shifts are bit-exact against a reference implementation, and covers the empty-read retry path.

`test_agent.py` builds synthetic project trees and checks that discovery picks the right files, catches a drifted bitstream and stale sources, ignores an FSBL, and emits a correctly ordered `xsdb` script. It also pins the two bugs that made a successful run poison the next one: XSA extraction byproducts must not count as stale, and the core must be reset before it is halted.

`test_build.py` puts a fake `vivado` on `PATH` and exercises detached launch, log streaming without gaps or duplication, reattaching to a running build, and the programming modes. It also drives a fake Linux board and a fake u-boot over a fake serial port, covering login, command exit status, chunked file transfer with digest verification, report parsing and the metric comparisons. A few of its checks invoke `fpgatest scan` expecting no cable, so they fail while a board is attached.

## Files

| | |
|---|---|
| `fpgatest` | CLI: config, SSH control master, reverse tunnel, build streaming, UART capture, run logs |
| `xvcd.py` | XVC 1.0 server over FTDI MPSSE; also runnable standalone |
| `remote_agent.py` | Build-machine side: discovery, coherence, detached Vivado, report parsing, `hw_server` and `xsdb` |
| `fpgatest.toml` | Configuration and testbench |
| `fpgatest.example.toml` | Annotated template |
| `setup.sh` | Virtualenv bootstrap |

`xvcd.py` stands alone if all you want is the cable exposed over TCP:

```bash
python3 xvcd.py --port 2542
python3 xvcd.py --scan
```
