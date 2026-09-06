"""Minimal SIP client: REGISTER + INVITE/ACK/BYE over TCP, with digest auth.

Scoped deliberately narrow: one server, one account, one call at a time,
TCP transport, G.711-only SDP. Not a general-purpose SIP stack - there's no
need for one here, jambonz always talks to the same known server.
"""

import hashlib
import queue
import random
import socket
import threading
import time


def _rand_hex(n: int = 8) -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


def _md5hex(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _parse_message(raw: bytes):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    start_line = lines[0].decode(errors="replace")
    headers = []
    for line in lines[1:]:
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        headers.append((name.strip().decode(), value.strip().decode(errors="replace")))
    return start_line, headers, body


def _get_header(headers, name):
    name = name.lower()
    for k, v in headers:
        if k.lower() == name:
            return v
    return None


def _parse_auth_challenge(header_value: str) -> dict:
    # Digest realm="...", nonce="...", algorithm=MD5, qop="auth"
    params = {}
    rest = header_value.split(" ", 1)[1] if " " in header_value else header_value
    for part in rest.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        params[k.strip().lower()] = v.strip().strip('"')
    return params


def _parse_sdp_answer(body: bytes):
    remote_ip = None
    remote_port = None
    for line in body.decode(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            remote_ip = line.split()[-1]
        elif line.startswith("m=audio "):
            remote_port = int(line.split()[1])
    if remote_ip is None or remote_port is None:
        raise ValueError(f"could not parse SDP answer: {body!r}")
    return remote_ip, remote_port


DEBUG = True


class SipTcpTransport:
    """Reads complete SIP messages off a TCP stream (frames on Content-Length)."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buf = b""

    def read_message(self) -> bytes:
        while True:
            # Discard leading bare-CRLF ping/pong noise (SIP double-CRLF
            # keepalives, ours or the server's own). A real SIP message can
            # never start with CRLF, so this is always safe - but if left
            # in place, a leading CRLF shifts _parse_message's line-split so
            # the real status-line lands in lines[1] instead of lines[0],
            # silently misrouting the response as an unrecognized inbound
            # request and dropping it (diagnosed after 718 bytes sat unread
            # in the socket's recv queue while a call appeared to "time out").
            while self._buf.startswith(b"\r\n"):
                self._buf = self._buf[2:]
            if b"\r\n\r\n" in self._buf:
                break
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("SIP TCP connection closed")
            self._buf += chunk
        header_end = self._buf.index(b"\r\n\r\n") + 4
        content_length = 0
        for line in self._buf[:header_end].split(b"\r\n"):
            if line.lower().startswith(b"content-length"):
                content_length = int(line.split(b":", 1)[1].strip())
        while len(self._buf) < header_end + content_length:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("SIP TCP connection closed")
            self._buf += chunk
        message = self._buf[: header_end + content_length]
        self._buf = self._buf[header_end + content_length :]
        return message

    def send(self, data: bytes):
        if DEBUG:
            print(f"--- SEND ---\n{data.decode(errors='replace')}\n------------")
        self.sock.sendall(data)


class SipError(Exception):
    pass


class SipClient:
    def __init__(self, server_ip, server_port, domain, username, password, transport="tcp"):
        self.server_ip = server_ip
        self.server_port = server_port
        self.domain = domain
        self.username = username
        self.password = password

        self.sock = None
        self.reader = None
        self.local_ip = None
        self.local_port = None

        self.call_id = f"{_rand_hex(16)}"
        self.cseq = 0
        self._responses: "queue.Queue[tuple]" = queue.Queue()
        self._recv_thread = None
        self._running = False

        self.on_remote_bye = None  # callback, no args

    # --- connection setup -------------------------------------------------

    def connect(self, local_ip: str):
        self.local_ip = local_ip
        self.sock = socket.create_connection((self.server_ip, self.server_port), timeout=10)
        # The 10s connect-phase timeout otherwise carries over to every later
        # recv() too - any idle gap longer than 10s (routine between calls)
        # raised socket.timeout, which _recv_loop treated as fatal and killed
        # the receiver thread for good, silently. That's what was causing
        # "lost"/delayed responses: the OS still had the bytes buffered, but
        # nothing was left calling recv() to drain them. Block indefinitely
        # once actually connected.
        self.sock.settimeout(None)
        self.local_port = self.sock.getsockname()[1]

        # Diagnosed: a consumer NAT/router idle-timing out this TCP session
        # (mid-air for 50-110s) between calls, silently dropping the
        # connection while both ends still think it's up - TCP eventually
        # retransmits and delivers, but long after our own request timeouts
        # gave up on the transaction. TCP_NODELAY + SO_KEEPALIVE + an
        # application-level SIP double-CRLF ping every 25s keep the NAT
        # mapping warm well under any observed ~50s+ idle-eviction window.
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt_name in ("TCP_KEEPIDLE", "TCP_KEEPALIVE"):  # Linux vs macOS naming
            opt = getattr(socket, opt_name, None)
            if opt is not None:
                try:
                    self.sock.setsockopt(socket.IPPROTO_TCP, opt, 20)
                except OSError:
                    pass
        if hasattr(socket, "TCP_KEEPINTVL"):
            try:
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            except OSError:
                pass
        if hasattr(socket, "TCP_KEEPCNT"):
            try:
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
            except OSError:
                pass

        self.reader = SipTcpTransport(self.sock)
        self._running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keepalive_thread.start()

    def _keepalive_loop(self, interval: float = 25.0):
        while self._running:
            time.sleep(interval)
            if not self._running:
                break
            try:
                self.sock.sendall(b"\r\n\r\n")
            except OSError:
                break

    def close(self):
        self._running = False
        try:
            self.sock.close()
        except Exception:
            pass

    def _recv_loop(self):
        while self._running:
            try:
                raw = self.reader.read_message()
            except Exception as e:
                if DEBUG:
                    import traceback

                    print(f"--- RECV LOOP CRASHED: {e!r} ---")
                    traceback.print_exc()
                break
            if not raw.strip():
                continue
            if DEBUG:
                print(f"--- RECV ---\n{raw.decode(errors='replace')}\n------------")
            try:
                start_line, headers, body = _parse_message(raw)
                if start_line.startswith("SIP/2.0"):
                    self._responses.put((start_line, headers, body))
                else:
                    self._handle_inbound_request(start_line, headers, body)
            except Exception as e:
                if DEBUG:
                    import traceback

                    print(f"--- ERROR HANDLING MESSAGE: {e!r} ---")
                    traceback.print_exc()
        if DEBUG:
            print("--- RECV LOOP EXITED ---")

    def _handle_inbound_request(self, start_line, headers, body):
        method = start_line.split(" ", 1)[0]
        via = _get_header(headers, "Via") or ""
        from_h = _get_header(headers, "From") or ""
        to_h = _get_header(headers, "To") or ""
        call_id = _get_header(headers, "Call-ID") or ""
        cseq = _get_header(headers, "CSeq") or f"0 {method}"
        resp = (
            f"SIP/2.0 200 OK\r\n"
            f"Via: {via}\r\n"
            f"From: {from_h}\r\n"
            f"To: {to_h}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq}\r\n"
            f"Content-Length: 0\r\n\r\n"
        ).encode()
        try:
            self.reader.send(resp)
        except Exception:
            pass
        if method == "BYE" and self.on_remote_bye:
            self.on_remote_bye()

    def _next_cseq(self) -> int:
        self.cseq += 1
        return self.cseq

    def _wait_final_response(self, method: str, cseq: int, timeout: float = 15.0):
        # Match on the exact "<number> <METHOD>" CSeq value, not just the
        # method suffix - otherwise a stale/leftover response for some OTHER
        # transaction sharing the same method (e.g. a delayed 401 for a
        # previous INVITE attempt still sitting in the queue) gets matched to
        # THIS wait call, discarding the real response for good and wasting
        # this call's whole timeout budget on data that isn't ours.
        expected_cseq = f"{cseq} {method}"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                start_line, headers, body = self._responses.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            status = int(start_line.split(" ")[1])
            resp_cseq = (_get_header(headers, "CSeq") or "").strip()
            if resp_cseq != expected_cseq:
                continue
            if status < 200:
                continue  # provisional (100 Trying, 180 Ringing, ...)
            return status, headers, body
        raise SipError(f"timed out waiting for response to {method}")

    def _contact_uri(self) -> str:
        return f"<sip:{self.username}@{self.local_ip}:{self.local_port};transport=tcp>"

    # --- REGISTER -----------------------------------------------------

    def register(self, expires: int = 3600):
        tag = _rand_hex(8)
        branch = "z9hG4bK" + _rand_hex(12)
        cseq = self._next_cseq()
        req = self._build_register(branch, tag, cseq, expires, auth_header=None)
        self.reader.send(req)
        status, headers, _body = self._wait_final_response("REGISTER", cseq)

        if status in (401, 407):
            challenge_header = "WWW-Authenticate" if status == 401 else "Proxy-Authenticate"
            challenge = _get_header(headers, challenge_header)
            if not challenge:
                raise SipError(f"REGISTER challenged ({status}) but no {challenge_header} header")
            auth = self._build_auth_header(
                challenge, method="REGISTER", uri=f"sip:{self.domain}", proxy=(status == 407)
            )
            branch2 = "z9hG4bK" + _rand_hex(12)
            cseq2 = self._next_cseq()
            req2 = self._build_register(branch2, tag, cseq2, expires, auth_header=auth)
            self.reader.send(req2)
            status, headers, _body = self._wait_final_response("REGISTER", cseq2)

        if status != 200:
            raise SipError(f"REGISTER failed: {status}")

        self._register_tag = tag

    def unregister(self):
        """REGISTER with Expires: 0 - releases our binding so it doesn't
        linger server-side across repeated test runs (stale bindings/dialog
        state have been observed to cause spurious 491s on later INVITEs)."""
        if not getattr(self, "_register_tag", None):
            return
        try:
            branch = "z9hG4bK" + _rand_hex(12)
            cseq = self._next_cseq()
            req = self._build_register(branch, self._register_tag, cseq, expires=0, auth_header=None)
            self.reader.send(req)
            status, headers, _body = self._wait_final_response("REGISTER", cseq, timeout=5.0)
            if status in (401, 407):
                challenge_header = "WWW-Authenticate" if status == 401 else "Proxy-Authenticate"
                challenge = _get_header(headers, challenge_header)
                if challenge:
                    auth = self._build_auth_header(
                        challenge, method="REGISTER", uri=f"sip:{self.domain}", proxy=(status == 407)
                    )
                    branch2 = "z9hG4bK" + _rand_hex(12)
                    cseq2 = self._next_cseq()
                    req2 = self._build_register(branch2, self._register_tag, cseq2, expires=0, auth_header=auth)
                    self.reader.send(req2)
                    self._wait_final_response("REGISTER", cseq2, timeout=5.0)
        except Exception:
            pass  # best-effort on shutdown

    def _build_register(self, branch, tag, cseq, expires, auth_header):
        lines = [
            f"REGISTER sip:{self.domain} SIP/2.0",
            f"Via: SIP/2.0/TCP {self.local_ip}:{self.local_port};branch={branch};rport",
            "Max-Forwards: 70",
            f"From: <sip:{self.username}@{self.domain}>;tag={tag}",
            f"To: <sip:{self.username}@{self.domain}>",
            f"Call-ID: {self.call_id}",
            f"CSeq: {cseq} REGISTER",
            f"Contact: {self._contact_uri()}",
            f"Expires: {expires}",
            "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS",
        ]
        if auth_header:
            lines.append(auth_header)
        lines.append("Content-Length: 0")
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    # --- INVITE / ACK / BYE --------------------------------------------

    def invite(self, number: str, local_rtp_port: int, _attempt: int = 1):
        # Each call gets its own fresh Call-ID (distinct from the REGISTER's) -
        # reusing one Call-ID across calls/registration confuses the server's
        # dialog/transaction matching (observed as spurious 491 "Request Pending").
        call_id = f"{_rand_hex(16)}@{self.local_ip}"
        tag = _rand_hex(8)
        branch = "z9hG4bK" + _rand_hex(12)
        cseq = self._next_cseq()
        sdp = self._build_sdp_offer(local_rtp_port)
        req = self._build_invite(number, call_id, branch, tag, cseq, sdp, auth_header=None)
        self.reader.send(req)
        status, headers, body = self._wait_final_response("INVITE", cseq, timeout=32.0)
        accepted_cseq = cseq

        if status != 200 and status not in (401, 407):
            # RFC 3261 17.1.1.3: the INVITE client transaction MUST ack any
            # non-2xx final response (same branch/CSeq as the INVITE it
            # answers) - otherwise the server transaction sits in "Completed"
            # waiting on Timer H (~32s), and a same-Call-ID retry arriving in
            # that window is rejected 491 "received before ACK".
            self._ack_non_2xx(number, call_id, branch, tag, cseq, headers)
            raise SipError(f"INVITE failed: {status}")

        if status in (401, 407):
            self._ack_non_2xx(number, call_id, branch, tag, cseq, headers)

            challenge_header = "WWW-Authenticate" if status == 401 else "Proxy-Authenticate"
            challenge = _get_header(headers, challenge_header)
            if not challenge:
                raise SipError(f"INVITE challenged ({status}) but no {challenge_header} header")
            auth = self._build_auth_header(
                challenge, method="INVITE", uri=f"sip:{number}@{self.domain}", proxy=(status == 407)
            )
            branch2 = "z9hG4bK" + _rand_hex(12)
            cseq2 = self._next_cseq()
            req2 = self._build_invite(number, call_id, branch2, tag, cseq2, sdp, auth_header=auth)
            self.reader.send(req2)
            status, headers, body = self._wait_final_response("INVITE", cseq2, timeout=32.0)
            accepted_cseq = cseq2

            if status != 200:
                self._ack_non_2xx(number, call_id, branch2, tag, cseq2, headers)
                raise SipError(f"INVITE failed: {status}")

        to_header = _get_header(headers, "To")
        from_header = _get_header(headers, "From")
        self._dialog = {
            "to": to_header,
            "from": from_header,
            "cseq_ack": accepted_cseq,
            "call_id": call_id,
        }

        remote_ip, remote_port = _parse_sdp_answer(body)

        ack_branch = "z9hG4bK" + _rand_hex(12)
        ack = self._build_ack(number, ack_branch)
        self.reader.send(ack)

        return remote_ip, remote_port

    def bye(self):
        if not getattr(self, "_dialog", None):
            return
        branch = "z9hG4bK" + _rand_hex(12)
        cseq = self._next_cseq()
        d = self._dialog
        req = (
            f"BYE sip:{self.server_ip}:{self.server_port} SIP/2.0\r\n"
            f"Via: SIP/2.0/TCP {self.local_ip}:{self.local_port};branch={branch};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: {d['from']}\r\n"
            f"To: {d['to']}\r\n"
            f"Call-ID: {d['call_id']}\r\n"
            f"CSeq: {cseq} BYE\r\n"
            "Content-Length: 0\r\n\r\n"
        ).encode()
        self.reader.send(req)
        try:
            self._wait_final_response("BYE", cseq, timeout=5.0)
        except SipError:
            pass
        self._dialog = None

    def _ack_non_2xx(self, number, call_id, branch, tag, cseq, response_headers):
        """ACK for a non-2xx final response (RFC 3261 17.1.1.3): transaction-
        layer ACK, NOT the dialog-forming one - same branch and CSeq number
        as the INVITE it answers (method ACK), To taken from the response
        (carries the server's to-tag)."""
        to_header = _get_header(response_headers, "To") or f"<sip:{number}@{self.domain}>"
        req = (
            f"ACK sip:{number}@{self.domain} SIP/2.0\r\n"
            f"Via: SIP/2.0/TCP {self.local_ip}:{self.local_port};branch={branch};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:{self.username}@{self.domain}>;tag={tag}\r\n"
            f"To: {to_header}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq} ACK\r\n"
            "Content-Length: 0\r\n\r\n"
        ).encode()
        self.reader.send(req)

    def _build_invite(self, number, call_id, branch, tag, cseq, sdp, auth_header):
        lines = [
            f"INVITE sip:{number}@{self.domain} SIP/2.0",
            f"Via: SIP/2.0/TCP {self.local_ip}:{self.local_port};branch={branch};rport",
            "Max-Forwards: 70",
            f"From: <sip:{self.username}@{self.domain}>;tag={tag}",
            f"To: <sip:{number}@{self.domain}>",
            f"Call-ID: {call_id}",
            f"CSeq: {cseq} INVITE",
            f"Contact: {self._contact_uri()}",
            "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS",
            "Content-Type: application/sdp",
        ]
        if auth_header:
            lines.append(auth_header)
        lines.append(f"Content-Length: {len(sdp)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + sdp

    def _build_ack(self, number, branch):
        d = self._dialog
        lines = [
            f"ACK sip:{number}@{self.domain} SIP/2.0",
            f"Via: SIP/2.0/TCP {self.local_ip}:{self.local_port};branch={branch};rport",
            "Max-Forwards: 70",
            f"From: {d['from']}",
            f"To: {d['to']}",
            f"Call-ID: {d['call_id']}",
            f"CSeq: {d['cseq_ack']} ACK",
            "Content-Length: 0",
        ]
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    def _build_sdp_offer(self, local_rtp_port: int) -> bytes:
        sdp = (
            "v=0\r\n"
            f"o=softphone 0 0 IN IP4 {self.local_ip}\r\n"
            "s=softphone\r\n"
            f"c=IN IP4 {self.local_ip}\r\n"
            "t=0 0\r\n"
            f"m=audio {local_rtp_port} RTP/AVP 0 101\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
            "a=fmtp:101 0-15\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )
        return sdp.encode()

    def _build_auth_header(self, challenge_value, method, uri, proxy: bool) -> str:
        params = _parse_auth_challenge(challenge_value)
        realm = params.get("realm", self.domain)
        nonce = params["nonce"]
        qop = params.get("qop")
        cnonce = _rand_hex(8)
        nc = "00000001"

        ha1 = _md5hex(f"{self.username}:{realm}:{self.password}")
        ha2 = _md5hex(f"{method}:{uri}")
        if qop:
            response = _md5hex(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
            qop_part = f', cnonce="{cnonce}", nc={nc}, qop={qop}'
        else:
            response = _md5hex(f"{ha1}:{nonce}:{ha2}")
            qop_part = ""

        header_name = "Proxy-Authorization" if proxy else "Authorization"
        return (
            f'{header_name}: Digest username="{self.username}", realm="{realm}", '
            f'nonce="{nonce}", uri="{uri}", response="{response}", algorithm=MD5{qop_part}'
        )
