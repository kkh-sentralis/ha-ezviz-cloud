# EZVIZ Cloud

Fork de l'intégration EZVIZ de Home Assistant, avec deux corrections que
l'amont n'apporte pas — et **rien à installer ni à configurer en plus**.

## Installation

1. HACS → dépôt personnalisé → ce dépôt, type **Integration**
2. Télécharger **EZVIZ Cloud**, redémarrer Home Assistant
3. *Paramètres → Ajouter une intégration → EZVIZ*, entrer son compte

C'est tout. Pas d'add-on, pas de fichier à créer, aucun jeton à coller.

## Ce que ce fork corrige

### 1. L'AOV ne casse plus le compte

Une caméra sur batterie en mode *Always-On Video* renvoie le mode de
fonctionnement `7`, absent de l'énumération `BatteryCameraWorkMode` de la
version épinglée. L'intégration lève une exception et **aucune** caméra du
compte ne se configure.

Le correctif existe en amont, mais Home Assistant épingle une version obsolète
de la bibliothèque — **y compris sur sa branche `dev`** :

```
HA 2026.9.3     pyezvizapi == 1.0.0.7
HA dev          pyezvizapi == 1.0.0.7
PyPI            1.0.5.0          ← contient ALWAYS_ON_VIDEO = 7
```

Ce fork épingle `1.0.5.0`. Compatibilité vérifiée symbole par symbole : les 20
symboles importés et les 8 méthodes appelées sont présents, signatures
inchangées. La seule rupture de la bibliothèque (`get_device_messages_list`)
porte sur une méthode que l'intégration n'appelle jamais.

### 2. Les caméras sur batterie diffusent enfin

Elles n'exposent aucun serveur RTSP — sur une HB8C, les 200 premiers ports TCP
sont filtrés, caméra éveillée. L'URL locale que construit l'amont ne répond
jamais, et l'entité reste avec `supported_features: 0`.

Ce fork ajoute une vue HTTP interne qui rend le flux du cloud EZVIZ en
MPEG-TS, et la caméra la renvoie comme source. Le RTSP local **reste
prioritaire** quand il est configuré.

```
compte EZVIZ  →  pyezvizapi.cloud_stream  →  vue interne  →  ffmpeg de HA
```

**Aucun jeton à gérer** : la session du compte suffit, et la bibliothèque la
renouvelle d'elle-même quand elle expire.

## Resynchroniser avec l'amont

Le fork est volontairement minimal : **2 fichiers modifiés sur 21**.

| Fichier | Diff |
|---|---|
| `manifest.json` | 3 lignes — version de la bibliothèque, version HACS, tracker |
| `camera.py` | 25 lignes — source de flux cloud en repli |
| `stream_proxy.py` | nouveau, autonome |

Pour suivre une nouvelle version de Home Assistant : recopier les 21 fichiers
depuis `homeassistant/components/ezviz/`, rejouer ces deux patchs, garder
`stream_proxy.py`.

## Documentation

- [`docs/FINDINGS.md`](docs/FINDINGS.md) — le protocole WebSocket EZVIZ,
  rétro-conçu avant de découvrir que la bibliothèque le gérait déjà
- [`docs/PLAN.md`](docs/PLAN.md) — l'avancement et les décisions
- [`docs/ws_bridge.legacy.py`](docs/ws_bridge.legacy.py) — le pont autonome
  écrit pour la première approche, gardé comme référence du protocole

## Licence

Le code de `custom_components/ezviz/` provient de Home Assistant, sous licence
Apache 2.0.
