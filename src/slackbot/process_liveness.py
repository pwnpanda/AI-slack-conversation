"""Process-existence check used to decide whether a CC session is still alive.

`os.kill(pid, 0)` is the canonical Unix "does this process exist" probe: it
delivers no signal, but raises ProcessLookupError if the PID is dead and
PermissionError if it exists under another uid. Both outcomes are useful here.
"""

from __future__ import annotations

import os


def pid_is_alive(pid: int | None) -> bool:
    """Return True if `pid` is a running process under our uid, False if not.

    None or non-positive PIDs return True ('unknown — do not block'), so that
    legacy registry rows without a recorded PID don't get marked dead. Real PID
    death is only asserted when we have positive evidence.
    """
    if pid is None or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists but is owned by a different uid. Treat as alive — we can't
        # signal it but it isn't dead.
        return True
    return True
