#!/usr/bin/env bash
# ==============================================================
# Overleaf <-> 本代码库 双向同步脚本（基于 git subtree）
#
# 前置准备（只需做一次，且需要你手动操作，不要把 token 提交进仓库）：
#   1. 登录 Overleaf -> Account Settings (https://www.overleaf.com/user/settings)
#      -> Git Integration -> 生成一个 Git 认证 token。
#   2. 找到你的 Overleaf 项目 ID：项目 URL 形如
#        https://www.overleaf.com/project/<PROJECT_ID>
#   3. 导出环境变量（建议写进你本地的 ~/.bashrc 或一个不会被提交的 .env.local）：
#        export OVERLEAF_PROJECT_ID="<PROJECT_ID>"
#        export OVERLEAF_GIT_TOKEN="<你的token>"
#   4. 用 git credential helper 缓存 token，避免每次手动输入：
#        git config --global credential.helper store
#      或者直接在下面生成的 remote URL 里内嵌 token（见 REMOTE_URL 拼接逻辑）。
#
# 用法：
#   bash docs/our_tex/scripts/sync_to_overleaf.sh init      # 首次初始化：用本地内容去"种"到一个空的 Overleaf 项目
#   bash docs/our_tex/scripts/sync_to_overleaf.sh push      # 把本地改动推到 Overleaf（触发云端重新编译）
#   bash docs/our_tex/scripts/sync_to_overleaf.sh pull      # 把 Overleaf 网页端的改动合并回本地（两边历史相关时用）
#   bash docs/our_tex/scripts/sync_to_overleaf.sh overwrite # 反向初始化：Overleaf 项目已有内容（如模板），
#                                                            # 用它整个覆盖本地 docs/our_tex（scripts/ 目录会保留）
#
# 原理：
#   Overleaf 项目本质上是一个 Git remote (https://git.overleaf.com/<id>)。
#   git subtree 把代码库里的 docs/our_tex 目录当成一个独立的“子仓库”，
#   push 时把该目录的 commit 历史投影推送到 Overleaf remote 的 main 分支，
#   pull 时反向把 Overleaf 的 commit 合并回 docs/our_tex 目录。
#   官方文档: https://docs.overleaf.com/integrations-and-add-ons/git-integration-and-github-synchronization/git-integration
#
# 关于 git subtree 命令本身：
#   本机系统自带的 git（如 CentOS 7 默认的 2.8.x）常常不打包 git-subtree
#   （它属于 git 官方 contrib/ 工具，很多发行版精简掉了）。
#   本脚本会自动检测：
#     1) 如果系统 git 已经自带 subtree 子命令，直接用；
#     2) 否则回退使用 vendor/git-subtree.sh（从 git 官方仓库拉取的原版脚本），
#        通过临时设置 GIT_EXEC_PATH/PATH 让 `git subtree` 命令可用，
#        不需要 root 权限、不修改系统 git 安装。
# ==============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel)"
PREFIX="docs/our_tex"
REMOTE_NAME="overleaf"
BRANCH="main"

: "${OVERLEAF_PROJECT_ID:?请先 export OVERLEAF_PROJECT_ID=<你的Overleaf项目ID>}"
: "${OVERLEAF_GIT_TOKEN:?请先 export OVERLEAF_GIT_TOKEN=<你的Overleaf Git token>}"

# ---------- 确保 `git subtree` 命令可用（系统自带优先，否则用 vendor 版本） ----------
ensure_git_subtree() {
  if git subtree --help >/dev/null 2>&1 || git help -a 2>/dev/null | grep -q '^  subtree'; then
    return 0
  fi

  local vendor_script="${SCRIPT_DIR}/vendor/git-subtree.sh"
  if [ ! -f "${vendor_script}" ]; then
    echo "!! 系统 git 不带 subtree 子命令，且未找到内置脚本: ${vendor_script}" >&2
    echo "   请从 https://raw.githubusercontent.com/git/git/master/contrib/subtree/git-subtree.sh 下载后放到该路径" >&2
    exit 1
  fi

  local vendor_bin_dir
  vendor_bin_dir="$(mktemp -d)"
  cp "${vendor_script}" "${vendor_bin_dir}/git-subtree"
  chmod +x "${vendor_bin_dir}/git-subtree"

  # git-subtree.sh 自检要求 GIT_EXEC_PATH 必须是 $PATH 的第一个目录，
  # 且该目录下要能找到 git-sh-setup，所以把系统 exec-path 里的文件
  # 一并软链接过去，让这一个目录同时满足两个条件。
  local real_exec_path
  real_exec_path="$(git --exec-path)"
  for f in "${real_exec_path}"/*; do
    ln -sf "$f" "${vendor_bin_dir}/$(basename "$f")" 2>/dev/null || true
  done

  export GIT_EXEC_PATH="${vendor_bin_dir}"
  export PATH="${vendor_bin_dir}:${PATH}"
  echo ">> 系统 git 未自带 subtree，已启用内置版本 (${vendor_script})"
}

ensure_git_subtree

# username 固定为 "git"，password 用 token；直接拼进 URL 方便脚本免交互
REMOTE_URL="https://git:${OVERLEAF_GIT_TOKEN}@git.overleaf.com/${OVERLEAF_PROJECT_ID}"

cd "${REPO_ROOT}"

ensure_remote() {
  if git remote get-url "${REMOTE_NAME}" >/dev/null 2>&1; then
    git remote set-url "${REMOTE_NAME}" "${REMOTE_URL}"
  else
    git remote add "${REMOTE_NAME}" "${REMOTE_URL}"
  fi
}

cmd="${1:-}"
case "${cmd}" in
  init)
    ensure_remote
    echo ">> 首次初始化：将 ${PREFIX} 作为 subtree 推送到 Overleaf remote (${BRANCH})"
    git subtree push --prefix="${PREFIX}" "${REMOTE_NAME}" "${BRANCH}"
    echo "✅ 初始化完成，去 Overleaf 网页端确认项目内容已同步"
    ;;
  push)
    ensure_remote
    echo ">> 推送 ${PREFIX} 的最新改动到 Overleaf（会触发云端重新编译）"
    git subtree push --prefix="${PREFIX}" "${REMOTE_NAME}" "${BRANCH}"
    echo "✅ 推送完成"
    ;;
  pull)
    ensure_remote
    echo ">> 从 Overleaf 拉取改动合并回 ${PREFIX}"
    git subtree pull --prefix="${PREFIX}" "${REMOTE_NAME}" "${BRANCH}" --squash -m "sync: pull from overleaf"
    echo "✅ 拉取完成，请检查 git diff 确认合并结果"
    ;;
  overwrite)
    ensure_remote
    if [ ! -d "${PREFIX}" ]; then
      echo "!! ${PREFIX} 目录不存在" >&2
      exit 1
    fi
    echo ">> 警告：这会用 Overleaf remote (${REMOTE_NAME}/${BRANCH}) 的内容覆盖本地 ${PREFIX}"
    echo ">> 会保留 ${PREFIX}/scripts/ 目录（同步脚本本身），其余内容会被删除后由 Overleaf 内容替代"
    read -r -p "确认继续？输入 yes 继续: " confirm
    if [ "${confirm}" != "yes" ]; then
      echo "已取消"
      exit 0
    fi

    # 备份 scripts/，因为 git subtree add 要求目标目录不存在
    tmp_scripts_backup="$(mktemp -d)/scripts"
    if [ -d "${PREFIX}/scripts" ]; then
      cp -r "${PREFIX}/scripts" "${tmp_scripts_backup}"
    fi

    # git subtree add 要求 prefix 目录不能已存在，先移出工作区（用 git rm 走版本控制）
    git rm -r --quiet "${PREFIX}"
    rmdir "${PREFIX}" 2>/dev/null || true
    git commit -m "chore: remove local ${PREFIX} before overwriting from overleaf template" --quiet

    echo ">> 从 Overleaf remote 拉取模板内容到 ${PREFIX}"
    git subtree add --prefix="${PREFIX}" "${REMOTE_NAME}" "${BRANCH}" --squash -m "Add ${PREFIX} from overleaf ACM template"

    # 恢复 scripts/（同步脚本本身，不应被模板覆盖）
    if [ -d "${tmp_scripts_backup}" ]; then
      rm -rf "${PREFIX}/scripts"
      cp -r "${tmp_scripts_backup}" "${PREFIX}/scripts"
      git add "${PREFIX}/scripts"
      git commit -m "chore: restore sync scripts after overleaf template overwrite" --quiet
    fi

    echo "✅ 覆盖完成：${PREFIX} 现在是 Overleaf 模板内容 + 原有 scripts/"
    echo "   请检查 ${PREFIX}/main.tex 等文件，确认模板结构，必要时调整 compile.sh 里的 MAIN 变量"
    ;;
  *)
    echo "用法: $0 {init|push|pull|overwrite}"
    exit 1
    ;;
esac
