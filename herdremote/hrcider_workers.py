"""Bounded recovery for Cider worker loops; never logs exception bodies."""
import functools
import logging
import threading

log = logging.getLogger('herdremote.cider')


class WorkerErrors:
    def __init__(self):
        self.lock = threading.Lock()
        self.seen = set()

    def note(self, worker, error):
        key = worker
        with self.lock:
            if key in self.seen:
                return
            self.seen.add(key)
        # Do not include str(error), tracebacks, frames or request contents:
        # host exceptions can contain private URLs and credentials.
        log.error('Cider worker exception worker=%s type=%s; continuing', worker, type(error).__name__)


def run_worker(target, stop, name, errors=None):
    errors = errors if errors is not None else WorkerErrors()
    while not stop.is_set():
        try:
            return target()
        except Exception as error:
            errors.note(name, error)
            # Interruptible backoff prevents an unexpected fault from spinning.
            stop.wait(.25)


def survive(stop_attr='stop'):
    def decorate(method):
        @functools.wraps(method)
        def guarded(self, *args, **kwargs):
            return run_worker(lambda: method(self, *args, **kwargs),
                              getattr(self, stop_attr), method.__name__, self.worker_errors)
        return guarded
    return decorate
