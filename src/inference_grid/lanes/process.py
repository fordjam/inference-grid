"""Wait for child exit without reaping its PID before process-group cleanup."""
from contextlib import closing
import errno
import os
import select
import signal
import subprocess
import time


def exited_without_reaping(proc, timeout):
    """Return False on timeout. Caller must not poll/wait before group cleanup."""
    if hasattr(select, 'kqueue'):
        with closing(select.kqueue()) as queue:
            event = select.kevent(proc.pid, filter=select.KQ_FILTER_PROC,
                                  flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                  fflags=select.KQ_NOTE_EXIT)
            try:
                events = queue.control([event], 1, timeout)
                for item in events:
                    if item.flags & select.KQ_EV_ERROR:
                        if item.data == errno.ESRCH:
                            return True
                        raise OSError(item.data, 'process exit monitor failed')
                return bool(events)
            except ProcessLookupError:
                # Already exited before registration; our unreaped child still
                # reserves its PID. wait() below will collect its exit status.
                return True
    if hasattr(os, 'waitid') and hasattr(os, 'WNOWAIT'):
        deadline = time.monotonic() + timeout
        while True:
            result = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            if result is not None:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))
    raise RuntimeError('non-reaping child supervision unavailable')


def signal_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin reports EPERM for a group containing only zombies. Verify
        # membership before ignoring it; a live unsignalled member is a failure.
        rows = subprocess.check_output(['/bin/ps', '-axo', 'pgid=,stat='], text=True)
        for row in rows.splitlines():
            fields = row.split()
            if len(fields) != 2:
                raise RuntimeError('unreadable process-group membership')
            if int(fields[0]) == proc.pid and not fields[1].startswith('Z'):
                raise RuntimeError('live process-group member could not be stopped')


def stop_group_before_reap(proc):
    """Keep the group leader's PID reserved through the final group signal."""
    if proc.returncode is not None:
        raise RuntimeError('child already reaped; group signalling refused')
    signal_group(proc, signal.SIGTERM)
    try:
        exited_without_reaping(proc, 0.2)
    finally:
        signal_group(proc, signal.SIGKILL)
        proc.wait()
    return proc.returncode
