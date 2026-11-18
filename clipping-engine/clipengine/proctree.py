"""Recorder processes that are stopped as a whole TREE.

A live recorder is often not the process that writes the segment file: ``python -m yt_dlp`` runs its own ffmpeg
for live HLS, and package-manager shims (e.g. Chocolatey's ``ffmpeg.exe``) start the real ffmpeg as a child.
Stopping only the direct child leaves the real writer running: it keeps appending to the segment and, on
Windows, holds it open so the join fails with WinError 32 (Session 12, CI run 44283099213).

``RecorderProcess`` wraps ``subprocess.Popen`` with the same small interface the monitor uses
(``poll/wait/terminate/kill/pid/returncode``) and guarantees that every descendant is gone once the recorder has
been stopped or has exited:

* Windows: the process is created suspended, placed in a Job Object with KILL_ON_JOB_CLOSE, then resumed, so
  every descendant is in the job from its first instruction. Stopping terminates the job. If the engine itself
  dies, Windows closes the job handle and the recorders die with it (no invisible writers after a crash).
* POSIX: the process starts in its own session/process group; stopping signals the whole group.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time

log = logging.getLogger(__name__)

_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                      "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC_LIMIT), ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _BASIC_ACCOUNTING(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    _JobObjectBasicAccountingInformation = 1
    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)     # private instance: never alter global argtypes
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.QueryInformationJobObject.restype = wintypes.BOOL
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.c_void_p]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.restype = ctypes.c_long
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

    class _Job:
        def __init__(self) -> None:
            h = _k32.CreateJobObjectW(None, None)
            if not h:
                raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
            info = _EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(h, _JobObjectExtendedLimitInformation, ctypes.byref(info),
                                                ctypes.sizeof(info)):
                err = ctypes.get_last_error()
                _k32.CloseHandle(h)
                raise OSError(err, "SetInformationJobObject failed")
            self.h = h

        def assign(self, process_handle: int) -> bool:
            return bool(_k32.AssignProcessToJobObject(self.h, process_handle))

        def active(self) -> int:
            acc = _BASIC_ACCOUNTING()
            if not _k32.QueryInformationJobObject(self.h, _JobObjectBasicAccountingInformation, ctypes.byref(acc),
                                                  ctypes.sizeof(acc), None):
                return 0
            return int(acc.ActiveProcesses)

        def terminate(self) -> None:
            _k32.TerminateJobObject(self.h, 1)

        def close(self) -> None:
            if self.h:
                _k32.CloseHandle(self.h)
                self.h = None


class RecorderProcess:
    """``subprocess.Popen`` for a recorder whose whole process tree is stopped together (see module docstring)."""

    def __init__(self, cmd: list[str], *, grace: float = 3.0, **popen_kwargs) -> None:
        self.grace = grace              # how long descendants may finish after the top process exits by itself
        self._job = None
        self._reaped = False
        self._tree_ok = True
        if os.name == "nt":
            flags = popen_kwargs.pop("creationflags", 0) | _CREATE_NO_WINDOW
            try:
                self._job = _Job()
                flags |= _CREATE_SUSPENDED
            except OSError as e:
                log.warning("recorder: could not create a Job Object (%s); only the top process can be stopped", e)
                self._tree_ok = False
            self._p = subprocess.Popen(cmd, creationflags=flags, **popen_kwargs)
            if self._job is not None:
                handle = int(self._p._handle)      # CPython's process handle (stable across 3.10-3.13)
                if not self._job.assign(handle):
                    log.warning("recorder pid %s: could not join the Job Object (error %s); only the top process "
                                "can be stopped", self._p.pid, ctypes.get_last_error())
                    self._job.close()
                    self._job = None
                    self._tree_ok = False
                if _ntdll.NtResumeProcess(handle) != 0:     # never leave a recorder suspended
                    self._p.kill()
                    self._p.wait()
                    self._close_job()
                    raise OSError(f"could not resume recorder process {self._p.pid}")
        else:
            popen_kwargs.pop("creationflags", None)
            self._p = subprocess.Popen(cmd, start_new_session=True, **popen_kwargs)

    # -- Popen-compatible surface used by the monitor ---------------------------------------------------------
    @property
    def pid(self) -> int:
        return self._p.pid

    @property
    def returncode(self):
        return self._p.returncode

    @property
    def tree_controlled(self) -> bool:
        return self._tree_ok

    def poll(self):
        r = self._p.poll()
        if r is not None:
            self._reap(self.grace)
        return r

    def wait(self, timeout: float | None = None):
        r = self._p.wait(timeout=timeout)
        self._reap(self.grace)
        return r

    def terminate(self) -> None:
        self._stop(signal.SIGTERM)

    def kill(self) -> None:
        self._stop(signal.SIGKILL if os.name != "nt" else signal.SIGTERM)

    # -- tree handling --------------------------------------------------------------------------------------
    def _stop(self, sig) -> None:
        if os.name == "nt":
            if self._job is not None:
                self._job.terminate()          # every descendant, at once
            try:
                self._p.kill()                  # (TerminateProcess; harmless if the job already ended it)
            except OSError:
                pass
            return
        try:
            os.killpg(self._p.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                self._p.send_signal(sig)
            except OSError:
                pass

    def _tree_alive(self) -> bool:
        if os.name == "nt":
            return self._job is not None and self._job.active() > 0
        try:
            os.killpg(self._p.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _reap(self, grace: float) -> None:
        """The top process has exited: let descendants finish briefly, then stop them and wait until they are
        gone, so no handle on the segment survives the recorder (bounded; never blocks indefinitely)."""
        if self._reaped:
            return
        self._reaped = True
        end = time.monotonic() + max(0.0, grace)
        while self._tree_alive() and time.monotonic() < end:
            time.sleep(0.05)
        if self._tree_alive():
            log.warning("recorder pid %s exited but left child process(es) running; stopping them", self._p.pid)
            if os.name == "nt":
                self._job.terminate()
            else:
                try:
                    os.killpg(self._p.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            end = time.monotonic() + 10
            while self._tree_alive() and time.monotonic() < end:
                time.sleep(0.05)
            if self._tree_alive():
                log.error("recorder pid %s: child process(es) still running after being stopped", self._p.pid)
        self._close_job()

    def _close_job(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None
