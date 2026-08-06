"""
xvcd.py -- Xilinx Virtual Cable (XVC 1.0) server over an FTDI MPSSE JTAG cable.

Runs natively on Apple Silicon macOS (libusb via pyftdi), so no Linux VM and no
USB/IP is needed to expose the Blackboard's JTAG port to a remote hw_server.

Wire layout assumed (Digilent / Real Digital FT2232H channel A, ADBUS):
    AD0 = TCK (out)   AD1 = TDI (out)   AD2 = TDO (in)   AD3 = TMS (out)

Why this is fast enough over a WAN link:
  hw_server chunks its JTAG traffic to whatever byte count we advertise in the
  `getinfo:` reply. Advertising a large vector length collapses a bitstream
  download from thousands of internet round trips into a few dozen. That is the
  entire reason this beats USB/IP, which pays a round trip per USB transfer.
"""

from __future__ import annotations

import logging
import socket
import socketserver
import struct
import sys
import threading
import time

log = logging.getLogger("xvcd")

# MPSSE opcodes (LSB-first, TDI driven on falling edge, TDO sampled on rising).
CMD_BYTES_RW = 0x39  # clock data bytes in and out
CMD_BITS_RW = 0x3B   # clock data bits in and out
CMD_TMS_RW = 0x6B    # clock data to TMS pin, reading TDO

# Known Zynq-7000 JTAG IDCODEs, version nibble masked off.
IDCODES = {
    0x3723093: "xc7z007s",
    0x3728093: "xc7z014s",
    0x373C093: "xc7z012s",
    0x3722093: "xc7z010",
    0x373B093: "xc7z015",
    0x3727093: "xc7z020",
    0x372C093: "xc7z030",
    0x4BA00477: "ARM DAP (Cortex-A9)",
}


class CableError(RuntimeError):
    pass


def resolve_frequency(ftdi, requested: float, returned=None) -> float:
    """Work out the TCK frequency actually in effect.

    pyftdi has moved this around between releases: `open_mpsse_from_url` returns
    the achieved frequency in some versions, and the `Ftdi.frequency` property
    exists in some but was dropped by 0.57. None of this is worth crashing over
    -- the value only feeds the XVC `settck` reply -- so try each source in turn
    and fall back to what we asked for."""
    for candidate in (returned,
                      getattr(ftdi, "frequency", None),
                      getattr(ftdi, "_frequency", None),
                      requested):
        if isinstance(candidate, (int, float)) and candidate > 0:
            return float(candidate)
    return float(requested)


def _bit(buf: bytes, i: int) -> int:
    return (buf[i >> 3] >> (i & 7)) & 1


class FtdiJtag:
    """Minimal MPSSE JTAG shifter exposing exactly what XVC needs."""

    # Flush thresholds: keep one USB transaction reasonably large but bounded.
    MAX_WRITE = 16384
    MAX_READ = 8192
    MAX_CHUNK = 4096

    # How long a read may make no progress at all before we call the cable dead.
    READ_DEADLINE_S = 5.0

    def __init__(self, url: str = "ftdi://ftdi:2232h/1", frequency: float = 10e6):
        try:
            from pyftdi.ftdi import Ftdi
        except ImportError as exc:  # pragma: no cover
            raise CableError(
                "pyftdi is not installed for this interpreter:\n"
                f"  {sys.executable}\n"
                "  Run ./setup.sh -- it creates .venv and fpgatest re-executes\n"
                "  itself into it automatically."
            ) from exc

        self._ftdi = Ftdi()
        try:
            returned = self._ftdi.open_mpsse_from_url(
                url, direction=0x0B, initial=0x08, frequency=frequency
            )
        except Exception as exc:
            raise CableError(
                f"could not open FTDI cable at {url!r}: {exc}\n"
                "  - is the board powered and enumerated? (see: fpgatest doctor)\n"
                "  - on macOS the Apple FTDI driver may hold the interface; "
                "'fpgatest doctor' explains the workaround"
            ) from exc

        # No adaptive clocking, no three-phase clocking, no loopback. These are
        # safe to reassert; the divide-by-5 command (0x8A) deliberately is not,
        # because pyftdi has already chosen a divisor for the base clock it
        # believes is in effect and changing it here would silently multiply the
        # real TCK by five.
        self._ftdi.write_data(bytes([0x97, 0x8D, 0x85]))
        self.frequency = resolve_frequency(self._ftdi, frequency, returned)

        # ps7_init is hundreds of tiny register accesses, each its own MPSSE
        # round trip. At the FTDI default 16 ms latency timer the chip sits on
        # every short reply until the timer expires: throughput collapses (we
        # measured 465 -> 16 kbit/s) and short reads come back empty often
        # enough to look like a wedged cable. 1 ms is the usual MPSSE setting
        # for JTAG. Not fatal if the pyftdi build does not expose it.
        try:
            self._ftdi.set_latency_timer(1)
        except Exception:
            log.debug("could not lower the FTDI latency timer", exc_info=True)

        self._wbuf = bytearray()
        self._pending: list[tuple[str, int]] = []
        self._read_len = 0
        self._lock = threading.Lock()

        # Without these numbers a slow bitstream download and a wedged one look
        # identical from the outside, which is exactly the situation you do not
        # want to be in while staring at a terminal.
        # "connections" separates "hw_server never reached us" from "hw_server
        # reached us and then did nothing", which need completely different
        # fixes. getinfo/settck are not shifts, so counting shifts alone
        # cannot tell them apart.
        self.stats = {"shifts": 0, "bits": 0, "started": None, "last": None,
                      "connections": 0, "getinfo": 0, "settck": 0,
                      "trace": [], "last_error": None}

    # ---------------------------------------------------------------- plumbing

    def close(self) -> None:
        try:
            self._ftdi.close()
        except Exception:
            pass

    def _queue(self, cmd: bytes, kind: str, nbits: int) -> None:
        self._wbuf += cmd
        self._pending.append((kind, nbits))
        self._read_len += (nbits // 8) if kind == "bytes" else 1
        if len(self._wbuf) >= self.MAX_WRITE or self._read_len >= self.MAX_READ:
            self._flush()

    def _emit_bits(self, val: int, n: int) -> None:
        cur = self._cur
        tdo = self._tdo
        for k in range(n):
            if (val >> k) & 1:
                tdo[cur >> 3] |= 1 << (cur & 7)
            cur += 1
        self._cur = cur

    def _emit_bytes(self, data: bytes) -> None:
        if (self._cur & 7) == 0:
            off = self._cur >> 3
            self._tdo[off:off + len(data)] = data
            self._cur += len(data) * 8
        else:
            for b in data:
                self._emit_bits(b, 8)

    def _flush(self) -> None:
        if not self._wbuf:
            return
        self._ftdi.write_data(bytes(self._wbuf))
        want = self._read_len
        got = bytearray()
        # An empty read is normal, not fatal: the FTDI returns nothing until it
        # has data or its latency timer fires, and during ps7_init's very short
        # transactions that happens constantly. Only a run of empty reads with
        # no progress for READ_DEADLINE_S means the cable is genuinely wedged.
        deadline = time.monotonic() + self.READ_DEADLINE_S
        while len(got) < want:
            chunk = self._ftdi.read_data_bytes(want - len(got), attempt=8)
            if chunk:
                got += chunk
                deadline = time.monotonic() + self.READ_DEADLINE_S
            elif time.monotonic() > deadline:
                raise CableError(
                    f"FTDI read timeout ({len(got)}/{want} bytes) after "
                    f"{self.READ_DEADLINE_S:.0f}s with no progress -- cable wedged?"
                )

        pos = 0
        for kind, nbits in self._pending:
            if kind == "bytes":
                n = nbits // 8
                self._emit_bytes(bytes(got[pos:pos + n]))
                pos += n
            else:
                # Bit- and TMS-mode reads arrive right-aligned against bit 7.
                self._emit_bits(got[pos] >> (8 - nbits), nbits)
                pos += 1

        self._wbuf.clear()
        self._pending.clear()
        self._read_len = 0

    # ------------------------------------------------------------------- shift

    def shift(self, nbits: int, tms: bytes, tdi: bytes) -> bytes:
        """Shift `nbits` of (TMS, TDI) and return the captured TDO bits.

        All buffers are LSB-first within each byte, per the XVC spec.
        """
        with self._lock:
            now = time.monotonic()
            if self.stats["started"] is None:
                self.stats["started"] = now
            self.stats["shifts"] += 1
            self.stats["bits"] += nbits
            self.stats["last"] = now

            self._tdo = bytearray((nbits + 7) // 8)
            self._cur = 0

            i = 0
            while i < nbits:
                # Fast path. During a data shift TMS stays low for millions of
                # bits, and measuring that run one bit at a time in Python is
                # what makes a bitstream download crawl. bytes.count runs in C,
                # so ask it whether a whole chunk is clear and skip the scan
                # entirely when it is -- which is almost always.
                if (i & 7) == 0:
                    b0 = i >> 3
                    span = min(self.MAX_CHUNK, (nbits - i) >> 3)
                    if span and tms.count(0, b0, b0 + span) == span:
                        self._queue(
                            bytes([CMD_BYTES_RW])
                            + struct.pack("<H", span - 1)
                            + tdi[b0:b0 + span],
                            "bytes", span * 8)
                        i += span * 8
                        continue

                if _bit(tms, i) == 0:
                    run = 1
                    while i + run < nbits and _bit(tms, i + run) == 0:
                        run += 1

                    if run >= 8 and (i & 7) == 0:
                        # Fast path: whole bytes of TDI with TMS held low.
                        nbytes = min(run // 8, self.MAX_CHUNK)
                        start = i >> 3
                        payload = tdi[start:start + nbytes]
                        self._queue(
                            bytes([CMD_BYTES_RW])
                            + struct.pack("<H", nbytes - 1)
                            + payload,
                            "bytes",
                            nbytes * 8,
                        )
                        i += nbytes * 8
                    else:
                        # Bit mode, sized so that we land byte-aligned when a
                        # long TMS-low run is waiting behind us.
                        n = (8 - (i & 7)) if (run >= 8 and (i & 7)) else min(run, 8)
                        val = 0
                        for k in range(n):
                            val |= _bit(tdi, i + k) << k
                        self._queue(bytes([CMD_BITS_RW, n - 1, val]), "bits", n)
                        i += n
                else:
                    # TMS transitions are rare (TAP state moves), so emit them
                    # one bit at a time. Correctness over micro-optimisation.
                    payload = 0x01 | (_bit(tdi, i) << 7)
                    self._queue(bytes([CMD_TMS_RW, 0x00, payload]), "tms", 1)
                    i += 1

            self._flush()
            return bytes(self._tdo)

    # ------------------------------------------------------------- chain probe

    def scan_chain(self, max_devices: int = 8) -> list[int]:
        """Reset the TAP and read out the IDCODE of every device in the chain."""
        # 5x TMS=1 -> Test-Logic-Reset; 1x TMS=0 -> Run-Test/Idle
        self.shift(6, bytes([0b00011111]), bytes([0x00]))
        # Run-Test/Idle -> Select-DR -> Capture-DR -> Shift-DR  (TMS 1,0,0)
        self.shift(3, bytes([0b00000001]), bytes([0x00]))

        nbits = 32 * max_devices
        nbytes = nbits // 8
        tdo = self.shift(nbits, bytes(nbytes), b"\xff" * nbytes)

        ids: list[int] = []
        for k in range(max_devices):
            word = struct.unpack_from("<I", tdo, k * 4)[0]
            if word in (0x00000000, 0xFFFFFFFF):
                break
            if not word & 1:  # valid IDCODEs always have bit 0 set
                break
            ids.append(word)

        self.shift(5, bytes([0b00011111]), bytes([0x00]))  # back to reset
        return ids


def describe_idcode(idcode: int) -> str:
    name = IDCODES.get(idcode & 0x0FFFFFFF) or IDCODES.get(idcode)
    return f"0x{idcode:08X} ({name})" if name else f"0x{idcode:08X} (unknown)"


class _XvcHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        peer = self.client_address
        log.info("hw_server connected from %s:%s", *peer[:2])
        cable: FtdiJtag = self.server.cable          # type: ignore[attr-defined]
        vector_bytes: int = self.server.vector_bytes  # type: ignore[attr-defined]
        sock = self.request
        stats = cable.stats
        stats["connections"] += 1
        shifts_at_start = stats["shifts"]

        # Relative timestamps: hw_server holds the socket open and lets ~2s
        # elapse between getinfo and settck, so "when did it close" is the
        # question the trace has to answer, not just "what did it send".
        t0 = time.monotonic()

        def trace(msg: str) -> None:
            if len(stats["trace"]) < 60:
                stats["trace"].append(f"+{time.monotonic() - t0:6.3f}s  {msg}")

        def recv_exact(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("peer closed")
                buf += chunk
            return buf

        def recv_until(term: bytes, limit: int = 32) -> bytes:
            buf = b""
            while not buf.endswith(term):
                c = sock.recv(1)
                if not c:
                    raise ConnectionError("peer closed")
                buf += c
                if len(buf) > limit:
                    raise ConnectionError(f"malformed command {buf!r}")
            return buf

        try:
            while True:
                cmd = recv_until(b":")
                if cmd == b"getinfo:":
                    stats["getinfo"] += 1
                    reply = f"xvcServer_v1.0:{vector_bytes}\n"
                    trace(f"getinfo -> {reply.strip()}")
                    sock.sendall(reply.encode())
                elif cmd == b"settck:":
                    stats["settck"] += 1
                    period_ns = struct.unpack("<I", recv_exact(4))[0]
                    # We keep the cable at its configured frequency and simply
                    # report it back; hw_server accepts a slower-than-requested
                    # actual period.
                    actual = max(period_ns, int(1e9 / cable.frequency))
                    trace(f"settck {period_ns} ns -> {actual} ns")
                    sock.sendall(struct.pack("<I", actual))
                elif cmd == b"shift:":
                    nbits = struct.unpack("<I", recv_exact(4))[0]
                    # Per-connection, not global: fpgatest's own start-up chain
                    # scan already spends four shifts, so a global budget of 3
                    # meant hw_server's first shifts -- the only ones that
                    # matter here -- were never recorded at all.
                    if stats["shifts"] - shifts_at_start < 3:
                        trace(f"shift {nbits} bits")
                    nbytes = (nbits + 7) // 8
                    tms = recv_exact(nbytes)
                    tdi = recv_exact(nbytes)
                    sock.sendall(cable.shift(nbits, tms, tdi))
                else:
                    raise ConnectionError(f"unknown XVC command {cmd!r}")
        except (ConnectionError, OSError) as exc:
            log.info("session from %s:%s ended: %s", peer[0], peer[1], exc)
            trace(f"closed: {exc}")
        except CableError as exc:
            log.error("cable failure: %s", exc)
            stats["last_error"] = str(exc)
            trace(f"cable error: {exc}")
        finally:
            if stats["shifts"] == shifts_at_start and stats["getinfo"]:
                trace("client left without shifting anything")
                self.server.note_barren_connection()  # type: ignore[attr-defined]


class XvcServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    # hw_server drops a cable whose advertised vector length it dislikes, and
    # it does so silently -- it just closes the socket after getinfo. Rather
    # than making the user guess, back off automatically and let the next
    # attempt succeed.
    FLOOR_VECTOR_BYTES = 2048

    def __init__(self, host: str, port: int, cable: FtdiJtag, vector_bytes: int):
        super().__init__((host, port), _XvcHandler)
        self.cable = cable
        self.vector_bytes = vector_bytes

    def note_barren_connection(self) -> bool:
        """A client came, asked getinfo, and left without shifting anything."""
        if self.vector_bytes <= self.FLOOR_VECTOR_BYTES:
            return False
        self.vector_bytes = max(self.FLOOR_VECTOR_BYTES, self.vector_bytes // 8)
        log.warning("client disconnected without shifting; retrying with "
                    "vector_len=%d", self.vector_bytes)
        self.cable.stats["trace"].append(
            f"backed off vector_len to {self.vector_bytes}")
        return True


def serve(host: str = "127.0.0.1", port: int = 2542, url: str = "ftdi://ftdi:2232h/1",
          frequency: float = 10e6, vector_bytes: int = 262144) -> XvcServer:
    """Start an XVC server in a background thread and return it."""
    cable = FtdiJtag(url, frequency)
    server = XvcServer(host, port, cable, vector_bytes)
    threading.Thread(target=server.serve_forever, daemon=True,
                     name="xvcd").start()
    log.info("XVC listening on %s:%d @ %.1f MHz, vector_len=%d B",
             host, port, cable.frequency / 1e6, vector_bytes)
    return server


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Standalone XVC server for FTDI JTAG cables")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2542)
    ap.add_argument("--url", default="ftdi://ftdi:2232h/1")
    ap.add_argument("--freq", type=float, default=10e6)
    ap.add_argument("--vector-bytes", type=int, default=262144)
    ap.add_argument("--scan", action="store_true", help="probe the chain and exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.scan:
        cable = FtdiJtag(args.url, args.freq)
        for idcode in cable.scan_chain():
            print(describe_idcode(idcode))
        cable.close()
        return 0

    server = serve(args.host, args.port, args.url, args.freq, args.vector_bytes)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
