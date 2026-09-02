#!/usr/bin/env bash
# 生成可分发源码包：只包含运行所需的源码、配置模板和文档，绝不包含本机密钥或构建产物。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

readonly PACKAGE_NAME="FitMind-AI-source"
OUTPUT_PATH="${1:-./${PACKAGE_NAME}.zip}"
if [[ "$OUTPUT_PATH" != /* ]]; then
  OUTPUT_PATH="$PWD/${OUTPUT_PATH#./}"
fi
readonly OUTPUT_PATH

if ! command -v zip >/dev/null 2>&1; then
  printf '找不到 zip 命令，请安装系统的 zip 工具后重试。\n' >&2
  exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
  printf '找不到 rsync 命令，请安装系统的 rsync 工具后重试。\n' >&2
  exit 1
fi

rm -f "$OUTPUT_PATH"

# 先放进临时目录，保证解压后有一个干净的顶层项目目录，不会把文件散落到下载目录。
STAGING_DIR="$(mktemp -d "${TMPDIR:-/tmp}/${PACKAGE_NAME}.XXXXXX")"
trap 'rm -rf "$STAGING_DIR"' EXIT
rsync -a \
  --exclude='.git/' \
  --exclude='.env' \
  --exclude='.codebuddy/' \
  --exclude='node_modules/' \
  --exclude='.next/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='.pytest_cache/' \
  --exclude='.coverage' \
  --exclude='coverage/' \
  --exclude='htmlcov/' \
  --exclude='dist/' \
  --exclude='build/' \
  --exclude='out/' \
  --exclude='*.tsbuildinfo' \
  --exclude='*.zip' \
  --exclude='*.log' \
  --exclude='*.docx' \
  --exclude='.DS_Store' \
  --exclude='picture/' \
  ./ "$STAGING_DIR/$PACKAGE_NAME/"

(cd "$STAGING_DIR" && zip -qr "$OUTPUT_PATH" "$PACKAGE_NAME")

printf '已生成源码包：%s\n' "$OUTPUT_PATH"
