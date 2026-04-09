#!/bin/sh
set -eu

forum_url="https://forum.makemkv.com/forum/viewtopic.php?f=5&t=1053"

key="$({
  curl -fsSL --retry 3 --retry-delay 2 "$forum_url" \
    | tr '\n' ' ' \
    | grep -Eo '<code>T-[A-Za-z0-9]{20,}</code>' \
    | sed -E 's#</?code>##g' \
    | head -n 1
} || true)"

if [ -z "$key" ]; then
  echo "无法从官方论坛帖子抓取 MakeMKV beta key: $forum_url" >&2
  exit 1
fi

printf '%s\n' "$key"
