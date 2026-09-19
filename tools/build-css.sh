#!/bin/sh
# Compila evprices/web/static/src/app.css -> evprices/web/static/app.css com o Tailwind CLI standalone
# (binário único, sem Node). O arquivo gerado é versionado: quem muda um template roda este script e comita o app.css.
set -e
cd "$(dirname "$0")/.."
BIN=tools/tailwindcss
VER=v3.4.17
if [ ! -x "$BIN" ]; then
  case "$(uname -m)" in x86_64) A=x64;; aarch64|arm64) A=arm64;; *) echo "arquitetura não suportada"; exit 1;; esac
  curl -sL -o "$BIN" "https://github.com/tailwindlabs/tailwindcss/releases/download/$VER/tailwindcss-linux-$A"
  chmod +x "$BIN"
fi
"$BIN" -c tailwind.config.js -i evprices/web/static/src/app.css -o evprices/web/static/app.css --minify "$@"
# cache-busting: cada fonte referenciada no CSS ganha ?v=<md5 do arquivo> (service worker e Cloudflare nunca servem versão velha)
for f in evprices/web/static/fonts/*.woff2; do
  h=$(md5sum "$f" | cut -c1-8); n=$(basename "$f")
  sed -i "s#/static/fonts/$n)#/static/fonts/$n?v=$h)#g" evprices/web/static/app.css
done
