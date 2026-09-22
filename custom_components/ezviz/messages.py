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
SERVICE_FETCH_DEVICE_INFO = "fetch_device_info"

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


# Capacites liees au verrouillage, telles que `SupportExt` les numerote.
# Un interphone qui sait ouvrir en declare au moins une.
LOCK_CAPABILITIES = {
    "78": "SupportUnLock",
    "415": "SupportAssociateDoorlockOnline",
    "541": "SupportWifiLock",
    "592": "SupportRemoteOpenDoor",
    "648": "SupportRemoteUnlock",
    "662": "SupportLocalLockGate",
    "679": "SupportLockConfigWay",
    "690": "SupportDoorLookStateShow",
}

INFO_SCHEMA = vol.Schema({vol.Required(ATTR_SERIAL): cv.string})


async def _fetch_device_info(call: ServiceCall) -> ServiceResponse:
    """Rendre ce qu'un appareil declare savoir faire, et qui peut l'ouvrir.

    `CardKeyInfo` n'est pas expose par la bibliotheque : c'est un appel direct,
    repere en observant l'application officielle. Il rend les identifiants
    enroles -- badges, visages, paumes -- avec les noms qu'on leur a donnes.
    """
    hass = call.hass
    client = _cloud_client(hass)
    serial = call.data[ATTR_SERIAL]

    found = _find_coordinator(hass)
    device = (found.data or {}).get(serial, {}) if found else {}
    support = device.get("supportExt") or {}

    def _card_keys() -> Any:
        host = client._token.get("api_url")  # noqa: SLF001 - pas d'accesseur public
        answer = client._session.get(  # noqa: SLF001
            f"https://{host}/v3/iot-feature/feature/{serial}"
            f"/global/0/KeyMgr/CardKeyInfo",
            timeout=15,
        )
        return {"status": answer.status_code, "body": answer.json()}

    try:
        keys = await hass.async_add_executor_job(_card_keys)
    except (HTTPError, PyEzvizError, ValueError) as err:
        keys = {"error": str(err)}

    return {
        "lock_capabilities": {
            name: support.get(number)
            for number, name in LOCK_CAPABILITIES.items()
            if number in support
        },
        "support_ext_count": len(support),
        "card_keys": keys,
        "alarm_fields": {
            key: value for key, value in device.items() if "alarm" in key.lower()
        },
    }


def _find_coordinator(hass: HomeAssistant) -> Any:
    """Le premier coordinateur charge, ou None."""
    from .const import DOMAIN  # noqa: PLC0415 - import tardif, cycle sinon

    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = getattr(entry, "runtime_data", None)
        if coordinator is not None:
            return coordinator
    return None


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
    hass.services.async_register(
        DOMAIN,
        SERVICE_FETCH_DEVICE_INFO,
        _fetch_device_info,
        schema=INFO_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
