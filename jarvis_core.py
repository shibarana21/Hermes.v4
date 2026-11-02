#!/usr/bin/env python3
"""
J.A.R.V.I.S. CORE  —  companion server for the Hermes Chat UI v4
=================================================================
One process that:
  * serves the UI (same origin -> no CORS needed)
  * reverse-proxies Hermes at /hermes/*  (API key stays on the server, never in the browser)
  * writes an audit log of every tool call + approval (JSONL)
  * enforces approval RULES (auto-deny / auto-allow regex) before you even see the card
  * /core/sys   live CPU / RAM / disk / net / battery / temps / top processes  (psutil)
  * /core/stt   offline speech-to-text   (faster-whisper)          [optional]
  * /core/tts   offline text-to-speech   (Piper, Hindi voices)     [optional]
  * /core/wake  "hey jarvis" wake word on this PC's mic (openWakeWord + sounddevice) [optional]
  * /core/file  read-only file viewer limited to --file-root folders [optional]

Install (minimum):   pip install aiohttp psutil
Voice (optional):    pip install faster-whisper piper-tts
                     python -m piper.download_voices hi_IN-pratham-medium --data-dir ./voices
Wake (optional):     pip install openwakeword sounddevice

Run:                 python jarvis_core.py            -> open http://localhost:8800
Phone on Wi-Fi:      python jarvis_core.py --host 0.0.0.0 --https --token MYSECRET
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import ssl
import sys
import time
import wave
from pathlib import Path

try:
    from aiohttp import web, ClientSession, ClientTimeout
except ImportError:  # pragma: no cover
    sys.exit("aiohttp missing ->  pip install aiohttp psutil")

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

HERE = Path(__file__).resolve().parent
VERSION = "4.0"
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-length", "content-encoding", "host"}


# ------------------------------------------------------------------ helpers
def read_hermes_env() -> dict:
    """Pick API_SERVER_KEY / PORT from ~/.hermes/.env so zero config is needed."""
    out = {}
    p = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / ".env"
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def now() -> float:
    return round(time.time(), 3)


class Audit:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, kind: str, **data):
        rec = {"ts": now(), "kind": kind, **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def tail(self, n=200, kind=None):
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if kind and r.get("kind") != kind:
                continue
            out.append(r)
            if len(out) >= n:
                break
        return out


DEFAULT_RULES = {
    "enabled": True,
    # auto-DENY: never even ask
    "deny": [r"rm\s+-rf\s+/(\s|$)", r"mkfs", r"dd\s+if=.*of=/dev/", r":\(\)\s*\{", r"shutdown|reboot|poweroff",
             r"format\s+[a-z]:", r"del\s+/s\s+/q\s+c:\\"],
    # auto-ALLOW once: harmless read-only stuff
    "allow": [r"^\s*(ls|dir|pwd|whoami|date|uptime|df -h|free -h|cat\s+[\w./-]+\.(txt|md|log))\s*$"],
}


class Rules:
    def __init__(self, path: Path):
        self.path = path
        self.data = dict(DEFAULT_RULES)
        try:
            self.data.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")

    def update(self, d: dict):
        for k in ("deny", "allow"):
            if k in d:
                lst = [str(x) for x in d[k] if str(x).strip()]
                for rx in lst:
                    re.compile(rx)  # validate -> raises re.error
                self.data[k] = lst
        if "enabled" in d:
            self.data["enabled"] = bool(d["enabled"])
        self.save()

    def verdict(self, command: str):
        if not self.data.get("enabled") or not command:
            return None, None
        for rx in self.data.get("deny", []):
            if re.search(rx, command, re.I):
                return "deny", rx
        for rx in self.data.get("allow", []):
            if re.search(rx, command, re.I):
                return "once", rx
        return None, None


# ------------------------------------------------------------------ optional engines
class STT:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self.model = None
        self.error = None
        try:
            import faster_whisper  # noqa: F401
            self.available = True
        except Exception as e:  # pragma: no cover
            self.available, self.error = False, f"faster-whisper not installed ({e.__class__.__name__})"

    def _load(self):
        if self.model is None:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        return self.model

    def transcribe(self, wav_bytes: bytes, lang: str | None):
        import numpy as np
        with wave.open(io.BytesIO(wav_bytes)) as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if sw != 2:
            raise ValueError("send 16-bit PCM WAV")
        a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
        if sr != 16000:
            n = int(len(a) * 16000 / sr)
            a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
        t = time.time()
        segs, info = self._load().transcribe(a, language=(lang or None), vad_filter=True, beam_size=1)
        text = "".join(s.text for s in segs).strip()
        return {"text": text, "language": info.language, "prob": round(info.language_probability, 3),
                "secs": round(time.time() - t, 2), "audio_secs": round(len(a) / 16000, 2)}


class TTS:
    def __init__(self, voice_dir: Path, default_voice: str):
        self.voice_dir, self.default = voice_dir, default_voice
        self.cache = {}
        self.error = None
        try:
            import piper  # noqa: F401
            self.available = True
        except Exception as e:  # pragma: no cover
            self.available, self.error = False, f"piper-tts not installed ({e.__class__.__name__})"

    def voices(self):
        return sorted(p.stem for p in self.voice_dir.glob("*.onnx")) if self.voice_dir.exists() else []

    def synth(self, text: str, voice: str | None, rate: float = 1.0) -> bytes:
        from piper import PiperVoice
        try:
            from piper import SynthesisConfig
        except ImportError:  # older piper
            SynthesisConfig = None
        name = voice or self.default
        if name not in self.voices():
            name = (self.voices() or [None])[0]
        if not name:
            raise FileNotFoundError(f"no Piper voice in {self.voice_dir} - run: python -m piper.download_voices "
                                    f"{self.default} --data-dir {self.voice_dir}")
        if name not in self.cache:
            self.cache[name] = PiperVoice.load(str(self.voice_dir / f"{name}.onnx"))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            if SynthesisConfig and rate and abs(rate - 1) > 0.01:
                self.cache[name].synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=1 / max(0.3, rate)))
            else:
                self.cache[name].synthesize_wav(text, w)
        return buf.getvalue()


class Wake:
    """openWakeWord on the server mic -> pushes events to SSE subscribers."""

    def __init__(self, model: str, threshold: float):
        self.model_name, self.threshold = model, threshold
        self.subs: set[asyncio.Queue] = set()
        self.running = False
        self.error = None
        self.last = 0.0
        try:
            import openwakeword  # noqa: F401
            import sounddevice  # noqa: F401
            self.available = True
        except Exception as e:
            self.available, self.error = False, f"openwakeword/sounddevice missing ({e.__class__.__name__})"

    def start(self, loop: asyncio.AbstractEventLoop):
        if not self.available or self.running:
            return
        import threading
        self.running = True
        threading.Thread(target=self._run, args=(loop,), daemon=True).start()

    def _run(self, loop):
        try:
            import numpy as np
            import sounddevice as sd
            from openwakeword.model import Model
            try:
                import openwakeword.utils as u
                u.download_models([self.model_name])
            except Exception:
                pass
            m = Model(wakeword_models=[self.model_name], inference_framework="onnx")
            with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1280) as s:
                while self.running:
                    block, _ = s.read(1280)
                    scores = m.predict(np.squeeze(block))
                    for name, sc in scores.items():
                        if sc >= self.threshold and time.time() - self.last > 2:
                            self.last = time.time()
                            ev = {"word": name, "score": round(float(sc), 3), "ts": now()}
                            loop.call_soon_threadsafe(self._push, ev)
        except Exception as e:
            self.error, self.running = f"{e.__class__.__name__}: {e}", False

    def _push(self, ev):
        for q in list(self.subs):
            q.put_nowait(ev)


# ------------------------------------------------------------------ app
class Core:
    def __init__(self, a):
        env = read_hermes_env()
        self.hermes = a.hermes.rstrip("/")
        if self.hermes.endswith("/v1"):
            self.hermes = self.hermes[:-3]
        self.key = os.environ.get("HERMES_API_KEY") or env.get("API_SERVER_KEY", "")
        self.token = a.token or os.environ.get("JARVIS_CORE_TOKEN", "")
        # Capacitor app (http://localhost) / dusre origins ke liye CORS
        self.cors = set(a.cors or []) | {"http://localhost", "https://localhost", "capacitor://localhost"}
        self.ui = Path(a.ui_dir).resolve()
        data = Path(a.data_dir).expanduser().resolve()
        self.audit = Audit(data / "audit.jsonl")
        self.rules = Rules(data / "rules.json")
        self.stt = STT(a.whisper) if not a.no_voice else None
        self.tts = TTS(Path(a.voice_dir).expanduser().resolve(), a.voice) if not a.no_voice else None
        self.wake = Wake(a.wake_model, a.wake_threshold) if a.wake else None
        self.roots = [Path(r).expanduser().resolve() for r in (a.file_root or [])]
        self.http: ClientSession | None = None
        self._net = None
        self.started = time.time()

    # ---- auth -----------------------------------------------------------
    def _cors_ok(self, origin):
        if not origin:
            return False
        if "*" in self.cors:
            return True
        return origin in self.cors

    @web.middleware
    async def guard(self, request, handler):
        p = request.path
        origin = request.headers.get("Origin", "")
        cors = self._cors_ok(origin) and (p.startswith("/core") or p.startswith("/hermes"))
        if cors and request.method == "OPTIONS":
            return web.Response(status=204, headers={
                "Access-Control-Allow-Origin": origin, "Vary": "Origin",
                "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": request.headers.get("Access-Control-Request-Headers", "Content-Type, X-Core-Token"),
                "Access-Control-Max-Age": "600"})
        if cors:
            request["cors_origin"] = origin
        return await self._guarded(request, handler, p)

    async def _cors_hdr(self, request, resp):
        o = request.get("cors_origin")
        if o:
            resp.headers["Access-Control-Allow-Origin"] = o
            resp.headers["Vary"] = "Origin"

    async def _guarded(self, request, handler, p):
        if self.token and (p.startswith("/core") or p.startswith("/hermes")) and p != "/core/health":
            got = request.headers.get("X-Core-Token") or request.query.get("token", "")
            if got != self.token:
                return web.json_response({"error": "core token required (Settings -> CORE TOKEN)"}, status=401)
        return await handler(request)

    async def on_start(self, app):
        self.http = ClientSession(timeout=ClientTimeout(total=None, sock_connect=10))
        if self.wake:
            self.wake.start(asyncio.get_running_loop())

    async def on_stop(self, app):
        if self.wake:
            self.wake.running = False
        if self.http:
            await self.http.close()

    # ---- health ---------------------------------------------------------
    async def health(self, request):
        hermes_ok = False
        try:
            async with self.http.get(self.hermes + "/health", timeout=ClientTimeout(total=3)) as r:
                hermes_ok = r.status == 200
        except Exception:
            pass
        mods = {
            "proxy": {"ok": hermes_ok, "target": self.hermes, "key": bool(self.key)},
            "sys": {"ok": psutil is not None},
            "stt": {"ok": bool(self.stt and self.stt.available), "model": self.stt and self.stt.model_name,
                    "err": self.stt and self.stt.error},
            "tts": {"ok": bool(self.tts and self.tts.available and self.tts.voices()),
                    "voices": self.tts.voices() if self.tts else [],
                    "err": (self.tts.error if self.tts and self.tts.error else
                            (None if not self.tts or self.tts.voices() else "no voice downloaded"))},
            "wake": {"ok": bool(self.wake and self.wake.running), "err": self.wake.error if self.wake else "disabled (--wake)"},
            "file": {"ok": bool(self.roots), "roots": [str(r) for r in self.roots]},
            "rules": {"ok": self.rules.data.get("enabled", False)},
        }
        return web.json_response({"core": VERSION, "uptime": round(time.time() - self.started),
                                  "auth": bool(self.token), "modules": mods})

    # ---- proxy ----------------------------------------------------------
    async def proxy(self, request: web.Request):
        tail = request.match_info["tail"]
        url = f"{self.hermes}/{tail}"
        if request.query_string:
            url += "?" + request.query_string
        fwd = {k: v for k, v in request.headers.items()
               if k.lower() in ("content-type", "idempotency-key", "x-hermes-session-id", "accept", "last-event-id")}
        if self.key:
            fwd["Authorization"] = "Bearer " + self.key
        body = await request.read()
        if request.method == "POST" and tail.endswith("/approval"):
            try:
                self.audit.log("approval.decision", path=tail, **json.loads(body or b"{}"), by="operator")
            except ValueError:
                pass
        try:
            up = await self.http.request(request.method, url, data=body or None, headers=fwd)
        except Exception as e:
            return web.json_response({"error": {"message": f"Hermes unreachable at {self.hermes}: {e}"}}, status=502)
        ctype = up.headers.get("Content-Type", "")
        hdrs = {k: v for k, v in up.headers.items() if k.lower() not in HOP}
        if "text/event-stream" not in ctype:
            data = await up.read()
            up.release()
            return web.Response(status=up.status, body=data, headers=hdrs)
        # ---- streaming SSE passthrough with audit + rules
        resp = web.StreamResponse(status=up.status, headers={**hdrs, "Cache-Control": "no-cache",
                                                             "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        buf, ev_name = b"", None
        try:
            async for chunk in up.content.iter_any():
                buf += chunk
                while b"\n\n" in buf:
                    frame, buf = buf.split(b"\n\n", 1)
                    await resp.write(frame + b"\n\n")
                    extra = await self._inspect(frame, tail)
                    if extra:
                        await resp.write(extra)
            if buf:
                await resp.write(buf)
        except asyncio.CancelledError:
            up.close()  # browser hung up -> Hermes sees disconnect -> run interrupted
            raise
        except ConnectionResetError:
            up.close()
            pass  # client disconnected (ESC / tab closed)
            return resp
        finally:
            up.release()
        try:
            await resp.write_eof()
        except ConnectionResetError:
            pass
        return resp

    async def _inspect(self, frame: bytes, tail: str):
        ev, data = None, []
        for line in frame.decode("utf-8", "replace").splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if not data:
            return None
        try:
            d = json.loads("\n".join(data))
        except ValueError:
            return None
        if not isinstance(d, dict):
            return None
        sid = d.get("session_id")
        if ev in ("tool.started", "tool.completed", "tool.failed"):
            self.audit.log(ev, tool=d.get("tool_name"), preview=str(d.get("preview") or "")[:400],
                           args=str(d.get("args") or "")[:400], session=sid, run=d.get("run_id"))
        elif ev == "hermes.tool.progress" and d.get("status") in ("running", "completed"):
            self.audit.log("tool." + ("started" if d["status"] == "running" else "completed"),
                           tool=d.get("tool"), preview=str(d.get("label") or "")[:400], route=tail)
        elif ev in ("run.failed", "error"):
            self.audit.log(ev, message=str(d.get("message") or d.get("error") or "")[:400], session=sid)
        elif ev == "approval.request":
            cmd = str(d.get("command") or "")
            self.audit.log("approval.request", command=cmd[:600], description=str(d.get("description") or "")[:300],
                           run=d.get("run_id"), session=sid)
            verdict, rx = self.rules.verdict(cmd)
            if verdict and d.get("run_id"):
                if verdict not in (d.get("choices") or ["once", "deny"]):
                    verdict = "deny"
                ok = False
                try:
                    async with self.http.post(f"{self.hermes}/v1/runs/{d['run_id']}/approval",
                                              json={"choice": verdict, "request_id": d.get("request_id")},
                                              headers={"Authorization": "Bearer " + self.key} if self.key else {}) as r:
                        ok = r.status < 300
                except Exception:
                    pass
                self.audit.log("approval.decision", choice=verdict, rule=rx, command=cmd[:600], by="rule", ok=ok,
                               run=d.get("run_id"))
                out = {"choice": verdict, "rule": rx, "command": cmd, "ok": ok, "run_id": d.get("run_id"),
                       "request_id": d.get("request_id")}
                return f"event: core.rule\ndata: {json.dumps(out, ensure_ascii=False)}\n\n".encode()
        return None

    # ---- system ---------------------------------------------------------
    async def sys(self, request):
        if psutil is None:
            return web.json_response({"error": "pip install psutil"}, status=501)
        vm, du = psutil.virtual_memory(), psutil.disk_usage(str(Path.home().anchor or "/"))
        n = psutil.net_io_counters()
        t = time.time()
        up = down = 0.0
        if self._net:
            dt = max(0.001, t - self._net[0])
            up, down = (n.bytes_sent - self._net[1]) / dt, (n.bytes_recv - self._net[2]) / dt
        self._net = (t, n.bytes_sent, n.bytes_recv)
        bat = None
        try:
            b = psutil.sensors_battery()
            if b:
                bat = {"pct": round(b.percent), "plugged": b.power_plugged}
        except Exception:
            pass
        temps = {}
        try:
            for k, arr in (psutil.sensors_temperatures() or {}).items():
                if arr:
                    temps[k] = round(max(x.current for x in arr), 1)
        except Exception:
            pass
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                procs.append(p.info)
            except Exception:
                pass
        procs.sort(key=lambda x: (x.get("cpu_percent") or 0, x.get("memory_percent") or 0), reverse=True)
        return web.json_response({
            "cpu": psutil.cpu_percent(interval=None), "cores": psutil.cpu_percent(interval=None, percpu=True),
            "ram": {"pct": vm.percent, "used": vm.used, "total": vm.total},
            "disk": {"pct": du.percent, "used": du.used, "total": du.total},
            "net": {"up": round(up), "down": round(down)}, "battery": bat, "temps": temps,
            "boot": psutil.boot_time(), "load": os.getloadavg() if hasattr(os, "getloadavg") else None,
            "procs": [{"pid": p["pid"], "name": p["name"], "cpu": round(p.get("cpu_percent") or 0, 1),
                       "mem": round(p.get("memory_percent") or 0, 1)} for p in procs[:8]],
        })

    # ---- voice ----------------------------------------------------------
    async def stt_h(self, request):
        if not (self.stt and self.stt.available):
            return web.json_response({"error": "STT offline: pip install faster-whisper"}, status=501)
        audio = await request.read()
        if len(audio) < 1000:
            return web.json_response({"error": "audio too short"}, status=400)
        lang = request.query.get("lang", "").split("-")[0] or None
        try:
            res = await asyncio.get_running_loop().run_in_executor(None, self.stt.transcribe, audio, lang)
        except Exception as e:
            return web.json_response({"error": f"{e.__class__.__name__}: {e}"}, status=400)
        return web.json_response(res)

    async def tts_h(self, request):
        if not (self.tts and self.tts.available):
            return web.json_response({"error": "TTS offline: pip install piper-tts"}, status=501)
        d = await request.json()
        text = str(d.get("text", ""))[:4000].strip()
        if not text:
            return web.json_response({"error": "empty text"}, status=400)
        try:
            wav = await asyncio.get_running_loop().run_in_executor(
                None, self.tts.synth, text, d.get("voice"), float(d.get("rate") or 1))
        except Exception as e:
            return web.json_response({"error": f"{e.__class__.__name__}: {e}"}, status=400)
        return web.Response(body=wav, content_type="audio/wav")

    async def wake_h(self, request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        if not (self.wake and self.wake.running):
            err = self.wake.error if self.wake else "start core with --wake"
            await resp.write(f"event: wake.off\ndata: {json.dumps({'error': err})}\n\n".encode())
            return resp
        q: asyncio.Queue = asyncio.Queue()
        self.wake.subs.add(q)
        await resp.write(b"event: wake.ready\ndata: {}\n\n")
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    await resp.write(f"event: wake\ndata: {json.dumps(ev)}\n\n".encode())
                except TimeoutError:
                    await resp.write(b": keepalive\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.wake.subs.discard(q)
        return resp

    # ---- audit / rules / file -------------------------------------------
    async def audit_h(self, request):
        n = min(1000, int(request.query.get("n", 200)))
        return web.json_response({"data": self.audit.tail(n, request.query.get("kind"))})

    async def rules_get(self, request):
        return web.json_response(self.rules.data)

    async def rules_put(self, request):
        try:
            self.rules.update(await request.json())
        except re.error as e:
            return web.json_response({"error": f"bad regex: {e}"}, status=400)
        self.audit.log("rules.update", by="operator")
        return web.json_response(self.rules.data)

    async def file_h(self, request):
        if not self.roots:
            return web.json_response({"error": "file viewer off: start core with --file-root <folder>"}, status=501)
        p = Path(request.query.get("path", "")).expanduser()
        try:
            p = p.resolve()
        except OSError:
            return web.json_response({"error": "bad path"}, status=400)
        if not any(p == r or r in p.parents for r in self.roots):
            return web.json_response({"error": "outside allowed --file-root"}, status=403)
        if p.is_dir():
            items = sorted(({"name": c.name, "dir": c.is_dir(), "size": c.stat().st_size if c.is_file() else 0}
                            for c in p.iterdir() if not c.name.startswith(".")), key=lambda x: (not x["dir"], x["name"]))
            return web.json_response({"path": str(p), "dir": True, "items": items[:500]})
        if not p.is_file():
            return web.json_response({"error": "not found"}, status=404)
        if p.stat().st_size > 512_000:
            return web.json_response({"error": "file > 500 KB"}, status=413)
        return web.json_response({"path": str(p), "dir": False, "text": p.read_text("utf-8", "replace")})

    # ---- static ---------------------------------------------------------
    async def index(self, request):
        return web.FileResponse(self.ui / "index.html", headers={"Cache-Control": "no-cache"})

    def build(self):
        app = web.Application(middlewares=[self.guard], client_max_size=40 * 1024 * 1024)
        app.on_response_prepare.append(self._cors_hdr)
        app.on_startup.append(self.on_start)
        app.on_cleanup.append(self.on_stop)
        r = app.router
        r.add_get("/core/health", self.health)
        r.add_get("/core/sys", self.sys)
        r.add_post("/core/stt", self.stt_h)
        r.add_post("/core/tts", self.tts_h)
        r.add_get("/core/wake", self.wake_h)
        r.add_get("/core/audit", self.audit_h)
        r.add_get("/core/rules", self.rules_get)
        r.add_put("/core/rules", self.rules_put)
        r.add_get("/core/file", self.file_h)
        r.add_route("*", "/hermes/{tail:.*}", self.proxy)
        r.add_get("/", self.index)
        r.add_static("/", self.ui, show_index=False)
        return app


def self_signed(dirp: Path):
    """Create cert.pem/key.pem once (needs `cryptography`, else falls back to openssl CLI)."""
    cert, key = dirp / "cert.pem", dirp / "key.pem"
    if cert.exists() and key.exists():
        return cert, key
    dirp.mkdir(parents=True, exist_ok=True)
    try:
        import datetime
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "jarvis.local")])
        c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
             .serial_number(x509.random_serial_number())
             .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
             .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=825))
             .sign(k, hashes.SHA256()))
        key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                        serialization.NoEncryption()))
        cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    except ImportError:
        os.system(f'openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=jarvis.local" '
                  f'-keyout "{key}" -out "{cert}"')
    return cert, key


def main():
    ap = argparse.ArgumentParser(description="J.A.R.V.I.S. core for Hermes Chat UI v4")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to allow phone on same Wi-Fi")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--hermes", default=os.environ.get("HERMES_URL", "http://127.0.0.1:8642"))
    ap.add_argument("--cors", action="append", default=[], help="extra allowed Origin for /core & /hermes (repeatable, '*' = any). Phone app ke liye http://localhost default allowed hai")
    ap.add_argument("--token", default="", help="require X-Core-Token (strongly advised with --host 0.0.0.0)")
    ap.add_argument("--ui-dir", default=str(HERE))
    ap.add_argument("--data-dir", default=str(Path.home() / ".jarvis-core"))
    ap.add_argument("--whisper", default="small", help="tiny|base|small|medium|large-v3 (small = good Hindi on CPU)")
    ap.add_argument("--voice-dir", default=str(HERE / "voices"))
    ap.add_argument("--voice", default="hi_IN-pratham-medium")
    ap.add_argument("--no-voice", action="store_true")
    ap.add_argument("--wake", action="store_true", help="listen for 'hey jarvis' on this PC's mic")
    ap.add_argument("--wake-model", default="hey_jarvis")
    ap.add_argument("--wake-threshold", type=float, default=0.5)
    ap.add_argument("--file-root", action="append", help="allow read-only file viewer in this folder (repeatable)")
    ap.add_argument("--https", action="store_true", help="self-signed HTTPS (phone mic needs a secure origin)")
    a = ap.parse_args()

    core = Core(a)
    if a.host not in ("127.0.0.1", "localhost") and not core.token:
        print("!! WARNING: LAN mode without --token: anyone on your Wi-Fi can drive Hermes.")
    ctx = None
    if a.https:
        cert, key = self_signed(Path(a.data_dir).expanduser() / "tls")
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(str(cert), str(key))
    scheme = "https" if ctx else "http"
    print(f"""
  J.A.R.V.I.S. CORE v{VERSION}
  UI      : {scheme}://{'localhost' if a.host in ('127.0.0.1', '0.0.0.0') else a.host}:{a.port}
  Hermes  : {core.hermes}   key: {'yes' if core.key else 'NO (set API_SERVER_KEY in ~/.hermes/.env)'}
  STT     : {'faster-whisper ' + a.whisper if core.stt and core.stt.available else 'off'}
  TTS     : {', '.join(core.tts.voices()) if core.tts and core.tts.voices() else 'off'}
  Wake    : {'hey jarvis (starting)' if core.wake and core.wake.available else ('off' if not a.wake else core.wake.error)}
  Audit   : {core.audit.path}
""")
    web.run_app(core.build(), host=a.host, port=a.port, ssl_context=ctx, print=None)


if __name__ == "__main__":
    main()
