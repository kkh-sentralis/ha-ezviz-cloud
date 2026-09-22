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
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web
from pyezvizapi.exceptions import PyEzvizError

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

        def _produce() -> None:
            # Import tardif : ce module tire subprocess, threading et ffmpeg, et
            # l'importer au chargement de la plateforme bloque la boucle.
            from pyezvizapi.cloud_stream import (  # noqa: PLC0415
                copy_cloud_stream_to_mpegts,
            )

            try:
                copy_cloud_stream_to_mpegts(client, serial, writer)
            except PyEzvizError:
                _LOGGER.exception("EZVIZ cloud stream failed for %s", serial)
            except Exception:  # noqa: BLE001 - le thread ne doit jamais tuer HA
                _LOGGER.exception("Unexpected EZVIZ cloud stream error for %s", serial)
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
