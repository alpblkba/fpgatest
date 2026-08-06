"""
Hardware-free correctness test for the MPSSE/XVC bit plumbing in xvcd.py.

A fake FTDI device interprets the MPSSE opcodes we emit and drives a software
model of a JTAG TAP controller holding a two-device IDCODE chain. If our command
encoding, bit ordering or TDO reassembly is wrong, scan_chain() will not come
back with the expected IDCODEs.

Run: python3 test_xvcd.py
"""

import struct
import sys
import threading

import xvcd

# --------------------------------------------------------------------- TAP model

NEXT = {
    "TLR":  ("RTI",  "TLR"),
    "RTI":  ("RTI",  "SELDR"),
    "SELDR": ("CAPDR", "SELIR"),
    "CAPDR": ("SHDR", "E1DR"),
    "SHDR": ("SHDR", "E1DR"),
    "E1DR": ("PDR",  "UPDR"),
    "PDR":  ("PDR",  "E2DR"),
    "E2DR": ("SHDR", "UPDR"),
    "UPDR": ("RTI",  "SELDR"),
    "SELIR": ("CAPIR", "TLR"),
    "CAPIR": ("SHIR", "E1IR"),
    "SHIR": ("SHIR", "E1IR"),
    "E1IR": ("PIR",  "UPIR"),
    "PIR":  ("PIR",  "E2IR"),
    "E2IR": ("SHIR", "UPIR"),
    "UPIR": ("RTI",  "SELDR"),
}


class TapChain:
    """IDCODE-mode JTAG chain. idcodes[0] is the device closest to TDO."""

    def __init__(self, idcodes):
        self.idcodes = idcodes
        self.len = 32 * len(idcodes)
        self.state = "TLR"
        self.dr = 0

    def _load(self):
        self.dr = 0
        for i, code in enumerate(self.idcodes):
            self.dr |= (code & 0xFFFFFFFF) << (32 * i)

    def clock(self, tms, tdi):
        tdo = self.dr & 1 if self.state == "SHDR" else 0
        if self.state == "SHDR":
            self.dr = (self.dr >> 1) | ((tdi & 1) << (self.len - 1))
        self.state = NEXT[self.state][tms & 1]
        if self.state in ("CAPDR", "TLR"):
            self._load()
        return tdo


# -------------------------------------------------------------- fake FTDI device

class FakeFtdi:
    frequency = 10e6

    def __init__(self, chain, empty_reads=0):
        self.chain = chain
        self.out = bytearray()
        self.empty_reads = empty_reads
        self.latency = None

    def write_data(self, buf):
        i = 0
        while i < len(buf):
            op = buf[i]
            if op in (0x8A, 0x97, 0x8D, 0x85, 0x8B):
                i += 1
            elif op == xvcd.CMD_BYTES_RW:
                n = struct.unpack_from("<H", buf, i + 1)[0] + 1
                data = buf[i + 3:i + 3 + n]
                for b in data:
                    got = 0
                    for k in range(8):
                        got |= self.chain.clock(0, (b >> k) & 1) << k
                    self.out.append(got)
                i += 3 + n
            elif op == xvcd.CMD_BITS_RW:
                n = buf[i + 1] + 1
                b = buf[i + 2]
                got = 0
                for k in range(n):
                    got |= self.chain.clock(0, (b >> k) & 1) << k
                # Hardware returns bits right-aligned against bit 7.
                self.out.append((got << (8 - n)) & 0xFF)
                i += 3
            elif op == xvcd.CMD_TMS_RW:
                n = buf[i + 1] + 1
                b = buf[i + 2]
                tdi = (b >> 7) & 1
                got = 0
                for k in range(n):
                    got |= self.chain.clock((b >> k) & 1, tdi) << k
                self.out.append((got << (8 - n)) & 0xFF)
                i += 3
            else:
                raise AssertionError(f"unexpected MPSSE opcode 0x{op:02X} at {i}")
        return len(buf)

    def read_data_bytes(self, n, attempt=1):
        # Real hardware returns nothing until it has data or its latency timer
        # fires. empty_reads makes the fake do the same so the retry path is
        # exercised rather than assumed.
        if self.empty_reads > 0:
            self.empty_reads -= 1
            return b""
        chunk = bytes(self.out[:n])
        del self.out[:n]
        return chunk

    def set_latency_timer(self, ms):
        self.latency = ms

    def close(self):
        pass


def make_cable(idcodes, empty_reads=0):
    cable = xvcd.FtdiJtag.__new__(xvcd.FtdiJtag)
    cable._ftdi = FakeFtdi(TapChain(idcodes), empty_reads)
    cable.frequency = 10e6
    cable._wbuf = bytearray()
    cable._pending = []
    cable._read_len = 0
    cable._lock = threading.Lock()
    cable.stats = {"shifts": 0, "bits": 0, "started": None, "last": None,
                   "connections": 0, "getinfo": 0, "settck": 0,
                   "trace": [], "last_error": None}
    return cable


# ------------------------------------------------------------------------ checks

def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  -- ' + detail if detail and not cond else ''}")
    return cond


def main():
    ok = True
    print("scan_chain, Blackboard-like chain (ARM DAP + xc7z007s):")
    expected = [0x4BA00477, 0x03723093]
    got = make_cable(expected).scan_chain()
    ok &= check("IDCODEs recovered", got == expected,
                f"expected {[hex(x) for x in expected]}, got {[hex(x) for x in got]}")
    ok &= check("idcode is decoded to a part name",
                "xc7z007s" in xvcd.describe_idcode(0x03723093))

    print("single-device chain:")
    got = make_cable([0x03727093]).scan_chain()
    ok &= check("IDCODE recovered", got == [0x03727093], str([hex(x) for x in got]))

    print("frequency resolution across pyftdi versions:")

    class _Old:      frequency = 9_000_000        # pyftdi <= 0.56
    class _New:      frequency_max = 30_000_000   # 0.57 dropped .frequency
    class _Private:  _frequency = 7_500_000

    ok &= check("uses the value open_mpsse returned when it gives one",
                xvcd.resolve_frequency(_New(), 10e6, 8e6) == 8e6)
    ok &= check("falls back to the .frequency property",
                xvcd.resolve_frequency(_Old(), 10e6, None) == 9e6)
    ok &= check("falls back to the private attribute",
                xvcd.resolve_frequency(_Private(), 10e6, None) == 7.5e6)
    ok &= check("falls back to what we asked for (pyftdi 0.57)",
                xvcd.resolve_frequency(_New(), 10e6, None) == 10e6)
    ok &= check("ignores a non-numeric return value",
                xvcd.resolve_frequency(_New(), 10e6, "nope") == 10e6)
    ok &= check("ignores zero", xvcd.resolve_frequency(_New(), 10e6, 0) == 10e6)

    print("shift() bit plumbing at awkward lengths:")
    # Drive the chain into Shift-DR, then read the IDCODE out in odd-sized
    # slices. Byte-aligned fast path, unaligned bit path and TMS path all mix.
    for nbits in (1, 3, 7, 8, 9, 15, 16, 17, 31, 32, 33, 64, 100):
        cable = make_cable([0x03723093])
        cable.shift(6, bytes([0b00011111]), bytes([0x00]))
        cable.shift(3, bytes([0b00000001]), bytes([0x00]))
        nbytes = (nbits + 7) // 8
        tdo = cable.shift(nbits, bytes(nbytes), b"\xff" * nbytes)
        want = 0x03723093 & ((1 << min(nbits, 32)) - 1)
        got_int = int.from_bytes(tdo, "little") & ((1 << min(nbits, 32)) - 1)
        ok &= check(f"nbits={nbits}", got_int == want,
                    f"expected 0x{want:X}, got 0x{got_int:X}")
        ok &= check(f"nbits={nbits} return length", len(tdo) == nbytes)

    print("randomised equivalence against a bit-exact TAP reference:")
    # The byte-level fast path (bytes.count) must produce exactly the same TDO
    # as clocking the model one bit at a time. Mixed TMS densities exercise the
    # fast path, the bit path and the TMS path together.
    import random
    random.seed(7)
    mismatches = 0
    for _ in range(400):
        nbits = random.randint(1, 4000)
        nbytes = (nbits + 7) // 8
        density = random.choice((0.0, 0.01, 0.3, 1.0))
        tms = bytes(random.getrandbits(8) if random.random() < density else 0
                    for _ in range(nbytes))
        tdi = bytes(random.getrandbits(8) for _ in range(nbytes))

        got = make_cable([0x03723093]).shift(nbits, tms, tdi)

        reference = TapChain([0x03723093])
        want = bytearray(nbytes)
        for k in range(nbits):
            if reference.clock((tms[k >> 3] >> (k & 7)) & 1,
                               (tdi[k >> 3] >> (k & 7)) & 1):
                want[k >> 3] |= 1 << (k & 7)
        if bytes(want) != got:
            mismatches += 1
    ok &= check("400 randomised shifts are bit-exact", mismatches == 0,
                f"{mismatches} mismatched")

    print("telemetry:")
    cable = make_cable([0x03723093])
    cable.shift(64, bytes(8), b"\xff" * 8)
    cable.shift(32, bytes(4), b"\xff" * 4)
    ok &= check("shift and bit counters advance",
                cable.stats["shifts"] == 2 and cable.stats["bits"] == 96,
                str(cable.stats))
    ok &= check("start time is recorded once", cable.stats["started"] is not None)

    print("empty FTDI reads are retried, not treated as a dead cable:")
    # ps7_init's tiny transactions make the FTDI return empty reads constantly.
    # Failing on the first one wedged a run mid-ps7_init with
    # "FTDI read timeout (0/63 bytes)".
    ok &= check("scan still succeeds through a burst of empty reads",
                make_cable([0x03723093], empty_reads=25).scan_chain() == [0x03723093])

    slow = make_cable([0x03723093])
    slow.READ_DEADLINE_S = 0.05
    slow._ftdi.empty_reads = 10**9
    try:
        slow.shift(32, bytes(4), b"\xff" * 4)
        ok &= check("a cable that never answers is still reported", False,
                    "no CableError raised")
    except xvcd.CableError as exc:
        ok &= check("a cable that never answers is still reported",
                    "wedged" in str(exc), str(exc))

    print("vector-length back-off:")
    # hw_server silently closes a cable whose advertised vector length it does
    # not like, so the server has to notice and retreat by itself.
    class _Srv:
        FLOOR_VECTOR_BYTES = xvcd.XvcServer.FLOOR_VECTOR_BYTES
        note_barren_connection = xvcd.XvcServer.note_barren_connection

        def __init__(self, v, cable):
            self.vector_bytes, self.cable = v, cable

    cable = make_cable([0x03723093])
    srv = _Srv(262144, cable)
    steps = []
    while srv.note_barren_connection():
        steps.append(srv.vector_bytes)
    ok &= check("retreats toward a value hw_server is known to accept",
                steps and steps[-1] == 2048, str(steps))
    ok &= check("stops at the floor rather than looping forever",
                srv.note_barren_connection() is False)
    ok &= check("no retreat when already at the floor",
                _Srv(2048, cable).note_barren_connection() is False)
    ok &= check("each retreat is recorded for the report",
                any("backed off" in line for line in cable.stats["trace"]))

    print("\n" + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
