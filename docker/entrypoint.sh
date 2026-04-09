#!/bin/sh
set -eu

MAKEMKV_ROOT="/opt/makemkv"
MAKEMKV_BIN="$MAKEMKV_ROOT/bin/makemkvcon"
SETTINGS_DIR="$MAKEMKV_ROOT/conf"
SETTINGS_FILE="$SETTINGS_DIR/settings.conf"
DEFAULT_PROFILE="$MAKEMKV_ROOT/default.mmcp.xml"
FETCH_KEY_BIN="/usr/local/bin/fetch_makemkv_beta_key.sh"
KEY_INVALID_PATTERN='registration key to continue|Evaluation period has expired|This application version is too old|registration key.*invalid|Cannot parse registration key|Your registration key has expired'

ensure_runtime_layout() {
  : "${MAKEMKV_PROFILE:=$DEFAULT_PROFILE}"
  export MAKEMKV_PROFILE
  mkdir -p /root/.MakeMKV
  ln -sf "$SETTINGS_FILE" /root/.MakeMKV/settings.conf
  ln -sf "$MAKEMKV_BIN" /usr/local/bin/makemkvcon
  ln -sf "$DEFAULT_PROFILE" /usr/local/bin/default.mmcp.xml
}

ensure_built_in_config() {
  if [ ! -f "$SETTINGS_FILE" ]; then
    echo "缺少内置 MakeMKV 配置文件: $SETTINGS_FILE" >&2
    exit 1
  fi
  if [ ! -f "$MAKEMKV_PROFILE" ]; then
    echo "MakeMKV profile 不存在: $MAKEMKV_PROFILE" >&2
    exit 1
  fi
}

update_settings_key() {
  key="$1"
  if grep -q '^app_Key=' "$SETTINGS_FILE"; then
    sed -i "s|^app_Key=.*$|app_Key=$key|" "$SETTINGS_FILE"
  else
    printf 'app_Key=%s\n' "$key" >> "$SETTINGS_FILE"
  fi
}

makemkv_key_is_valid() {
  output="$($MAKEMKV_BIN -r info disc:9999 2>&1 || true)"
  if printf '%s' "$output" | grep -Eiq "$KEY_INVALID_PATTERN"; then
    return 1
  fi
  return 0
}

refresh_makemkv_key() {
  key="$($FETCH_KEY_BIN)"
  update_settings_key "$key"
}

ensure_valid_makemkv_key() {
  if makemkv_key_is_valid; then
    return 0
  fi

  echo "MakeMKV key 已失效，正在从官方论坛抓取最新 beta key..." >&2
  refresh_makemkv_key

  if ! makemkv_key_is_valid; then
    echo "MakeMKV key 刷新后仍不可用，请检查网络或论坛帖子格式是否变化。" >&2
    exit 1
  fi
}

ensure_runtime_layout
ensure_built_in_config
ensure_valid_makemkv_key

exec bluray_remux "$@"
