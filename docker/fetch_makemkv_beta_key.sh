#!/bin/sh
set -eu

third_party_url="https://cable.ayra.ch/makemkv/api.php?raw"
forum_url="https://forum.makemkv.com/forum/viewtopic.php?f=5&t=1053"
user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0"

fetch_url() {
  curl -fsSL --retry 1 --retry-delay 1 --connect-timeout 5 --max-time 15 -A "$user_agent" "$1"
}

fetch_third_party_key() {
  fetch_url "$third_party_url" \
    | tr -d '\r' \
    | sed -n '1{s/^[[:space:]]*//; s/[[:space:]]*$//; p; q;}'
}

fetch_forum_key() {
  fetch_url "$forum_url" \
    | tr '\n' ' ' \
    | grep -Eo '<code[^>]*>[[:space:]]*[^<]+[[:space:]]*</code>' \
    | sed -E 's#^<code[^>]*>[[:space:]]*##; s#[[:space:]]*</code>$##' \
    | head -n 1
}

key="$(fetch_third_party_key || true)"

if [ -z "$key" ]; then
  echo "无法从第三方接口抓取 MakeMKV beta key，尝试从官方论坛抓取: $third_party_url" >&2
  key="$(fetch_forum_key || true)"
fi

if [ -z "$key" ]; then
  echo "无法从第三方接口或官方论坛抓取 MakeMKV beta key: $third_party_url, $forum_url" >&2
  exit 1
fi

printf '%s\n' "$key"
