import time
import queue
from typing import Callable, Optional, Dict, Any
from pypresence import Presence, exceptions
from concurrent.futures import ThreadPoolExecutor
from threading import Event



class DiscordRPCWrapper:
    def __init__(self, client_id: str, update_interval: float = 15.0):
        self.client_id = client_id
        self.update_interval = update_interval
        self._queue: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue()
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._running = Event()
        self._rpc: Optional[Presence] = None
        self._event_hooks: Dict[str, Callable[[Any], None]] = {}

    def hook(self, event_name: str):
        """Decorator for setting event hooks"""
        def decorator(func: Callable[[Any], None]):
            self._event_hooks[event_name] = func
            return func
        return decorator

    def start(self):
        self._running.set()
        self._executor.submit(self._run)

    def stop(self):
        self._running.clear()
        self._queue.put(None)
        if self._rpc:
            try:
                self._rpc.clear()
                self._rpc.close()
            except Exception:
                pass
        self._executor.shutdown(wait=True)

    def update_presence(self, **kwargs):
        self._queue.put(kwargs)

    def _run(self):
        while self._running.is_set():
            try:
                self._rpc = Presence(self.client_id)
                self._rpc.connect()
                if "on_ready" in self._event_hooks:
                    self._event_hooks["on_ready"]()
            except Exception as e:
                if "on_error" in self._event_hooks:
                    self._event_hooks["on_error"](e)
                time.sleep(5)
                continue

            while self._running.is_set():
                try:
                    payload = self._queue.get(timeout=self.update_interval)
                    if payload is None:
                        break
                    self._rpc.update(**payload)
                except queue.Empty:
                    continue
                except exceptions.InvalidID as e:
                    if "on_error" in self._event_hooks:
                        self._event_hooks["on_error"](e)
                    break
                except Exception as e:
                    if "on_error" in self._event_hooks:
                        self._event_hooks["on_error"](e)
                    break

            try:
                self._rpc.close()
            except Exception:
                pass

            time.sleep(2)