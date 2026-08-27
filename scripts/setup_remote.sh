#!/usr/bin/env bash
# V-001 一键建立 GitHub 远端（需在系统终端运行，Cursor 沙箱内无外网）
# 用法：bash scripts/setup_remote.sh
# 说明：GitHub 仓库名不支持中文，统一使用 ASCII 名 bid-agent
set -euo pipefail

REPO_DIR="/Users/heidi/Desktop/bid-agent 项目"
REPO_NAME="bid-agent"          # GitHub 仓库名（ASCII，中文名会被 GitHub 拒绝）
GH_ACCOUNT="Heidi-DDM"               # 如账号不同，改这里或改用手动方式

cd "$REPO_DIR"

echo "==> [1/4] 检查 gh 登录状态与 workflow scope"
if ! gh auth status -h github.com >/dev/null 2>&1; then
  echo "    gh 未登录或 token 失效。"
  echo "    将启动交互式登录：选择 GitHub.com → HTTPS → Login with a web browser（浏览器授权，无需输入密码）"
  echo "    如果你已有 Personal Access Token：选 Paste an authentication token 后粘贴（token 不要发给任何人/任何 AI）"
  gh auth login -h github.com
fi
gh auth status -h github.com
# GitHub 要求 token 带 workflow scope 才能创建/修改 .github/workflows/*（否则推送被拒）
if ! gh auth status -h github.com 2>&1 | grep -q "workflow"; then
  echo "    token 缺少 workflow scope，启动授权追加（浏览器操作，无需密码）"
  gh auth refresh -h github.com -s workflow
fi

echo "==> [2/4] 推送前安全检查（白名单核对，禁止提交项必须被忽略）"
git status --short | grep -E "企业资料台账20260821|验证受限材料|\.(xlsx|xls|pdf|docx|png)$" \
  && { echo "    !! 发现敏感/禁止文件将被提交，已中止。处理后重新运行。"; exit 1; } \
  || echo "    未发现敏感文件（git status 层面）"
for f in "验证受限材料/博野五标段/real_evidence_boye.json" "企业资料台账20260821"; do
  if git check-ignore "$f" >/dev/null 2>&1; then
    echo "    已忽略：$f"
  else
    echo "    !! 未被忽略：$f —— 请检查 .gitignore，已中止。"; exit 1
  fi
done

echo "==> [3/4] 创建私有仓库并推送"
if git remote -v | grep -q origin; then
  echo "    已存在 origin：$(git remote get-url origin)，跳过创建，直接推送"
else
  gh repo create "$REPO_NAME" --private --source=. --remote=origin
fi
git push -u origin main

echo "==> [4/4] 完成确认"
git remote -v
echo "    仓库地址：https://github.com/$GH_ACCOUNT/$REPO_NAME"
echo "    CI 将在推送后自动运行（.github/workflows/quality.yml），可在仓库 Actions 页查看"
echo "    完成后请把 CI run 链接回填 docs/验证/R009-匹配引擎与黄金样本验证清单.md 的 V-001 行"