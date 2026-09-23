#!/usr/bin/env bash
# WenQu 全量备份(Go-Live 备份恢复 / 上线七步"迁移前自动备份")。
# 两方同源:schema+向量+全文索引 → pg_dump -Fc;原始文件 → MinIO mc mirror。
# 用法:BACKUP_DIR=/var/backups/wenqu ./deploy/backup.sh
# 凭据全部从仓库根 .env 读取,不落脚本、不进镜像。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && { pwd -W 2>/dev/null || pwd; })"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
TS="$(date +%Y%m%dT%H%M%S)"
DEST="$BACKUP_DIR/$TS"
# CD 从 runner 工作区调用时显式传 compose 文件与 .env 路径;默认值保持人工在 ~/wenqu 直接跑不变。
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
ENV_FILE="${COMPOSE_ENV_FILE:-$ROOT/.env}"
COMPOSE="docker compose -f $ROOT/$COMPOSE_FILE --env-file $ENV_FILE"

get_env() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }
PG_USER="$(get_env POSTGRES_USER)"
PG_DB="$(get_env POSTGRES_DB)"; PG_DB="${PG_DB:-wenqu}"
MINIO_USER="$(get_env MINIO_ROOT_USER)"
MINIO_PASS="$(get_env MINIO_ROOT_PASSWORD)"
BUCKET="$(get_env MINIO_BUCKET)"; BUCKET="${BUCKET:-wenqu-docs}"
[ -n "$PG_USER" ] && [ -n "$MINIO_USER" ] || { echo "缺少 POSTGRES_USER/MINIO_ROOT_USER(.env)"; exit 1; }
# MC_HOST 是 URL 形式,凭据含 @ : / 或空白会解析错位;备份是发布硬门,宁可这里说清楚而非让 mc 报玄学错。
case "$MINIO_USER$MINIO_PASS$BUCKET" in
  *[!A-Za-z0-9._-]*) echo "MinIO 凭据/桶名含 URL 保留字符(如 @ : / 空格),backup.sh 无法安全传参"; exit 1 ;;
esac

mkdir -p "$DEST/pg" "$DEST/minio"
echo "[backup] $DEST 开始 $(date -Iseconds)"

# 1) Postgres:custom 格式(索引定义随 schema 导出,恢复时重建)
$COMPOSE exec -T postgres pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$DEST/pg/${PG_DB}.dump"

# 2) MinIO:整桶镜像到容器临时目录,再 cp 出(mc 在 minio 镜像内自带)
# 凭据必须走 MC_HOST_<alias> 环境变量并以位置参数传入:mc 是 Go 程序,以 - 开头的口令会被
# 当成命令行选项直接报错;拼进 sh -c 字符串又会被口令里的引号截断。
$COMPOSE exec -T minio sh -c '
  export MC_HOST_bkw="http://$1:$2@127.0.0.1:9000"
  rm -rf /tmp/mirror && mc mirror --overwrite "bkw/$3" /tmp/mirror
' backup "$MINIO_USER" "$MINIO_PASS" "$BUCKET"
$COMPOSE cp minio:/tmp/mirror/. "$DEST/minio/"

# 3) 清单:alembic 版本 + 表数 + MinIO 对象清单(恢复后核对用)
{
  echo "created_at=$(date -Iseconds)"
  echo "db=$PG_USER/$PG_DB bucket=$BUCKET"
  $COMPOSE exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -Atc \
    "SELECT 'alembic='||version_num FROM alembic_version" || echo "alembic=UNKNOWN"
  $COMPOSE exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -Atc \
    "SELECT 'public_tables='||count(*) FROM information_schema.tables WHERE table_schema='public'"
  find "$DEST/minio" -type f | sed "s|$DEST/minio/||" | sort > "$DEST/minio_manifest.txt"
  echo "minio_objects=$(wc -l < "$DEST/minio_manifest.txt")"
} | tee "$DEST/manifest.txt"
du -sh "$DEST"
echo "[backup] 完成 $DEST"

# 保留策略:只报告"超出最近 BACKUP_KEEP 份"的旧备份,绝不自动删 ——
# 删备份和回库同属不可逆操作,一律留给人执行(口径与 deploy.sh 的 RESTORE_HINT 一致)。
KEEP="${BACKUP_KEEP:-7}"
ALL="$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*T[0-9]*' 2>/dev/null | sort -r)" || true
TOTAL="$(printf '%s\n' "$ALL" | grep -c . || true)"
EXPIRED="$(printf '%s\n' "$ALL" | tail -n +$((KEEP + 1)))"
if [ -n "$EXPIRED" ]; then
  echo "[backup] 共 $TOTAL 份,已超出保留份数 $KEEP,以下旧备份仍在盘上(未删除):"
  printf '%s\n' "$EXPIRED" | while read -r d; do du -sh "$d"; done
  echo "[backup] 确认要回收磁盘时,人工执行(不可逆): rm -rf $(printf '%s\n' "$EXPIRED" | tr '\n' ' ')"
else
  echo "[backup] 共 $TOTAL 份,未超出保留份数 $KEEP"
fi
