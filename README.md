<div align="center">

# EZVIZ

**Intégration EZVIZ pour Home&nbsp;Assistant**

Le flux vidéo des caméras sur batterie, nativement.

[![Version](https://img.shields.io/github/v/release/kkh-sentralis/ha-ezviz-cloud?style=for-the-badge&color=41BDF5&labelColor=1C1C1C)](https://github.com/kkh-sentralis/ha-ezviz-cloud/releases)
[![HACS](https://img.shields.io/badge/HACS-Integration-41BDF5?style=for-the-badge&labelColor=1C1C1C)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.9+-41BDF5?style=for-the-badge&labelColor=1C1C1C)](https://www.home-assistant.io)
[![Licence](https://img.shields.io/badge/Licence-Apache%202.0-41BDF5?style=for-the-badge&labelColor=1C1C1C)](NOTICE)

</div>

---

Cette intégration **remplace l'intégration EZVIZ
officielle** et en conserve toutes les fonctions —
découverte du compte, PTZ, sirène, détection de
mouvement, batterie, capteurs et commutateurs.

Elle y ajoute ce qui manquait : **l'image des caméras
sur batterie**.

## Le problème

Les caméras EZVIZ sur batterie n'ouvrent aucun flux
RTSP local. L'intégration officielle le cherche sur le
réseau, ne le trouve jamais, et ces caméras restent
muettes dans Home Assistant.

Le mode **Always-On Video** aggrave la situation : une
seule caméra dans ce mode empêche la configuration de
l'ensemble du compte.

## Ce que vous obtenez

| | |
|---|---|
| **Flux vidéo** | via le cloud EZVIZ, sans add-on |
| **Instantanés** | vignettes et automatisations |
| **Son** | si la caméra le permet, sinon repli |
| **Retourner l'image** | bouton, pour les caméras montées à l'envers |
| **Always-On Video** | pris en charge |
| **Caméras filaires** | RTSP local, inchangé |
| **Aucun jeton** | la session du compte suffit |

## Installation

> **Prérequis** — Home Assistant 2026.9 ou supérieur,
> HACS, et un compte EZVIZ.

**1.** Dans HACS, ajouter ce dépôt en dépôt
personnalisé, de type *Integration*.

**2.** Télécharger **EZVIZ**, puis redémarrer Home
Assistant.

**3.** *Paramètres → Appareils et services → Ajouter
une intégration → EZVIZ*, et renseigner son compte.

Ni add-on, ni fichier de configuration, ni clé d'API.

## Configuration

L'intégration demande les identifiants du compte
EZVIZ et sa région.

### Flux des caméras sur batterie

Ces caméras n'ont pas de RTSP local : leur flux passe
par l'Open Platform d'EZVIZ, qui demande une clé
applicative.

Sur [open.ezviz.com](https://open.ezviz.com), créer
une application et relever **AppKey** et
**AppSecret**. Les renseigner ensuite dans les
options de l'intégration.

Ces deux valeurs sont **permanentes**. Le jeton
qu'elles produisent expire au bout de sept jours,
mais il est renouvelé automatiquement : rien à
refaire.

### Résolution du flux

L'option **Largeur du flux** règle le transcodage :

| Valeur | Rendu |
|---|---|
| `1280` | 720p — le plus fluide |
| `1920` | 1080p — par défaut |
| `2560` | 1440p — machines confortables |
| `0` | résolution native, sans redimensionnement |

Le Pi n'ayant pas d'encodeur H.264 matériel, le coût
suit le nombre de pixels. Un encodeur en retard ne
ralentit pas : il **perd des images**. Baisser cette
valeur est le premier réglage à tenter si le flux
saccade.

### Source du flux

| Caméra | Source |
|---|---|
| Filaire, RTSP configuré | RTSP local |
| Sur batterie | Cloud EZVIZ |

Le choix est automatique, caméra par caméra.

<details>
<summary><b>Compatibilité</b></summary>

<br>

Validée sur **EZVIZ HB8C**. Les autres modèles sur
batterie partagent le même protocole et devraient se
comporter à l'identique.

Les caméras filaires conservent le fonctionnement de
l'intégration officielle : le code d'origine est
repris sans modification sur ce chemin.

</details>

<details>
<summary><b>Dépannage</b></summary>

<br>

Activer les traces détaillées dans
`configuration.yaml` :

```yaml
logger:
  logs:
    custom_components.ezviz: debug
```

Le journal indique alors le format détecté pour chaque
flux, ainsi que la sortie complète de ffmpeg.

</details>

<details>
<summary><b>Mise à jour depuis l'amont</b></summary>

<br>

Cette intégration suit le code officiel de Home
Assistant et n'en modifie que trois fichiers.

Pour l'aligner sur une nouvelle version : reprendre
`homeassistant/components/ezviz/`, rejouer les
correctifs, conserver `stream_proxy.py` et
`translations/`.

</details>

---

<div align="center">
<sub>

Dérivé de l'intégration EZVIZ de Home&nbsp;Assistant,
créée et maintenue par
**[@RenierM26](https://github.com/RenierM26)**,
également auteur de
[pyezvizapi](https://github.com/RenierM26/pyEzvizApi).

Sous licence Apache&nbsp;2.0.

</sub>
</div>
