# EZVIZ

Intégration EZVIZ pour Home Assistant, avec prise en
charge du flux vidéo des caméras sur batterie.

Elle remplace l'intégration officielle et en conserve
l'intégralité des fonctions : découverte du compte,
PTZ, sirène, détection de mouvement, niveau de
batterie, capteurs et commutateurs.

## Pourquoi

Les caméras EZVIZ sur batterie ne diffusent aucun
flux RTSP local. L'intégration officielle cherche ce
flux sur le réseau, ne le trouve pas, et ces caméras
restent sans image dans Home Assistant.

Par ailleurs, le mode *Always-On Video* empêche
l'intégration officielle de se configurer : une seule
caméra dans ce mode suffit à bloquer l'ensemble du
compte.

Cette intégration corrige les deux points.

## Fonctionnalités

- **Flux vidéo** des caméras sur batterie, via le
  cloud EZVIZ, sans add-on ni service externe
- **Instantanés** pour les vignettes et les
  automatisations
- **Mode Always-On Video** pris en charge
- **Caméras filaires** inchangées : le flux RTSP
  local reste prioritaire
- **Aucun jeton à gérer** : la session du compte
  suffit et se renouvelle seule

## Prérequis

- Home Assistant 2026.9 ou supérieur
- HACS
- Un compte EZVIZ

## Installation

**1.** Dans HACS, ajouter ce dépôt en dépôt
personnalisé, de type *Integration*.

**2.** Télécharger **EZVIZ**, puis redémarrer Home
Assistant.

**3.** Aller dans *Paramètres → Appareils et services
→ Ajouter une intégration*, choisir **EZVIZ** et
renseigner son compte.

Aucune autre étape n'est requise : ni add-on, ni
fichier de configuration, ni clé d'API.

## Configuration

L'intégration ne demande que les identifiants du
compte EZVIZ et la région correspondante.

Le flux vidéo est sélectionné automatiquement :

| Caméra | Source du flux |
|---|---|
| Filaire, RTSP configuré | RTSP local |
| Sur batterie | Cloud EZVIZ |

## Compatibilité

Testée sur EZVIZ HB8C. Les autres modèles sur
batterie suivent le même protocole et devraient
fonctionner à l'identique.

Les caméras filaires conservent le comportement de
l'intégration officielle, dont le code est repris
sans modification sur ce point.

## Dépannage

Les traces détaillées s'activent en ajoutant à
`configuration.yaml` :

```yaml
logger:
  logs:
    custom_components.ezviz: debug
```

Le journal indique alors le format détecté pour
chaque flux ainsi que la sortie de ffmpeg.

## Mise à jour depuis l'amont

Cette intégration suit le code officiel de Home
Assistant et n'en modifie que trois fichiers. Pour
l'aligner sur une nouvelle version : reprendre le
dossier `homeassistant/components/ezviz/`, rejouer
les correctifs, et conserver `stream_proxy.py` ainsi
que `translations/`.

## Licence

Code dérivé de Home Assistant, distribué sous licence
Apache 2.0. Voir [NOTICE](NOTICE).
