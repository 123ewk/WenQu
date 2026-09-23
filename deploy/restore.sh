#!/usr/bin/env bash
# WenQu 全量恢复(配合 deploy/backup.sh 的产物;演练与实恢复同一入口)。
# 用法:BACKUP_DIR=/var/backups/wenq... ./deploy/restore.sh backups/20260920T120000
# 计时输出对照 RTO(架构设计:4h),末行打印总耗时。
set -euo pipefail

SRC="${1:?用法: restore.sh <备份目录>}"
# pwd -W:Git Bash 下给出 G:/… 形式的宿主路径(docker.exe 认这个);Linux 下无 -W,回落 pwd。
# 配合 Windows 演练需 MSYS_NO_PATHCONV=1(见下 pg_restore 注释),两平台各取所需。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && { pwd -W 2>/dev/null || pwd; })"
# 与 backup.sh 同一组入参口径:CD 传绝对路径,人工跑走默认。
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
ENV_FILE="${COMPOSE_ENV_FILE:-$ROOT/.env}"
COMPOSE="docker compose -f $ROOT/$COMPOSE_FILE --env-file $ENV_FILE"
START=$(date +%s)

get_env() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }
PG_USER="$(get_env POSTGRES_USER)"
PG_DB="$(get_env POSTGRES_DB)"; PG_DB="${PG_DB:-wenqu}"
MINIO_USER="$(get_env MINIO_ROOT_USER)"
MINIO_PASS="$(get_env MINIO_ROOT_PASSWORD)"
BUCKET="$(get_env MINIO_BUCKET)"; BUCKET="${BUCKET:-wenqu-docs}"
[ -f "$SRC/manifest.txt" ] && [ -f "$SRC/pg/${PG_DB}.dump" ] || { echo "备份目录不完整: $SRC"; exit 1; }

echo "[restore] 从 $SRC 开始 $(date -Iseconds)"
# 0) 停写入方,避免恢复到半空库时前台报错
$COMPOSE stop server worker || true

# 1) Postgres:踢掉残留连接 → 整库重建 → 恢复
$COMPOSE exec -T postgres psql -U "$PG_USER" -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$PG_DB' AND pid<>pg_backend_pid();" >/dev/null
# CREATE/DROP DATABASE 不能与别的语句同处一个隐式事务(psql -c 多语句会包成一个事务块)→ 拆开
$COMPOSE exec -T postgres psql -U "$PG_USER" -d postgres -qc "DROP DATABASE IF EXISTS $PG_DB WITH (FORCE);"
$COMPOSE exec -T postgres psql -U "$PG_USER" -d postgres -qc "CREATE DATABASE $PG_DB OWNER $PG_USER;"
# pg_restore 读容器内文件;Windows/Git Bash 演练请带 MSYS_NO_PATHCONV=1 运行本脚本
# (否则宿主机会把 "/tmp/restore.dump" 参数改写成 Windows 路径,Linux 生产无此问题)
$COMPOSE cp "$SRC/pg/${PG_DB}.dump" postgres:/tmp/restore.dump
$COMPOSE exec -T postgres pg_restore -U "$PG_USER" -d "$PG_DB" --no-owner /tmp/restore.dump

# 2) MinIO:备份目录灌回桶(先清桶内孤儿对象由 mc mirror --remove 完成;临时源目录先清空)
$COMPOSE exec -T minio sh -c "rm -rf /tmp/restore"
$COMPOSE cp "$SRC/minio/." minio:/tmp/restore/
$COMPOSE exec -T minio sh -c \
  "mc alias set bkw http://127.0.0.1:9000 '$MINIO_USER' '$MINIO_PASS' >/dev/null \
   && mc mirror --overwrite --remove /tmp/restore bkw/$BUCKET"

# 3) 核对:alembic 版本 + 表数 + 对象数对齐 manifest
$COMPOSE exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -Atc \
  "SELECT version_num FROM alembic_version" | sed 's/^/[restore] alembic=/'
$COMPOSE exec -T minio sh -c \
  "mc alias set bkw http://127.0.0.1:9000 '$MINIO_USER' '$MINIO_PASS' >/dev/null && mc ls --recursive bkw/$BUCKET | wc -l" \
  | sed 's/^/[restore] minio_objects=/'
grep minio_objects "$SRC/manifest.txt" | sed 's/^/[backup]  manifest_/'

# 4) 起回服务并探活
$COMPOSE up -d server worker
for i in $(seq 1 30); do
  $COMPOSE exec -T server python -c \
    "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3)" \
    >/dev/null 2>&1 && break
  sleep 2
done

ELAPSED=$(( $(date +%s) - START ))
echo "[restore] 完成,总耗时 ${ELAPSED}s(RTO 目标 4h 内)"
