"""Worker threads for device and network work, with the window's busy guard.

Device jobs share one lane, because they all go through the single PodLink
serial connection: they run one at a time, foreground before background.
Network jobs each get a thread of their own. Results come back to the GUI
thread through a queued signal; nothing here blocks the event loop.
"""
from __future__ import annotations

import itertools
import queue
import threading
import traceback

from .qt import QtCore, Signal


class Cancelled(Exception):
    """Raised inside a job after its cancel() was requested."""


class Job:
    def __init__(self, runner: "JobRunner", job_id: int, label: str,
                 foreground: bool):
        self.runner = runner
        self.id = job_id
        self.label = label
        self.foreground = foreground
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def progress(self, fraction: float | None = None, text: str | None = None,
                 data: object = None) -> None:
        """Thread-safe; fraction None means indeterminate."""
        self.runner._progress.emit(self.id, fraction, text, data)


class JobRunner(QtCore.QObject):
    busy_changed = Signal(bool)
    activity = Signal(str, object)      # label, fraction or None; "" when idle
    _done = Signal(int, object, object)
    _progress = Signal(int, object, object, object)

    ERRORS = (OSError, RuntimeError, ValueError, TypeError, TimeoutError,
              KeyError, LookupError, AttributeError)

    def __init__(self, parent: QtCore.QObject | None = None):
        super().__init__(parent)
        self._ids = itertools.count(1)
        self._order = itertools.count()
        self._callbacks: dict[int, tuple] = {}
        self._jobs: dict[int, Job] = {}
        self._lane: queue.PriorityQueue = queue.PriorityQueue()
        self._lane_thread: threading.Thread | None = None
        self._busy = 0
        self._done.connect(self._finish, QtCore.Qt.ConnectionType.QueuedConnection)
        self._progress.connect(self._on_progress,
                               QtCore.Qt.ConnectionType.QueuedConnection)
        self.inline = False     # tests: run jobs synchronously

    @property
    def busy(self) -> bool:
        return self._busy > 0

    def jobs(self) -> list[Job]:
        return list(self._jobs.values())

    def run(self, label: str, fn, on_done=None, on_error=None, *,
            device: bool = True, foreground: bool = True,
            on_progress=None) -> Job:
        """Run fn(job) off the GUI thread; callbacks run on the GUI thread."""
        job = Job(self, next(self._ids), label, foreground)
        self._jobs[job.id] = job
        self._callbacks[job.id] = (on_done, on_error, on_progress)
        if foreground:
            self._busy += 1
            if self._busy == 1:
                self.busy_changed.emit(True)
            self.activity.emit(label, None)
        if self.inline:
            self._execute(job, fn)
            QtCore.QCoreApplication.processEvents()
        elif device:
            self._lane.put((0 if foreground else 1, next(self._order), job, fn))
            if self._lane_thread is None or not self._lane_thread.is_alive():
                self._lane_thread = threading.Thread(
                    target=self._drain, name="astrapod-device", daemon=True)
                self._lane_thread.start()
        else:
            threading.Thread(target=self._execute, args=(job, fn),
                             name="astrapod-net", daemon=True).start()
        return job

    def _drain(self) -> None:
        while True:
            try:
                _priority, _order, job, fn = self._lane.get(timeout=2.0)
            except queue.Empty:
                return
            self._execute(job, fn)

    def _execute(self, job: Job, fn) -> None:
        if job.cancelled:
            self._done.emit(job.id, None, Cancelled())
            return
        try:
            result = fn(job)
        except Cancelled as error:
            self._done.emit(job.id, None, error)
        except self.ERRORS as error:
            traceback.print_exc()
            self._done.emit(job.id, None, error)
        else:
            self._done.emit(job.id, result, None)

    def _on_progress(self, job_id, fraction, text, data) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        _done, _error, on_progress = self._callbacks.get(job_id, (None, None, None))
        if job.foreground:
            self.activity.emit(text or job.label, fraction)
        if on_progress is not None:
            on_progress(fraction, text, data)

    def _finish(self, job_id, result, error) -> None:
        job = self._jobs.pop(job_id, None)
        on_done, on_error, _progress = self._callbacks.pop(job_id, (None, None, None))
        if job is not None and job.foreground:
            self._busy -= 1
            if self._busy == 0:
                self.busy_changed.emit(False)
                self.activity.emit("", None)
            else:
                rest = [item for item in self._jobs.values() if item.foreground]
                if rest:
                    self.activity.emit(rest[-1].label, None)
        if error is not None:
            if on_error is not None:
                on_error(error)
        elif on_done is not None:
            on_done(result)
