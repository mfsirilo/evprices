#!/bin/sh
# Gera evprices/web/static/fonts/material-symbols.woff2 com SÓ os ícones usados nos templates/JS (Google Fonts API,
# parâmetro icon_names). Rode depois de usar um ícone novo; o arquivo é versionado.
set -e
cd "$(dirname "$0")/.."
NAMES=$( { grep -rhoE 'material-symbols-outlined[^>]*>[a-z_0-9]+<' evprices/web/templates evprices/web/static/*.js | sed -E 's/.*>([a-z_0-9]+)<$/\1/';
           grep -rhoE '\{# icons: [a-z_0-9 ]+ #\}' evprices/web/templates | sed -E 's/\{# icons: (.*) #\}/\1/' | tr ' ' '\n';
         } | sort -u | tr '\n' ',' | sed 's/,$//')
echo "ícones: $NAMES"
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
CSS=$(curl -sf -A "$UA" "https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200&icon_names=$NAMES&display=block")
URL=$(echo "$CSS" | grep -oE 'https://[^)]+' | head -1)
[ -n "$URL" ] || { echo "não achei o woff2 na resposta do Google Fonts"; echo "$CSS" | head -5; exit 1; }
curl -sf -A "$UA" -o evprices/web/static/fonts/material-symbols.woff2 "$URL"
ls -la evprices/web/static/fonts/material-symbols.woff2
