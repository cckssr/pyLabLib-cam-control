"""USB transport session for a Thorlabs TSI scientific camera.

The framing is confirmed from live captures (protocol.md): fixed 1024-byte NUL-terminated
ASCII frames out on 0x02 and back in on 0x82, strictly one response per command, and a bulk
image stream on 0x83 read in 16 KiB chunks.

Notably the camera needs no firmware download and no vendor control requests: in both
captures ThorCam only ever issues standard GET_DESCRIPTOR / SET_CONFIGURATION, the device
never re-enumerates, and every camera operation rides the ASCII command channel.
"""

from __future__ import annotations

import errno
import time

import usb.core
import usb.util

from .protocol import (
    CMD_IN_EP,
    CMD_OUT_EP,
    COMMAND_FRAME_BYTES,
    DATA_CHUNK_BYTES,
    DATA_IN_EP,
    FRAME_MAGIC,
    KNOWN_PIDS,
    USB_TIMEOUT_MS,
    VID,
    CommandError,
    decode_command_frame,
    encode_command_frame,
    parse_response,
)

# How much to ask dev.read() for in one call while draining a frame off DATA_IN_EP. This is
# an I/O-efficiency knob, unrelated to DATA_CHUNK_BYTES (the 16 KiB grain the camera pads
# frames to on the wire): per-call overhead dominates at DATA_CHUNK_BYTES granularity, so a
# bigger read window collapses many small libusb calls into a few, without changing the
# byte-exact accounting in read_frame_bytes(). 1 MiB stays comfortably under typical OS
# single-transfer limits.
READ_BURST_BYTES = 1 << 20

# Errnos that mean "this endpoint is in a bad state", as opposed to a real fault. Only EPIPE
# (a genuine stall) is safe to clear a halt for; EIO is what a SuperSpeed endpoint whose
# sequence numbers no longer match the host reports, and nothing short of a bus reset fixes
# that -- see clear_halt's warning in _open_once().
_TRANSIENT_OPEN_ERRNOS = {errno.EIO, errno.EACCES, errno.ENODEV}


class StreamDesyncError(RuntimeError):
    """A frame read stopped part-way, so the image stream is no longer frame-aligned."""


class TsiCameraTransport:
    def __init__(
        self, dev: usb.core.Device, read_burst_bytes: int = READ_BURST_BYTES
    ) -> None:
        self.dev = dev
        self.pid = dev.idProduct
        # Overridable so callers can trade I/O-call count against read granularity; see
        # live_stream.py's --read-burst-bytes for a diagnostic use of this.
        self.read_burst_bytes = read_burst_bytes
        # Set when a command's reply did not arrive in time: it may still turn up late, and
        # must then be discarded instead of being taken as the answer to the next command.
        self._reply_may_be_pending = False

    @classmethod
    def open(
        cls,
        pid: int | None = None,
        serial: str | None = None,
        read_burst_bytes: int = READ_BURST_BYTES,
        retry_seconds: float = 5.0,
        find=None,
    ) -> TsiCameraTransport:
        """Open the first matching camera: claim interface 0 and start talking.

        Deliberately does *not* clear endpoint halts or reset the bus first -- see
        ``_open_once()`` for why that is unsafe on this hardware.

        Retries only the brief window where a device the OS has not finished releasing
        answers EIO/EACCES/ENODEV -- see ``_TRANSIENT_OPEN_ERRNOS``. It retries the *open*
        without touching endpoint state; a genuinely wedged device surfaces as an error the
        caller can respond to with an explicit ``reset_device()`` rather than being papered
        over silently.

        ``find`` replaces ``usb.core.find`` for locating candidate devices, e.g. to hand in
        an emulated device (see ``fake.py``).
        """
        deadline = time.monotonic() + retry_seconds
        while True:
            try:
                return cls._open_once(pid, serial, read_burst_bytes, find)
            except usb.core.USBError as exc:
                if (
                    exc.errno not in _TRANSIENT_OPEN_ERRNOS
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(0.5)

    @classmethod
    def _open_once(
        cls,
        pid: int | None,
        serial: str | None,
        read_burst_bytes: int,
        find=None,
    ) -> TsiCameraTransport:
        """One attempt at open() -- both families share this transport.

        With ``serial`` set, every matching device is tried in turn -- the requested camera
        is not necessarily the one that enumerates first.

        **Do not add a speculative ``dev.clear_halt()`` here.** On these USB 3.0 SuperSpeed
        cameras, ``CLEAR_FEATURE(ENDPOINT_HALT)`` resets the endpoint's sequence numbers on
        the device while the host keeps its own, so clearing a healthy endpoint *creates* a
        mismatch rather than fixing one, and the device stays wedged across process
        boundaries until a real bus reset. See protocol.md ("Reconnects") for the measured
        evidence. A halt that is *actually* set still gets cleared -- on the EPIPE that
        reports it, at the point of use (``command()``, ``read_frame_bytes()``,
        ``stream.FrameStream``). That is the only time clearing is both necessary and safe.
        """
        find = usb.core.find if find is None else find
        candidates = [
            dev
            for dev in find(find_all=True, idVendor=VID)
            if dev.idProduct == pid or (pid is None and dev.idProduct in KNOWN_PIDS)
        ]
        if not candidates:
            wanted = f"PID {pid:#06x}" if pid is not None else "any known camera PID"
            raise RuntimeError(f"no camera found (VID {VID:#06x}, {wanted})")

        seen = []
        for dev in candidates:
            transport = None
            try:
                try:
                    usb.util.claim_interface(dev, 0)
                except usb.core.USBError:
                    dev.set_configuration()
                    usb.util.claim_interface(dev, 0)

                transport = cls(dev, read_burst_bytes=read_burst_bytes)
                if serial is None:
                    return transport

                found = transport.command("sernum")
            except (usb.core.USBError, CommandError):
                # Don't leak this candidate's claim into open()'s retry loop above.
                if transport is not None:
                    transport.close()
                else:
                    # Best-effort: the claim never succeeded, so there may be nothing to
                    # dispose, and failing here would mask the real USBError being raised.
                    try:
                        usb.util.dispose_resources(dev)
                    except Exception:
                        pass
                raise
            if found == serial:
                return transport
            seen.append(found)
            transport.close()

        raise RuntimeError(
            f"no camera with serial {serial!r} (found: {', '.join(seen)})"
        )

    @staticmethod
    def reset_device(pid: int | None = None, serial: str | None = None) -> None:
        """Issue a USB bus reset -- the only thing that recovers a wedged endpoint.

        Deliberately *not* called automatically anywhere: a reset re-enumerates the device,
        which has been observed to take a camera off the bus entirely until it was
        physically replugged. Reach for it only when a fresh open keeps failing with EIO on
        the first command; ordinary use should never need it, since ``open()`` no longer
        clears halts speculatively.
        """
        for dev in usb.core.find(find_all=True, idVendor=VID):
            if dev.idProduct == pid or (pid is None and dev.idProduct in KNOWN_PIDS):
                try:
                    dev.reset()
                finally:
                    usb.util.dispose_resources(dev)
                return
        raise RuntimeError("no camera found to reset")

    def close(self) -> None:
        try:
            usb.util.release_interface(self.dev, 0)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(self.dev)

    def __enter__(self) -> TsiCameraTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def command(self, command: str, timeout_ms: int = USB_TIMEOUT_MS) -> str:
        """Send an ASCII command and return its response, raising CommandError on refusal.

        Every command gets exactly one response frame, including the ones that only
        acknowledge ("arm" -> "armed", "start" -> "running", "stop" -> "stopped"). `stop`
        is the slow one -- the camera takes ~100 ms to answer it -- so callers that shorten
        the timeout should leave room for that.

        Retries around a stalled command endpoint, which a process killed mid-transfer can
        leave set, and which survives close/reopen. Two details matter for correctness:

        * The response endpoint is drained before each retry. The channel is strictly one
          response per command, so a stale frame left queued by a failed attempt would be
          returned as the answer to the *next* command and every command after it -- an
          off-by-one that never resolves on its own.
        * Only the write is retried after a stall. A read timeout is not retried, because
          the command may well have been executed; re-sending it would run it twice.
        """
        if self._reply_may_be_pending:
            self._drain_command_replies()
            self._reply_may_be_pending = False
        last: usb.core.USBError | None = None
        for _attempt in range(3):
            try:
                self.dev.write(
                    CMD_OUT_EP, encode_command_frame(command), timeout=timeout_ms
                )
            except usb.core.USBError as exc:
                if exc.errno != errno.EPIPE:
                    raise
                last = exc
                self._recover_command_channel()
                continue
            try:
                frame = self.dev.read(
                    CMD_IN_EP, COMMAND_FRAME_BYTES, timeout=timeout_ms
                )
            except usb.core.USBTimeoutError:
                self._reply_may_be_pending = True
                raise
            return parse_response(command, decode_command_frame(bytes(frame)))
        raise last if last is not None else RuntimeError("unreachable")

    def _recover_command_channel(self) -> None:
        """Clear a stalled command endpoint and discard anything stale queued on it."""
        for ep in (CMD_OUT_EP, CMD_IN_EP):
            try:
                self.dev.clear_halt(ep)
            except usb.core.USBError:
                pass
        self._drain_command_replies()

    def _drain_command_replies(self) -> None:
        """Discard every reply frame still queued on the command response endpoint."""
        while True:
            try:
                if not len(self.dev.read(CMD_IN_EP, COMMAND_FRAME_BYTES, timeout=50)):
                    return
            except usb.core.USBError:
                return

    def flush_data_endpoint(
        self, timeout_ms: int = 200, max_bytes: int = 256 << 20
    ) -> int:
        """Read DATA_IN_EP until it falls silent, discarding everything. Returns bytes dropped.

        Needed on both sides of an acquisition: the camera keeps pushing after ``stop``, and
        the FX3 holds what the host never collected. Left in place, that residue becomes the
        tail of the previous session's frame on the *next* arm's first read, offsetting every
        frame after it -- so flushing before ``start`` is what makes the first frame of a
        session decode at all, not just tidiness. Bounded by ``max_bytes`` so a camera that
        was never stopped can't spin here forever.
        """
        dropped = 0
        while dropped < max_bytes:
            try:
                chunk = self.dev.read(
                    DATA_IN_EP, self.read_burst_bytes, timeout=timeout_ms
                )
            except usb.core.USBTimeoutError:
                return dropped
            except usb.core.USBError as exc:
                if exc.errno != errno.EPIPE:
                    raise
                try:
                    self.dev.clear_halt(DATA_IN_EP)
                except usb.core.USBError:
                    pass
                return dropped
            if not len(chunk):
                return dropped
            dropped += len(chunk)
        return dropped

    def read_frame_bytes(
        self, total_bytes: int, timeout_ms: int = USB_TIMEOUT_MS
    ) -> bytes:
        """Read one frame's worth of image data off DATA_IN_EP.

        ``total_bytes`` should come from frames.frame_bytes_on_wire(), which rounds the
        frame up to a whole 16 KiB chunk the way the camera pads it.

        Returns ``b""`` if no frame was pending -- pyusb signals that by *raising*
        USBTimeoutError, not by returning an empty buffer, so it has to be caught. A timeout
        part-way through a frame is different and more serious: the stream is then left
        mid-frame and every later frame would decode misaligned, so the endpoint is drained
        and StreamDesyncError raised rather than returning a short frame.

        A read can also fail with a pipe error (the endpoint STALLed) instead of timing out.
        That is handled the same way as a mid-frame timeout, plus clearing the halt first: a
        stalled endpoint returns nothing but errors on every further read until cleared, so
        leaving it set would turn every subsequent poll into the same crash instead of a
        recoverable desync.
        """
        chunks: list[bytes] = []
        received = 0
        while received < total_bytes:
            want = min(self.read_burst_bytes, total_bytes - received)
            try:
                chunk = self.dev.read(DATA_IN_EP, want, timeout=timeout_ms)
            except usb.core.USBTimeoutError:
                if received == 0:
                    return b""
                self.drain()
                raise StreamDesyncError(
                    f"frame truncated after {received}/{total_bytes} bytes; stream resynced"
                ) from None
            except usb.core.USBError as exc:
                if exc.errno != errno.EPIPE:
                    raise
                self.dev.clear_halt(DATA_IN_EP)
                if received == 0:
                    return b""
                raise StreamDesyncError(
                    f"frame truncated after {received}/{total_bytes} bytes "
                    "(endpoint stalled); cleared and resynced"
                ) from None
            chunks.append(bytes(chunk))
            received += len(chunk)
        return b"".join(chunks)

    def drain(self, timeout_ms: int = 100) -> int:
        """Discard whatever is still queued on DATA_IN_EP. Returns the bytes dropped.

        Used to get back to a frame boundary after a partial read -- callers rely on this to
        recover, so it must not itself raise for anything short of the device going away.

        A desynced read can leave the endpoint STALLed (pipe error) rather than just timing
        out. A stalled bulk endpoint returns nothing but an error on every further read until
        it is explicitly cleared, so a plain retry-on-timeout loop never recovers -- the
        whole stream would otherwise crash right here, on the one path that exists to make
        it resilient.
        """
        dropped = 0
        while True:
            try:
                chunk = self.dev.read(DATA_IN_EP, DATA_CHUNK_BYTES, timeout=timeout_ms)
            except usb.core.USBTimeoutError:
                return dropped
            except usb.core.USBError as exc:
                if exc.errno != errno.EPIPE:
                    raise
                self.dev.clear_halt(DATA_IN_EP)
                return dropped
            if not len(chunk):
                return dropped
            dropped += len(chunk)

    def resync_to_frame_start(
        self, max_chunks: int = 512, timeout_ms: int = 100
    ) -> bytes:
        """Discard bytes off DATA_IN_EP until a whole chunk starting with FRAME_MAGIC is
        found, and return that chunk (the caller must treat it as the first bytes of the
        next frame, not discard it).

        Unlike ``drain()``, which waits for the stream to fall idle, this is for a desync
        where the camera is still free-running and the stream never goes idle -- see
        protocol.md's "Continuous multi-frame streaming hung forever" for why ``drain()``
        alone hangs in that case. Chunk markers share FRAME_MAGIC's first two bytes but never
        its third (a
        marker's third byte is its chunk index mod 256, never 0 within a single frame), so
        checking only whole, chunk-aligned reads against the full 4-byte magic can't
        false-positive on one. Bounded at ``max_chunks`` (a few frames' worth) so a truly
        broken stream still can't hang this forever.
        """
        for _ in range(max_chunks):
            try:
                chunk = self.dev.read(DATA_IN_EP, DATA_CHUNK_BYTES, timeout=timeout_ms)
            except usb.core.USBTimeoutError:
                return b""
            except usb.core.USBError as exc:
                if exc.errno != errno.EPIPE:
                    raise
                self.dev.clear_halt(DATA_IN_EP)
                return b""
            chunk = bytes(chunk)
            if not chunk:
                return b""
            if chunk[:4] == FRAME_MAGIC:
                return chunk
        raise StreamDesyncError(
            f"no frame boundary found within {max_chunks} chunks -- stream may be corrupt"
        )
