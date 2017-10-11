import threading

class StoppableThread(threading.Thread):
    def __init__(self):
        super(StoppableThread, self).__init__(name=type(self).__name__)
        self._stop = threading.Event()

    @property
    def stop_requested(self):
        return self._stop.is_set()

    def stopThread(self):
        self._stop.set()

    def wait(self, timeout=None):
        return self._stop.wait(timeout)
