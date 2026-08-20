#!/usr/bin/env bash
# ==============================================================
# 本地 LaTeX 编译脚本（不依赖 latexmk，仅用 pdflatex + bibtex）
#
# 用法：
#   bash docs/our_tex/scripts/compile.sh
#
# 目的：
#   在把改动 push 到 Overleaf 之前，先在本地跑一遍标准四遍编译
#   (pdflatex -> bibtex -> pdflatex -> pdflatex)，
#   尽量保证 Overleaf 云端编译大概率也能通过（两边都是标准 TeX Live）。
# ==============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEX_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
MAIN="main"

cd "${TEX_DIR}"

echo "[1/4] pdflatex (pass 1)"
pdflatex -interaction=nonstopmode -halt-on-error "${MAIN}.tex" || {
  echo "!! pdflatex 第一遍失败，请查看上方日志 (${MAIN}.log)";
  exit 1;
}

if [ -s "${MAIN}.bib" ] || grep -q '\\bibliography{' "${MAIN}.tex" 2>/dev/null; then
  echo "[2/4] bibtex"
  bibtex "${MAIN}" || echo "!! bibtex 有告警，通常是未引用条目，可忽略"
else
  echo "[2/4] 跳过 bibtex（未检测到 \\bibliography 引用）"
fi

echo "[3/4] pdflatex (pass 2, 解析引用)"
pdflatex -interaction=nonstopmode -halt-on-error "${MAIN}.tex" >/dev/null

echo "[4/4] pdflatex (pass 3, 稳定交叉引用/目录)"
pdflatex -interaction=nonstopmode -halt-on-error "${MAIN}.tex" >/dev/null

echo ""
echo "✅ 编译完成: ${TEX_DIR}/${MAIN}.pdf"
