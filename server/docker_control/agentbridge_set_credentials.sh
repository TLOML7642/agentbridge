#!/bin/bash
# Official interactive CLI only. Never put a password in argv, logs or .env.
set -euo pipefail
set +x
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CFG="$ROOT/astrbot/data/cmd_config.json"
CONTAINER="${AGENTBRIDGE_ASTRBOT_CONTAINER:-agentbridge-astrbot}"
[[ $# -le 1 ]] || { echo "Usage: $0 [username]" >&2; exit 1; }
if [[ $EUID -eq 0 ]]; then
  PRIV=()
elif sudo -n true 2>/dev/null; then
  PRIV=(sudo -n)
else
  sudo -v
  PRIV=(sudo)
fi
if docker info >/dev/null 2>&1; then DOCKER=(docker); else DOCKER=(sudo docker); fi

config_state() {
  "${PRIV[@]}" python3 - "$CFG" <<'PY'
import hashlib, json, sys
d = json.load(open(sys.argv[1], encoding="utf-8-sig"))["dashboard"]
assert isinstance(d.get("username"), str) and d["username"]
assert d.get("pbkdf2_password"), "Missing upgraded password storage"
print(d["username"])
print(hashlib.sha256(d["pbkdf2_password"].encode()).hexdigest())
assert not d.get("password_change_required", False), "Password setup required"
PY
}
ready() {
  local end=$((SECONDS + 90))
  while (( SECONDS < end )); do
    if [[ "$("${DOCKER[@]}" inspect -f '{{.State.Running}}' "$CONTAINER")" == true ]] &&
       "${DOCKER[@]}" exec "$CONTAINER" python -c 'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:6185/api/auth/setup-status",timeout=2)); assert d.get("status")=="ok" and d.get("data",{}).get("setup_required") is False' >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}
BEFORE="$(config_state)"
USERNAME="${1:-${BEFORE%%$'\n'*}}"
[[ "$USERNAME" != -* && -n "$USERNAME" ]] || { echo "Invalid username" >&2; exit 1; }
IMAGE="$("${DOCKER[@]}" inspect -f '{{.Config.Image}}' "$CONTAINER")"
WAS_RUNNING="$("${DOCKER[@]}" inspect -f '{{.State.Running}}' "$CONTAINER")"
[[ "$WAS_RUNNING" == true || "$WAS_RUNNING" == false ]] || exit 1
# An active reset flag would silently undo an official CLI change at the next start.
"${DOCKER[@]}" inspect -f '{{json .Config.Env}}' "$CONTAINER" |
  python3 -c 'import json,sys; e=dict(x.split("=",1) for x in json.load(sys.stdin) if "=" in x); assert e.get("ASTRBOT_RESET_DASHBOARD_PASSWORD","").lower() not in ("1","true","yes"), "Disable the reset flag before using the CLI"'
BACKUP=""
CHANGED=0
SUCCESS=0
recover() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ $SUCCESS -eq 0 ]]; then
    echo "维护失败，正在恢复原配置和运行状态。" >&2
    if [[ $CHANGED -eq 1 && -n "$BACKUP" ]]; then
      if ! "${DOCKER[@]}" stop "$CONTAINER" >/dev/null ||
         [[ "$("${DOCKER[@]}" inspect -f '{{.State.Running}}' "$CONTAINER")" != false ]]; then
        echo "无法确保停服，未覆盖配置；保留私有回滚材料：$BACKUP" >&2
        exit 1
      fi
      "${PRIV[@]}" cp -p "$BACKUP/cmd_config.json" "$CFG" || exit 1
      "${PRIV[@]}" sha256sum -c "$BACKUP/source.sha256" >/dev/null || exit 1
    fi
    if [[ "$WAS_RUNNING" == true ]]; then
      if ! "${DOCKER[@]}" start "$CONTAINER" >/dev/null || ! ready; then
        echo "原服务恢复未就绪；保留私有回滚材料：$BACKUP" >&2
        exit 1
      fi
    fi
    echo "原状态已恢复；私有回滚材料：${BACKUP:-未创建}" >&2
    [[ $rc -ne 0 ]] || rc=1
    exit "$rc"
  fi
}
# Stop failure aborts before the CLI or any configuration write.
if [[ "$WAS_RUNNING" == true ]]; then
  "${DOCKER[@]}" stop "$CONTAINER" >/dev/null
fi
[[ "$("${DOCKER[@]}" inspect -f '{{.State.Running}}' "$CONTAINER")" == false ]] || exit 1
trap recover EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
BACKUP="$("${PRIV[@]}" mktemp -d /var/tmp/agentbridge-credentials.XXXXXXXX)"
"${PRIV[@]}" cp -p "$CFG" "$BACKUP/cmd_config.json"
"${PRIV[@]}" sh -c 'sha256sum "$1" > "$2/source.sha256"' sh "$CFG" "$BACKUP"
# Re-read after stop because AstrBot may flush its last in-memory configuration.
BEFORE="$(config_state)"
CHANGED=1
echo "官方 CLI 将交互输入新密码，输入不回显。"
"${DOCKER[@]}" run --rm -it --network none --entrypoint astrbot \
  -v "$ROOT/astrbot/data:/AstrBot/data" -w /AstrBot "$IMAGE" password --username "$USERNAME"
AFTER="$(config_state)"
[[ "${AFTER%%$'\n'*}" == "$USERNAME" && "${AFTER#*$'\n'}" != "${BEFORE#*$'\n'}" ]]
if [[ "$WAS_RUNNING" == true ]]; then
  "${DOCKER[@]}" start "$CONTAINER" >/dev/null
  ready
  [[ "$(config_state)" == "$AFTER" ]]
fi
SUCCESS=1
"${PRIV[@]}" rm -f "$BACKUP/cmd_config.json" "$BACKUP/source.sha256"
"${PRIV[@]}" rmdir "$BACKUP"
echo "官方 CLI 更新和配置校验完成，原运行状态已保留。请用新密码交互登录验证。"
