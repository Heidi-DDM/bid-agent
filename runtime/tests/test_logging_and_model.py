# F018 §5 日志脱敏 + §6 模型内网检查测试（纯逻辑）
import logging

import pytest

from runtime.core import model
from runtime.core.logging_utils import RecordSensitiveFilter, mask_sensitive


class TestMaskSensitive:
    def test_id_card_masked(self):
        assert mask_sensitive("身份证 130123199001011234 结尾") == "身份证 [REDACTED] 结尾"

    def test_mobile_masked(self):
        assert mask_sensitive("手机 13800138000 联系") == "手机 [REDACTED] 联系"

    def test_both_masked(self):
        out = mask_sensitive("130123199001011234 13800138000")
        assert "[REDACTED]" in out
        assert "13800138000" not in out

    def test_normal_text_unchanged(self):
        assert mask_sensitive("正常日志：任务完成 job=1") == "正常日志：任务完成 job=1"


class TestRecordFilter:
    def test_filter_masks_record(self):
        logger = logging.getLogger("test.mask")
        logger.addFilter(RecordSensitiveFilter())
        logger.handlers = []
        record = logger.makeRecord(
            logger.name, logging.INFO, __file__, 1, "身份证 130123199001011234", (), None
        )
        logger.filters[0].filter(record)
        assert "130123199001011234" not in record.msg
        assert "[REDACTED]" in record.msg

    def test_filter_args_tuple_masked(self):
        logger = logging.getLogger("test.mask.args")
        logger.addFilter(RecordSensitiveFilter())
        record = logger.makeRecord(
            logger.name, logging.INFO, __file__, 1, "user %s", ("13800138000",), None
        )
        logger.filters[0].filter(record)
        assert record.args == ("[REDACTED]",)


class TestModelAllowed:
    def test_localhost_allowed(self):
        assert model.check_model_allowed("http://127.0.0.1:8001") is True
        assert model.check_model_allowed("http://localhost:8001") is True

    def test_private_ip_allowed(self):
        assert model.check_model_allowed("http://192.168.1.10:8001") is True
        assert model.check_model_allowed("http://10.0.0.5:8001") is True

    def test_public_domain_denied(self):
        assert model.check_model_allowed("http://api.aliyuncs.com/v1") is False

    def test_unset_denied(self):
        assert model.check_model_allowed(None) is False

    def test_health_when_not_allowed(self):
        result = model.health(base_url=None)
        assert result.ok is False
        assert "内网" in (result.error or "")