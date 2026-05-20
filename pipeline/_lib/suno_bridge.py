"""WebSocket server the YTR Suno Bridge Chrome extension connects to.

Sync facade — `get_bridge()` starts the server (idempotent) and returns the
singleton. Pipeline code calls `wait_for_auth()` to get the Bearer + apiBase
from the extension. Optionally `fetch(...)` proxies a request through the
extension (i.e. inside a real suno.com tab).
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass

try:
    import websockets
    from websockets.server import serve as _ws_serve
except ImportError as e:  # noqa: F841
    websockets = None  # raised when bridge is first used


BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 18792


class SunoBridgeError(RuntimeError):
    pass


@dataclass
class Auth:
    bearer: str
    api_base: str
    age_ms: int


class SunoBridge:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None  # current extension WebSocket (asyncio object)
        self._pending: dict[int, Future] = {}
        self._next_id = 1
        self._started = threading.Event()
        self._connected = threading.Event()
        # Signals fired when matching extension events arrive — pipeline can
        # block on these instead of polling.
        self._template_event = threading.Event()
        self._bearer_event = threading.Event()

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            if websockets is None:
                raise SunoBridgeError(
                    "websockets package not installed. add `websockets>=12` to requirements.txt"
                )
            self._thread = threading.Thread(target=self._run, daemon=True, name="suno-bridge")
            self._thread.start()
            if not self._started.wait(timeout=5):
                raise SunoBridgeError("bridge thread didn't start within 5s")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        async def main() -> None:
            async with _ws_serve(self._handle, BRIDGE_HOST, BRIDGE_PORT):
                self._started.set()
                # Run forever
                await asyncio.Future()

        try:
            loop.run_until_complete(main())
        except Exception as e:  # noqa: BLE001
            print(f"[suno-bridge] server error: {e}")

    async def _handle(self, ws) -> None:
        # Only one extension at a time. New connection replaces old.
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = ws
        self._connected.set()
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                # Free-form events the extension pushes for visibility
                if msg.get("type") == "event":
                    import sys as _sys
                    text = msg.get("text", "")
                    print(f"[ext] {text}", file=_sys.stderr)
                    low = text.lower()
                    if "template saved" in low:
                        self._template_event.set()
                    if "bearer captured" in low:
                        self._bearer_event.set()
                    continue
                if isinstance(msg.get("id"), int) and msg["id"] in self._pending:
                    fut = self._pending.pop(msg["id"])
                    if not fut.done():
                        fut.set_result(msg)
        except Exception:
            pass
        finally:
            if self._ws is ws:
                self._ws = None
                self._connected.clear()

    # --- commands -----------------------------------------------------------

    def _send(self, cmd: dict, *, timeout: float = 30) -> dict:
        if not self._connected.is_set() or self._ws is None or self._loop is None:
            raise SunoBridgeError(
                "Chrome extension not connected. Install chrome-extension/ "
                "(chrome://extensions → Load unpacked) and keep Chrome running."
            )
        cmd_id = self._next_id
        self._next_id += 1
        cmd = {**cmd, "id": cmd_id}
        fut: Future = Future()
        self._pending[cmd_id] = fut

        async def do_send() -> None:
            await self._ws.send(json.dumps(cmd))  # type: ignore[union-attr]

        try:
            asyncio.run_coroutine_threadsafe(do_send(), self._loop).result(timeout=5)
        except Exception as e:
            self._pending.pop(cmd_id, None)
            raise SunoBridgeError(f"failed to send {cmd['cmd']}: {e}") from e

        try:
            return fut.result(timeout=timeout)
        except Exception as e:
            self._pending.pop(cmd_id, None)
            raise SunoBridgeError(f"timeout waiting for {cmd['cmd']} response") from e

    def wait_for_extension(self, timeout: float = 60) -> None:
        if not self._connected.wait(timeout):
            raise SunoBridgeError(
                "YTR Suno Bridge extension never connected. Install it from "
                "chrome-extension/ and make sure Chrome is open."
            )

    def wait_for_auth(self, timeout: float = 120) -> Auth:
        """Block until the extension reports a captured Bearer + apiBase."""
        deadline = time.monotonic() + timeout
        printed = False
        while time.monotonic() < deadline:
            self.wait_for_extension(timeout=max(0.1, deadline - time.monotonic()))
            try:
                r = self._send({"cmd": "getAuth"}, timeout=10)
            except SunoBridgeError:
                time.sleep(0.5)
                continue
            bearer = r.get("bearer")
            api_base = r.get("apiBase")
            if bearer and api_base:
                return Auth(bearer=bearer, api_base=api_base,
                            age_ms=int(r.get("age_ms") or 0))
            if not printed:
                print("[suno-bridge] waiting for Bearer — open https://suno.com/ "
                      "in your Chrome (any page that hits the API: /create, /me, /song/...)")
                printed = True
            time.sleep(1.0)
        raise SunoBridgeError(
            "no Bearer captured. Open suno.com in the Chrome where the YTR "
            "Suno Bridge extension is installed, then retry."
        )

    def get_auth(self) -> Auth | None:
        try:
            r = self._send({"cmd": "getAuth"}, timeout=10)
        except SunoBridgeError:
            return None
        bearer = r.get("bearer")
        api_base = r.get("apiBase")
        if not bearer:
            return None
        return Auth(bearer=bearer, api_base=api_base,
                    age_ms=int(r.get("age_ms") or 0))

    def get_generate_template(self) -> str | None:
        try:
            r = self._send({"cmd": "getGenerateTemplate"}, timeout=10)
        except SunoBridgeError:
            return None
        return r.get("template")

    def clear_generate_template(self) -> None:
        try:
            self._send({"cmd": "clearGenerateTemplate"}, timeout=5)
        except SunoBridgeError:
            pass
        self._template_event.clear()

    def reload_suno_tab(self) -> bool:
        try:
            r = self._send({"cmd": "reloadSunoTab"}, timeout=10)
            return bool(r.get("ok"))
        except SunoBridgeError:
            return False

    def wait_for_template(self, timeout: float = 180) -> str:
        """Return a template — block until extension captures one if needed."""
        tpl = self.get_generate_template()
        if tpl:
            return tpl
        import sys as _sys
        print(
            f"\n[bridge] no generate template captured yet.\n"
            f"         → open https://suno.com/create in your Chrome and click\n"
            f"           'Create' once (any prompt). The extension will capture\n"
            f"           the request shape; this script will pick up automatically.\n"
            f"         (waiting up to {int(timeout)}s)",
            file=_sys.stderr,
        )
        self._template_event.clear()
        if self._template_event.wait(timeout=timeout):
            tpl = self.get_generate_template()
            if tpl:
                return tpl
        raise SunoBridgeError(
            "template still missing. Click 'Create' on suno.com once, then re-run."
        )

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict | None = None,
        body: bytes | str | None = None,
        timeout: float = 120,
    ) -> tuple[int, dict, bytes]:
        """Proxy a request through the extension's open suno.com tab."""
        body_arg: str | None
        if isinstance(body, (bytes, bytearray)):
            body_arg = body.decode("utf-8", errors="replace")
        else:
            body_arg = body
        r = self._send(
            {"cmd": "fetch", "url": url, "method": method,
             "headers": headers or {}, "body": body_arg},
            timeout=timeout,
        )
        if "error" in r:
            raise SunoBridgeError(r["error"])
        body_b64 = r.get("body_b64") or ""
        return int(r["status"]), r.get("headers") or {}, base64.b64decode(body_b64)


_singleton: SunoBridge | None = None


def get_bridge() -> SunoBridge:
    global _singleton
    if _singleton is None:
        _singleton = SunoBridge()
        _singleton.start()
    return _singleton
