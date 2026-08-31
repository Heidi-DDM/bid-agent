# F018 §5：日志只记录元数据与错误摘要
# 禁止记录：完整招标原文、身份证号、手机号。
import logging
import re

# 身份证号（18 位，末位可为 X）
_ID_CARD = re.compile(r"\b\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx]\b")
# 手机号（11 位）
_MOBILE = re.compile(r"\b1[3-9]\d{9}\b")
_MASK = "[REDACTED]"


def mask_sensitive(text: str) -> str:
    """把日志文本中的身份证号与手机号替换为占位符。"""
    text = _ID_CARD.sub(_MASK, text)
    return _MOBILE.sub(_MASK, text)


class RecordSensitiveFilter(logging.Filter):
    """对 record 的格式化消息做脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask_sensitive(record.msg)
        # args 中可能携带敏感信息
        if record.args:
            try:
                args = record.args
                if isinstance(args, dict):
                    args = {k: mask_sensitive(str(v)) for k, v in args.items()}
                elif isinstance(args, tuple):
                    args = tuple(mask_sensitive(str(a)) if not isinstance(a, BaseException) else a for a in args)
                record.args = args
            except Exception:
                pass  # 脱敏失败不阻塞日志
        return True