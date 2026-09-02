#!/usr/bin/env bash
# R019/F019 §6 验收③ + R025/F025 §7：验证数据层隔离——public_data 角色无法读取
# enterprise/admission/audit/knowledge 明细；knowledge_reader 仅可读 RAG 层。
# 前置：已执行 scripts/setup_rbac.sql
# 用法：bash scripts/verify_rbac.sh [数据库名] [管理员角色]
#   - 数据库名默认 bid_agent
#   - 管理员角色默认当前系统用户（Homebrew PostgreSQL 的超级用户，socket trust 连接）
# 说明：验证角色为 NOLOGIN，不能直接 psql -U 登录；必须以管理员连接后 SET ROLE 切换验证。
set -euo pipefail

DB="${1:-bid_agent}"
ADMIN="${2:-$(whoami)}"
FAIL=0

# 以管理员连接，SET ROLE 到目标角色后执行 SQL；NOLOGIN 角色只能经 SET ROLE 验证
run_as() {
  local role="$1"
  local sql="$2"
  psql -h /tmp -U "$ADMIN" -d "$DB" -tAc "SET ROLE $role; $sql" >/dev/null 2>&1
}

expect_denied() {
  local desc="$1"
  local role="$2"
  local sql="$3"
  if run_as "$role" "$sql"; then
    echo "[FAIL] ${desc}：${role} 竟然可以访问（应被拒绝）"
    FAIL=1
  else
    echo "[ok] ${desc}：${role} 被拒绝"
  fi
}

expect_allowed() {
  local desc="$1"
  local role="$2"
  local sql="$3"
  if run_as "$role" "$sql"; then
    echo "[ok] ${desc}：${role} 可访问"
  else
    echo "[FAIL] ${desc}：${role} 被拒绝（应允许）"
    FAIL=1
  fi
}

echo "== 正向：public_data_reader 可读公开层 =="
expect_allowed "public_data 材料" public_data_reader "SELECT 1 FROM public_data.materials LIMIT 1"
expect_allowed "public_data 项目" public_data_reader "SELECT 1 FROM public_data.projects LIMIT 1"

echo "== 反向：public_data_reader 不得读其他层 =="
expect_denied "企业资质明细" public_data_reader "SELECT 1 FROM enterprise_data.qualifications LIMIT 1"
expect_denied "私有证据元数据" public_data_reader "SELECT 1 FROM enterprise_data.evidence_files LIMIT 1"
expect_denied "审批记录" public_data_reader "SELECT 1 FROM admission_data.approvals LIMIT 1"
expect_denied "审计事件" public_data_reader "SELECT 1 FROM audit_data.audit_events LIMIT 1"

echo "== 反向：enterprise_data_reader 不得读审计 =="
expect_denied "审计事件" enterprise_data_reader "SELECT 1 FROM audit_data.audit_events LIMIT 1"

echo "== 反向：audit_reader 只读审计，不得读企业明细 =="
expect_allowed "审计事件" audit_reader "SELECT 1 FROM audit_data.audit_events LIMIT 1"
expect_denied "企业资质明细" audit_reader "SELECT 1 FROM enterprise_data.qualifications LIMIT 1"

echo "== RAG 层：knowledge_reader 可读，public_data_reader 不得读（L3 企业证据泄漏防护） =="
expect_allowed "RAG 分片" knowledge_reader "SELECT 1 FROM knowledge_data.knowledge_chunks LIMIT 1"
expect_allowed "检索运行快照" knowledge_reader "SELECT 1 FROM knowledge_data.retrieval_runs LIMIT 1"
expect_denied "RAG 分片" public_data_reader "SELECT 1 FROM knowledge_data.knowledge_chunks LIMIT 1"
expect_denied "RAG 向量" public_data_reader "SELECT 1 FROM knowledge_data.knowledge_embeddings LIMIT 1"
expect_denied "RAG 分片" audit_reader "SELECT 1 FROM knowledge_data.knowledge_chunks LIMIT 1"

if [ "$FAIL" -eq 0 ]; then
  echo "== 全部通过：数据层隔离生效（F019 §6 验收③ / R025 验收①） =="
else
  echo "== 存在失败项，请检查 setup_rbac.sql 是否执行、角色是否被授予多余权限 =="
  exit 1
fi