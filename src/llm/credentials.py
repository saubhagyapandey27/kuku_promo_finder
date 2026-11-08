"""Explicit job-owned credentials, shared by that job's worker threads only."""
import threading
from .base import CapabilityError, TruncatedError
from ..util import ModerationBlock, PersistentRateLimit


class JobCredentials:
    def __init__(self, values):
        self._values = dict(values)
        self._lock = threading.RLock()
        self._backends = []
        self._closed = False

    def get(self, name):
        with self._lock:
            return self._values.get(name)

    def redact(self, text):
        with self._lock:
            if self._closed: return '[job credentials discarded]'
            for secret in self._values.values():
                if secret: text = text.replace(secret, '[credential redacted]')
        return text

    def wrap(self, backend):
        context = self
        self._backends.append(backend)
        class Guarded:
            def call(self, *args, **kwargs):
                try:
                    raw = backend.call(*args, **kwargs)
                    raw.text = context.redact(raw.text)
                    return raw
                except Exception as exc:
                    message = context.redact(str(exc))
                    raw = getattr(exc, 'raw', None)
                    if raw: raw.text = context.redact(raw.text)
                    if isinstance(exc, TruncatedError): clean = TruncatedError(message, raw)
                    elif isinstance(exc, CapabilityError): clean = CapabilityError(message)
                    elif isinstance(exc, ModerationBlock): clean = ModerationBlock(message)
                    elif isinstance(exc, PersistentRateLimit): clean = PersistentRateLimit(message)
                    else: clean = RuntimeError(message)
                    raise clean from None
        return Guarded()

    def clear(self):
        # Remove references and close SDK transports; Python cannot promise memory zeroization.
        with self._lock:
            self._closed = True
            self._values.clear()
        for backend in self._backends:
            backend.close()
        self._backends.clear()

    def __repr__(self):
        return 'JobCredentials(<redacted>)'
