"""Length-framed Windows named-pipe transport with cancellable I/O.

This module has no third-party dependency and imports Win32 only when used, so
the scanner's fail-closed and protocol tests remain runnable on other platforms.
"""

from __future__ import annotations

import ctypes
import json
import os
import struct
import time
from ctypes import wintypes

MAX_FRAME_BYTES = 64 * 1024


def request_json(pipe_name: str, envelope: dict, *, timeout_seconds: float) -> dict:
    """Send one request and receive one response before the deadline."""
    if os.name != "nt":
        raise OSError("Windows named pipes are required")
    encoded = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if not encoded or len(encoded) > MAX_FRAME_BYTES:
        raise ValueError("SUPERVISOR_REQUEST_TOO_LARGE")
    with _WindowsPipe(pipe_name, timeout_seconds) as pipe:
        pipe.write(struct.pack(">I", len(encoded)) + encoded)
        length = struct.unpack(">I", pipe.read_exact(4))[0]
        if not 0 < length <= MAX_FRAME_BYTES:
            raise ValueError("SUPERVISOR_RESPONSE_FRAME_INVALID")
        body = pipe.read_exact(length)
    response = json.loads(body.decode("utf-8"))
    if not isinstance(response, dict):
        raise TypeError("SUPERVISOR_RESPONSE_INVALID")
    return response


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class _WindowsPipe:
    def __init__(self, pipe_name: str, timeout_seconds: float) -> None:
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        self._api.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self._api.CreateFileW.restype = wintypes.HANDLE
        self._api.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        self._api.WaitNamedPipeW.restype = wintypes.BOOL
        self._api.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self._api.CreateEventW.restype = wintypes.HANDLE
        self._api.CloseHandle.argtypes = [wintypes.HANDLE]
        self._api.CloseHandle.restype = wintypes.BOOL
        self._api.ReadFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped),
        ]
        self._api.ReadFile.restype = wintypes.BOOL
        self._api.WriteFile.argtypes = self._api.ReadFile.argtypes
        self._api.WriteFile.restype = wintypes.BOOL
        self._api.GetOverlappedResult.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_Overlapped),
            ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        ]
        self._api.GetOverlappedResult.restype = wintypes.BOOL
        self._api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._api.WaitForSingleObject.restype = wintypes.DWORD
        self._api.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Overlapped)]
        self._api.CancelIoEx.restype = wintypes.BOOL
        self._deadline = time.monotonic() + max(timeout_seconds, 0.01)
        self._handle = None
        invalid_handle = ctypes.c_void_p(-1).value
        while True:
            handle = self._api.CreateFileW(
                pipe_name, 0xC0000000, 0, None, 3, 0x40000000, None,
            )
            if handle != invalid_handle:
                self._handle = handle
                break
            error = ctypes.get_last_error()
            if error not in {2, 5, 231}:  # NOT_FOUND, ACCESS_DENIED, PIPE_BUSY during reconnect gap
                raise ctypes.WinError(error)
            if time.monotonic() >= self._deadline:
                raise TimeoutError("scanner pipe connection timed out")
            if not self._api.WaitNamedPipeW(pipe_name, self._remaining_ms()):
                error = ctypes.get_last_error()
                if error in {2, 5, 121, 231}:  # transient gap or semaphore timeout
                    time.sleep(0.005)
                    continue
                if error == 0:
                    continue
                raise ctypes.WinError(error)

    def _remaining_ms(self) -> int:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("scanner pipe request timed out")
        return max(1, int(remaining * 1000))

    def _io(self, data: bytes | None, size: int) -> bytes | int:
        event = self._api.CreateEventW(None, True, False, None)
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())
        overlapped = _Overlapped(hEvent=event)
        if data is not None:
            # ``create_string_buffer(data, len(data))`` reserves space for a
            # trailing NUL and raises when the requested size equals the
            # payload length.  Win32 receives an exact byte buffer here.
            buffer = (ctypes.c_char * size).from_buffer_copy(data)
        else:
            buffer = ctypes.create_string_buffer(size)
        transferred = wintypes.DWORD()
        try:
            operation = self._api.WriteFile if data is not None else self._api.ReadFile
            complete = operation(self._handle, buffer, size, ctypes.byref(transferred), ctypes.byref(overlapped))
            if not complete:
                error = ctypes.get_last_error()
                if error != 997:  # ERROR_IO_PENDING
                    raise ctypes.WinError(error)
                try:
                    remaining_ms = self._remaining_ms()
                except TimeoutError:
                    self._api.CancelIoEx(self._handle, ctypes.byref(overlapped))
                    self._api.WaitForSingleObject(event, 0xFFFFFFFF)
                    raise
                wait = self._api.WaitForSingleObject(event, remaining_ms)
                if wait == 258:  # WAIT_TIMEOUT
                    # A timed-out coroutine is insufficient: the pending Win32
                    # operation must be cancelled before its buffer is freed.
                    self._api.CancelIoEx(self._handle, ctypes.byref(overlapped))
                    self._api.WaitForSingleObject(event, 0xFFFFFFFF)
                    raise TimeoutError("scanner pipe request timed out")
                if wait != 0:
                    raise ctypes.WinError(ctypes.get_last_error())
                if not self._api.GetOverlappedResult(self._handle, ctypes.byref(overlapped), ctypes.byref(transferred), False):
                    raise ctypes.WinError(ctypes.get_last_error())
            if transferred.value == 0:
                raise OSError("scanner pipe closed before response")
            return transferred.value if data is not None else buffer.raw[:transferred.value]
        finally:
            self._api.CloseHandle(event)

    def write(self, frame: bytes) -> None:
        remaining = frame
        while remaining:
            count = self._io(remaining, len(remaining))
            remaining = remaining[count:]

    def read_exact(self, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = self._io(None, remaining)
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        if self._handle:
            self._api.CloseHandle(self._handle)
            self._handle = None
