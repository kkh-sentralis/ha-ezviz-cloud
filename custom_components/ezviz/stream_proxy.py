"""Expose le flux live du cloud EZVIZ a Home Assistant, sans rien d'externe.

Les cameras sur batterie n'ouvrent aucun serveur RTSP : sur une HB8C, les 200
premiers ports TCP sont filtres, camera eveillee. L'URL locale que construit
l'integration amont ne repond donc jamais. Leur seul flux vivant passe par un
WebSocket proprietaire, celui du lecteur officiel.

Ce module fait la jonction : une vue HTTP interne qui rend du MPEG-TS, et une
URL signee que la camera renvoie comme source. Aucun add-on, aucun fichier,
aucun jeton a gerer.

Le chemin des octets, depuis la mise en place du pont en sous-processus :

    ws_bridge.py  --tube-->  ffmpeg  --tube-->  boucle asyncio  -->  reponse

Le premier tube est copie par le NOYAU : pas un octet du flux ne traverse
l'interpreteur de Home Assistant, qui ne voit plus que du MPEG-TS deja
transcode, par blocs de 32 Ko.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Mapping
from datetime import timedelta
from threading import Lock
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from aiohttp import web
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback

if TYPE_CHECKING:
    from .coordinator import EzvizDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# L'API Open Platform exige un agent de navigateur pour resoudre une adresse
# ezopen : sans lui, elle refuse la demande.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

STREAM_URL = "/api/ezviz_cloud_stream/{serial}"
STREAM_VIEW_NAME = "api:ezviz_cloud_stream"

# Le pont, livre avec l'integration : l'utilisateur n'a aucun fichier a poser.
BRIDGE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ws_bridge.py")

# La signature doit survivre a la session de visionnage, pas plus.
SIGNATURE_LIFETIME = timedelta(hours=12)

# Repli si le composant http ne publie pas son port.
DEFAULT_HTTP_PORT = 8123

# Taille de lecture sur la sortie de ffmpeg.
BLOCK_SIZE = 32 * 1024

# Sondage d'entree de ffmpeg, dimensionne pour le DIRECT et non pour l'analyse.
PROBE_SIZE = 32 * 1024      # octets
ANALYZE_DURATION = 500_000  # microsecondes, soit une demi-seconde

# Au-dela, on considere que cette ouverture ne donnera rien. Le direct ne
# tolere pas qu'on attende plus longtemps.
FIRST_OUTPUT_TIMEOUT = 10.0


def _codec_args(width: int) -> list[str]:
    """Les arguments de codage, pour une largeur donnee.

    ⛔ JAMAIS D'AUDIO. Ces cameras annoncent une piste mp2 a « 0 canaux » :
    ffmpeg n'en deduit ni taille de trame ni frequence, refuse d'ecrire
    l'en-tete MPEG-TS, et la video tombe avec elle. Il faut huit secondes pour
    le constater, et cette ouverture perdue consomme une session WebSocket que
    le cloud rationne -- mesure : deux sessions, et le debit tombe de 1,50 a
    0,15 Mbps.

    Une seule ouverture, une seule session, pas de repli : c'est ce que fait le
    montage de reference, et c'est pourquoi il demarre en quatre secondes.

    Une largeur NULLE laisse passer le flux tel quel, sans decodage ni
    reencodage : cadence pleine, cout processeur nul, au prix d'un H.265 que
    tous les navigateurs ne lisent pas aussi bien -- dans le lecteur de Home
    Assistant, cela donne souvent une image noire.
    """
    if not width:
        return ["-c:v", "copy", "-an"]
    return [
        "-vf", f"scale={width}:-2",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-g", "30",
        "-b:v", "2M",
        "-an",
    ]


class _OpenPlatformToken:
    """Jeton Open Platform, renouvele tout seul.

    L'AppKey et l'AppSecret ne periment pas ; le jeton qu'ils produisent vaut
    sept jours. On le garde en cache et on le redemande des qu'il approche de
    son terme, ou des que le serveur le refuse. L'utilisateur saisit ses
    identifiants une fois et n'y revient jamais.
    """

    # Marge avant le terme : on renouvelle sans attendre le refus.
    RENEW_BEFORE = 3600.0

    def __init__(self) -> None:
        self._value: str | None = None
        self._expires_at = 0.0
        self._lock = Lock()

    def get(
        self, host: str, app_key: str, app_secret: str, *, force: bool = False
    ) -> str:
        """Rendre un jeton valide, en le renouvelant si besoin."""
        with self._lock:
            now = time.time()
            if not force and self._value and now < self._expires_at - self.RENEW_BEFORE:
                return self._value
            body = urlencode({"appKey": app_key, "appSecret": app_secret}).encode()
            request = Request(
                f"https://{host}/api/lapp/token/get",
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urlopen(request, timeout=20) as response:  # noqa: S310
                answer = json.loads(response.read())
            if answer.get("code") != "200":
                raise PyEzvizError(
                    f"jeton Open Platform refuse : {answer.get('code')} "
                    f"{answer.get('msg')}"
                )
            data = answer["data"]
            self._value = str(data["accessToken"])
            # expireTime est en millisecondes.
            self._expires_at = float(data.get("expireTime", 0)) / 1000
            _LOGGER.debug(
                "EZVIZ : jeton Open Platform renouvele, valable jusqu'au %s",
                time.strftime("%Y-%m-%d", time.localtime(self._expires_at)),
            )
            return self._value


_TOKEN = _OpenPlatformToken()


def _resolve_ezopen(
    host: str, token: str, serial: str, channel: int = 1
) -> tuple[str, str]:
    """Adresse ezopen:// -> (url wss, jeton de session).

    ⚠ multipart obligatoire : en urlencode, l'API repond « parametre vide ».
    """
    boundary = uuid.uuid4().hex
    fields = {
        "accessToken": token,
        "ezopen": f"ezopen://open.ezviz.com/{serial}/{channel}.hd.live",
        "isFlv": "false",
        "isHttp": "false",
        "userAgent": BROWSER_USER_AGENT,
    }
    body = b"".join(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
        for name, value in fields.items()
    ) + f"--{boundary}--\r\n".encode()

    request = Request(
        f"https://{host}/api/lapp/live/url/ezopen",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": BROWSER_USER_AGENT,
        },
    )
    with urlopen(request, timeout=25) as response:  # noqa: S310
        answer = json.loads(response.read())
    if answer.get("code") != "200":
        raise PyEzvizError(
            f"resolution ezopen refusee : {answer.get('code')} {answer.get('msg')}"
        )
    return str(answer["data"]["url"]), str(answer["data"]["token"])


def _live_url(host: str, app_key: str, app_secret: str, serial: str) -> str:
    """L'URL WebSocket complete, prete a etre ouverte. Bloquant : executeur.

    Sans `ssn`, `auth`, `biz` et `cln`, le serveur repond « 6110 get stream
    error » : ce sont les valeurs que positionne le lecteur officiel.
    """
    token = _TOKEN.get(host, app_key, app_secret)
    try:
        url, session = _resolve_ezopen(host, token, serial)
    except PyEzvizError:
        # Jeton expire ou revoque : on en redemande un et on reessaie UNE fois.
        # C'est tout ce que l'utilisateur aura a faire -- c'est-a-dire rien.
        token = _TOKEN.get(host, app_key, app_secret, force=True)
        url, session = _resolve_ezopen(host, token, serial)
    return f"{url}&ssn={session}&auth=1&biz=4&cln=100"


def _nudge(client: Any, serial: str) -> None:
    """Toucher la camera pour la sortir de veille, sans attendre de reponse.

    Le montage de reference ne reveille pas du tout et diffuse quand meme. On
    envoie donc la sollicitation, mais elle ne doit rien couter au temps
    d'ouverture : elle part en parallele de la resolution de l'adresse.
    """
    try:
        client.get_detection_sensibility(serial)
    except Exception as err:  # noqa: BLE001 - une sollicitation ne doit rien casser
        _LOGGER.debug("EZVIZ %s : reveil sans effet (%s)", serial, err)


async def _drain(stream: asyncio.StreamReader | None, serial: str, who: str) -> None:
    """Journaliser la sortie d'erreur d'un enfant, et surtout la VIDER.

    ⛔ Sans cette lecture, le tube se remplit et l'enfant se bloque en ecrivant
    dedans. C'est aussi la seule fenetre sur ses echecs : `DEVNULL` est ce qui
    rendait ceux de la bibliotheque indiagnosticables.
    """
    if stream is None:
        return
    with suppress(Exception):  # noqa: BLE001 - journaliser ne doit rien casser
        while raw := await stream.readline():
            if line := raw.decode("utf-8", "replace").strip():
                _LOGGER.debug("EZVIZ %s %s: %s", serial, who, line)


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
        from .const import (  # noqa: PLC0415 - import tardif, cycle sinon
            CONF_APP_KEY,
            CONF_APP_SECRET,
            CONF_OPEN_HOST,
            CONF_STREAM_WIDTH,
            DEFAULT_OPEN_HOST,
            DEFAULT_STREAM_WIDTH,
        )

        found = _find_account(self.hass, serial)
        if found is None:
            raise web.HTTPNotFound(text=f"Unknown EZVIZ camera {serial}")
        client, options = found

        app_key = options.get(CONF_APP_KEY, "")
        app_secret = options.get(CONF_APP_SECRET, "")
        open_host = options.get(CONF_OPEN_HOST) or DEFAULT_OPEN_HOST
        stream_width = int(options.get(CONF_STREAM_WIDTH, DEFAULT_STREAM_WIDTH))
        if not app_key or not app_secret:
            _LOGGER.warning(
                "EZVIZ %s : flux indisponible, AppKey et AppSecret non "
                "renseignes dans les options de l'integration",
                serial,
            )
            raise web.HTTPServiceUnavailable(
                text="EZVIZ Open Platform credentials are not configured"
            )

        # La sollicitation de reveil part maintenant et on ne l'attend pas :
        # elle serait du cout pur pour une camera deja eveillee, c'est-a-dire
        # la plupart du temps.
        self.hass.async_add_executor_job(_nudge, client, serial)

        try:
            live_url = await self.hass.async_add_executor_job(
                _live_url, open_host, app_key, app_secret, serial
            )
        except (PyEzvizError, HTTPError, OSError) as err:
            _LOGGER.error("EZVIZ %s : adresse du flux introuvable (%s)", serial, err)
            raise web.HTTPBadGateway(text="EZVIZ live address unavailable") from err

        return await self._pipe(request, serial, live_url, open_host, stream_width)

    async def _pipe(
        self,
        request: web.Request,
        serial: str,
        live_url: str,
        open_host: str,
        stream_width: int,
    ) -> web.StreamResponse:
        """Monter pont -> ffmpeg -> reponse, et tenir jusqu'a la fermeture."""
        ffmpeg_binary = get_ffmpeg_manager(self.hass).binary

        # Le tube qui relie les deux enfants. Le noyau y copie les octets
        # directement : Home Assistant n'en voit aucun.
        read_fd, write_fd = os.pipe()
        bridge: asyncio.subprocess.Process | None = None
        remux: asyncio.subprocess.Process | None = None
        logs: list[asyncio.Task[None]] = []
        try:
            try:
                bridge = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", BRIDGE_SCRIPT,
                    "--origin", f"https://{open_host}",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=write_fd,
                    stderr=asyncio.subprocess.PIPE,
                )
            finally:
                # Une fois herite par l'enfant, ce bout ne nous sert plus ; le
                # garder ouvert empecherait ffmpeg de voir la fin du flux.
                os.close(write_fd)
                write_fd = -1

            # L'URL porte le jeton de session : elle passe par l'entree
            # standard, pas par la ligne de commande ou tout le systeme la
            # lirait.
            assert bridge.stdin is not None
            bridge.stdin.write(live_url.encode() + b"\n")
            await bridge.stdin.drain()
            bridge.stdin.close()

            remux = await asyncio.create_subprocess_exec(
                ffmpeg_binary, "-hide_banner", "-loglevel", "warning",
                # Le flux n'a pas d'horodatage : l'horloge murale donne une
                # base monotone, sans quoi le lecteur se fige a chaque
                # irregularite.
                "-use_wallclock_as_timestamps", "1",
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "-probesize", str(PROBE_SIZE),
                "-analyzeduration", str(ANALYZE_DURATION),
                "-f", "hevc", "-i", "pipe:0",
                *_codec_args(stream_width),
                "-muxdelay", "0", "-muxpreload", "0",
                "-f", "mpegts", "pipe:1",
                stdin=read_fd,
                stdout=asyncio.subprocess.PIPE,
                # ⛔ JAMAIS DEVNULL : c'est ce que fait la bibliotheque, et
                # c'est pourquoi ses echecs etaient indiagnosticables.
                stderr=asyncio.subprocess.PIPE,
            )
            os.close(read_fd)
            read_fd = -1

            logs = [
                asyncio.create_task(_drain(bridge.stderr, serial, "pont")),
                asyncio.create_task(_drain(remux.stderr, serial, "ffmpeg")),
            ]

            assert remux.stdout is not None
            try:
                chunk = await asyncio.wait_for(
                    remux.stdout.read(BLOCK_SIZE), FIRST_OUTPUT_TIMEOUT
                )
            except TimeoutError:
                chunk = b""
            if not chunk:
                _LOGGER.warning(
                    "EZVIZ %s : aucune image en %s s", serial, FIRST_OUTPUT_TIMEOUT
                )
                raise web.HTTPGatewayTimeout(text="EZVIZ live stream produced no data")

            # On ne prepare la reponse qu'ici : tant qu'aucun octet n'est parti,
            # un echec peut encore se dire avec un vrai code HTTP.
            response = web.StreamResponse(headers={"Content-Type": "video/mp2t"})
            await response.prepare(request)
            # Le lecteur qui se ferme coupe la connexion : c'est la fin normale
            # d'une session, pas une panne a remonter en erreur.
            with suppress(ConnectionResetError):
                await response.write(chunk)
                while chunk := await remux.stdout.read(BLOCK_SIZE):
                    await response.write(chunk)
        finally:
            for fd in (read_fd, write_fd):
                if fd >= 0:
                    os.close(fd)
            for task in logs:
                task.cancel()
            for child in (remux, bridge):
                if child is not None and child.returncode is None:
                    with suppress(ProcessLookupError):
                        child.kill()
        return response


@callback
def _find_account(
    hass: HomeAssistant, serial: str
) -> tuple[Any, Mapping[str, Any]] | None:
    """Retrouver le client EZVIZ et les options du compte qui gere ce serie."""
    from .const import DOMAIN  # noqa: PLC0415 - import tardif, cycle sinon

    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator: EzvizDataUpdateCoordinator | None = getattr(
            entry, "runtime_data", None
        )
        if coordinator is not None and serial in (coordinator.data or {}):
            return coordinator.ezviz_client, entry.options
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
