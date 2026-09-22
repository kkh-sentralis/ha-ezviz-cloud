> Ce document décrit le protocole WebSocket rétro-conçu sur une HB8C.
> L'intégration s'en sert : `ws_bridge.py`, livré avec elle, en est
> l'implémentation, lancée en sous-processus par `stream_proxy.py`.

# EZVIZ HB8C — comment obtenir le flux live

Relevé le 2026-09-22 sur `HB8C-XX1234567` (modèle `CS-HB8c-R100-1N4WFL`), compte EU.

## Ce qui NE marche pas, et pourquoi

| Piste | Résultat |
|---|---|
| RTSP local (554) | port fermé, 200 ports TCP filtrés caméra éveillée |
| `/api/lapp/live/address/get` (HLS/RTMP/FLV) | URL valide, flux = `ErrCode/9053` |
| `/api/lapp/v2/live/address/get` | idem |
| En-têtes navigateur (Referer/UA/Origin) | sans effet |
| `quality` 1/2, `expireTime` long | sans effet |
| `wakeUp`, `live/video/open`, `video/quality/set` | 404 sur l'hôte EU |

`9053` ne signifie pas « appareil endormi » : le service HLS n'est pas provisionné
pour cet appareil. Le lecteur de la console n'utilise PAS le HLS.

## Ce qui marche

Le transport réel est un **WebSocket propriétaire**, découvert en lisant `ezuikit-js`.

```bash
curl -X POST "https://ieuopen.ezvizlife.com/api/lapp/live/url/ezopen" \
  -F "accessToken=$TOKEN" \
  -F "ezopen=ezopen://open.ezviz.com/XX1234567/1.hd.live" \
  -F "isFlv=false" -F "isHttp=false" -F "userAgent=$UA"
```

⚠️ **multipart/form-data obligatoire** — en `x-www-form-urlencoded`, l'API répond
`传入参数为空` (paramètre vide). C'est ce qui m'a fait tourner en rond.

Réponse :

```json
{ "url": "wss://vtmparis.ezvizlife.com:20006/live?dev=XX1234567&chn=1&stream=1",
  "token": "dv.…" }
```

Les drapeaux `isFlv` / `isHttp` ne changent rien : pas de HTTP-FLV sur ce modèle.

## Capture d'image — la voie simple, validée

```bash
curl -X POST "https://ieuopen.ezvizlife.com/api/lapp/device/capture" \
  -d "accessToken=$TOKEN&deviceSerial=XX1234567&channelNo=1"
```

Renvoie un `picUrl` téléchargeable : **JPEG 1280x720, ~98 Ko, image courante**.
C'est ce que fait le composant HACS chinois `myezviz`.

## ✅ LE LIVE — RÉSOLU

La console avait la réponse dans ses logs : le jeton va dans **`ssn`**, et trois
paramètres manquaient. Sans eux, le serveur répond `6110 get stream error`.

```
wss://vtmparis.ezvizlife.com:20006/live
  ?dev=XX1234567&chn=1&stream=1
  &ssn=<jeton de live/url/ezopen>&auth=1&biz=4&cln=100
```

⚠️ Désactiver le keepalive WebSocket : le serveur ne répond pas aux pings et la
connexion tombe au bout de 20 s (`keepalive ping timeout`).

Le serveur accuse `{"statusCode":0,"errorMsg":"ok"}`, puis envoie un entête
`IMKH` de 40 octets, puis **un paquet RTP par message binaire** — H.265,
payload type 96, fragmenté en unités FU (NAL 49, RFC 7798).

`ws_bridge.py` fait la dépaquetisation et écrit du H.265 Annex-B sur stdout.

Mesuré sur 26 Mo : VPS/SPS/PPS x33, IDR_W_RADL x33, ~4360 images
intermédiaires. Flux valide et décodable, 2560x1440, ~2,2 Mbps.

## Comment l'intégration s'en sert

Aucun fichier à déposer, aucun add-on : `ws_bridge.py` est livré par HACS avec
l'intégration, et `stream_proxy.py` le lance en sous-processus.

```
ws_bridge.py  --tube noyau-->  ffmpeg  --tube noyau-->  boucle asyncio  -->  réponse
```

Le pont a son propre interpréteur, comme le montage go2rtc manuel. Dépaqueter
du RTP dans un fil d'exécuteur disputait le verrou global à la boucle
d'événements : 6,5 images/s contre 10,5 pour le même flux hors du processus.

L'URL de flux porte le jeton de session : elle passe par l'entrée standard du
pont, jamais par la ligne de commande.

## Déjà disponible sans rien coder

`image.hb8c_bh0697892_derniere_image_du_mouvement` — JPEG 1280x720 servi par HA,
mis à jour à chaque détection. Testé : HTTP 200, 95 908 octets.

## Bonus

`device/capacity` annonce `ptz_preset: 1`, `support_ptz_homing_point: 1`.
Les préréglages PTZ sont supportés : `/api/lapp/device/preset/move`.
