#!/usr/bin/env bash

set -u

errors=0
warnings=0

ok() {
  printf '[OK]   %s\n' "$1"
}

warn() {
  printf '[WARN] %s\n' "$1"
  warnings=$((warnings + 1))
}

fail() {
  printf '[FAIL] %s\n' "$1"
  errors=$((errors + 1))
}

if command -v git >/dev/null 2>&1; then
  ok "$(git --version)"
else
  fail 'Git 未安装'
fi

if command -v python3.12 >/dev/null 2>&1; then
  python_version="$(python3.12 --version 2>&1)"
  ok "$python_version"
else
  fail 'Python 3.12 未安装或不在 PATH'
fi

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  ok '当前目录是本地 Git 仓库'
else
  fail '当前目录尚未初始化 Git 仓库'
fi

if git config user.name >/dev/null 2>&1 && git config user.email >/dev/null 2>&1; then
  ok 'Git 作者信息已配置'
else
  warn 'Git 作者信息未完整配置，首次提交前需要设置 user.name 和 user.email'
fi

if command -v docker >/dev/null 2>&1; then
  ok "$(docker --version)"
  if docker compose version >/dev/null 2>&1; then
    ok "$(docker compose version)"
  else
    fail 'Docker Compose v2 不可用'
  fi
  if docker info >/dev/null 2>&1; then
    ok 'Docker daemon 正在运行'
  else
    fail 'Docker 已安装，但 daemon 未运行'
  fi
else
  fail 'Docker/兼容容器运行时未安装'
fi

if command -v uv >/dev/null 2>&1; then
  ok "$(uv --version)"
else
  warn 'uv 未安装；可以先使用 Python venv，但建议在正式实现前安装 uv'
fi

if command -v psql >/dev/null 2>&1; then
  ok "$(psql --version)"
else
  warn 'psql 未安装；可先通过 Docker 容器内的 psql 操作数据库'
fi

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  ok 'GitHub CLI 已登录'
else
  warn 'GitHub CLI 未安装或未登录；不影响本地开发'
fi

key_is_set=0
if [ -n "${MODEL_API_KEY:-}" ] || [ -n "${DASHSCOPE_API_KEY:-}" ]; then
  key_is_set=1
elif [ -f .env ] && grep -Eq '^(MODEL_API_KEY|DASHSCOPE_API_KEY)=.+$' .env; then
  key_is_set=1
fi

if [ "$key_is_set" -eq 1 ]; then
  ok '模型 API Key 已配置（未显示内容）'
else
  warn '模型 API Key 尚未配置；不影响离线开发，但无法调用真实模型'
fi

printf 'Summary: %d error(s), %d warning(s)\n' "$errors" "$warnings"

if [ "$errors" -gt 0 ]; then
  exit 1
fi
