# runtime/tests 全局夹具：开发期身份回退开关
# R024/F020 §5.1：业务路由身份来源 = Bearer token（正式）；测试沿用 X-Role/X-Actor 头
# 需要 AUTH_DEV_HEADERS=true（dev 回退）。此处 setdefault 双保险（若 runtime/.env 已加载
# 同值则无副作用）；正式认证专项用例自行 monkeypatch 置 false 验证 fail-closed。
import os

os.environ.setdefault("AUTH_DEV_HEADERS", "true")
