"""Terminate an orphaned child even when its parent cannot run a finally block."""

import os
import signal
import threading
import time


def watch_parent(*, process_group=False):
    parent = os.getppid()

    def monitor():
        while True:
            time.sleep(0.1)
            if os.getppid() != parent or parent == 1:
                if process_group:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                os._exit(1)

    threading.Thread(target=monitor, daemon=True, name="parent-watch").start()
