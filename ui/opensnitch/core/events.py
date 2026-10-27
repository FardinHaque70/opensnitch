"""Synchronous domain events; adapters own thread delivery and scheduling."""

class Event:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)
        return lambda: self._callbacks.remove(callback)

    def emit(self, *args):
        for callback in tuple(self._callbacks):
            callback(*args)
