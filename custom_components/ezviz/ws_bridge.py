#!/usr/bin/env python3
"""Pont EZVIZ : WebSocket proprietaire -> H.265 Annex-B sur la sortie standard.

Ce fichier est un SCRIPT, pas un module de l'integration. Home Assistant ne
l'importe jamais : il le lance en sous-processus, et lit sa sortie par un tube
que le systeme relie directement a ffmpeg.

Pourquoi un processus separe. Depaqueter du RTP, c'est boucler en Python sur
quelques centaines de paquets par seconde. Fait dans un fil d'executeur, ce
travail dispute le verrou global a la boucle d'evenements de Home Assistant, et
la cadence s'effondre : 6,5 images par seconde contre 10,5 pour le meme flux
traite par un interpreteur qui n'a que cela a faire. Le tube, lui, est copie
par le noyau -- pas un octet du flux ne traverse le processus de Home
Assistant.

⛔ AUCUNE DEPENDANCE, bibliotheque standard seulement : ce script doit demarrer
avec le meme interpreteur que Home Assistant sans rien exiger de plus.

L'URL du flux arrive par l'entree standard, une ligne, puis fermeture. Elle
porte le jeton de session : la passer en argument l'exposerait dans la liste
des processus.

    echo "$URL" | python3 ws_bridge.py --origin https://ieuopen.ezvizlife.com \
        | ffmpeg -f hevc -i - ...
"""

import argparse
import base64
import json
import os
import socket
import ssl
import struct
import sys
from urllib.parse import urlsplit

# Le serveur refuse les clients qui ne s'annoncent pas comme un navigateur.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

ANNEX_B_START = b"\x00\x00\x00\x01"
RTP_VERSION_2 = 0x80
H265_FRAGMENTATION_UNIT = 49
IMKH_HEADER = b"IMKH"


class WebSocket:
    """Client WebSocket en lecture seule, strictement bibliotheque standard.

    Ce pont ne fait que LIRE : le serveur pousse le flux de lui-meme. Le
    masquage cote client, la fragmentation sortante et les extensions sont donc
    hors sujet.

    ⚠ Le serveur EZVIZ ne repond pas aux pings : tout keepalive applicatif
    ferme la connexion au bout d'une vingtaine de secondes.
    """

    def __init__(self, url: str, origin: str, timeout: float = 15.0) -> None:
        parts = urlsplit(url)
        port = parts.port or 443
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        raw = socket.create_connection((parts.hostname, port), timeout=timeout)
        # ⛔ LE NOM N'EST PAS VERIFIE, LA CHAINE L'EST.
        #
        # Le serveur VTM est designe par une ADRESSE IP dans la reponse de
        # l'API, et son certificat est emis pour un nom d'hote : la
        # verification du nom echoue forcement (« IP address mismatch »). On
        # garde la validation de la chaine -- le certificat reste celui
        # d'EZVIZ -- et on renonce a la seule correspondance du nom.
        context = ssl.create_default_context()
        context.check_hostname = False
        self._sock = context.wrap_socket(raw)
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall(
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parts.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: {origin}\r\n"
            f"User-Agent: {USER_AGENT}\r\n\r\n".encode()
        )
        self._buffer = b""
        head = self._until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise SystemExit(f"handshake refuse : {head.splitlines()[0]!r}")

    def _until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("fermeture pendant la poignee de main")
            self._buffer += chunk
        head, self._buffer = self._buffer.split(marker, 1)
        return head

    def _read(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("connexion fermee")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def messages(self):
        """Rendre (opcode, charge utile) pour chaque message reassemble."""
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

            if this_opcode == 0x8:  # fermeture
                return
            if this_opcode in (0x9, 0xA):  # ping / pong
                continue
            if this_opcode != 0x0:  # nouvelle trame
                opcode, payload = this_opcode, bytearray()
            payload.extend(data)
            if fin and opcode is not None:
                yield opcode, bytes(payload)
                opcode, payload = None, bytearray()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class Depacketizer:
    """RTP -> H.265 Annex-B (RFC 7798).

    Le flux arrive en RTP, payload type 96, fragmente en unites FU. Chaque
    fragment porte deux octets d'entete de charge utile puis un octet FU dont
    les bits de poids fort marquent le debut et la fin de l'unite NAL.
    """

    def __init__(self) -> None:
        self._fragment = None

    def feed(self, packet: bytes) -> bytes:
        """Rendre les octets Annex-B produits par ce paquet RTP."""
        if len(packet) < 13 or packet[0] & 0xC0 != RTP_VERSION_2:
            return b""
        header = 12 + 4 * (packet[0] & 0x0F)
        if packet[0] & 0x10:  # extension
            if len(packet) < header + 4:
                return b""
            header += 4 + 4 * int.from_bytes(packet[header + 2 : header + 4], "big")
        payload = packet[header:]
        if len(payload) < 3:
            return b""

        if (payload[0] >> 1) & 0x3F != H265_FRAGMENTATION_UNIT:
            return ANNEX_B_START + payload

        fu = payload[2]
        if fu & 0x80:  # debut de fragment
            inner = ((fu & 0x3F) << 1) | (payload[0] & 0x81)
            self._fragment = bytearray([inner, payload[1]])
        if self._fragment is None:
            return b""
        self._fragment.extend(payload[3:])
        if fu & 0x40:  # fin de fragment
            out = ANNEX_B_START + bytes(self._fragment)
            self._fragment = None
            return out
        return b""


def main() -> int:
    """Lire l'URL sur l'entree standard, ecrire du H.265 sur la sortie."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--origin", required=True, help="Origin annonce au serveur VTM"
    )
    parser.add_argument(
        "--url",
        help="URL wss complete ; par defaut lue sur l'entree standard, ce qui "
        "evite d'exposer le jeton de session dans la liste des processus",
    )
    args = parser.parse_args()

    url = args.url or sys.stdin.readline().strip()
    if not url:
        print("aucune URL de flux recue", file=sys.stderr)
        return 2

    out = sys.stdout.buffer
    depack = Depacketizer()
    written = 0
    websocket = WebSocket(url, origin=args.origin)
    try:
        for opcode, message in websocket.messages():
            if opcode == 0x1:  # statut, en texte
                status = json.loads(message)
                if status.get("statusCode") != 0:
                    print(f"flux refuse : {message[:120]!r}", file=sys.stderr)
                    return 1
                continue
            if message[:4] == IMKH_HEADER:
                continue
            annex_b = depack.feed(message)
            if annex_b:
                out.write(annex_b)
                out.flush()
                written += len(annex_b)
    except (BrokenPipeError, ConnectionError, OSError) as err:
        # ffmpeg s'est arrete, ou le serveur a coupe : ce n'est pas une panne,
        # c'est la fin normale d'une session de visionnage.
        print(f"fin de flux apres {written} octets : {err}", file=sys.stderr)
        return 0
    finally:
        websocket.close()
    print(f"fin de flux apres {written} octets", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
