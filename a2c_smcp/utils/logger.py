# filename: logger.py
# @Time    : 2025/8/15 14:39
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
a2c_sMCP 日志模块
Logger module for a2c_sMCP project.

通过环境变量控制日志：
- A2C_SMCP_LOG_LEVEL: 控制日志等级（debug, info, warning, error, critical）
- A2C_SMCP_LOG_SILENT: 设为 1/true/yes 时禁用所有日志输出
- A2C_SMCP_LOG_FILE: 设置日志文件路径，启用文件输出
"""

import logging
import os
import sys
from pathlib import Path
from typing import Any

# 直接从环境变量获取配置
LOG_LEVEL = os.environ.get("A2C_SMCP_LOG_LEVEL", "info").lower()
SILENT_MODE = os.environ.get("A2C_SMCP_LOG_SILENT", "0").lower() in ("1", "true", "yes")
LOG_FILE = os.environ.get("A2C_SMCP_LOG_FILE")

# 创建全局 logger 实例
logger = logging.getLogger("a2c_smcp")
logger.propagate = False  # 防止日志向上传播到根logger

if not SILENT_MODE:
    # 配置日志级别
    level_map = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }
    logger.setLevel(level_map.get(LOG_LEVEL, logging.INFO))

    # 创建通用日志格式
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    # 配置控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 配置文件输出（如果指定了日志文件）
    if LOG_FILE:
        try:
            # 确保日志目录存在
            log_path = Path(LOG_FILE)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            file_handler = logging.FileHandler(LOG_FILE)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception as e:
            logger.error(f"创建日志文件失败: {str(e)}. 回退到仅控制台输出")

    logger.info("日志系统已初始化 - 级别: %s, 文件: %s", LOG_LEVEL.upper(), LOG_FILE if LOG_FILE else "N/A")

else:
    # 静默模式：禁用日志
    logger.disabled = True
    logger.addHandler(logging.NullHandler())  # 防止无handler的警告


def get_logger(module: str = "") -> logging.Logger:
    """获取模块子 logger / Get a child logger for the given module.

    子 logger 自动继承父 logger 的 handlers、level 和 formatter。
    日志格式中 %(name)s 会自动显示为 a2c_smcp.agent / a2c_smcp.server 等。

    Args:
        module: 子模块名 (如 "agent", "server", "computer")。
                空字符串返回根 a2c_smcp logger。
    """
    if module:
        return logging.getLogger(f"a2c_smcp.{module}")
    return logger


class ContextLogger(logging.LoggerAdapter):
    """在日志消息前附加结构化上下文标签。

    用于在关键代码路径中注入瞬态上下文（如 SID、computer 名称、req_id），
    方便多实例场景下的问题定位。

    Usage::

        ctx = ContextLogger(logger, {"sid": sid, "computer": name})
        ctx.info("Tool call started")
        # Output: [sid=abc123] [computer=my-pc] Tool call started
    """

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        if self.extra:
            ctx = " ".join(f"[{k}={v}]" for k, v in self.extra.items() if v is not None)
            if ctx:
                msg = f"{ctx} {msg}"
        return msg, kwargs


def truncate(value: Any, max_len: int = 500) -> str:
    """截断大文本用于安全日志输出 / Truncate large text for safe logging.

    保留首尾各 max_len//2 字符，中间标注总长度。
    适用于 LLM 项目中可能包含大量文本的工具参数和返回值。

    Args:
        value: 待截断的值，会先转为 str。
        max_len: 最大允许长度，默认 500。
    """
    s = str(value)
    if len(s) <= max_len:
        return s
    half = max_len // 2
    return f"{s[:half]}...({len(s)} chars)...{s[-half:]}"
