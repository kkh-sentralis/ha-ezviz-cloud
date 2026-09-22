# EZVIZ

L'intégration EZVIZ de Home Assistant, avec le flux
des caméras sur batterie.

## Installation

1. HACS → dépôt personnalisé, type **Integration**
2. Télécharger **EZVIZ**, redémarrer
3. *Paramètres → Ajouter une intégration → EZVIZ*

Pas d'add-on. Aucun fichier à créer. Aucun jeton.

## Ce que ce fork corrige

### L'AOV ne casse plus le compte

Une caméra sur batterie en *Always-On Video*
renvoie le mode `7`, absent de l'énumération de la
version épinglée. L'intégration lève, et **aucune**
caméra du compte ne se configure.

Le correctif existe en amont, mais Home Assistant
épingle une version obsolète — y compris sur `dev` :

| Source | Version |
|---|---|
| HA 2026.9.3 | `1.0.0.7` |
| HA `dev` | `1.0.0.7` |
| PyPI | **`1.0.5.0`** |

Seule la `1.0.5.0` contient `ALWAYS_ON_VIDEO`.

### Les caméras sur batterie diffusent

Elles n'ouvrent aucun serveur RTSP : sur une HB8C,
les 200 premiers ports TCP sont filtrés, caméra
éveillée. L'URL locale de l'amont ne répond jamais.

Ce fork ajoute une vue interne qui rend le flux du
cloud, alimentée par la **session du compte**. Rien
à renouveler.

> Les caméras filaires ne sont pas touchées. Avec
> un mot de passe RTSP configuré, le chemin local
> reste prioritaire, code amont inchangé.

## Trois pièges rencontrés

**`FFmpeg exited with status 234`**
Le flux est alimenté dès le premier paquet, souvent
un PES isolé. ffmpeg ne se synchronise que sur un
pack header `00 00 01 BA`.

**`Could not write header`**
La caméra annonce une piste audio `mp2` à 0 canaux.
ffmpeg refuse le conteneur, et la vidéo tombe avec.
On ne remuxe que la vidéo.

**Diagnostic impossible**
La bibliothèque lance ffmpeg avec `stderr=DEVNULL` :
elle jette la seule information qui explique ses
échecs. On la capture.

## Resynchroniser avec l'amont

Le fork est minimal : **3 fichiers modifiés sur 21**.

| Fichier | Diff |
|---|---|
| `manifest.json` | version de la bibliothèque |
| `camera.py` | flux et instantané en repli |
| `select.py` | mode batterie rendu en entier |
| `stream_proxy.py` | nouveau |
| `translations/` | nouveau |

Pour suivre une version de HA : recopier les 21
fichiers depuis `homeassistant/components/ezviz/`,
rejouer les trois patchs, garder `stream_proxy.py`
et `translations/`.

## Licence

Le code de `custom_components/ezviz/` vient de Home
Assistant, sous Apache 2.0. Voir [NOTICE](NOTICE).
