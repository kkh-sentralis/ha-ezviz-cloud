<h1 align="center">EZVIZ</h1>

<p align="center">
  <em>L'intégration EZVIZ de Home Assistant, avec le flux des caméras sur batterie.</em>
</p>

<p align="center">
  <a href="https://github.com/kkh-sentralis/ha-ezviz-cloud/releases"><img alt="Version" src="https://img.shields.io/github/v/release/kkh-sentralis/ha-ezviz-cloud?style=flat-square&color=0a84ff"></a>
  <img alt="HACS" src="https://img.shields.io/badge/HACS-custom-0a84ff?style=flat-square">
  <img alt="Home Assistant" src="https://img.shields.io/badge/Home%20Assistant-2026.9%2B-0a84ff?style=flat-square">
  <img alt="Licence" src="https://img.shields.io/badge/licence-Apache%202.0-0a84ff?style=flat-square">
</p>

---

## Installation

1. HACS → dépôt personnalisé → ce dépôt, type **Integration**
2. Télécharger **EZVIZ**, redémarrer Home Assistant
3. *Paramètres → Ajouter une intégration → EZVIZ*, entrer son compte

**Rien d'autre.** Pas d'add-on, aucun fichier à créer, aucun jeton à coller.

---

## Les deux corrections

### 1&nbsp;· L'AOV ne casse plus le compte

Une caméra sur batterie en *Always-On Video* renvoie le mode de fonctionnement
`7`, absent de l'énumération de la version épinglée. L'intégration lève, et
**aucune** caméra du compte ne se configure.

Le correctif existe en amont, mais Home Assistant épingle une version obsolète
— y compris sur sa branche `dev` :

| | |
|---|---|
| HA 2026.9.3 | `pyezvizapi == 1.0.0.7` |
| HA `dev` | `pyezvizapi == 1.0.0.7` |
| PyPI | **`1.0.5.0`** — contient `ALWAYS_ON_VIDEO = 7` |

### 2&nbsp;· Les caméras sur batterie diffusent

Elles n'exposent aucun serveur RTSP : sur une HB8C, les 200 premiers ports TCP
sont filtrés, caméra éveillée. L'URL locale que construit l'amont ne répond
jamais, et l'entité reste avec `supported_features: 0`.

```
compte EZVIZ  →  session  →  transport VTM  →  vue interne  →  ffmpeg de HA
```

Aucun jeton à gérer : la session du compte porte tout, et la bibliothèque la
renouvelle d'elle-même.

> **Les caméras filaires ne sont pas affectées.** Quand un mot de passe RTSP est
> configuré, le chemin local d'origine reste prioritaire, code amont inchangé.
> Le cloud n'est qu'un repli.

---

## Trois pièges, et ce qu'ils ont appris

| Symptôme | Cause |
|---|---|
| `FFmpeg exited with status 234` | le flux est alimenté **dès le premier paquet**, souvent un PES isolé. ffmpeg ne peut se synchroniser qu'à partir d'un pack header `00 00 01 BA`. |
| `Could not write header` | la caméra annonce une piste audio `mp2` à **0 canaux**. ffmpeg n'en déduit ni taille de trame ni fréquence, refuse le conteneur, et la vidéo tombe avec. On ne remuxe que la vidéo. |
| Diagnostic impossible | `copy_cloud_stream_to_mpegts` lance ffmpeg avec `stderr=DEVNULL` : il **jette la seule information** qui explique ses échecs. On la capture. |

---

## Resynchroniser avec l'amont

Le fork est volontairement minimal : **3 fichiers modifiés sur 21**.

| Fichier | Nature du diff |
|---|---|
| `manifest.json` | version de la bibliothèque, nom, version HACS |
| `camera.py` | source de flux et instantané en repli cloud |
| `select.py` | le mode batterie arrive en entier, plus par son nom |
| `stream_proxy.py` | **nouveau**, autonome |
| `translations/` | **nouveau** — HA les génère à la compilation, pas dans le dépôt |

Pour suivre une version de Home Assistant : recopier les 21 fichiers depuis
`homeassistant/components/ezviz/`, rejouer ces trois patchs, garder
`stream_proxy.py` et `translations/`.

---

## Licence

Le code de `custom_components/ezviz/` provient de Home Assistant, sous licence
Apache&nbsp;2.0. Voir [`NOTICE`](NOTICE).
