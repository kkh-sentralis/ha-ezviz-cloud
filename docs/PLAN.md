# EZVIZ HB8C dans Home Assistant — plan et avancement

Session du 2026-09-22. Objectif : le flux live de la caméra HB8C sur la page
de son appareil, à côté du PTZ et de la batterie, avec l'AOV qui fonctionne.

## Ce qui marche DÉJÀ (ne pas refaire)

| | |
|---|---|
| Intégration EZVIZ officielle | `loaded`, 100 entités, 4 caméras |
| Flux live | **fonctionne**, ~2 s de latence |
| `camera.hb8c_jardin_live` | caméra générique HA, `supported_features=2` (STREAM) |
| go2rtc (add-on AlexxIT) | installé, lit `/config/go2rtc.yaml` |
| Sauvegardes HA | quotidiennes 04:30, 7 copies |

⚠️ **AOV est DÉSACTIVÉ sur la caméra** (dans l'app EZVIZ) : c'est le
contournement actuel. Le remettre casse l'intégration tant que la
bibliothèque n'est pas à jour.

## La chaîne actuelle, de bout en bout

```
app EZVIZ (AOV OFF)
  → /config/ezviz_token.txt      jeton Open Platform, EXPIRE LE 2026-09-29
  → /config/ws_bridge.py         WebSocket propriétaire → H.265 Annex-B
  → /config/ezviz_stream.sh      tube + transcodage H.264 720p
  → /config/go2rtc.yaml          exec: → RTSP sur :8554
  → camera.hb8c_jardin_live      caméra générique HA
```

## Les trois découvertes qui commandent la suite

**1. Le bug AOV est déjà corrigé en amont.**
`pyezvizapi` master contient `ALWAYS_ON_VIDEO = 7` dans `BatteryCameraWorkMode`.

**2. Home Assistant est très en retard, et ne bouge pas.**

```
HA 2026.9.3    : pyezvizapi == 1.0.0.7
HA branche dev : pyezvizapi == 1.0.0.7     ← même sur dev
PyPI           : 1.0.5.0
```

Aucun correctif ne viendra de l'amont. C'est ce qui justifie le fork.

**3. La 1.0.5.0 sait déjà diffuser.** Modules ajoutés depuis la 1.0.0.7 :

```
cloud_stream.py  local_stream.py  stream.py  models.py
device_factory.py  feature.py  hcnetsdk.py  smart_plug.py
```

`cloud_stream.py` expose `get_cloud_stream_info()`, `open_cloud_stream()`,
`copy_cloud_stream_to_mpegts()`, `decrypt_hikvision_ps_video()`.
**Notre `ws_bridge.py` est peut-être devenu inutile** — à vérifier en premier.

## Plan et avancement

| # | Étape | État |
|---|---|---|
| 1 | Reverse-engineering du protocole WebSocket | ✅ fait, voir FINDINGS.md |
| 2 | Pont H.265 sans dépendance (`ws_bridge.py`) | ✅ fait et validé |
| 3 | Chaîne go2rtc + transcodage H.264 | ✅ fait, ~2 s de latence |
| 4 | Caméra générique dans HA | ✅ fait |
| 5 | **Comparer l'API 1.0.0.7 vs 1.0.5.0** | ✅ **compatible à 100 %** |
| 6 | `cloud_stream.py` remplace-t-il `ws_bridge.py` ? | ✅ **OUI — session du compte, pas de jeton** |
| 7 | Copier l'intégration dans `custom_components/ezviz/` | ✅ 21 fichiers |
| 8 | `manifest.json` → `pyezvizapi==1.0.5.0` | ✅ + version HACS |
| 9 | ~~Adapter le code aux changements d'API~~ | ✅ **inutile** |
| 10 | Surcharge de flux dans `camera.py` | ✅ 48 lignes |
| 10b | Vue interne `stream_proxy.py`, plus d'add-on ni de fichiers | ✅ |
| 10c | Publier sur GitHub + installer par HACS | 🔄 EN ATTENTE DU FEU VERT |
| 11 | Réactiver l'AOV et vérifier que ça tient | ⬜ |
| 12 | Supprimer la caméra générique devenue inutile | ⬜ |
| 13 | ~~Renouvellement automatique du jeton~~ | ✅ **sans objet** |

## Le fork : périmètre

Source : `home-assistant/core`, tag `2026.9.3`, `homeassistant/components/ezviz/`
21 fichiers, 82 Ko. `camera.py` ne fait que 7 Ko.

Cible : `/config/custom_components/ezviz/` — même domaine, donc il prend le
dessus sur l'intégration intégrée.

## Compatibilité : MESURÉE, et totale

La bibliothèque n'a fait qu'AJOUTER (50 -> 190 symboles publics dans
`client.py`). Contrôle exhaustif contre 1.0.5.0 :

```
20 symboles importés par l'integration  -> 20 presents, 0 manquant
8 methodes client appelees              -> 8 presentes, signatures compatibles
ALWAYS_ON_VIDEO                         -> present
```

Seule rupture trouvee dans toute la bibliotheque : `get_device_messages_list`
a perdu des parametres. **L'integration ne l'appelle jamais.**

Le fork se reduit donc a : copier, changer une ligne de manifeste, ajouter
`async_stream_source()`. Aucune adaptation de code.

## Références utiles

```
Caméra      BH0697892, modèle CS-HB8c-R100-1N4WFL, IP 192.168.1.65
Compte      kamzouj@cloudguard.fr, région ieuopen.ezvizlife.com
go2rtc      http://192.168.1.65:1984  (API), rtsp://192.168.1.65:8554/ezviz_hb8c
HA          http://homeassistant.local  (port 80, pas 8123)
Lib amont   https://github.com/RenierM26/pyEzvizApi
Bug AOV     https://github.com/home-assistant/core/issues/176789 (ouvert)
```

⚠️ **À FAIRE, sécurité** : régénérer AppKey et AppSecret (ils ont circulé en
clair), puis mettre à jour `/config/ezviz_token.txt`.
