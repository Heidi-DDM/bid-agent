-- 首次初始化时创建扩展（迁移 0004 的 vector 类型依赖；见 docs/11 §6）
CREATE EXTENSION IF NOT EXISTS vector;
