"""RTP send/receive over UDP, wired to the local mic/speakers via sounddevice.

G.711 mu-law (PT 0) only, 8kHz mono, 20ms frames (160 samples/packet) -
matches the jambonz FreeSWITCH mediaserver, which is configured G.711-only.
"""

import queue
import random
import socket
import struct
import threading

import numpy as np
import sounddevice as sd

from . import g711

SAMPLE_RATE = 8000
FRAME_SAMPLES = 160  # 20ms at 8kHz


def _build_rtp_header(seq: int, ts: int, ssrc: int, payload_type: int = 0, marker: bool = False) -> bytes:
    b0 = 0x80  # version 2, no padding, no extension, CSRC count 0
    b1 = (0x80 if marker else 0x00) | (payload_type & 0x7F)
    return struct.pack("!BBHII", b0, b1, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)


class RtpSession:
    """One RTP media session for the duration of a call."""

    def __init__(self, bind_ip: str = "0.0.0.0"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind_ip, 0))
        self.local_port = self.sock.getsockname()[1]

        self.seq = random.randint(0, 0xFFFF)
        self.ts = random.randint(0, 0xFFFFFFFF)
        self.ssrc = random.randint(0, 0xFFFFFFFF)

        self._remote = None
        self._running = False
        self._recv_thread = None
        self._in_stream = None
        self._out_stream = None
        self._playback_queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=25)

    def start(self, remote_ip: str, remote_port: int):
        self._remote = (remote_ip, remote_port)
        self._running = True

        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

        self._in_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            callback=self._mic_callback,
        )
        self._out_stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            callback=self._speaker_callback,
        )
        self._in_stream.start()
        self._out_stream.start()

    def stop(self):
        self._running = False
        for stream in (self._in_stream, self._out_stream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
        self._in_stream = None
        self._out_stream = None
        try:
            self.sock.close()
        except Exception:
            pass

    def _mic_callback(self, indata, frames, time_info, status):
        if not self._running or self._remote is None:
            return
        pcm = indata[:, 0]
        payload = g711.encode(pcm)
        header = _build_rtp_header(self.seq, self.ts, self.ssrc, payload_type=0)
        self.seq = (self.seq + 1) & 0xFFFF
        self.ts = (self.ts + frames) & 0xFFFFFFFF
        try:
            self.sock.sendto(header + payload, self._remote)
        except OSError:
            pass

    def _speaker_callback(self, outdata, frames, time_info, status):
        try:
            data = self._playback_queue.get_nowait()
        except queue.Empty:
            data = np.zeros(frames, dtype="int16")
        if len(data) < frames:
            data = np.pad(data, (0, frames - len(data)))
        outdata[:, 0] = data[:frames]

    def _recv_loop(self):
        self.sock.settimeout(0.5)
        while self._running:
            try:
                data, _addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 12:
                continue
            payload = data[12:]
            pcm = g711.decode(payload)
            try:
                self._playback_queue.put_nowait(pcm)
            except queue.Full:
                pass
