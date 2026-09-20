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
COMPOSE="docker compose -f $ROOT/docker-compose.yml --env-file $ROOT/.env"

get_env() { grep -E "^$1=" "$ROOT/.env" | head -1 | cut -d= -f2-; }
PG_USER="$(get_env POSTGRES_USER)"
PG_DB="$(get_env POSTGRES_DB)"; PG_DB="${PG_DB:-wenqu}"
MINIO_USER="$(get_env MINIO_ROOT_USER)"
MINIO_PASS="$(get_env MINIO_ROOT_PASSWORD)"
BUCKET="$(get_env MINIO_BUCKET)"; BUCKET="${BUCKET:-wenqu-docs}"
[ -n "$PG_USER" ] && [ -n "$MINIO_USER" ] || { echo "缺少 POSTGRES_USER/MINIO_ROOT_USER(.env)"; exit 1; }

mkdir -p "$DEST/pg" "$DEST/minio"
echo "[backup] $DEST 开始 $(date -Iseconds)"

# 1) Postgres:custom 格式(索引定义随 schema 导出,恢复时重建)
$COMPOSE exec -T postgres pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$DEST/pg/${PG_DB}.dump"

# 2) MinIO:整桶镜像到容器临时目录,再 cp 出(mc 在 minio 镜像内自带)
# 注意:必须用显式 alias 的桶路径 —— minio 镜像内的 "local" alias 不保证已用当前 root 凭据配好
$COMPOSE exec -T minio sh -c \
  "mc alias set bkw http://127.0.0.1:9000 '$MINIO_USER' '$MINIO_PASS' >/dev/null \
   && rm -rf /tmp/mirror && mc mirror --overwrite bkw/$BUCKET /tmp/mirror"
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
