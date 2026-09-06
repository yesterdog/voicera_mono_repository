import socket
import threading
import time

from .rtp_audio import RtpSession
from .sip import SipClient, SipError


def _get_local_ip_for(target_ip: str, target_port: int) -> str:
    """Local interface IP the OS would use to reach target_ip - no packets sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, target_port))
        return s.getsockname()[0]
    finally:
        s.close()


class Softphone:
    """Ties SipClient + RtpSession together with simple string state for the UI."""

    def __init__(self, config: dict):
        self.config = config
        self.sip: SipClient = None
        self.rtp: RtpSession = None
        self.state = "starting"  # starting, registered, calling, in_call, error
        self.error = None
        self.call_start_time = None  # epoch seconds, set while in_call
        self._lock = threading.Lock()
        self._generation = 0  # bumped on reconfigure to invalidate in-flight start() calls

    def start(self):
        my_generation = self._generation
        try:
            local_ip = _get_local_ip_for(self.config["server_ip"], self.config["server_port"])
            sip = SipClient(
                server_ip=self.config["server_ip"],
                server_port=self.config["server_port"],
                domain=self.config["domain"],
                username=self.config["username"],
                password=self.config["password"],
            )
            sip.on_remote_bye = self._on_remote_bye
            sip.connect(local_ip)
            sip.register()
            if my_generation != self._generation:
                # A newer reconfigure() started while we were connecting -
                # don't clobber its state with this stale attempt's result.
                sip.close()
                return
            self.sip = sip
            self.state = "registered"
        except Exception as e:  # noqa: BLE001
            if my_generation == self._generation:
                self.state = "error"
                self.error = str(e)

    def reconfigure(self, new_config: dict):
        """Tear down the current registration/call and start fresh with new
        connection settings (server/port/domain/username/password)."""
        with self._lock:
            self._generation += 1
            if self.rtp is not None:
                self.rtp.stop()
                self.rtp = None
            if self.sip is not None:
                try:
                    self.sip.unregister()
                except Exception:
                    pass
                try:
                    self.sip.close()
                except Exception:
                    pass
                self.sip = None
            self.config = new_config
            self.call_start_time = None
            self.error = None
            self.state = "starting"
        self.start()

    def call(self, number: str):
        with self._lock:
            if self.state not in ("registered",):
                return
            self.state = "calling"
        try:
            rtp = RtpSession(bind_ip="0.0.0.0")
            remote_ip, remote_port = self.sip.invite(number, rtp.local_port)
            rtp.start(remote_ip, remote_port)
            self.rtp = rtp
            self.call_start_time = time.time()
            self.state = "in_call"
        except SipError as e:
            self.state = "registered"
            self.error = str(e)
        except Exception as e:  # noqa: BLE001
            self.state = "error"
            self.error = str(e)

    def hangup(self):
        with self._lock:
            if self.rtp is not None:
                self.rtp.stop()
                self.rtp = None
            if self.sip is not None:
                try:
                    self.sip.bye()
                except Exception:
                    pass
            self.call_start_time = None
            if self.state != "error":
                self.state = "registered"

    def _on_remote_bye(self):
        with self._lock:
            if self.rtp is not None:
                self.rtp.stop()
                self.rtp = None
            self.call_start_time = None
            if self.state != "error":
                self.state = "registered"

    def get_status(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "server": f"{self.config['server_ip']}:{self.config['server_port']}",
            "domain": self.config["domain"],
            "call_start_time": self.call_start_time,
        }

    def get_config(self) -> dict:
        return dict(self.config)
