"""Service de consultation des messages EZVIZ.

Le capteur « nom du type de la derniere alarme » ne montre qu'UN message : le
plus recent que le cloud classe dans le paquet « All alarm » (sous-type 92).
Tout ce qu'EZVIZ range ailleurs -- et c'est peut-etre le cas des ouvertures de
porte -- reste invisible.

Ce service rend la liste brute, telle que l'API la renvoie, pour n'importe quel
sous-type. Il sert a decouvrir ce qu'un appareil rapporte reellement, sans
supposer.

    action: ezviz.fetch_messages
    data:
      serial: BH7719633
      subtype: "92,2701,9904"
      limit: 20
"""

from __future__ import annotations

from functools import partial
import logging
from typing import Any

import voluptuous as vol
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

_LOGGER = logging.getLogger(__name__)

SERVICE_FETCH_MESSAGES = "fetch_messages"

ATTR_SERIAL = "serial"
ATTR_SUBTYPE = "subtype"
ATTR_LIMIT = "limit"
ATTR_DATE = "date"

# Le paquet que l'application affiche sous « Toutes les alarmes ».
DEFAULT_SUBTYPE = "92"

SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_SERIAL): cv.string,
        vol.Optional(ATTR_SUBTYPE, default=DEFAULT_SUBTYPE): cv.string,
        vol.Optional(ATTR_LIMIT, default=20): vol.All(int, vol.Range(min=1, max=50)),
        # Une date vide demande les messages les plus recents, toutes dates
        # confondues : c'est ce que fait l'application a l'ouverture.
        vol.Optional(ATTR_DATE, default=""): cv.string,
    }
)


def _cloud_client(hass: HomeAssistant) -> Any:
    """Le client du premier compte EZVIZ charge."""
    from .const import DOMAIN  # noqa: PLC0415 - import tardif, cycle sinon

    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = getattr(entry, "runtime_data", None)
        if coordinator is not None:
            return coordinator.ezviz_client
    raise HomeAssistantError("Aucun compte EZVIZ n'est charge")


async def _fetch_messages(call: ServiceCall) -> ServiceResponse:
    """Rendre la liste brute des messages, sans interpretation."""
    hass = call.hass
    client = _cloud_client(hass)
    serial = call.data.get(ATTR_SERIAL)

    try:
        payload = await hass.async_add_executor_job(
            partial(
                client.get_device_messages_list,
                serials=serial,
                s_type=call.data[ATTR_SUBTYPE],
                limit=call.data[ATTR_LIMIT],
                date=call.data[ATTR_DATE],
                end_time="",
            )
        )
    except (HTTPError, PyEzvizError) as err:
        raise HomeAssistantError(f"EZVIZ a refuse la demande : {err}") from err

    messages = payload.get("message") or payload.get("messages") or []
    if not isinstance(messages, list):
        messages = []

    # On rend aussi le reste de la reponse : les champs qu'on ignore
    # aujourd'hui sont exactement ceux qu'on cherchera demain.
    return {
        "count": len(messages),
        "messages": messages,
        "meta": payload.get("meta", {}),
    }


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Enregistrer le service une seule fois, quel que soit le nombre de comptes."""
    from .const import DOMAIN  # noqa: PLC0415 - import tardif, cycle sinon

    if hass.services.has_service(DOMAIN, SERVICE_FETCH_MESSAGES):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_FETCH_MESSAGES,
        _fetch_messages,
        schema=SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
