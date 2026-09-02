-- R019/F019 §2/§6 验收③ + R025/F025 §7：数据库逻辑分层 RBAC
-- 五类只读角色只授予所需 schema 权限；匹配服务通过受控视图读取企业资料。
-- 用法（作为 postgres 超级用户执行，需先跑 alembic upgrade head）：
--   psql -h /tmp -U <管理员> -d bid_agent -f scripts/setup_rbac.sql
-- 注意：本脚本创建只读角色；应用账号 bid_agent 保持全量访问（R020 再收紧到最小权限）。
-- 幂等：角色已存在则跳过创建，GRANT 重复执行安全。

-- 1) public_data 读取角色：可读公开材料/项目/溯源，不可读企业明细
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'public_data_reader') THEN
    CREATE ROLE public_data_reader NOLOGIN;
  END IF;
END$$;
GRANT USAGE ON SCHEMA public_data TO public_data_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public_data TO public_data_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public_data GRANT SELECT ON TABLES TO public_data_reader;

-- 2) enterprise_data 读取角色：仅数据管理员/匹配服务（受控视图）使用
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'enterprise_data_reader') THEN
    CREATE ROLE enterprise_data_reader NOLOGIN;
  END IF;
END$$;
GRANT USAGE ON SCHEMA enterprise_data TO enterprise_data_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA enterprise_data TO enterprise_data_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA enterprise_data GRANT SELECT ON TABLES TO enterprise_data_reader;

-- 3) 审批层角色：仅审批人/授权角色可见（F003 权限矩阵 approver_only）
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'admission_approver') THEN
    CREATE ROLE admission_approver NOLOGIN;
  END IF;
END$$;
GRANT USAGE ON SCHEMA admission_data TO admission_approver;
GRANT SELECT ON ALL TABLES IN SCHEMA admission_data TO admission_approver;
ALTER DEFAULT PRIVILEGES IN SCHEMA admission_data GRANT SELECT ON TABLES TO admission_approver;

-- 4) 审计只读角色：审计事件仅追加，只授权 SELECT（F019 §3 audit_events / F009 §9）
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'audit_reader') THEN
    CREATE ROLE audit_reader NOLOGIN;
  END IF;
END$$;
GRANT USAGE ON SCHEMA audit_data TO audit_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA audit_data TO audit_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA audit_data GRANT SELECT ON TABLES TO audit_reader;

-- 5) knowledge_data 读取角色：RAG 检索/核验/回放服务与数据管理员（F025 §7）
--    分片与向量为候选证据入口（L3 含企业证据），只授 SELECT；写入由应用账号执行。
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'knowledge_reader') THEN
    CREATE ROLE knowledge_reader NOLOGIN;
  END IF;
END$$;
GRANT USAGE ON SCHEMA knowledge_data TO knowledge_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA knowledge_data TO knowledge_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge_data GRANT SELECT ON TABLES TO knowledge_reader;

-- 验证口径（F019 §6 验收③ / R025 验收①）：
-- public_data_reader 不得读取 enterprise_data.* / admission_data.approvals / audit_data.* / knowledge_data.*
-- 具体验证命令见 scripts/verify_rbac.sh