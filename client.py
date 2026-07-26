# client.py — remote desktop host
# streams screen + audio to a browser controller via WebRTC,
# with JPEG/PCM-over-WS fallback when WebRTC can't punch through

import asyncio
import fractions
import json
import queue
import sys
import time
import traceback
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np
import pyautogui
import sounddevice as sd
import websockets

warnings.filterwarnings("ignore", category=DeprecationWarning, module="websockets")

# ── config ────────────────────────────────────────────────────────────────────
WS_URL          = "wss://n3knjvls-3000.use.devtunnels.ms"
ACCESS_KEY      = "stream123"
ROLE            = "host"

JPEG_QUALITY    = 80
TARGET_FPS      = 30
FRAME_INTERVAL  = 1.0 / TARGET_FPS

VIDEO_FPS       = 30
VIDEO_MAX_WIDTH = 1280  # downscale wide frames to ~720p to cut bandwidth

SAMPLE_RATE     = 48000
CHANNELS        = 2
AUDIO_CHUNK     = 960
# ──────────────────────────────────────────────────────────────────────────────

def log(tag, msg):
    print(f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}", flush=True)

log("BOOT", f"Python {sys.version.split()[0]}  starting client.py")

pyautogui.PAUSE    = 0
pyautogui.FAILSAFE = False

# ── aiortc (primary path) ──────────────────────────────────────────────────────
try:
    from aiortc import (
        RTCPeerConnection,
        RTCSessionDescription,
        RTCConfiguration,
        RTCIceServer,
        VideoStreamTrack,
        MediaStreamTrack,
    )
    from aiortc.sdp import candidate_from_sdp
    import av
    from av import VideoFrame, AudioFrame
    _aiortc_available = True
    log("RTC", f"aiortc + PyAV available ✓  (av {av.__version__})")
except ImportError as e:
    _aiortc_available  = False
    candidate_from_sdp = None
    log("RTC", f"aiortc/av NOT installed ({e}) — JPEG-over-WS only")

def build_ice_servers():
    if not _aiortc_available:
        return None
    servers = [
        RTCIceServer(urls="turn:turn.anyfirewall.com:443?transport=tcp",
                     username="webrtc", credential="webrtc"),
        RTCIceServer(urls="turn:turn.bistri.com:80",
                     username="homeo", credential="homeo"),
        RTCIceServer(urls="stun:stun.l.google.com:19302"),
        RTCIceServer(urls="stun:stun1.l.google.com:19302"),
        RTCIceServer(urls="stun:stun2.l.google.com:19302"),
        RTCIceServer(urls="stun:stun3.l.google.com:19302"),
        RTCIceServer(urls="stun:stun4.l.google.com:19302"),
    ]
    return RTCConfiguration(iceServers=servers)

# ── optional pycaw (mute PC speakers for "audio swallow") ─────────────────────
try:
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    _devices   = AudioUtilities.GetSpeakers()
    _interface = _devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    _volume    = cast(_interface, POINTER(IAudioEndpointVolume))
    _pycaw_available = True
except Exception:
    _pycaw_available = False
    log("AUDIO", "pycaw unavailable — speaker mute disabled")

def set_pc_speaker_mute(muted: bool) -> None:
    if not _pycaw_available:
        return
    try:
        _volume.SetMute(1 if muted else 0, None)
    except Exception as e:
        log("AUDIO", f"pycaw mute error: {e}")

# ── screen capture (dxcam → mss fallback) ─────────────────────────────────────
_use_dxcam    = False
_dxcam_cam    = None
_mss_instance = None
_mss_monitor  = None

def _init_capture() -> bool:
    global _use_dxcam, _dxcam_cam, _mss_instance, _mss_monitor
    try:
        import dxcam as _dxcam_mod
        if _dxcam_cam is not None:
            try: _dxcam_cam.stop()
            except Exception: pass
        _dxcam_cam = _dxcam_mod.create(output_color="BGR")
        _dxcam_cam.start(target_fps=0)
        _use_dxcam = True
        log("CAPTURE", "dxcam DXGI GPU capture ✓")
        return True
    except Exception as e:
        log("CAPTURE", f"dxcam unavailable ({e}) — using mss")
        _use_dxcam = False
        if _mss_instance is None:
            from mss import mss as _mss_lib
            _mss_instance = _mss_lib()
            _mss_monitor  = _mss_instance.monitors[1]
        return False

def grab_frame() -> Optional[np.ndarray]:
    if _use_dxcam and _dxcam_cam is not None:
        try:
            return _dxcam_cam.get_latest_frame()
        except Exception:
            return None
    if _mss_instance is not None:
        try:
            img = _mss_instance.grab(_mss_monitor)
            arr = np.frombuffer(img.raw, dtype=np.uint8).reshape(img.height, img.width, 4)
            return np.ascontiguousarray(arr[:, :, :3])
        except Exception:
            return None
    return None

_init_capture()

# ── JPEG encoder (simplejpeg → PyTurboJPEG → cv2) ─────────────────────────────
try:
    import simplejpeg
    log("ENCODE", "simplejpeg ✓")

    def encode_jpeg(frame: np.ndarray) -> bytes:
        return simplejpeg.encode_jpeg(frame, quality=JPEG_QUALITY, colorspace="BGR")

except ImportError:
    try:
        from turbojpeg import TurboJPEG, TJPF_BGR, TJSAMP_420
        _tj = TurboJPEG()
        log("ENCODE", "PyTurboJPEG ✓")

        def encode_jpeg(frame: np.ndarray) -> bytes:
            return _tj.encode(frame, quality=JPEG_QUALITY,
                              pixel_format=TJPF_BGR, jpeg_subsample=TJSAMP_420)

    except Exception:
        import cv2
        _cv2_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
        log("ENCODE", "cv2 fallback")

        def encode_jpeg(frame: np.ndarray) -> bytes:
            ok, buf = cv2.imencode(".jpg", frame, _cv2_params)
            return bytes(buf) if ok else b""

# ── audio capture (shared by WebRTC track + fallback) ─────────────────────────
_audio_q: "queue.Queue[bytes]" = queue.Queue(maxsize=8)
_audio_capture_enabled = True

def _audio_callback(indata: np.ndarray, frames: int, t, status) -> None:
    if status:
        log("AUDIO", f"InputStream status: {status}")
    if not _audio_capture_enabled:
        return
    pcm = (indata * 32767).astype(np.int16)
    try:
        _audio_q.put_nowait(pcm.tobytes())
    except queue.Full:
        # drop oldest, push newest
        try:
            _audio_q.get_nowait()
            _audio_q.put_nowait(pcm.tobytes())
        except Exception:
            pass

def find_loopback_device() -> Optional[int]:
    keywords = ("stereo mix", "loopback", "what u hear", "wave out mix")
    for i, dev in enumerate(sd.query_devices()):
        name = dev["name"].lower()
        if dev["max_input_channels"] > 0 and any(k in name for k in keywords):
            return i
    return None

# ── input handler ─────────────────────────────────────────────────────────────
_KEY_MAP = {
    "Control": "ctrl", "Shift": "shift", "Alt": "alt",
    "Enter": "enter", "Backspace": "backspace", "Escape": "esc",
    "ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right",
    " ": "space", "Space": "space", "Tab": "tab", "Delete": "delete",
    "Home": "home", "End": "end", "PageUp": "pageup", "PageDown": "pagedown",
    "CapsLock": "capslock", "Meta": "win",
    **{f"F{i}": f"f{i}" for i in range(1, 13)},
}

def handle_input(data: dict) -> None:
    w, h = pyautogui.size()
    try:
        t = data["type"]
        if   t == "mousemove":  pyautogui.moveTo(data["x"] * w, data["y"] * h, _pause=False)
        elif t == "mousedown":  pyautogui.mouseDown(button=data["button"])
        elif t == "mouseup":    pyautogui.mouseUp(button=data["button"])
        elif t == "scroll":     pyautogui.scroll(data.get("delta", 0))
        elif t == "keydown":    pyautogui.keyDown(_KEY_MAP.get(data["key"], data["key"].lower()))
        elif t == "keyup":      pyautogui.keyUp(_KEY_MAP.get(data["key"], data["key"].lower()))
    except Exception as e:
        log("INPUT", f"{type(e).__name__}: {e}")

# ── WebRTC media tracks ───────────────────────────────────────────────────────
_capture_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
_last_video_recv = 0.0  # monotonic time of last successful video frame

if _aiortc_available:

    class ScreenVideoTrack(VideoStreamTrack):
        kind = "video"

        def __init__(self, fps: int = VIDEO_FPS):
            super().__init__()
            self._fps = fps
            self._last_shape = None
            self._black = None

        def _downscale(self, frame: np.ndarray) -> np.ndarray:
            h, w = frame.shape[0], frame.shape[1]
            if w <= VIDEO_MAX_WIDTH:
                return frame
            new_w = VIDEO_MAX_WIDTH
            new_h = int(h * (new_w / w)) & ~1  # keep even
            try:
                import cv2
                return cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
            except Exception:
                ys = np.linspace(0, h - 1, new_h).astype(np.int32)
                xs = np.linspace(0, w - 1, new_w).astype(np.int32)
                return frame[ys][:, xs]

        async def recv(self) -> "VideoFrame":
            global _last_video_recv
            loop = asyncio.get_running_loop()
            try:
                pts, time_base = await self.next_timestamp()
                frame = await loop.run_in_executor(_capture_executor, grab_frame)

                if frame is None:
                    if self._black is None:
                        h, w = (self._last_shape or (720, 1280))
                        self._black = np.zeros((h, w, 3), dtype=np.uint8)
                    frame = self._black
                else:
                    self._last_shape = (frame.shape[0], frame.shape[1])
                    self._black = None
                    frame = self._downscale(frame)

                vf = VideoFrame.from_ndarray(np.ascontiguousarray(frame), format="bgr24")
                vf.pts = pts
                vf.time_base = time_base

                _last_video_recv = loop.time()
                return vf
            except Exception as e:
                log("VIDEO", f"recv() FAILED: {type(e).__name__}: {e}")
                traceback.print_exc()
                raise

    class LoopbackAudioTrack(MediaStreamTrack):
        kind = "audio"

        def __init__(self):
            super().__init__()
            self._sample_rate = SAMPLE_RATE
            self._channels = CHANNELS
            self._samples = AUDIO_CHUNK
            self._timestamp = 0
            self._time_base = fractions.Fraction(1, SAMPLE_RATE)

        async def recv(self) -> "AudioFrame":
            try:
                try:
                    raw = _audio_q.get_nowait()
                    pcm = np.frombuffer(raw, dtype=np.int16)
                except queue.Empty:
                    pcm = np.zeros(self._samples * self._channels, dtype=np.int16)
                    await asyncio.sleep(self._samples / self._sample_rate)

                n = pcm.size // self._channels
                # interleaved s16 → shape (1, n*channels) for packed 's16'
                data = np.ascontiguousarray(pcm.reshape(1, n * self._channels))
                frame = AudioFrame.from_ndarray(
                    data, format="s16",
                    layout="stereo" if self._channels == 2 else "mono",
                )
                frame.sample_rate = self._sample_rate
                frame.pts = self._timestamp
                frame.time_base = self._time_base
                self._timestamp += n
                return frame
            except Exception as e:
                log("AUDIO", f"recv() FAILED: {type(e).__name__}: {e}")
                traceback.print_exc()
                raise

# ── fallback send (JPEG/PCM over WS) ──────────────────────────────────────────
_ws_ref = None
_latest_jpeg: Optional[bytes] = None
_webrtc_media_connected = False

async def _ws_send_bytes(data: bytes) -> None:
    if _ws_ref is None:
        return
    try:
        if getattr(_ws_ref, "transport", None) is not None:
            buf = _ws_ref.transport.get_write_buffer_size()
            if buf > 2 * 1024 * 1024:
                return
        await _ws_ref.send(data)
    except Exception as e:
        log("WS", f"send failed: {type(e).__name__}: {e}")

# ── fallback loops ────────────────────────────────────────────────────────────
async def fallback_capture_loop() -> None:
    global _latest_jpeg
    loop            = asyncio.get_running_loop()
    last_good_frame = loop.time()
    REINIT_TIMEOUT  = 5.0
    was_suspended   = False

    while True:
        try:
            if _webrtc_media_connected:
                if not was_suspended:
                    log("CAPTURE", "WebRTC live — JPEG suspended")
                    was_suspended = True
                await asyncio.sleep(0.25)
                continue
            if was_suspended:
                log("CAPTURE", "WebRTC down — JPEG resumed")
                was_suspended = False

            frame = grab_frame()
            if frame is None:
                now = loop.time()
                if now - last_good_frame > REINIT_TIMEOUT:
                    log("CAPTURE", "no frames for 5s — reinit")
                    last_good_frame = now
                    await loop.run_in_executor(None, _init_capture)
                await asyncio.sleep(0.005)
                continue

            last_good_frame = loop.time()
            jpeg = await loop.run_in_executor(_capture_executor, encode_jpeg, frame)
            if jpeg:
                _latest_jpeg = jpeg
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("CAPTURE", f"{type(e).__name__}: {e}")
            await asyncio.sleep(0.5)

async def fallback_send_frames_loop() -> None:
    global _latest_jpeg
    loop = asyncio.get_running_loop()
    next_t = loop.time()
    while True:
        try:
            if not _webrtc_media_connected:
                jpeg, _latest_jpeg = _latest_jpeg, None
                if jpeg is not None:
                    await _ws_send_bytes(b"\x01" + jpeg)
            next_t += FRAME_INTERVAL
            await asyncio.sleep(max(0.0, next_t - loop.time()))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("SEND", f"{type(e).__name__}: {e}")
            await asyncio.sleep(0.05)

async def fallback_audio_loop() -> None:
    while True:
        try:
            if _webrtc_media_connected:
                await asyncio.sleep(0.1)
                continue
            try:
                pcm = _audio_q.get_nowait()
                await _ws_send_bytes(b"\x02" + pcm)
            except queue.Empty:
                await asyncio.sleep(0.002)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("AUDIO", f"{type(e).__name__}: {e}")
            await asyncio.sleep(0.05)

# if WebRTC claims connected but no video frame flows, fall back to JPEG
async def media_watchdog() -> None:
    global _webrtc_media_connected
    loop = asyncio.get_running_loop()
    while True:
        try:
            if _webrtc_media_connected and _aiortc_available:
                gap = loop.time() - (_last_video_recv or 0.0)
                if _last_video_recv and gap > 3.0:
                    log("WATCHDOG", f"no WebRTC video for {gap:.1f}s — JPEG fallback")
                    _webrtc_media_connected = False
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("WATCHDOG", f"{type(e).__name__}: {e}")
            await asyncio.sleep(1.0)

# ── WebRTC peer management ────────────────────────────────────────────────────
_rtc_peer: Optional["RTCPeerConnection"] = None
_rtc_lock = asyncio.Lock()

async def _close_peer() -> None:
    global _rtc_peer, _webrtc_media_connected
    if _rtc_peer is not None:
        try:
            await _rtc_peer.close()
        except Exception as e:
            log("RTC", f"close error: {e}")
    _rtc_peer = None
    _webrtc_media_connected = False

async def create_and_send_offer(ws) -> None:
    global _rtc_peer, _webrtc_media_connected

    if not _aiortc_available:
        log("RTC", "aiortc unavailable — fallback only")
        return

    async with _rtc_lock:
        await _close_peer()

        _rtc_peer = RTCPeerConnection(configuration=build_ice_servers())
        pc = _rtc_peer

        @pc.on("iceconnectionstatechange")
        async def on_ice_state():
            global _webrtc_media_connected
            state = pc.iceConnectionState
            if state in ("connected", "completed"):
                _webrtc_media_connected = True
                log("RTC", f"ice={state}  media live")
            elif state in ("failed", "disconnected", "closed"):
                _webrtc_media_connected = False
                log("RTC", f"ice={state}  media down")

        @pc.on("connectionstatechange")
        async def on_conn_state():
            global _webrtc_media_connected
            if pc.connectionState in ("failed", "closed"):
                _webrtc_media_connected = False

        @pc.on("icecandidate")
        async def on_ice(candidate):
            if candidate and _ws_ref is not None:
                try:
                    await ws.send(json.dumps({
                        "type": "rtc-signal",
                        "payload": {
                            "candidate":     candidate.candidate,
                            "sdpMid":        candidate.sdpMid,
                            "sdpMLineIndex": candidate.sdpMLineIndex,
                        },
                    }))
                except Exception as e:
                    log("RTC", f"candidate send failed: {e}")

        try:
            v = ScreenVideoTrack()
            a = LoopbackAudioTrack()
            pc.addTrack(v)
            pc.addTrack(a)
        except Exception as e:
            log("RTC", f"add tracks failed: {type(e).__name__}: {e}")
            traceback.print_exc()

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        try:
            await ws.send(json.dumps({
                "type": "rtc-signal",
                "payload": {
                    "type": pc.localDescription.type,
                    "sdp":  pc.localDescription.sdp,
                },
            }))
        except Exception as e:
            log("RTC", f"offer send failed: {e}")

async def handle_rtc_signal(ws, payload: dict) -> None:
    global _rtc_peer
    if not _aiortc_available or _rtc_peer is None:
        return

    sdp_type = payload.get("type")

    if sdp_type == "answer":
        try:
            await _rtc_peer.setRemoteDescription(
                RTCSessionDescription(sdp=payload["sdp"], type="answer"))
        except Exception as e:
            log("RTC", f"setRemoteDescription failed: {type(e).__name__}: {e}")
            traceback.print_exc()

    elif "candidate" in payload:
        try:
            raw = payload["candidate"]
            if raw.startswith("candidate:"):
                raw = raw[len("candidate:"):]
            cand = candidate_from_sdp(raw)
            sdp_mid = payload.get("sdpMid")
            sdp_idx = payload.get("sdpMLineIndex")
            # only set if the controller actually provided them
            if sdp_mid is not None:
                cand.sdpMid = sdp_mid
            if sdp_idx is not None:
                cand.sdpMLineIndex = sdp_idx
            await _rtc_peer.addIceCandidate(cand)
        except Exception as e:
            log("RTC", f"ICE candidate error: {type(e).__name__}: {e}")

# ── receive messages (per session) ────────────────────────────────────────────
async def receive_messages(ws) -> None:
    try:
        async for msg in ws:
            if not isinstance(msg, str):
                continue
            try:
                data = json.loads(msg)
            except Exception:
                continue

            mtype = data.get("type")

            if mtype == "input":
                handle_input(data.get("data", {}))

            elif mtype == "audio-swallow":
                set_pc_speaker_mute(bool(data.get("active", False)))

            elif mtype == "rtc-signal" and _aiortc_available:
                await handle_rtc_signal(ws, data.get("payload", {}))

            elif mtype == "room-state":
                if _aiortc_available and data.get("hasController"):
                    await create_and_send_offer(ws)

            elif mtype == "peer-joined":
                if _aiortc_available and data.get("role") == "controller":
                    await create_and_send_offer(ws)

            elif mtype == "peer-left":
                if data.get("role") == "controller":
                    await _close_peer()
    except websockets.ConnectionClosed:
        pass

# ── session ───────────────────────────────────────────────────────────────────
async def run_session(ws) -> None:
    global _ws_ref
    _ws_ref = ws

    tasks = [
        asyncio.create_task(receive_messages(ws),        name="receive_messages"),
        asyncio.create_task(fallback_send_frames_loop(), name="fallback_send_frames"),
        asyncio.create_task(fallback_audio_loop(),       name="fallback_audio"),
        asyncio.create_task(media_watchdog(),            name="media_watchdog"),
    ]

    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_peer()
        _ws_ref = None

# ── main ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    loopback_idx = find_loopback_device()
    capture_task = asyncio.create_task(fallback_capture_loop(), name="capture_loop")

    reconnect_delay     = 3.0
    MAX_RECONNECT_DELAY = 60.0
    MIN_GOOD_SESSION    = 10.0

    while True:
        if capture_task.done():
            log("CAPTURE", "capture loop exited — restarting")
            capture_task = asyncio.create_task(fallback_capture_loop(), name="capture_loop")

        audio_stream  = None
        session_start = asyncio.get_event_loop().time()

        try:
            log("WS", f"connecting → {WS_URL}")
            async with websockets.connect(
                WS_URL,
                max_size=16 * 1024 * 1024,
                ping_interval=30,
                ping_timeout=60,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps({"type": "join", "key": ACCESS_KEY, "role": ROLE}))
                log("WS", f"connected  room={ACCESS_KEY}  role={ROLE}")

                audio_stream = sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="float32",
                    blocksize=AUDIO_CHUNK,
                    callback=_audio_callback,
                    device=loopback_idx,
                )
                audio_stream.start()

                try:
                    await run_session(ws)
                finally:
                    try:
                        audio_stream.stop()
                        audio_stream.close()
                    except Exception:
                        pass
                    audio_stream = None
                    while not _audio_q.empty():
                        try: _audio_q.get_nowait()
                        except queue.Empty: break

        except asyncio.CancelledError:
            raise
        except (websockets.ConnectionClosed, OSError, ConnectionRefusedError) as e:
            log("WS", f"disconnected: {type(e).__name__}: {e}")
        except Exception as e:
            log("ERROR", f"{type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            if audio_stream is not None:
                try:
                    audio_stream.stop()
                    audio_stream.close()
                except Exception:
                    pass

        session_duration = asyncio.get_event_loop().time() - session_start
        if session_duration >= MIN_GOOD_SESSION:
            reconnect_delay = 3.0
        else:
            reconnect_delay = min(reconnect_delay * 1.8, MAX_RECONNECT_DELAY)

        log("WS", f"reconnecting in {reconnect_delay:.0f}s")
        await asyncio.sleep(reconnect_delay)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        log("SYSTEM", "stopped")
