#!/usr/bin/env bash
# WenQu 磁盘回收清单(严格只读,不改任何东西):把"该删哪些"算成一条能直接复制的命令,删除动作永远留给人。
#
# 用法: deploy/cleanup-report.sh [all|images|backups]     (默认 all)
# 入参口径与 deploy.sh / backup.sh 一致(WENQU_STATE_FILE / BACKUP_DIR / BACKUP_KEEP),CD 与人工同一入口。
# 镜像保留规则: docker ps 里真在用的 + 版本号最高的两版(当前 + 上一版 = 回滚目标) + latest。
# 为什么一律不自动删: 省的是可再生的空间,赌的是不可再生的回滚保险与备份 —— 不对等。
set -euo pipefail

MODE="${1:-all}"
case "$MODE" in
  all | images | backups) ;;
  *) echo "用法: cleanup-report.sh [all|images|backups]"; exit 1 ;;
esac

STATE_FILE="${WENQU_STATE_FILE:-$HOME/wenqu/.deploy-state}"
ARCHIVE_LOG="$STATE_FILE.archives"          # deploy.sh 推 ACR 后追加的归档流水(ok/fail 各一行)
BACKUP_DIR="${BACKUP_DIR:-$HOME/wenqu/backups}"
KEEP="${BACKUP_KEEP:-7}"

hr() { printf -- '---- %s ----\n' "$*"; }

# ---- 旧镜像 tag ----
report_images() {
  hr "旧镜像 tag(保留:在用 + 最高两版 + latest)"
  # 在用与否看 docker ps,不看 state 文件:人工 compose up 过的机器上两者会不一致。
  local in_use all_tags versions t ver dead=""
  in_use="$(docker ps --format '{{.Image}}' | sort -u)"
  # 只认本项目三个服务的镜像名 <前缀>/wenqu-{server,parser,web} + 版本 tag;
  # postgres/minio 等第三方镜像、以及 <none> 悬空标签一律不进清单。
  all_tags="$(docker images --format '{{.Repository}}:{{.Tag}}' | awk -F: '
    $1 ~ /(^|\/)wenqu-(server|parser|web)$/ && ($2 == "latest" || $2 ~ /^v?[0-9][0-9.]*$/) { print }')"
  if [ -z "$all_tags" ]; then
    echo "本机没有 wenqu-{server,parser,web} 版本镜像,无需回收"
    return 0
  fi

  # 版本号按 -V(数值段)排序取最高两版:第三版及以后才是回收对象。
  versions="$(printf '%s\n' "$all_tags" | sed -n 's/.*:v\{0,1\}\([0-9][0-9.]*\)$/\1/p' | sort -uV | tail -2)"
  while read -r t; do
    [ -n "$t" ] || continue
    if printf '%s\n' "$in_use" | grep -qxF "$t"; then continue; fi
    case "$t" in *:latest) continue ;; esac
    ver="${t##*:}"; ver="${ver#v}"
    if printf '%s\n' "$versions" | grep -qxF "$ver"; then continue; fi
    dead="$dead$t"$'\n'
  done <<< "$all_tags"

  if [ -z "$dead" ]; then
    echo "没有可回收的旧镜像(最高两版 + 在用 + latest 之外已无残留)"
    return 0
  fi
  if [ -f "$ARCHIVE_LOG" ]; then
    local a
    while read -r t; do
      [ -n "$t" ] || continue
      a="$(grep -F " $t" "$ARCHIVE_LOG" | tail -1 | cut -d' ' -f1 || true)"
      printf '  %-70s 归档=%s\n' "$t" "${a:-查无}"
    done <<< "$dead"
    echo "  (归档=ok:ACR 上有这份,删了随时拉回;fail 或查无:本机删掉只能重建,建议先补推再删)"
  else
    echo "  ⚠ 没有归档流水($ARCHIVE_LOG 不存在),下面这些删掉前请自己确认 ACR 上有没有"
  fi
  echo "确认无误后人工执行(不可逆):"
  printf 'docker rmi %s\n' "$(printf '%s' "$dead" | tr '\n' ' ')"
}

# ---- 过期备份 ----
report_backups() {
  hr "备份保留(保留最近 $KEEP 份;删除永远人工)"
  if [ ! -d "$BACKUP_DIR" ]; then
    echo "备份目录 $BACKUP_DIR 还不存在,无需回收"
    return 0
  fi
  local all total expired
  all="$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*T[0-9]*' 2>/dev/null | sort -r || true)"
  total="$(printf '%s\n' "$all" | grep -c . || true)"
  expired="$(printf '%s\n' "$all" | tail -n +$((KEEP + 1)))"
  if [ -z "$expired" ]; then
    echo "共 $total 份,未超出保留份数 $KEEP"
    return 0
  fi
  echo "共 $total 份,已超出保留份数 $KEEP,以下旧备份仍在盘上(未删除):"
  printf '%s\n' "$expired" | while read -r d; do du -sh "$d"; done
  echo "确认要回收磁盘时人工执行(不可逆,且这是唯一离线保险):"
  printf 'rm -rf %s\n' "$(printf '%s' "$expired" | tr '\n' ' ')"
  echo "提醒:备份频率 = 发布频率,长期不发布就没有新备份;要日备份得另配 cron。"
}

# ---- 磁盘现状(只读) ----
report_disk() {
  hr "磁盘现状"
  df -h / | tail -2
  docker system df || echo "(docker system df 失败:daemon 没起来?)"
  if [ -f "$STATE_FILE" ]; then
    echo "当前生产版本: $(cat "$STATE_FILE")"
  fi
}

case "$MODE" in
  all) report_disk && report_images && report_backups ;;
  images) report_images ;;
  backups) report_backups ;;
esac
