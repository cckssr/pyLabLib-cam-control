"""Continuous frame reader for DATA_IN_EP.

Two invariants drive this module -- see protocol.md ("Resolved since Phase 2" and its
numbered subsections) for the failures that motivated them:

1. **Frame boundaries come from the wire, not from arithmetic.** Every 16 KiB chunk on
   DATA_IN_EP starts with a marker holding its index within the frame (see
   ``protocol.parse_chunk_marker``); index 0 starts a frame. A reader never predicts a
   frame's byte count to stay aligned, so one short frame cannot shift every read after it.
2. **The endpoint is never left unattended.** The camera free-runs once started, and drops
   the rest of any frame the host isn't actively reading. A dedicated reader (via
   ``bulkreader.AsyncBulkReader``, keeping several transfers permanently in flight) does
   nothing but read; decoding happens on the consumer's thread from the queue this fills.

A truncated frame is still *detected* (its chunk count is short) and reported rather than
being allowed to corrupt the frames after it -- forward progress never depends on the camera
behaving.
"""

from __future__ import annotations

import array
import errno
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import usb.core

from .bulkreader import AsyncBulkReader
from .protocol import (
    DATA_CHUNK_BYTES,
    DATA_IN_EP,
    parse_chunk_marker,
)

# How much one dev.read() asks for (synchronous path only -- see ``synchronous`` below).
# Must be a whole number of chunks so a full read lands on a chunk boundary; the parser
# tolerates short reads anyway, this just keeps the common case tidy. 1 MiB = 64 chunks,
# enough that per-call overhead is negligible while staying well under any OS
# single-transfer limit.
READ_BURST_BYTES = 64 * DATA_CHUNK_BYTES

# Bulk transfers kept in flight on DATA_IN_EP at once -- see bulkreader.py and protocol.md
# ("A synchronous read loop still lost frames") for why the endpoint must never be without a
# posted transfer. Four is comfortably more than the one-being-copied plus one-in-flight
# minimum, without tying up much memory (4 x TRANSFER_BYTES).
NUM_TRANSFERS = 4

# Size of one in-flight transfer. Smaller than READ_BURST_BYTES because several are queued:
# what matters is total bytes in flight, and smaller individual transfers give the assembler
# work sooner and lower latency per frame.
TRANSFER_BYTES = 16 * DATA_CHUNK_BYTES

# Frames kept queued for the consumer. Streaming is live data: when a consumer falls behind,
# the useful thing is the newest frame, not a growing backlog that adds latency and memory
# without ever catching up. Overflow drops the oldest and counts it, so "the consumer is too
# slow" stays visible in the stats instead of turning into unbounded memory growth.
DEFAULT_QUEUE_DEPTH = 4

# USB errnos a single failed read may report without the link being unusable -- see
# protocol.md ("Read errors must be survivable, not fatal"). A burst of them in a row does
# mean the link is failing, so the reader gives up after _MAX_CONSECUTIVE_READ_ERRORS rather
# than spinning.
_RECOVERABLE_READ_ERRNOS = {errno.EIO, errno.EOVERFLOW, errno.EPROTO}
_MAX_CONSECUTIVE_READ_ERRORS = 20


@dataclass
class StreamStats:
    """Counters for one arm/stop cycle. Every loss path increments exactly one of these."""

    bytes_read: int = 0
    chunks_seen: int = 0
    frames_complete: int = 0
    #: Frames the *camera* cut short because the host stopped draining fast enough. The
    #: frame's own chunk sequence proves it: it ended before its expected chunk count.
    frames_truncated: int = 0
    #: Frames dropped by *this* process because the consumer wasn't keeping up.
    frames_dropped_by_consumer: int = 0
    #: Chunks discarded while re-establishing chunk alignment after corruption.
    bytes_resynced: int = 0
    #: Gaps in the camera's own frame counter -- frames it never sent us at all.
    frames_missed: int = 0
    read_errors: int = 0
    #: Times expected_chunks was corrected from the wire because the geometry-derived
    #: prediction disagreed with the frames the camera actually sends.
    expected_chunks_corrected: int = 0

    def as_dict(self) -> dict[str, int]:
        """All counters as a plain dict, for logging or JSON serialization."""
        return dict(self.__dict__)


@dataclass
class RawFrame:
    """One frame exactly as it came off the wire, chunk markers still in place."""

    data: bytes
    frame_count: int | None
    chunk_count: int
    complete: bool
    received_at: float = field(default_factory=time.monotonic)


class ChunkAssembler:
    """Byte stream in, whole frames out, driven entirely by chunk-marker indices.

    Feed it whatever ``dev.read()`` returned -- reads need not be chunk-aligned or
    chunk-sized. It buffers, consumes ``DATA_CHUNK_BYTES`` at a time, and emits a RawFrame
    each time a frame ends (marked by the next chunk carrying index 0, or by the frame
    reaching ``expected_chunks``).

    ``expected_chunks`` is what makes "the camera truncated this one" distinguishable from
    "this one is fine": a frame that ends early is emitted with ``complete=False`` instead of
    being silently merged into its neighbour.
    """

    def __init__(self, expected_chunks: int, frame_count_of=None) -> None:
        self.expected_chunks = expected_chunks
        # Callable mapping a frame's first chunk -> its frame counter, so this class stays
        # free of family-specific header parsing.
        self._frame_count_of = frame_count_of
        self._buffer = bytearray()
        # Read cursor into _buffer. Consuming with ``del buf[:16384]`` instead would memmove
        # the whole remaining buffer once per chunk -- 64 times per 1 MiB read, i.e. tens of
        # MB of memmove per MB received -- which is real overhead at these data rates.
        self._pos = 0
        self._chunks: list[bytes] = []
        self._next_index: int | None = None
        self._aligned = False
        # A frame that hit expected_chunks and is waiting one chunk for confirmation.
        self._ready = False
        self.stats = StreamStats()

    def feed(self, data: bytes | memoryview) -> list[RawFrame]:
        """Consume bytes, returning any frames that completed."""
        self._compact()
        self._buffer += data
        self.stats.bytes_read += len(data)
        out: list[RawFrame] = []
        view = memoryview(self._buffer)

        while len(self._buffer) - self._pos >= DATA_CHUNK_BYTES:
            if not self._aligned:
                del view  # _align() mutates the buffer, so the view must go first
                if not self._align():
                    return out
                view = memoryview(self._buffer)
                continue
            start = self._pos
            chunk = bytes(view[start : start + DATA_CHUNK_BYTES])
            index = parse_chunk_marker(chunk)
            if index is None:
                # Whatever follows is not a chunk. Two very different situations reach here
                # and they must not be treated alike: a frame that had already reached
                # expected_chunks (``_ready``) is simply over -- something non-chunk-shaped
                # trails every frame (see protocol.md's `data_packets_per_frame` note), and
                # it must be delivered complete, not flushed as truncated. A frame still
                # mid-assembly really has lost alignment, and assembling across the gap would
                # produce a plausible-looking but shifted image.
                self._aligned = False
                self._flush(out, complete=self._ready)
                self._ready = False
                continue
            self._pos = start + DATA_CHUNK_BYTES
            self.stats.chunks_seen += 1
            self._accept(index, chunk, out)

        return out

    def _compact(self) -> None:
        """Drop already-consumed bytes from the front of the buffer.

        Done once per read rather than once per chunk, which is what keeps feed() linear.
        """
        if self._pos:
            del self._buffer[: self._pos]
            self._pos = 0

    def _align(self) -> bool:
        """Find a chunk boundary in the buffer. False if more bytes are needed to decide.

        A single valid-looking marker is not enough -- ``90 90 xx 00`` can occur inside pixel
        data. A real boundary repeats every DATA_CHUNK_BYTES, so a candidate is confirmed by
        finding another one exactly a chunk later whenever the buffer is long enough.

        Scans with ``bytes.find`` rather than testing every offset in Python: this runs on
        the reader thread, and a linear scan over a 1 MiB burst would stall the very reads
        that resyncing is supposed to restore.
        """
        self._compact()
        buf = self._buffer
        offset = 0
        while True:
            offset = buf.find(b"\x90\x90", offset)
            if offset < 0 or offset + 4 > len(buf):
                break
            if parse_chunk_marker(buf[offset : offset + 4]) is None:
                offset += 1
                continue
            nxt = offset + DATA_CHUNK_BYTES
            if nxt + 4 <= len(buf):
                if parse_chunk_marker(buf[nxt : nxt + 4]) is None:
                    offset += 1
                    continue
            elif offset != 0:
                # Can't confirm this candidate yet. Keep it at the front and retry once more
                # bytes arrive, rather than committing to an unverified boundary.
                self.stats.bytes_resynced += offset
                del buf[:offset]
                return False
            if offset:
                self.stats.bytes_resynced += offset
                del buf[:offset]
            self._aligned = True
            return True

        # Nothing plausible in here. Keep only the last byte, which could be the first half
        # of a marker split across two reads.
        drop = max(0, len(buf) - 1)
        self.stats.bytes_resynced += drop
        del buf[:drop]
        return False

    def _accept(self, index: int, chunk: bytes, out: list[RawFrame]) -> None:
        """Fold one recognized chunk into the frame in progress."""
        if index == 0:
            # A new frame starts here, so whatever was in progress is over.
            self._settle_expected(len(self._chunks))
            self._flush(out, complete=len(self._chunks) == self.expected_chunks)
            self._chunks = [chunk]
            self._next_index = 1
            self._ready = False
            return

        if self._ready and index == self._next_index % 256:
            # The frame reached expected_chunks but the camera kept numbering past it, so the
            # prediction was too small. Under-reading cannot invent extra chunks, so the wire
            # is right: grow and keep appending rather than emitting a short frame.
            # Chunk ``index`` exists, so the frame is at least index+1 chunks long. Growing
            # by exactly that keeps the correction tight: if the frame is longer still, the
            # next chunk lands here again and grows it again.
            self.stats.expected_chunks_corrected += 1
            self.expected_chunks = self._next_index + 1
            self._ready = False

        if self._next_index is None or index != self._next_index % 256:
            # Chunks went missing inside a frame. The frame is unusable, but the stream is
            # still aligned, so drop just this frame and pick up at the next index 0.
            self._flush(out, complete=False)
            self._chunks = []
            self._next_index = None
            self._ready = False
            return

        self._chunks.append(chunk)
        self._next_index += 1
        if len(self._chunks) == self.expected_chunks:
            # Hold rather than emit: one more chunk tells us whether this frame is really
            # over (next index 0) or whether expected_chunks is too small (the sequence
            # continues). Waiting one chunk costs microseconds; waiting for the next frame's
            # header would cost a whole frame period.
            self._ready = True

    def flush_ready(self) -> list[RawFrame]:
        """Emit a frame that is complete but still waiting on the next chunk to confirm it.

        Called when the endpoint falls idle or the stream stops -- otherwise the last frame
        of an acquisition would be held forever waiting for a successor that never comes.
        """
        out: list[RawFrame] = []
        if self._ready:
            self._flush(out, complete=True)
            self._next_index = None
            self._ready = False
        return out

    def _settle_expected(self, chunk_count: int) -> None:
        """Correct ``expected_chunks`` upward from what the wire delivers. Never downward.

        Growth is sound, and ``_accept`` acts on it too: a frame that keeps numbering chunks
        past the prediction proves the prediction too small, because under-reading cannot
        manufacture extra chunks. Shrinking is not sound: truncation only ever shortens a
        frame (see protocol.md, "The chunk-count prediction must only ever grow"), so a
        shorter run carries no information worth acting on.
        """
        if 0 < chunk_count <= 256 and chunk_count > self.expected_chunks:
            self.stats.expected_chunks_corrected += 1
            self.expected_chunks = chunk_count

    def _flush(self, out: list[RawFrame], complete: bool) -> None:
        """Emit the chunks assembled so far as a RawFrame, then clear frame-in-progress state."""
        if not self._chunks:
            return
        data = b"".join(self._chunks)
        count = None
        if self._frame_count_of is not None:
            try:
                count = self._frame_count_of(data)
            except Exception:
                count = None
        out.append(
            RawFrame(
                data=data,
                frame_count=count,
                chunk_count=len(self._chunks),
                complete=complete,
            )
        )
        if complete:
            self.stats.frames_complete += 1
        else:
            self.stats.frames_truncated += 1
        self._chunks = []
        self._next_index = None

    def reset(self) -> None:
        """Discard all buffered and in-progress state, e.g. after a read error."""
        self._buffer.clear()
        self._pos = 0
        self._chunks = []
        self._next_index = None
        self._aligned = False
        self._ready = False


class FrameStream:
    """Runs a reader thread that never stops draining DATA_IN_EP.

    Start it right after the camera's ``start``, stop it before ``stop``. ``next_frame()``
    hands the consumer whole frames off a bounded queue; the reader itself never blocks on
    the consumer, so slow decoding costs queued frames (counted) instead of desynchronising
    the wire.
    """

    def __init__(
        self,
        dev: usb.core.Device,
        expected_chunks: int,
        frame_count_of=None,
        read_burst_bytes: int = READ_BURST_BYTES,
        queue_depth: int = DEFAULT_QUEUE_DEPTH,
        timeout_ms: int = 1000,
        keep_incomplete: bool = False,
        synchronous: bool = False,
        tap=None,
    ) -> None:
        self._dev = dev
        self._assembler = ChunkAssembler(expected_chunks, frame_count_of)
        self._burst = array.array("B", bytes(read_burst_bytes))
        self._timeout_ms = timeout_ms
        self._keep_incomplete = keep_incomplete
        self._queue: deque[RawFrame] = deque()
        self._depth = queue_depth
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._last_frame_count: int | None = None
        self._last_data = time.monotonic()
        # Asynchronous by default: a synchronous read loop leaves DATA_IN_EP unattended
        # between calls, which a CS165CU answers by discarding the rest of the frame in
        # progress (bulkreader.py has the measurement). The synchronous path is kept for
        # offline tests against a fake device, which has no real libusb handle to drive.
        self._synchronous = synchronous
        self._reader: AsyncBulkReader | None = None
        self._idle_timer: threading.Thread | None = None
        # The assembler is touched from the libusb event thread, from the idle watcher, and
        # potentially from whichever thread runs a synchronous command (libusb dispatches
        # pending completions from there too). It holds mutable parse state, so every entry
        # point takes this.
        self._assembly = threading.Lock()
        # Optional callback receiving every byte as it arrives, before any parsing, for
        # capturing a stream to disk exactly as the wire delivered it.
        self._tap = tap

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the reader (async by default; ``synchronous`` for the offline test path)."""
        if self._thread is not None or self._reader is not None:
            raise RuntimeError("stream already running")
        self._stop.clear()
        self._assembler.reset()

        if self._synchronous:
            self._thread = threading.Thread(
                target=self._run, name="tsi-frame-reader", daemon=True
            )
            self._thread.start()
            return

        self._reader = AsyncBulkReader(
            self._dev,
            DATA_IN_EP,
            transfer_size=TRANSFER_BYTES,
            num_transfers=NUM_TRANSFERS,
            on_data=self._on_data,
            on_error=self._on_transfer_error,
        )
        self._reader.start()
        # Transfers carry no timeout, so nothing else would ever notice an idle endpoint --
        # and the last frame of an acquisition waits on exactly that (see
        # ChunkAssembler.flush_ready).
        self._idle_timer = threading.Thread(
            target=self._watch_idle, name="tsi-frame-idle", daemon=True
        )
        self._idle_timer.start()

    def _on_data(self, view: memoryview) -> None:
        """AsyncBulkReader callback: feed newly arrived bytes to the assembler and publish
        any frames it completes."""
        if self._tap is not None:
            self._tap(view)
        with self._assembly:
            frames = self._assembler.feed(view)
        self._last_data = time.monotonic()
        for frame in frames:
            self._publish(frame)

    def _on_transfer_error(self, status: int, name: str) -> bool:
        """A failed transfer. Keep it in rotation unless the device is gone."""
        from .bulkreader import RECOVERABLE_STATUSES

        self._assembler.stats.read_errors += 1
        if status not in RECOVERABLE_STATUSES:
            return False
        # Bytes were almost certainly lost, so the byte stream is no longer trustworthy at
        # its current offset; re-find a chunk boundary rather than assembling across the gap.
        with self._assembly:
            self._assembler.reset()
        if self._assembler.stats.read_errors > _MAX_CONSECUTIVE_READ_ERRORS:
            return False
        return True

    def _watch_idle(self) -> None:
        """Release a frame held for confirmation once the endpoint has gone quiet."""
        while not self._stop.wait(0.05):
            if time.monotonic() - self._last_data >= 0.10:
                with self._assembly:
                    frames = self._assembler.flush_ready()
                for frame in frames:
                    self._publish(frame)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the reader and release any frame still held pending confirmation."""
        self._stop.set()
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.stop()
        idle, self._idle_timer = self._idle_timer, None
        if idle is not None:
            idle.join(1.0)
        # No reader is running now, so the last frame held for confirmation can be released.
        with self._assembly:
            tail = self._assembler.flush_ready()
        for frame in tail:
            self._publish(frame)
        thread, self._thread = self._thread, None
        if thread is not None:
            # The thread can be inside a blocking read; it checks the flag between reads, so
            # the worst case is one timeout_ms.
            thread.join(timeout)

        with self._arrived:
            self._arrived.notify_all()

    def __enter__(self) -> FrameStream:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- consumer side -----------------------------------------------------

    def next_frame(self, timeout: float | None = 1.0) -> RawFrame | None:
        """Pop the oldest queued frame, or None if none arrived within ``timeout``."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._arrived:
            while not self._queue:
                if self._error is not None:
                    raise self._error
                if self._thread is None and self._stop.is_set():
                    return None
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._arrived.wait(0.05 if remaining is None else min(remaining, 0.05))
            return self._queue.popleft()

    @property
    def stats(self) -> StreamStats:
        """Counters for the running (or most recently stopped) acquisition."""
        return self._assembler.stats

    @property
    def error(self) -> BaseException | None:
        """The exception that ended the reader thread, if it stopped abnormally."""
        return self._error

    # -- reader thread -----------------------------------------------------

    def _run(self) -> None:
        """Synchronous read loop, used only when ``synchronous=True`` (the offline test
        path against a fake device with no real libusb handle to drive async transfers)."""
        view = memoryview(self._burst)
        errors = 0
        while not self._stop.is_set():
            try:
                n = self._dev.read(DATA_IN_EP, self._burst, timeout=self._timeout_ms)
            except usb.core.USBTimeoutError:
                # No data for a while is normal between acquisitions; keep the endpoint
                # attended rather than treating silence as an error. An idle endpoint does
                # mean any frame held for confirmation will get no successor, so release it
                # now -- otherwise the last frame of an acquisition never arrives.
                for frame in self._assembler.flush_ready():
                    self._publish(frame)
                continue
            except usb.core.USBError as exc:
                if exc.errno == errno.EPIPE:
                    # A genuine stall. This is the one situation where clearing the halt is
                    # correct -- see transport.py on why it must never be done speculatively.
                    self._assembler.stats.read_errors += 1
                    errors += 1
                    try:
                        self._dev.clear_halt(DATA_IN_EP)
                    except usb.core.USBError:
                        pass
                    self._assembler.reset()
                elif self._stop.is_set():
                    break
                elif exc.errno in _RECOVERABLE_READ_ERRNOS or exc.errno is None:
                    # A link-level transaction error. One is survivable: drop alignment,
                    # since bytes were almost certainly lost, and carry on. A stream of them
                    # means the link itself is failing, so don't spin on it forever.
                    self._assembler.stats.read_errors += 1
                    errors += 1
                    self._assembler.reset()
                    if errors > _MAX_CONSECUTIVE_READ_ERRORS:
                        self._error = exc
                        with self._arrived:
                            self._arrived.notify_all()
                        return
                else:
                    self._error = exc
                    with self._arrived:
                        self._arrived.notify_all()
                    return
                continue
            errors = 0
            if not n:
                continue
            for frame in self._assembler.feed(view[:n]):
                self._publish(frame)

    def _publish(self, frame: RawFrame) -> None:
        """Count any gap in the camera's frame counter, then queue the frame for the
        consumer, dropping the oldest queued frame first if the queue is already full."""
        stats = self._assembler.stats
        if frame.frame_count is not None:
            if self._last_frame_count is not None:
                gap = frame.frame_count - self._last_frame_count - 1
                if gap > 0:
                    stats.frames_missed += gap
            self._last_frame_count = frame.frame_count
        if not frame.complete and not self._keep_incomplete:
            return
        with self._arrived:
            while len(self._queue) >= self._depth:
                self._queue.popleft()
                stats.frames_dropped_by_consumer += 1
            self._queue.append(frame)
            self._arrived.notify()
