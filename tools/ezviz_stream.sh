#!/bin/sh
# Pont EZVIZ -> RTSP, pour la source `exec:` de go2rtc.
#
# go2rtc ne passe PAS par un shell : il decoupe la ligne en arguments et lance
# le binaire. Un « | » dans go2rtc.yaml arrive donc comme argument litteral a
# python3, et ffmpeg n'est jamais lance (erreur « streams: exec/rtsp »).
# Ce fichier existe pour que le tube soit reellement interprete.
#
# $1 est l'URL RTSP que go2rtc substitue a {output}.

SERIAL=BH0697892
TOKEN_FILE=/config/ezviz_token.txt

# ⛔ ON TRANSCODE, on ne copie pas.
#
# La camera emet du H.265 en 2560x1440. Les navigateurs ne decodent pas le
# HEVC de maniere fiable : go2rtc se rabat alors sur un transcodage MJPEG
# logiciel de l'image pleine, et le Pi rend UNE image toutes les dix secondes.
# Transcoder une fois en H.264 720p coute moins cher que de transcoder en
# MJPEG a chaque client, et tout navigateur sait l'afficher.
#
# `-fflags nobuffer -flags low_delay` : le flux brut n'a pas d'horodatage,
# sans ca ffmpeg accumule avant de rendre la main.
# `-g 30` : une image cle par seconde, pour que la lecture demarre vite.

python3 /config/ws_bridge.py --token-file "$TOKEN_FILE" --serial "$SERIAL" \
  | ffmpeg -hide_banner -loglevel warning \
      -fflags nobuffer -flags low_delay \
      -f hevc -i - \
      -vf scale=1280:-2 \
      -c:v libx264 -preset ultrafast -tune zerolatency -g 30 -b:v 2M \
      -an \
      -f rtsp -rtsp_transport tcp "$1"
