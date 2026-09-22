"""Expose le flux live du cloud EZVIZ a Home Assistant, sans rien d'externe.

Les cameras sur batterie n'ouvrent aucun serveur RTSP : sur une HB8C, les 200
premiers ports TCP sont filtres, camera eveillee. L'URL locale que construit
l'integration amont ne repond donc jamais.

Le cloud EZVIZ, lui, diffuse. `pyezvizapi.cloud_stream` sait en tirer du
MPEG-TS -- mais en ecrivant dans un flux binaire, de maniere bloquante. Or le
moteur `stream` de Home Assistant veut une URL a ouvrir avec ffmpeg.

Ce module fait la jonction : une vue HTTP interne qui rend le MPEG-TS, et une
URL signee que la camera renvoie comme source. Aucun add-on, aucun fichier,
aucun jeton a gerer : la session du compte EZVIZ suffit, et la bibliotheque la
renouvelle elle-meme quand elle expire.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import os
import socket
import ssl
import struct
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback

if TYPE_CHECKING:
    from .coordinator import EzvizDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

STREAM_URL = "/api/ezviz_cloud_stream/{serial}"
STREAM_VIEW_NAME = "api:ezviz_cloud_stream"

# La signature doit survivre a la session de visionnage, pas plus.
SIGNATURE_LIFETIME = timedelta(hours=12)

# Le producteur remplit d'avance pendant que le consommateur ecrit ; au-dela on
# le laisse bloquer, sinon une connexion lente ferait gonfler la memoire.
QUEUE_SIZE = 64

# Repli si le composant http ne publie pas son port.
DEFAULT_HTTP_PORT = 8123

# Taille de lecture sur la sortie de ffmpeg.
BLOCK_SIZE = 32 * 1024

# Sondage d'entree de ffmpeg, dimensionne pour le DIRECT et non pour l'analyse.
PROBE_SIZE = 96 * 1024        # octets
ANALYZE_DURATION = 1_000_000  # microsecondes, soit une seconde

# Au-dela, on considere que cette tentative ne donnera rien et on passe a la
# suivante. Le direct ne tolere pas qu'on attende plus longtemps.
FIRST_OUTPUT_TIMEOUT = 8.0

# Configuration retenue par camera, apres une premiere ouverture reussie.
#
# L'audio de ces modeles echoue quasi systematiquement (piste mp2 annoncee a
# « 0 canaux »). Le retenter a CHAQUE ouverture coute huit secondes avant meme
# d'essayer ce qui marche : sur une camera qu'on ouvre pour voir ce qui se
# passe maintenant, c'est redhibitoire.
_WORKING_CODEC: dict[str, int] = {}

# Delai laisse a la camera pour commencer a diffuser apres son reveil.
WAKE_SETTLE = 3.0

# ⛔ TRANSPORT WEBSOCKET, ET NON TCP.
#
# Mesure du 2026-09-22 : le transport TCP de la bibliotheque ne livre qu'UNE
# IMAGE PAR SECONDE -- verifie sans transcodage, l'encodeur hors de cause. Le
# WebSocket, sur le meme serveur et avec le meme jeton de session, rend
# 2,2 Mbps de H.265 propre. C'est le meme point d'entree, pas le meme tuyau.
#
# Le port du WebSocket differe de celui de ysproto ; on essaie le connu d'abord.
WEBSOCKET_PORTS = (20006, 8666)

# Entete proprietaire precedant les paquets RTP.
IMKH_HEADER = b"IMKH"


# Deux tentatives, dans cet ordre.
#
# Ces cameras annoncent parfois une piste audio mp2 a « 0 canaux » : ffmpeg n'en
# deduit ni taille de trame ni frequence, refuse d'ecrire l'en-tete MPEG-TS, et
# la VIDEO -- parfaitement valide -- tombe avec elle. On retente alors sans le
# son : mieux vaut une image muette que pas d'image.
# ⛔ ON TRANSCODE EN H.264, ON NE COPIE PAS.
#
# Ces cameras emettent du H.265 en 2560x1440. Copier ce flux tel quel deplace
# le cout sur le navigateur -- Chrome decode mal le HEVC -- et go2rtc finit
# parfois par le retranscoder de son cote. Resultat mesure : des gels toutes
# les deux ou trois secondes.
#
# Un transcodage unique en H.264 720p coute au Pi, mais rend un flux que TOUT
# navigateur lit nativement. C'est ce que faisait le montage go2rtc manuel,
# et c'est pourquoi LUI etait fluide.
# Hauteur cible du transcodage. Le Pi 5 n'a pas d'encodeur H.264 materiel :
# libx264 tourne en logiciel, et le cout suit le nombre de pixels.
#   720p  0,9 Mpx   confortable
#   1080p 2,1 Mpx   tient sans effort en ultrafast
#   1440p 3,7 Mpx   a la limite -- et un encodeur en retard ne ralentit pas,
#                   il PERD des images, ce qui saccade
TARGET_WIDTH = 1920

VIDEO_ARGS = [
    "-vf", f"scale={TARGET_WIDTH}:-2",
    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
    "-g", "30",
    "-b:v", "2M",
]

# Deux tentatives, dans cet ordre.
#
# Ces cameras annoncent parfois une piste audio mp2 a « 0 canaux » : ffmpeg n'en
# deduit ni taille de trame ni frequence, refuse d'ecrire l'en-tete MPEG-TS, et
# la VIDEO -- parfaitement valide -- tombe avec elle. On retente alors sans le
# son : mieux vaut une image muette que pas d'image.
CODEC_ATTEMPTS: tuple[tuple[list[str], str], ...] = (
    ([*VIDEO_ARGS, "-c:a", "aac", "-ac", "1", "-ar", "16000"], "avec audio"),
    ([*VIDEO_ARGS, "-an"], "sans audio"),
)


ANNEX_B_START = b"\x00\x00\x00\x01"
RTP_VERSION_2 = 0x80
H265_FRAGMENTATION_UNIT = 49


class _Depacketizer:
    """RTP -> H.265 Annex-B (RFC 7798).

    Le transport WebSocket transporte du H.265 en RTP, fragmente selon la
    RFC 7798. La bibliotheque n'expose pas de depaquetiseur : on le fait ici.

    Mesure du 2026-09-22 sur 26 Mo : VPS/SPS/PPS x33, IDR x33, flux decodable.
    """

    def __init__(self) -> None:
        self._fragment: bytearray | None = None

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


def _websocket_url(stream_url: str, port: int) -> str:
    """ysproto://…/live?… -> wss://…/live?… , pret pour le transport WebSocket.

    La bibliotheque bati cette URL pour son transport TCP, mais c'est le MEME
    point d'entree que celui qu'emploie le lecteur officiel en WebSocket : memes
    parametres dev, chn, stream, ssn, auth. Seuls changent le schema, le port,
    et deux valeurs que le lecteur web positionne differemment.
    """
    parts = urlsplit(stream_url)
    params = dict(parse_qsl(parts.query))
    params["cln"] = "100"  # le lecteur web s'annonce ainsi
    params["biz"] = "4"
    return urlunsplit(
        ("wss", f"{parts.hostname}:{port}", parts.path, urlencode(params), "")
    )


class _WebSocket:
    """Client WebSocket en lecture seule, strictement bibliotheque standard.

    Le conteneur de Home Assistant n'embarque ni `websockets` ni `aiohttp` pour
    un usage synchrone en fil d'executeur, et ce pont ne fait que LIRE : le
    serveur pousse le flux de lui-meme. Le masquage cote client, la
    fragmentation sortante et les extensions sont donc hors sujet.

    ⚠ Le serveur EZVIZ ne repond pas aux pings : tout keepalive applicatif
    ferme la connexion au bout d'une vingtaine de secondes.
    """

    def __init__(self, url: str, origin: str, timeout: float = 15.0) -> None:
        parts = urlsplit(url)
        port = parts.port or 443
        path = parts.path + (f"?{parts.query}" if parts.query else "")
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
            f"Origin: {origin}\r\n".encode() + b"\r\n"
        )
        self._buffer = b""
        head = self._until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"handshake refuse : {head.splitlines()[0]!r}")

    def _until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            if not (chunk := self._sock.recv(65536)):
                raise ConnectionError("fermeture pendant la poignee de main")
            self._buffer += chunk
        head, self._buffer = self._buffer.split(marker, 1)
        return head

    def _read(self, count: int) -> bytes:
        while len(self._buffer) < count:
            if not (chunk := self._sock.recv(65536)):
                raise ConnectionError("connexion fermee")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def messages(self):
        """Rendre (opcode, charge utile) pour chaque message reassemble."""
        opcode: int | None = None
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
        with suppress(OSError):
            self._sock.close()


class _QueueWriter:
    """Flux binaire qui reverse ce qu'on lui ecrit dans une file asyncio.

    `copy_cloud_stream_to_mpegts` ecrit depuis un thread d'executeur ; la
    reponse HTTP, elle, vit dans la boucle. Ce writer est le seul point de
    passage entre les deux, et il ne touche la file que par
    `call_soon_threadsafe`.
    """

    def __init__(self, hass: HomeAssistant, queue: asyncio.Queue[bytes | None]):
        self._hass = hass
        self._queue = queue
        self._loop = hass.loop

    def write(self, data: bytes) -> int:
        """Appele depuis le thread de l'executeur."""
        future = asyncio.run_coroutine_threadsafe(self._queue.put(data), self._loop)
        future.result()  # bloque le producteur si le consommateur prend du retard
        return len(data)

    def flush(self) -> None:
        """Rien a vider : la file EST le tampon."""

    def close(self) -> None:
        """Signale la fin du flux au consommateur."""
        asyncio.run_coroutine_threadsafe(self._queue.put(None), self._loop)


class EzvizCloudStreamView(HomeAssistantView):
    """Rend le flux live d'une camera en MPEG-TS."""

    url = STREAM_URL
    name = STREAM_VIEW_NAME
    # L'URL est signee : ffmpeg n'a pas de jeton porteur a presenter.
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Retenir hass pour retrouver le client au moment de la requete."""
        self.hass = hass

    async def get(self, request: web.Request, serial: str) -> web.StreamResponse:
        """Diffuser le flux de la camera demandee."""
        client = _find_client(self.hass, serial)
        if client is None:
            raise web.HTTPNotFound(text=f"Unknown EZVIZ camera {serial}")

        response = web.StreamResponse(headers={"Content-Type": "video/mp2t"})
        await response.prepare(request)

        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=QUEUE_SIZE)
        writer = _QueueWriter(self.hass, queue)

        ffmpeg_binary = get_ffmpeg_manager(self.hass).binary

        def _attempt(codec_args: list[str], label: str) -> bool:
            """Une tentative de diffusion. Rend True si des octets sont sortis.

            Le basculement se decide sur la PREMIERE sortie de ffmpeg : une fois
            qu'on a ecrit dans la reponse HTTP, on ne peut plus recommencer.
            """
            import select  # noqa: PLC0415
            import subprocess  # noqa: PLC0415
            import time  # noqa: PLC0415
            from threading import Thread  # noqa: PLC0415

            from pyezvizapi.cloud_stream import (  # noqa: PLC0415
                get_cloud_stream_info,
            )

            remux: subprocess.Popen[bytes] | None = None
            websocket: _WebSocket | None = None
            try:
                # Reveiller la camera AVANT d'ouvrir le flux, puis LUI LAISSER
                # LE TEMPS de s'executer. Sur batterie, elle ne pousse rien tant
                # qu'on ne l'a pas sollicitee, et ouvrir dans la foulee de
                # l'ordre fait echouer la premiere demande.
                with suppress(HTTPError, PyEzvizError):
                    client.get_detection_sensibility(serial)
                    time.sleep(WAKE_SETTLE)

                # refresh_vtm=True : sans lui, la liste des serveurs VTM n'est
                # pas rechargee et la resolution echoue sur « Could not find VTM
                # server ». open_cloud_stream le passe par defaut ; en appelant
                # get_cloud_stream_info directement, on herite du False.
                info = get_cloud_stream_info(client, serial, refresh_vtm=True)
                origin = f"https://{urlsplit(str(info['stream_url'])).hostname}"
                for port in WEBSOCKET_PORTS:
                    url = _websocket_url(str(info["stream_url"]), port)
                    try:
                        websocket = _WebSocket(url, origin=origin)
                        break
                    except (OSError, ConnectionError) as err:
                        _LOGGER.debug(
                            "EZVIZ %s : port %s refuse (%s)", serial, port, err
                        )
                if websocket is None:
                    _LOGGER.warning("EZVIZ %s : aucun port WebSocket ouvert", serial)
                    return False

                remux = subprocess.Popen(  # noqa: S603
                    [
                        ffmpeg_binary, "-hide_banner", "-loglevel", "warning",
                        # Le flux n'a pas d'horodatage : l'horloge murale donne
                        # une base monotone, sans quoi le lecteur se fige a
                        # chaque irregularite.
                        "-use_wallclock_as_timestamps", "1",
                        "-flags", "low_delay",
                        "-probesize", str(PROBE_SIZE),
                        "-analyzeduration", str(ANALYZE_DURATION),
                        "-f", "hevc", "-i", "pipe:0",
                        *codec_args,
                        "-muxdelay", "0", "-muxpreload", "0",
                        "-f", "mpegts", "pipe:1",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    # ⛔ JAMAIS DEVNULL : c'est ce que fait la bibliotheque, et
                    # c'est pourquoi ses echecs etaient indiagnosticables.
                    stderr=subprocess.PIPE,
                )

                def _log_ffmpeg() -> None:
                    assert remux is not None and remux.stderr is not None
                    for raw in remux.stderr:
                        if line := raw.decode("utf-8", "replace").strip():
                            _LOGGER.debug("EZVIZ %s ffmpeg: %s", serial, line)

                def _feed() -> None:
                    assert remux is not None and remux.stdin is not None
                    assert websocket is not None
                    depack = _Depacketizer()
                    try:
                        for opcode, message in websocket.messages():
                            if opcode == 0x1:  # statut, en texte
                                status = json.loads(message)
                                if status.get("statusCode") != 0:
                                    _LOGGER.warning(
                                        "EZVIZ %s : flux refuse %s",
                                        serial, message[:120],
                                    )
                                    break
                                continue
                            if message[:4] == IMKH_HEADER:
                                continue
                            if annex_b := depack.feed(message):
                                remux.stdin.write(annex_b)
                        remux.stdin.close()
                    except (BrokenPipeError, OSError, ConnectionError):
                        pass  # ffmpeg ou le serveur a ferme

                Thread(target=_log_ffmpeg, daemon=True).start()
                Thread(target=_feed, daemon=True).start()

                assert remux.stdout is not None
                # Attente bornee : read() reclamerait un bloc entier et
                # bloquerait indefiniment si rien ne sort.
                ready, _, _ = select.select(
                    [remux.stdout], [], [], FIRST_OUTPUT_TIMEOUT
                )
                if not ready or not (chunk := remux.stdout.read1(BLOCK_SIZE)):
                    _LOGGER.warning(
                        "EZVIZ %s : aucune image en %s s (%s)",
                        serial, FIRST_OUTPUT_TIMEOUT, label,
                    )
                    return False

                writer.write(chunk)
                while chunk := remux.stdout.read1(BLOCK_SIZE):
                    writer.write(chunk)
                return True
            except PyEzvizError:
                _LOGGER.exception("EZVIZ cloud stream failed for %s", serial)
                return False
            except Exception:  # noqa: BLE001 - le thread ne doit jamais tuer HA
                _LOGGER.exception("Unexpected EZVIZ cloud stream error for %s", serial)
                return False
            finally:
                if websocket is not None:
                    websocket.close()
                if remux is not None and remux.poll() is None:
                    remux.kill()

        def _produce() -> None:
            try:
                known = _WORKING_CODEC.get(serial)
                order = (
                    (known, *(i for i in range(len(CODEC_ATTEMPTS)) if i != known))
                    if known is not None
                    else range(len(CODEC_ATTEMPTS))
                )
                for index in order:
                    codec_args, label = CODEC_ATTEMPTS[index]
                    if _attempt(codec_args, label):
                        _WORKING_CODEC[serial] = index
                        return
                    _LOGGER.warning(
                        "EZVIZ %s : diffusion %s impossible, repli", serial, label
                    )
                _LOGGER.warning("EZVIZ %s : aucune diffusion possible", serial)
            finally:
                writer.close()

        producer = self.hass.async_add_executor_job(_produce)
        try:
            while (chunk := await queue.get()) is not None:
                await response.write(chunk)
        except ConnectionResetError:
            _LOGGER.debug("EZVIZ cloud stream closed by client for %s", serial)
        finally:
            producer.cancel()
        return response


@callback
def _find_client(hass: HomeAssistant, serial: str) -> Any | None:
    """Retrouver le client EZVIZ qui connait ce numero de serie."""
    from .const import DOMAIN  # noqa: PLC0415 - import tardif, cycle sinon

    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator: EzvizDataUpdateCoordinator | None = getattr(
            entry, "runtime_data", None
        )
        if coordinator is not None and serial in (coordinator.data or {}):
            return coordinator.ezviz_client
    return None


@callback
def async_register_stream_view(hass: HomeAssistant) -> None:
    """Enregistrer la vue une seule fois, quel que soit le nombre de comptes."""
    if hass.data.get(f"{STREAM_VIEW_NAME}_registered"):
        return
    hass.http.register_view(EzvizCloudStreamView(hass))
    hass.data[f"{STREAM_VIEW_NAME}_registered"] = True


@callback
def async_stream_url(hass: HomeAssistant, serial: str) -> str:
    """URL absolue et signee du flux, telle que ffmpeg peut l'ouvrir.

    On vise 127.0.0.1 volontairement : ffmpeg tourne dans le meme conteneur que
    Home Assistant, et cela evite de dependre d'un `internal_url` que beaucoup
    d'instances ne renseignent pas.
    """
    signed = async_sign_path(
        hass,
        STREAM_URL.format(serial=serial),
        SIGNATURE_LIFETIME,
        use_content_user=True,
    )
    # getattr defensif : le port est un detail d'implementation du composant
    # http, et une URL de flux ne merite pas de casser sur un renommage.
    port = getattr(hass.http, "server_port", DEFAULT_HTTP_PORT)
    return f"http://127.0.0.1:{port}{signed}"
