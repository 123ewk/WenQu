#!/usr/bin/env bash
# WenQu 生产部署入口(CD 与人工同一脚本)。
#
# 用法:  deploy/deploy.sh <镜像 tag,如 v1.2.0>
# 前置:  在仓库 checkout 内执行;密钥由 WENQU_ENV_FILE 指向的 .env 提供(脚本不读其内容,只交给 compose)。
#
# 三道硬门(顺序即安全边界):
#   1) 数据卷归属确认 —— compose 项目名决定 pgdata/minio 卷前缀,猜错等于对空库发布,故先探测再放行;
#   2) 迁移前备份     —— backup.sh 不成功就不进入 compose up(migrate 会改 schema,不可回退);
#   3) 健康校验       —— /system/info 通且 migration_dirty=false,否则把应用镜像回滚到上一版本。
# 回滚只回应用镜像:库结构一旦前移,脚本不会自动 DROP/RESTORE(破坏性操作留给人决策,只打印命令)。
set -euo pipefail

VERSION="${1:?用法: deploy/deploy.sh <镜像 tag,如 v1.2.0>}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${WENQU_ENV_FILE:-$HOME/wenqu/.env}"
STATE_FILE="${WENQU_STATE_FILE:-$HOME/wenqu/.deploy-state}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/wenqu/backups}"
WEB_URL="${WENQU_WEB_URL:-http://127.0.0.1}"
COMPOSE_FILE="${WENQU_COMPOSE_FILE:-docker-compose.prod.yml}"
IMAGE_PREFIX="${WENQU_IMAGE_PREFIX:-wenqu}"   # 走 ACR 时为 <ACR 实例域名>/<命名空间>(个人版是 crpi-xxx.<地域>.personal.cr.aliyuncs.com)
PUSH_ACR="${PUSH_ACR:-1}"                      # 0 = 只本机构建(离线演练)
APP_IMAGES=(server parser web)                 # worker 与 migrate 复用 server 镜像

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy][失败] %s\n' "$*" >&2; exit 1; }

[ -f "$ROOT/$COMPOSE_FILE" ] || die "找不到 $ROOT/$COMPOSE_FILE"
[ -f "$ENV_FILE" ] || die "找不到生产 .env: $ENV_FILE(用 WENQU_ENV_FILE 指定)"
# 缺 curl 会让门 3 空转 HEALTH_TRIES 次才失败,提前拒;此时可退化为 docker compose exec 探活
command -v curl >/dev/null || die "本机缺 curl,无法做健康校验(门 3 会空转到超时,先装 curl)"

# 存量卷前缀探测:卷名形如 <project>_wenqu_pgdata,每行一个拥有数据卷的候选项目。
detect_project() {
  docker volume ls --filter name=wenqu_pgdata --format '{{.Name}}' \
    | sed -n 's/_wenqu_pgdata$//p' | sort -u | grep . || true
}

# ---- 门 1:确认 compose 项目名,绝不对空库发布 --------------------------------
# 服务器可能残留演练项目卷(如 wenqu-drill),故多候选一律人工指定,不用"看起来像"的那个。
CANDIDATES="$(detect_project)"
if [ -z "$CANDIDATES" ]; then
  [ "${WENQU_ALLOW_FRESH:-0}" = "1" ] || die "没有存量 wenqu_pgdata 卷:首次部署需显式 WENQU_ALLOW_FRESH=1 确认建空库"
  PROJECT="${WENQU_COMPOSE_PROJECT:-wenqu}"
elif [ -n "${WENQU_COMPOSE_PROJECT:-}" ]; then
  printf '%s\n' "$CANDIDATES" | grep -qxF "$WENQU_COMPOSE_PROJECT" \
    || die "指定项目 $WENQU_COMPOSE_PROJECT 没有 wenqu_pgdata 卷(候选: $(printf '%s' "$CANDIDATES" | tr '\n' ' '))"
  PROJECT="$WENQU_COMPOSE_PROJECT"
elif [ "$(printf '%s\n' "$CANDIDATES" | wc -l)" -gt 1 ]; then
  die "探到多个候选项目: $(printf '%s' "$CANDIDATES" | tr '\n' ' ');请用 WENQU_COMPOSE_PROJECT 指定(docker volume ls | grep wenqu_pgdata)"
else
  PROJECT="$CANDIDATES"
fi
export COMPOSE_PROJECT_NAME="$PROJECT"
log "compose 项目 = $PROJECT(卷 ${PROJECT}_wenqu_pgdata / ${PROJECT}_wenqu_minio)"

# compose 统一入口:--env-file 喂 ${...} 插值,APP_ENV_FILE 让服务级 env_file 指到生产 .env(而非 checkout 目录)。
# TAG 由调用方设定:同一次发布内 build/up/回滚各自带自己的镜像 tag。
compose() {
  APP_IMAGE_PREFIX="$IMAGE_PREFIX" APP_IMAGE_TAG="$TAG" APP_ENV_FILE="$ENV_FILE" \
    docker compose -f "$ROOT/$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

PREV=""
[ -f "$STATE_FILE" ] && PREV="$(cat "$STATE_FILE" 2>/dev/null || true)"
log "目标版本 $VERSION(上一版本 ${PREV:-无})"

# ---- 构建 + 归档推 ACR --------------------------------------------------------
TAG="$VERSION"
compose build
if [ "$PUSH_ACR" = "1" ] && [ "$IMAGE_PREFIX" != "wenqu" ]; then
  for svc in "${APP_IMAGES[@]}"; do
    # 推送失败只告警不阻断:日常部署靠本机构建好的镜像,ACR 是归档位
    docker push "$IMAGE_PREFIX/wenqu-$svc:$VERSION" \
      || log "警告:wenqu-$svc:$VERSION 未归档到 ACR"
  done
fi

# ---- 门 2:迁移前强制备份 ------------------------------------------------------
BACKUP_DEST=""
if [ "${SKIP_BACKUP:-0}" = "1" ]; then
  log "SKIP_BACKUP=1:跳过备份(仅限演练,生产禁用)"
else
  # 失败时把已捕获的 stdout 打出来:backup.sh 的诊断信息一半在 stdout,吞掉就只能靠猜。
  BACKUP_OUT="$(BACKUP_DIR="$BACKUP_DIR" COMPOSE_FILE="$COMPOSE_FILE" COMPOSE_ENV_FILE="$ENV_FILE" \
    "$ROOT/deploy/backup.sh")" || { printf '%s\n' "$BACKUP_OUT"; die "备份未成功,拒绝进入迁移"; }
  printf '%s\n' "$BACKUP_OUT"
  BACKUP_DEST="$(printf '%s\n' "$BACKUP_OUT" | sed -n 's/^\[backup\] 完成 //p' | tail -1)"
  [ -n "$BACKUP_DEST" ] && [ -d "$BACKUP_DEST" ] || die "备份产物缺失: '$BACKUP_DEST'"
  log "迁移前备份就绪: $BACKUP_DEST"
fi
# 回库是破坏性操作,脚本只把可直接照抄的命令打出来,绝不代跑
RESTORE_HINT="${BACKUP_DEST:+COMPOSE_FILE=$COMPOSE_FILE COMPOSE_ENV_FILE=$ENV_FILE COMPOSE_PROJECT_NAME=$PROJECT $ROOT/deploy/restore.sh '$BACKUP_DEST'}"

# ---- 起服(migrate 在依赖链里,非零退出即阻断 server/worker)-------------------
rollback() {
  [ -n "$PREV" ] || { log "无上一版本记录,不自动回滚"; return 1; }
  TAG="$PREV"
  compose up -d --no-build || { log "回滚起服失败,需人工介入"; return 1; }
  printf '%s' "$PREV" > "$STATE_FILE"
  log "应用镜像已回滚到 $PREV"
}

if ! compose up -d; then
  rollback || true
  die "compose up 失败(migrate 未通过?)${RESTORE_HINT:+;回库命令: $RESTORE_HINT}"
fi

# ---- 门 3:健康校验 ------------------------------------------------------------
verify() {
  local body
  for _ in $(seq 1 "${HEALTH_TRIES:-40}"); do
    body="$(curl -fsS --max-time 5 "$WEB_URL/system/info" 2>/dev/null || true)"
    if [ -n "$body" ]; then
      log "健康: $body"
      printf '%s' "$body" | grep -q '"migration_dirty": *false' && return 0
      return 1
    fi
    sleep 3
  done
  log "$WEB_URL/system/info 无响应"
  return 1
}

if ! verify; then
  rollback || true
  [ -z "$RESTORE_HINT" ] || die "若本次 migrate 已改 schema,需人工回库: $RESTORE_HINT"
  die "健康校验未通过,已尝试回滚;请查 compose logs"
fi

printf '%s' "$VERSION" > "$STATE_FILE"
log "完成:生产已运行 $VERSION"
