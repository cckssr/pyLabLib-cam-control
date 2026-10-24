"""Keep several bulk IN transfers permanently in flight on one endpoint.

A synchronous read loop is not good enough for these cameras: ``dev.read()`` submits one
transfer, waits for it, and returns, leaving the endpoint with no transfer posted in between.
On a CS165CU, that gap alone is enough for the camera to discard the rest of the frame in
progress -- see protocol.md ("A synchronous read loop still lost frames") for the measurement.
This module keeps several transfers always posted instead, which is what every real streaming
USB driver does.

Transfers are resubmitted from inside their own completion callback, so while one buffer is
being copied out the others are still armed, and are submitted with **no timeout**
(``timeout_ms=0``): a bulk IN transfer that never expires is precisely the "always posted"
property needed here, and "no data for a while" is a question for the layer above, not an
error here.

Built on pyusb's own libusb handle, context and ctypes prototypes rather than a second USB
binding, because the interface can only be claimed once per process -- a separate libusb
context could not open the same camera alongside the command channel.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import POINTER, c_int, c_long, c_ubyte, c_void_p, cast

import usb
import usb.backend.libusb1 as _libusb1
import usb.core
from usb.backend.libusb1 import (  # noqa: E402  -- private, but the only way to share a handle
    _libusb_transfer,
    _libusb_transfer_cb_fn_p,
)

# _libusb_transfer's field layout and _libusb_transfer_cb_fn_p's signature are pyusb
# internals, not public API -- there is no supported way to submit a permanently-in-flight
# transfer through pyusb's own (synchronous) surface, so this module reaches past it (see
# the module docstring). Nothing pins pyusb from renaming or reshaping either one on a
# future release, and if that happens this module must fail loudly at import time -- with
# the installed version named -- rather than segfault inside a ctypes callback the first
# time a frame arrives. This checks only the fields _make_transfer()/_complete() actually
# set or read; ``dev._ctx``'s shape (also private) can only be checked against a real
# opened device, so that half of the guard lives in AsyncBulkReader.__init__ instead.
_REQUIRED_TRANSFER_FIELDS = {
    "dev_handle",
    "flags",
    "endpoint",
    "type",
    "timeout",
    "status",
    "length",
    "actual_length",
    "callback",
    "buffer",
    "num_iso_packets",
}


def _require_supported_backend() -> None:
    fields = {name for name, _ctype in _libusb_transfer._fields_}
    missing = _REQUIRED_TRANSFER_FIELDS - fields
    if missing or not callable(_libusb_transfer_cb_fn_p):
        raise RuntimeError(
            f"pyusb {usb.__version__}'s usb.backend.libusb1 internals no longer match what "
            "tsi_camera.bulkreader expects (missing _libusb_transfer fields: "
            f"{sorted(missing) or 'callback type not callable'}). This module depends on "
            "pyusb's private libusb1 backend layout to keep bulk transfers permanently in "
            "flight; pin pyusb to a tested version (see pyproject.toml) or update this "
            "guard together with the code that relies on it."
        )


_require_supported_backend()

_TRANSFER_TYPE_BULK = 2

_TRANSFER_COMPLETED = 0
_TRANSFER_ERROR = 1
_TRANSFER_TIMED_OUT = 2
_TRANSFER_CANCELLED = 3
_TRANSFER_STALL = 4
_TRANSFER_NO_DEVICE = 5
_TRANSFER_OVERFLOW = 6

_STATUS_NAMES = {
    _TRANSFER_COMPLETED: "completed",
    _TRANSFER_ERROR: "transfer error",
    _TRANSFER_TIMED_OUT: "timed out",
    _TRANSFER_CANCELLED: "cancelled",
    _TRANSFER_STALL: "endpoint stalled",
    _TRANSFER_NO_DEVICE: "device disconnected",
    _TRANSFER_OVERFLOW: "overflow",
}

# Statuses that mean this transfer failed but the stream may continue. A stall is included
# because the caller can clear it; the endpoint being genuinely gone is not.
RECOVERABLE_STATUSES = frozenset(
    {_TRANSFER_ERROR, _TRANSFER_TIMED_OUT, _TRANSFER_STALL, _TRANSFER_OVERFLOW}
)


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", c_long), ("tv_usec", c_long)]


class AsyncBulkReader:
    """Runs ``num_transfers`` bulk IN transfers in a permanent rotation on ``endpoint``.

    ``on_data(memoryview)`` is called for each completed transfer, on the event thread, with
    the bytes that arrived. It must not block for long: the transfer it came from is
    resubmitted immediately after it returns, and until then that buffer is out of rotation.
    The view is only valid for the duration of the call.

    ``on_error(status, name)`` is called for a failed transfer. Return True to keep the
    transfer in rotation, False to retire it; when every transfer has been retired the reader
    stops, and ``on_exhausted()`` is called (from the event thread) unless that happened
    because of ``stop()``.

    Exceptions raised by ``on_data`` are counted in ``callback_errors`` (the most recent one
    is kept in ``last_callback_error``) rather than propagated.
    """

    def __init__(
        self,
        dev: usb.core.Device,
        endpoint: int,
        transfer_size: int,
        num_transfers: int,
        on_data,
        on_error=None,
        on_exhausted=None,
    ) -> None:
        self._on_exhausted = on_exhausted
        self.callback_errors = 0
        self.last_callback_error: BaseException | None = None
        self._dev = dev
        self._endpoint = endpoint
        self._size = transfer_size
        self._on_data = on_data
        self._on_error = on_error

        try:
            ctx = dev._ctx
            self._lib = ctx.backend.lib
            self._ctx = ctx.backend.ctx
            self._handle = ctx.handle.handle
        except AttributeError as exc:
            raise RuntimeError(
                f"pyusb {usb.__version__}'s usb.core.Device no longer exposes the private "
                "._ctx.backend.lib/.ctx/.handle.handle chain tsi_camera.bulkreader shares a "
                "libusb handle through. Pin pyusb to a tested version (see pyproject.toml) "
                "or update this class together with the internals it depends on."
            ) from exc

        self._declare_prototypes()

        # The ctypes callback trampoline must outlive every transfer that references it, or
        # libusb calls into freed memory.
        self._callback = _libusb_transfer_cb_fn_p(self._complete)

        self._transfers: dict[int, tuple] = {}
        self._live = 0
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._num_transfers = num_transfers
        self._drained = threading.Event()

    # -- setup -------------------------------------------------------------

    def _declare_prototypes(self) -> None:
        """Set ctypes argtypes/restype for the libusb functions this class calls."""
        lib = self._lib
        lib.libusb_alloc_transfer.argtypes = [c_int]
        lib.libusb_alloc_transfer.restype = POINTER(_libusb_transfer)
        lib.libusb_free_transfer.argtypes = [POINTER(_libusb_transfer)]
        lib.libusb_submit_transfer.argtypes = [POINTER(_libusb_transfer)]
        lib.libusb_submit_transfer.restype = c_int
        lib.libusb_cancel_transfer.argtypes = [POINTER(_libusb_transfer)]
        lib.libusb_cancel_transfer.restype = c_int
        lib.libusb_handle_events_timeout_completed.argtypes = [
            c_void_p,
            POINTER(_Timeval),
            POINTER(c_int),
        ]
        lib.libusb_handle_events_timeout_completed.restype = c_int

    def _make_transfer(
        self,
    ) -> tuple[ctypes._Pointer[_libusb_transfer], ctypes.Array[c_ubyte]]:
        """Allocate one libusb transfer and its backing buffer, wired to ``_complete``."""
        ptr = self._lib.libusb_alloc_transfer(0)
        if not ptr:
            raise MemoryError("libusb_alloc_transfer failed")
        buf = (c_ubyte * self._size)()
        t = ptr.contents
        t.dev_handle = self._handle
        t.flags = 0
        t.endpoint = self._endpoint
        t.type = _TRANSFER_TYPE_BULK
        # No timeout. A bulk IN that never expires is exactly the "always posted" property
        # this class exists to provide; an idle stream is the caller's question, not an error.
        t.timeout = 0
        t.buffer = cast(buf, c_void_p)
        t.length = self._size
        t.callback = self._callback
        t.num_iso_packets = 0
        return ptr, buf

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Allocate and submit ``num_transfers`` transfers, then start the event thread."""
        if self._running:
            raise RuntimeError("reader already running")
        self._drained.clear()
        self._running = True

        for _ in range(self._num_transfers):
            ptr, buf = self._make_transfer()
            self._transfers[ctypes.addressof(ptr.contents)] = (ptr, buf)

        # Submit before starting the event thread. The other order looks harmless but is
        # not: the thread's exit condition is "no transfers left alive", which is trivially
        # true before anything has been submitted, so it would run once and stop -- leaving
        # nobody to dispatch completions, and stop() then freeing transfers libusb still
        # owned. Completions raised before the thread starts are queued by libusb, so
        # submitting first loses nothing.
        submitted = 0
        first_failure_rc = 0
        for ptr, _buf in list(self._transfers.values()):
            rc = self._lib.libusb_submit_transfer(ptr)
            if rc != 0:
                first_failure_rc = rc
                break
            submitted += 1
        with self._lock:
            self._live = submitted

        if submitted == 0:
            self._running = False
            self._free_all()
            if first_failure_rc == _libusb1.LIBUSB_ERROR_NO_MEM:
                # Linux caps how much DMA-able memory usbfs will pin across every process
                # on the system (usbfs_memory_mb) -- this reader asks for
                # num_transfers * transfer_size permanently, and libusb_submit_transfer
                # returns exactly this code, no more specific, when that cap is hit. macOS
                # and Windows have no equivalent limit, so this only fires on Linux.
                raise usb.core.USBError(
                    "no bulk transfer could be submitted: libusb reported LIBUSB_ERROR_NO_MEM "
                    "-- on Linux this means the usbfs_memory_mb cap was hit. Check "
                    "/sys/module/usbcore/parameters/usbfs_memory_mb and raise it, e.g. "
                    "`echo 0 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb` "
                    "(0 = unlimited) -- see README.md's Linux section for how to make that "
                    "persist across reboots. This reader needs "
                    f"{self._num_transfers * self._size} bytes in flight."
                )
            raise usb.core.USBError(
                f"no bulk transfer could be submitted (libusb error {first_failure_rc})"
            )

        self._thread = threading.Thread(
            target=self._run_events, name="tsi-usb-events", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Cancel every transfer and wait for its callback before freeing anything.

        Freeing a transfer libusb still owns is a use-after-free, and it does not present as
        a crash -- it presents as the next command on the device hanging forever.
        """
        if not self._running and not self._transfers:
            return
        self._running = False
        for ptr, _buf in list(self._transfers.values()):
            try:
                self._lib.libusb_cancel_transfer(ptr)
            except Exception:
                pass
        drained = self._drained.wait(timeout)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
        if drained:
            self._free_all()
        else:
            # Some callback never came back, so libusb may still own those transfers; leaking
            # them is the lesser evil compared with the use-after-free described above.
            self._transfers.clear()

    def _free_all(self) -> None:
        """Free every allocated transfer. Only safe once libusb no longer owns any of them."""
        for ptr, _buf in list(self._transfers.values()):
            self._lib.libusb_free_transfer(ptr)
        self._transfers.clear()

    def __enter__(self) -> AsyncBulkReader:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- event loop --------------------------------------------------------

    def _run_events(self) -> None:
        """Dispatch libusb transfer completions until every transfer has retired."""
        tv = _Timeval(0, 50_000)  # 50 ms, so a stop() is noticed promptly
        completed = c_int(0)
        while True:
            with self._lock:
                live = self._live
            if live <= 0:
                break
            self._lib.libusb_handle_events_timeout_completed(
                self._ctx, ctypes.byref(tv), ctypes.byref(completed)
            )
        self._drained.set()
        if self._running and self._on_exhausted is not None:
            # Every transfer retired on its own (errors, device gone) -- not via stop().
            try:
                self._on_exhausted()
            except Exception:
                pass

    def _complete(self, ptr) -> None:
        """libusb completion callback. Runs on the event thread (or on whichever thread is
        handling events -- pyusb's synchronous command calls dispatch them too, which libusb
        explicitly supports)."""
        t = ptr.contents
        status = int(t.status)

        if status == _TRANSFER_COMPLETED:
            n = int(t.actual_length)
            if n:
                try:
                    buf = self._transfers[ctypes.addressof(t)][1]
                    self._on_data(memoryview(buf)[:n])
                except Exception as exc:
                    # A consumer error must not strand the transfer: resubmitting below is
                    # what keeps the endpoint attended, and dropping out here would silently
                    # shrink the rotation until the stream stalled.
                    self.callback_errors += 1
                    self.last_callback_error = exc
        elif status == _TRANSFER_CANCELLED or not self._running:
            with self._lock:
                self._live -= 1
            return
        else:
            keep = True
            if self._on_error is not None:
                try:
                    keep = bool(
                        self._on_error(status, _STATUS_NAMES.get(status, str(status)))
                    )
                except Exception:
                    keep = False
            if not keep:
                with self._lock:
                    self._live -= 1
                return

        if not self._running:
            with self._lock:
                self._live -= 1
            return

        if self._lib.libusb_submit_transfer(ptr) != 0:
            with self._lock:
                self._live -= 1
