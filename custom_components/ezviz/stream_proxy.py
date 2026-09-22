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
import itertools
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web
from pyezvizapi.exceptions import PyEzvizError

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
FIRST_OUTPUT_TIMEOUT = 12.0

# En-tete de pack MPEG-PS : le seul point ou ffmpeg sait se synchroniser.
MPEG_PS_PACK_HEADER = b"\x00\x00\x01\xba"

# Combien de paquets on accepte de traverser pour trouver ce point.
SYNC_SEARCH_LIMIT = 200

# Deux tentatives, dans cet ordre.
#
# Ces cameras annoncent parfois une piste audio mp2 a « 0 canaux » : ffmpeg n'en
# deduit ni taille de trame ni frequence, refuse d'ecrire l'en-tete MPEG-TS, et
# la VIDEO -- parfaitement valide -- tombe avec elle. On retente alors sans le
# son : mieux vaut une image muette que pas d'image.
CODEC_ATTEMPTS: tuple[tuple[list[str], str], ...] = (
    (["-c:v", "copy", "-c:a", "aac", "-ac", "1", "-ar", "16000"], "avec audio"),
    (["-an", "-c:v", "copy"], "sans audio"),
)


ANNEX_B_START = b"\x00\x00\x00\x01"
RTP_VERSION_2 = 0x80
H265_FRAGMENTATION_UNIT = 49


class _Depacketizer:
    """RTP -> H.265 Annex-B (RFC 7798).

    `cloud_stream.copy_cloud_stream_to_mpegts` suppose que la charge utile est
    du MPEG-PS et lance `ffmpeg -f mpeg`. Les cameras sur batterie emettent du
    H.265 en RTP : ffmpeg sort alors en EINVAL (code 234). La bibliotheque
    connait pourtant ce cas -- son enumeration StreamTransport liste RTP -- mais
    le copieur ne l'exploite pas. On dépaquetise donc nous-memes.

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
            from threading import Thread  # noqa: PLC0415

            from pyezvizapi.cloud_stream import open_cloud_stream  # noqa: PLC0415
            from pyezvizapi.stream import (  # noqa: PLC0415
                StreamTransport,
                detect_transport,
            )

            remux: subprocess.Popen[bytes] | None = None
            try:
                with open_cloud_stream(client, serial) as stream:
                    stream.start()
                    payloads = stream.iter_payloads()

                    # ⛔ ATTENDRE UN EN-TETE DE PACK AVANT D'ALIMENTER FFMPEG.
                    #
                    # Le premier paquet recu est rarement un pack header : c'est
                    # le plus souvent un PES isole (000001bd, 000001c0). Demarre
                    # au milieu d'un PES, ffmpeg ne se synchronise jamais et sort
                    # en EINVAL -- le « status 234 » de
                    # copy_cloud_stream_to_mpegts, qui alimente des le premier
                    # paquet et souffre du meme defaut.
                    first = b""
                    for candidate in itertools.islice(payloads, SYNC_SEARCH_LIMIT):
                        if candidate.startswith(MPEG_PS_PACK_HEADER):
                            first = candidate
                            break
                    if not first:
                        _LOGGER.warning(
                            "EZVIZ %s : aucun en-tete de pack MPEG-PS", serial
                        )
                        return False

                    transport = detect_transport(first)
                    _LOGGER.debug(
                        "EZVIZ %s : transport %s, essai %s", serial, transport.name,
                        label,
                    )
                    input_format = (
                        "hevc" if transport is StreamTransport.RTP else "mpeg"
                    )
                    depack = (
                        _Depacketizer()
                        if transport is StreamTransport.RTP
                        else None
                    )

                    remux = subprocess.Popen(  # noqa: S603
                        [
                            ffmpeg_binary, "-hide_banner", "-loglevel", "warning",
                            "-fflags", "nobuffer", "-flags", "low_delay",
                            # ⛔ SONDAGE COURT. A 0,2 Mbps, un probesize de
                            # 5 Mo demande TROIS MINUTES avant la premiere
                            # image : go2rtc expire, le WebRTC ne s'etablit
                            # jamais, et le lecteur retombe sur du HLS
                            # tamponne. Quelques centaines de ko suffisent a
                            # reconnaitre un flux video.
                            "-probesize", str(PROBE_SIZE),
                            "-analyzeduration", str(ANALYZE_DURATION),
                            "-f", input_format, "-i", "pipe:0",
                            *codec_args,
                            "-f", "mpegts", "pipe:1",
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        # ⛔ JAMAIS DEVNULL : c'est ce que fait la bibliotheque,
                        # et c'est pourquoi son echec etait indiagnosticable.
                        stderr=subprocess.PIPE,
                    )

                    def _log_ffmpeg() -> None:
                        assert remux is not None and remux.stderr is not None
                        for raw in remux.stderr:
                            if line := raw.decode("utf-8", "replace").strip():
                                _LOGGER.debug("EZVIZ %s ffmpeg: %s", serial, line)

                    def _feed() -> None:
                        assert remux is not None and remux.stdin is not None
                        try:
                            for payload in itertools.chain([first], payloads):
                                data = depack.feed(payload) if depack else payload
                                if data:
                                    remux.stdin.write(data)
                            remux.stdin.close()
                        except (BrokenPipeError, OSError):
                            pass  # ffmpeg a ferme : sa sortie d'erreur le dira

                    Thread(target=_log_ffmpeg, daemon=True).start()
                    Thread(target=_feed, daemon=True).start()

                    assert remux.stdout is not None

                    # ⛔ ATTENTE BORNEE, ET LECTURES NON BLOQUANTES.
                    #
                    # read(n) attend n octets ENTIERS ou la fin du flux : si
                    # ffmpeg ne produit rien sans pour autant mourir, on attend
                    # indefiniment et le repli n'a jamais lieu. select() borne
                    # l'attente, read1() rend ce qui est disponible sans
                    # reclamer un bloc complet -- ce qui compte pour du direct.
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
                if remux is not None and remux.poll() is None:
                    remux.kill()

        def _produce() -> None:
            try:
                for codec_args, label in CODEC_ATTEMPTS:
                    if _attempt(codec_args, label):
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
