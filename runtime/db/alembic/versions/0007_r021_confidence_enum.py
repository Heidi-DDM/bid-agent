"""R021 修复：field_traces.confidence 列型 float → varchar(16)

背景（2026-09-02 真库 confirm 500 定位）：
- F005 §4.3 / §4.1 冻结口径：confidence 为 enum 字符串
  （confirmed=官方 / high=第三方 / low=转载=medium…… 取值域
  confirmed/high/medium/low），模型与解析器均输出该枚举
  （runtime/parsing/extractor.py CONFIDENCE_HIGH/MEDIUM/LOW）。
- 0002_r019_data_persistence 建表时误用 sa.Float()（double precision），
  与规格冲突。SQLite（沙盒）不校验列类型 → R021-3 单测全绿；
  真库 parse_service.confirm_main_card_fields 写 'high' 报
  DataError "invalid input syntax for type double precision"（真库唯一真相源）。
- 修复方向 A：本迁移把列改为 varchar(16)（对齐 F005 枚举口径），
  存量数值行（若有）经 USING ::text 兼容转换；方向 B（枚举→数值映射）
  与 F005 文档口径冲突，弃用。

Revision ID: 0007_r021_confidence_enum
Revises: 0006_r021_parse_candidates
Create Date: 2026-09-02
"""
from alembic import op

revision = "0007_r021_confidence_enum"
down_revision = "0006_r021_parse_candidates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # float8 → varchar 无隐式 cast，须显式 USING；NULL 行经 ::text 仍为 NULL
    op.execute(
        "ALTER TABLE public_data.field_traces "
        "ALTER COLUMN confidence TYPE VARCHAR(16) "
        "USING (confidence::text)"
    )


def downgrade() -> None:
    # 回退 double precision；列内若已有枚举字符串（high/medium/low/confirmed）
    # 无法数值化，downgrade 前须先清空或映射该列（开发期可整表重建）
    op.execute(
        "ALTER TABLE public_data.field_traces "
        "ALTER COLUMN confidence TYPE DOUBLE PRECISION "
        "USING (confidence::double precision)"
    )
