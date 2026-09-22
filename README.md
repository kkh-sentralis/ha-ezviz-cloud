# EZVIZ (Nairobi)

Fork de l'intégration EZVIZ de Home Assistant, avec deux corrections que
l'amont n'apporte pas.

## Pourquoi ce fork

**1. L'AOV casse l'intégration.** Une caméra sur batterie en mode *Always-On
Video* renvoie le mode de fonctionnement `7`, absent de l'énumération
`BatteryCameraWorkMode`. L'intégration lève une exception et **aucune** caméra
du compte ne se configure.

Le correctif existe en amont depuis longtemps, mais Home Assistant épingle une
version obsolète de la bibliothèque — **y compris sur sa branche `dev`** :

```
HA 2026.9.3     pyezvizapi == 1.0.0.7
HA dev          pyezvizapi == 1.0.0.7
PyPI            1.0.5.0          ← contient ALWAYS_ON_VIDEO = 7
```

Ce fork épingle `1.0.5.0`. Compatibilité vérifiée symbole par symbole : les 20
symboles importés et les 8 méthodes appelées sont tous présents, signatures
inchangées.

**2. Les caméras sur batterie n'ont pas de flux.** Elles n'exposent aucun
serveur RTSP — sur une HB8C, les 200 premiers ports TCP sont filtrés, même
caméra éveillée. L'URL locale que construit l'amont ne répond jamais, et
l'entité reste avec `supported_features: 0`.

Ce fork permet de **surcharger la source du flux** par caméra.

## Surcharger un flux

Créer `/config/ezviz_stream_overrides.json` :

```json
{
  "default": "rtsp://192.168.1.65:8554/ezviz_{serial}"
}
```

La clé `default` est un **gabarit qui vaut pour toutes les caméras** — y compris
celles que la découverte du compte ajoutera plus tard. `{serial}` y est remplacé
par le numéro de série.

Pour traiter une caméra à part, la nommer explicitement :

```json
{
  "default": "rtsp://192.168.1.65:8554/ezviz_{serial}",
  "BH0697892": "rtsp://192.168.1.65:8554/jardin"
}
```

Sans ce fichier, le comportement est **identique à l'intégration officielle**.
La caméra obtient alors le drapeau `STREAM`, et le flux apparaît **sur la page
de son appareil**, à côté du PTZ et de la batterie.

Pour alimenter cette URL depuis le cloud EZVIZ quand aucun RTSP local n'existe,
ce dépôt fournit la chaîne complète :

| Fichier | Rôle |
|---|---|
| [`docs/FINDINGS.md`](docs/FINDINGS.md) | le protocole WebSocket EZVIZ, documenté pas à pas |
| [`tools/ws_bridge.py`](tools/ws_bridge.py) | le pont WebSocket → H.265, **sans aucune dépendance** |
| [`tools/ezviz_stream.sh`](tools/ezviz_stream.sh) | tube + transcodage H.264 720p |
| [`tools/go2rtc.yaml.example`](tools/go2rtc.yaml.example) | la source `exec:` de go2rtc |
| [`docs/PLAN.md`](docs/PLAN.md) | l'avancement et les décisions |

Le pont tourne dans le conteneur de l'add-on go2rtc, qui n'a que la
bibliothèque standard de Python — d'où l'absence assumée de dépendances.

## Resynchroniser avec l'amont

Le fork est volontairement minimal : **2 fichiers modifiés sur 21**.

```
manifest.json   3 lignes   version de la bibliothèque, version HACS, tracker
camera.py      48 lignes   chargement et prise en compte des surcharges
```

Pour suivre une nouvelle version de Home Assistant, recopier les 21 fichiers
depuis `homeassistant/components/ezviz/` et rejouer ces deux patchs.
