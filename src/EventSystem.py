import json
import os
import threading
import asyncio
import websockets
import logging
from typing import Callable, Dict, Any, Awaitable

class EventLogger:
    def __init__(self, name: str, filename: str):
        # Make logs directory once
        self.logs_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(self.logs_dir, exist_ok=True)

        log_path = os.path.join(self.logs_dir, filename)

        self.logger = logging.getLogger(name)
        if not self.logger.handlers:  # avoid adding multiple handlers
            self.logger.setLevel(logging.INFO)

            handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
            formatter = logging.Formatter(
                "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
                "%H:%M:%S"
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

        self.logger.info(f"[Logger] Initialized for {name} -> {log_path}")

# --------------------------
# Async Client (frontend)
# --------------------------
class ClientEvent(EventLogger):
    def __init__(self, uri: str):
        super().__init__("ClientEvent", "ClientEvent.log")

        self.uri = uri
        self.event_handlers: Dict[str, Callable[[Any, str], Awaitable[None]]] = {}
        self.websocket = None
        self._connected = asyncio.Event()

    def on(self, event_name: str):
        """Decorator for async event handlers."""
        def decorator(func):
            self.event_handlers[event_name] = func
            return func
        return decorator

    async def connect(self):
        self.websocket = await websockets.connect(self.uri)
        self._connected.set()
        self.logger.info(f"[Client] Connected to {self.uri}")
        asyncio.create_task(self._listen())

    async def _listen(self):
        async for message in self.websocket:
            try:
                payload = json.loads(message)
                event = payload.get("event")
                data = payload.get("data")
                source = payload.get("source", "unknown")

                if event in self.event_handlers:
                    await self.event_handlers[event](data, source)
                else:
                    self.logger.warning(f"[Client] Unhandled event: {event}")
            except Exception as e:
                self.logger.exception(f"[Client ERROR] {e}")

    async def emit(self, event: str, data: Any = None, source="frontend"):
        await self._connected.wait()
        msg = json.dumps({"event": event, "data": data, "source": source} if data else {"event": event,
                                                                                        "source": source})
        await self.websocket.send(msg)
        self.logger.debug(f"[Client] Emitted event '{event}' with {data}")

    async def close(self):
        if self.websocket:
            await self.websocket.close()
            self.logger.info("[Client] Closed connection")

        self._connected.clear()


# --------------------------
# Sync Server (backend)
# --------------------------
class ServerEvent(EventLogger):
    def __init__(self, host="localhost", port=8765):
        super().__init__("ServerEvent", "ServerEvent.log")

        self.host = host
        self.port = port
        self.clients = set()
        self.loop = asyncio.new_event_loop()
        self.server_thread = threading.Thread(target=self._run_server, daemon=True)
        self.event_handlers: Dict[str, Callable[[dict], object]] = {}

    # --------------------------
    # Event registration (sync)
    # --------------------------
    def on(self, event_name: str):
        """Register a synchronous handler for a specific event."""
        def decorator(func: Callable[[dict], object]):
            self.event_handlers[event_name] = func
            return func
        return decorator

    # --------------------------
    # Server lifecycle
    # --------------------------
    def start(self):
        self.logger.info(f"[Server] Starting on ws://{self.host}:{self.port}")
        self.server_thread.start()

    def _run_server(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._serve())
        self.loop.run_forever()

    async def _serve(self):
        async def handler(websocket):
            self.clients.add(websocket)
            self.logger.info(f"[Server] Client connected ({len(self.clients)} total)")
            try:
                async for message in websocket:
                    await self._process_message(websocket, message)
            finally:
                self.clients.remove(websocket)
                self.logger.info(f"[Server] Client disconnected ({len(self.clients)} left)")

        await websockets.serve(handler, self.host, self.port)

    # --------------------------
    # Message handling
    # --------------------------
    async def _process_message(self, websocket, message: str):
        try:
            payload = json.loads(message)
            event = payload.get("event")
            data = payload.get("data")
            source = payload.get("source", "unknown")

            self.logger.debug(f"[Server] Received event '{event}' from {source}: {data}")

            if event in self.event_handlers:
                handler = self.event_handlers[event]
                threading.Thread(
                    target=self._call_handler,
                    args=(handler, event, data, websocket),
                    daemon=True,
                ).start()
            else:
                self.logger.warning(f"[Server] No handler for event '{event}'")
        except json.JSONDecodeError:
            self.logger.error("[Server] Invalid JSON received")

    def _call_handler(self, handler, event, data, websocket):
        try:
            result = handler(data)
            if result is not None:
                response = json.dumps({
                    "event": f"{event}_response",
                    "data": result,
                    "source": "server"
                })
                asyncio.run_coroutine_threadsafe(websocket.send(response), self.loop)
                self.logger.debug(f"[Server] Responded to '{event}' with {result}")
        except Exception as e:
            self.logger.exception(f"[Handler ERROR] {event}: {e}")

    # --------------------------
    # Emit events (to client)
    # --------------------------
    def emit(self, event: str, data: dict, source="server"):
        payload = json.dumps({"event": event, "data": data, "source": source})
        asyncio.run_coroutine_threadsafe(self._broadcast(payload), self.loop)
        self.logger.debug(f"[Server] Broadcasted event '{event}' with {data}")

    async def _broadcast(self, message: str):
        for ws in list(self.clients):
            try:
                await ws.send(message)
            except:
                self.clients.remove(ws)


    # --------------------------
    # close clients
    # --------------------------
    async def _close_clients(self):
        """Gracefully close all connected WebSocket clients."""
        for ws in list(self.clients):
            try:
                await ws.close()
            except Exception as e:
                self.logger.exception(f"[Server] Error closing client: {e}")
        self.clients.clear()

    # --------------------------
    # Stop server
    # --------------------------
    def stop(self):
        self.logger.info("[Server] Stopping...")

        async def shutdown():
            self.logger.info("[Server] Closing clients...")
            await self._close_clients()
            self.logger.info("[Server] Cancelling pending tasks...")
            tasks = [t for t in asyncio.all_tasks(self.loop) if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        self.loop.call_later(0.5, self.loop.stop)
        if self.server_thread.is_alive():
            self.server_thread.join(timeout=1)
        self.logger.info("[Server] Stopped.")


