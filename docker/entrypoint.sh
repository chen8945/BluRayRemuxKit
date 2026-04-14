#!/bin/sh
set -eu

MAKEMKV_ROOT="/opt/makemkv"
MAKEMKV_BIN="$MAKEMKV_ROOT/bin/makemkvcon"
SETTINGS_DIR="$MAKEMKV_ROOT/conf"
SETTINGS_FILE="$SETTINGS_DIR/settings.conf"
DEFAULT_PROFILE="$MAKEMKV_ROOT/default.mmcp.xml"
FETCH_KEY_BIN="/usr/local/bin/fetch_makemkv_beta_key.sh"
KEY_INVALID_PATTERN='registration key to continue|Evaluation period has expired|This application version is too old|registration key.*invalid|Cannot parse registration key|Your registration key has expired'
FETCH_RETRY_COUNT="${MAKEMKV_KEY_FETCH_RETRY_COUNT:-5}"
FETCH_RETRY_DELAY="${MAKEMKV_KEY_FETCH_RETRY_DELAY:-3}"

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

mark_makemkv_available() {
  export BLURAY_REMUX_MAKEMKV_AVAILABLE=1
  unset BLURAY_REMUX_MAKEMKV_REASON
}

mark_makemkv_unavailable() {
  reason="$1"
  export BLURAY_REMUX_MAKEMKV_AVAILABLE=0
  export BLURAY_REMUX_MAKEMKV_REASON="$reason"
  echo "$reason" >&2
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

fetch_makemkv_key_with_retry() {
  attempt=1
  while [ "$attempt" -le "$FETCH_RETRY_COUNT" ]; do
    if [ "$FETCH_RETRY_COUNT" -gt 1 ]; then
      echo "MakeMKV beta key 抓取尝试 ${attempt}/${FETCH_RETRY_COUNT}..." >&2
    fi
    if key="$($FETCH_KEY_BIN)"; then
      printf '%s\n' "$key"
      return 0
    fi
    if [ "$attempt" -lt "$FETCH_RETRY_COUNT" ]; then
      echo "MakeMKV beta key 抓取失败，${FETCH_RETRY_DELAY} 秒后重试..." >&2
      sleep "$FETCH_RETRY_DELAY"
    fi
    attempt=$((attempt + 1))
  done
  return 1
}

refresh_makemkv_key() {
  if ! key="$(fetch_makemkv_key_with_retry)"; then
    return 1
  fi
  if ! update_settings_key "$key"; then
    return 1
  fi
  return 0
}

ensure_valid_makemkv_key() {
  mark_makemkv_available
  if makemkv_key_is_valid; then
    return 0
  fi

  echo "MakeMKV key 已失效，正在从官方论坛抓取最新 beta key..." >&2
  if ! refresh_makemkv_key; then
    mark_makemkv_unavailable "警告：MakeMKV key 自动刷新失败，当前将禁用 MakeMKV 并继续启动容器。"
    return 0
  fi

  if ! makemkv_key_is_valid; then
    mark_makemkv_unavailable "警告：MakeMKV key 刷新后仍不可用，当前将禁用 MakeMKV 并继续启动容器。"
    return 0
  fi

  mark_makemkv_available
}

ensure_runtime_layout
ensure_built_in_config
ensure_valid_makemkv_key

exec bluray_remux "$@"
