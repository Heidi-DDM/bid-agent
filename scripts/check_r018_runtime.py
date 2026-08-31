#!/usr/bin/env python3
"""R018/F018：无依赖环境下验证纯逻辑模块与整体可编译性。

用法：
  python scripts/check_r018_runtime.py
等价 CI 命令（quality.yml）：
  python -m py_compile runtime/core/*.py runtime/db/*.py runtime/*.py
"""
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [
    "runtime/core/config.py",
    "runtime/core/logging_utils.py",
    "runtime/core/db.py",
    "runtime/core/jobs.py",
    "runtime/core/objects.py",
    "runtime/core/model.py",
    "runtime/__init__.py",
    "runtime/core/__init__.py",
    "runtime/db/__init__.py",
    "runtime/db/alembic/env.py",
    "runtime/db/alembic/versions/0001_r018_analysis_jobs.py",
    "runtime/tests/__init__.py",
    "runtime/tests/test_jobs_state_machine.py",
    "runtime/tests/test_objects.py",
    "runtime/tests/test_logging_and_model.py",
]


def main() -> int:
    failed = []
    for rel in TARGETS:
        path = ROOT / rel
        try:
            py_compile.compile(str(path), doraise=True)
            print(f"OK   {rel}")
        except py_compile.PyCompileError as exc:
            failed.append(rel)
            print(f"FAIL {rel}: {exc}")
    if failed:
        print(f"\n{len(failed)} 个文件编译失败: {failed}")
        return 1
    print(f"\n全部 {len(TARGETS)} 个文件编译通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())