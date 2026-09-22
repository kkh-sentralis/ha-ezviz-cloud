#!/usr/bin/env python3
"""Pont EZVIZ : WebSocket proprietaire -> H.265 Annex-B sur stdout.

La camera HB8C ne sert ni RTSP ni HLS (voir FINDINGS.md). Son seul flux
vivant passe par le WebSocket d'EZUIKit, qui transporte du RTP dans des
messages binaires, precedes d'un entete « IMKH » de 40 octets.

Ce pont refait ce que fait leur lecteur JavaScript :
  1. resout l'adresse ezopen:// en wss:// + jeton de session
  2. ouvre le WebSocket avec ssn/auth/biz/cln (sans eux : « get stream error »)
  3. depaquetise le RTP (RFC 7798) et ecrit du H.265 Annex-B sur stdout

⛔ AUCUNE DEPENDANCE. Il tourne dans le conteneur de l'add-on go2rtc, qui a
python3 mais ni aiohttp ni websockets, et qu'une mise a jour reinitialise.
Tout ce qui suit est de la bibliotheque standard.

    ws_bridge.py --token … --serial … | ffmpeg -f hevc -i - -c copy -f rtsp …
"""
import argparse
import base64
import json
import os
import socket
import ssl
import struct
import sys
import urllib.request
import uuid
from urllib.parse import urlsplit

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
START = b"\x00\x00\x00\x01"


def resolve(host, token, serial, channel, quality):
    """ezopen:// -> (url wss, jeton de session).

    ⚠ multipart obligatoire : en urlencoded l'API repond « parametre vide ».
    """
    ezopen = f"ezopen://open.ezviz.com/{serial}/{channel}.{quality}live"
    boundary = uuid.uuid4().hex
    body = b""
    for name, value in (
        ("accessToken", token), ("ezopen", ezopen),
        ("isFlv", "false"), ("isHttp", "false"), ("userAgent", UA),
    ):
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
    body += f"--{boundary}--\r\n".encode()

    request = urllib.request.Request(
        f"https://{host}/api/lapp/live/url/ezopen", data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": UA,
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        answer = json.loads(response.read())
    if answer.get("code") != "200":
        raise SystemExit(f"resolve: {answer.get('code')} {answer.get('msg')}")
    return answer["data"]["url"], answer["data"]["token"]


class WebSocket:
    """Client WebSocket en lecture seule, strictement stdlib.

    On n'emet jamais rien apres la poignee de main : le serveur pousse le flux
    de lui-meme. Le masquage cote client, la fragmentation sortante et les
    extensions sont donc hors sujet.
    """

    def __init__(self, url, origin, timeout=20):
        parts = urlsplit(url)
        port = parts.port or 443
        path = parts.path + ("?" + parts.query if parts.query else "")
        raw = socket.create_connection((parts.hostname, port), timeout=timeout)
        self._sock = ssl.create_default_context().wrap_socket(
            raw, server_hostname=parts.hostname
        )
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall(
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parts.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: {origin}\r\n"
            f"User-Agent: {UA}\r\n\r\n".encode()
        )
        self._buffer = b""
        head = self._until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise SystemExit(f"handshake refuse : {head.splitlines()[0]!r}")

    def _until(self, marker):
        while marker not in self._buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("connexion fermee pendant la poignee de main")
            self._buffer += chunk
        head, self._buffer = self._buffer.split(marker, 1)
        return head

    def _read(self, count):
        while len(self._buffer) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("connexion fermee")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def messages(self):
        """Rend (opcode, charge utile) pour chaque message reassemble."""
        opcode = None
        payload = bytearray()
        while True:
            first, second = self._read(2)
            fin = first & 0x80
            this_opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if second & 0x80 else None
            data = self._read(length) if length else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

            if this_opcode == 0x8:  # close
                return
            if this_opcode in (0x9, 0xA):  # ping/pong : ignores
                continue
            if this_opcode != 0x0:  # nouvelle trame
                opcode = this_opcode
                payload = bytearray()
            payload.extend(data)
            if fin and opcode is not None:
                yield opcode, bytes(payload)
                opcode = None
                payload = bytearray()

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


class Depacketizer:
    """RTP -> H.265 Annex-B. Reassemble les unites de fragmentation."""

    def __init__(self):
        self._fragment = None

    def feed(self, packet):
        if len(packet) < 13 or packet[0] & 0xC0 != 0x80:
            return b""
        header = 12 + 4 * (packet[0] & 0x0F)
        if packet[0] & 0x10:  # extension
            if len(packet) < header + 4:
                return b""
            header += 4 + 4 * int.from_bytes(packet[header + 2:header + 4], "big")
        payload = packet[header:]
        if len(payload) < 3:
            return b""

        nal_type = (payload[0] >> 1) & 0x3F
        if nal_type != 49:  # NAL entiere
            return START + payload

        # Unite de fragmentation : 2 octets PayloadHdr + 1 octet FU
        fu = payload[2]
        if fu & 0x80:  # debut
            inner = ((fu & 0x3F) << 1) | (payload[0] & 0x81)
            self._fragment = bytearray([inner, payload[1]])
        if self._fragment is None:
            return b""
        self._fragment.extend(payload[3:])
        if fu & 0x40:  # fin
            out = START + bytes(self._fragment)
            self._fragment = None
            return out
        return b""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", help="accessToken de l'Open Platform")
    parser.add_argument("--token-file", help="fichier contenant l'accessToken")
    parser.add_argument("--serial", required=True)
    parser.add_argument("--host", default="ieuopen.ezvizlife.com")
    parser.add_argument("--channel", default="1")
    parser.add_argument("--quality", default="hd.", help="'hd.' (HD) ou '' (fluide)")
    parser.add_argument("--max-bytes", type=int, default=0)
    args = parser.parse_args()

    token = args.token
    if args.token_file:
        token = open(args.token_file).read().strip()
    if not token:
        raise SystemExit("--token ou --token-file est requis")

    url, ssn = resolve(args.host, token, args.serial, args.channel, args.quality)
    full = f"{url}&ssn={ssn}&auth=1&biz=4&cln=100"

    out = sys.stdout.buffer
    depack = Depacketizer()
    written = 0
    ws = WebSocket(full, origin=f"https://{args.host}")
    try:
        for opcode, payload in ws.messages():
            if opcode == 0x1:  # texte : statut
                status = json.loads(payload)
                if status.get("statusCode") != 0:
                    raise SystemExit(f"flux refuse : {payload!r}")
                continue
            if payload[:4] == b"IMKH":
                continue
            chunk = depack.feed(payload)
            if chunk:
                out.write(chunk)
                out.flush()
                written += len(chunk)
            if args.max_bytes and written >= args.max_bytes:
                break
    finally:
        ws.close()
    print(f"{written} octets H.265", file=sys.stderr)


if __name__ == "__main__":
    main()
