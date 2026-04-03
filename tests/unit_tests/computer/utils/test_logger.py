# filename: test_logger.py
# @Time    : 2025/8/15 19:36
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
import importlib
import logging
import sys
from io import StringIO

import pytest

# 待测试模块名
MODULE_NAME = "a2c_smcp.utils.logger"


@pytest.fixture(autouse=True)
def reset_logging():
    """每个测试后重置 logging 状态"""
    yield
    # 重置 logging 模块
    logging.root.handlers = []
    logging.root.setLevel(logging.NOTSET)
    logging.Logger.manager.loggerDict.clear()


def reload_module(monkeypatch, env_vars=None):
    """重新加载指定模块，设置环境变量"""
    if env_vars:
        for key, value in env_vars.items():
            monkeypatch.setenv(key, value)

    # 移除模块缓存以便重新加载
    if MODULE_NAME in sys.modules:
        del sys.modules[MODULE_NAME]

    # 捕获标准输出以验证日志
    captured_output = StringIO()
    monkeypatch.setattr(sys, "stdout", captured_output)

    # 重新加载模块
    module = importlib.import_module(MODULE_NAME)
    return module, captured_output


def test_default_config(monkeypatch):
    """测试默认配置"""
    module, captured_output = reload_module(monkeypatch, env_vars={"A2C_SMCP_LOG_LEVEL": "debug"})

    # 验证日志级别为 DEBUG
    assert module.logger.level == logging.DEBUG

    # 验证日志输出
    module.logger.info("Test info message")
    module.logger.debug("Test debug message (should not appear)")

    output = captured_output.getvalue()
    assert "Test info message" in output
    assert "Test debug message (should not appear)" in output
    assert "日志系统已初始化" in output


def test_silent_mode(monkeypatch):
    """测试静默模式"""
    module, captured_output = reload_module(monkeypatch, {"A2C_SMCP_LOG_SILENT": "true"})

    # 验证日志被禁用
    assert module.logger.disabled

    # 验证日志输出被禁用
    module.logger.info("This should not appear")
    module.logger.error("This should not appear either")

    output = captured_output.getvalue()
    assert "This should not appear" not in output
    assert output == ""  # 确保静默模式完全没有输出


def test_log_levels(monkeypatch):
    """测试所有日志级别"""
    test_cases = [
        ("debug", logging.DEBUG),
        ("info", logging.INFO),
        ("warning", logging.WARNING),
        ("error", logging.ERROR),
        ("critical", logging.CRITICAL),
        ("invalid", logging.INFO),  # 测试无效值默认为 INFO
    ]

    for level_str, expected_level in test_cases:
        module, captured_output = reload_module(monkeypatch, {"A2C_SMCP_LOG_LEVEL": level_str})
        logger = module.logger

        # 清除输出缓冲区
        captured_output.truncate(0)
        captured_output.seek(0)

        # 发送不同级别的测试日志
        logger.debug(f"{level_str} debug test")
        logger.info(f"{level_str} info test")
        logger.warning(f"{level_str} warning test")
        logger.error(f"{level_str} error test")
        logger.critical(f"{level_str} critical test")

        output = captured_output.getvalue()

        # 验证低于当前级别的日志不会被输出
        if expected_level > logging.DEBUG:
            assert f"{level_str} debug test" not in output
        if expected_level > logging.INFO:
            assert f"{level_str} info test" not in output
        if expected_level > logging.WARNING:
            assert f"{level_str} warning test" not in output
        if expected_level > logging.ERROR:
            assert f"{level_str} error test" not in output

        # 验证等于或高于当前级别的日志会被输出
        if expected_level <= logging.DEBUG:
            assert f"{level_str} debug test" in output
        if expected_level <= logging.INFO:
            assert f"{level_str} info test" in output
        if expected_level <= logging.WARNING:
            assert f"{level_str} warning test" in output
        if expected_level <= logging.ERROR:
            assert f"{level_str} error test" in output
        if expected_level <= logging.CRITICAL:
            assert f"{level_str} critical test" in output


def test_log_output_to_console(monkeypatch):
    """测试控制台日志输出"""
    module, captured_output = reload_module(monkeypatch)

    # 验证日志消息格式
    module.logger.warning("Test warning message")
    output = captured_output.getvalue()
    assert "WARNING" in output
    assert "Test warning message" in output
    assert "- a2c_smcp - WARNING - Test warning message" in output


def test_log_output_to_file(monkeypatch, tmp_path):
    """测试文件日志输出"""
    log_file = tmp_path / "test.log"

    module, captured_output = reload_module(monkeypatch, {"A2C_SMCP_LOG_FILE": str(log_file), "A2C_SMCP_LOG_LEVEL": "debug"})

    # 验证日志文件创建
    assert log_file.exists()

    # 写入日志
    test_message = "File output test message"
    module.logger.info(test_message)
    module.logger.debug("Debug message")

    # 验证文件内容
    file_content = log_file.read_text()
    assert test_message in file_content
    assert "Debug message" in file_content

    # 验证控制台仍有输出
    output = captured_output.getvalue()
    assert test_message in output


def test_log_output_to_file_directory_creation(monkeypatch, tmp_path):
    """测试日志文件目录自动创建"""
    log_file = tmp_path / "non_existent_dir" / "test.log"
    assert not log_file.parent.exists()  # 确保目录不存在

    module, _ = reload_module(monkeypatch, {"A2C_SMCP_LOG_FILE": str(log_file)})

    # 验证目录被创建
    assert log_file.parent.exists()

    # 写入日志
    test_message = "Directory creation test"
    module.logger.info(test_message)

    # 验证日志文件创建成功
    assert log_file.exists()
    assert test_message in log_file.read_text()


def test_logger_independence(monkeypatch):
    """验证我们配置的logger不影响其他logger"""
    # 配置我们的logger为静默
    module, _ = reload_module(monkeypatch, {"A2C_SMCP_LOG_SILENT": "1"})

    # 创建另一个独立的logger
    other_logger = logging.getLogger("other_logger")
    other_logger.setLevel(logging.INFO)

    # 捕获其他logger的输出
    captured_output = StringIO()
    handler = logging.StreamHandler(captured_output)
    other_logger.addHandler(handler)

    # 记录消息
    other_logger.info("Independent logger test")

    # 验证我们的logger处于静默状态
    module.logger.info("This should be silent")
    assert captured_output.getvalue() == "Independent logger test\n"

    # 验证我们logger的静默设置不影响其他logger
    assert "Independent logger test" in captured_output.getvalue()
    assert "This should be silent" not in captured_output.getvalue()


def test_log_format_customization(monkeypatch):
    """测试日志格式（虽然我们不能通过env改变格式，但检查默认格式）"""
    module, captured_output = reload_module(monkeypatch)

    module.logger.info("Format test")
    output = captured_output.getvalue()

    # 验证日志格式组件
    assert " - a2c_smcp - " in output
    assert " - Format test" in output

    # 验证时间戳格式
    from datetime import datetime

    timestamp = output.split(" - ")[0]
    print(datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S,%f"))


def test_multiple_handlers(monkeypatch, tmp_path):
    """测试同时输出到控制台和文件"""
    log_file = tmp_path / "combined.log"

    module, captured_output = reload_module(monkeypatch, {"A2C_SMCP_LOG_FILE": str(log_file), "A2C_SMCP_LOG_LEVEL": "debug"})

    # 验证有两个handler
    assert len(module.logger.handlers) == 2

    # 写入日志
    test_message = "Multiple handlers test"
    module.logger.info(test_message)

    # 验证控制台输出
    output = captured_output.getvalue()
    assert test_message in output

    # 验证文件输出
    file_content = log_file.read_text()
    assert test_message in file_content


def test_silent_mode_with_file(monkeypatch, tmp_path):
    """测试静默模式下不输出到文件"""
    log_file = tmp_path / "silent.log"

    module, captured_output = reload_module(monkeypatch, {"A2C_SMCP_LOG_SILENT": "yes", "A2C_SMCP_LOG_FILE": str(log_file)})

    # 验证日志被禁用
    assert module.logger.disabled

    # 尝试写入日志
    module.logger.info("Should not appear")

    # 验证控制台无输出
    assert captured_output.getvalue() == ""

    # 验证无日志文件创建
    assert not log_file.exists()


# ==================== get_logger 子 Logger 测试 ====================


def test_get_logger_child(monkeypatch):
    """子 logger 名称正确且输出包含模块标识"""
    module, captured_output = reload_module(monkeypatch)

    child = module.get_logger("agent")
    assert child.name == "a2c_smcp.agent"

    child.warning("agent test message")
    output = captured_output.getvalue()
    assert "- a2c_smcp.agent - WARNING - agent test message" in output


def test_child_logger_inherits_config(monkeypatch):
    """子 logger 继承父 logger 的 level"""
    module, captured_output = reload_module(monkeypatch, {"A2C_SMCP_LOG_LEVEL": "warning"})

    child = module.get_logger("server")
    # 子 logger 未设置 level，应继承父 logger 的 WARNING
    child.info("should not appear")
    child.warning("should appear")

    output = captured_output.getvalue()
    assert "should not appear" not in output or output.count("should not appear") == 0
    assert "should appear" in output


def test_get_logger_empty_returns_root(monkeypatch):
    """空字符串返回根 logger"""
    module, _ = reload_module(monkeypatch)
    root = module.get_logger("")
    assert root.name == "a2c_smcp"
    assert root is module.logger


# ==================== ContextLogger 测试 ====================


def test_context_logger_adapter(monkeypatch):
    """ContextLogger 输出包含 [key=value] 标签"""
    module, captured_output = reload_module(monkeypatch)

    ctx = module.ContextLogger(module.logger, {"sid": "abc123", "computer": "my-pc"})
    ctx.warning("test message")

    output = captured_output.getvalue()
    assert "[sid=abc123]" in output
    assert "[computer=my-pc]" in output
    assert "test message" in output


def test_context_logger_none_values(monkeypatch):
    """ContextLogger 过滤 None 值"""
    module, captured_output = reload_module(monkeypatch)

    ctx = module.ContextLogger(module.logger, {"sid": "abc", "computer": None})
    ctx.warning("test")

    output = captured_output.getvalue()
    assert "[sid=abc]" in output
    assert "computer" not in output


def test_context_logger_empty_extra(monkeypatch):
    """ContextLogger 无 extra 时不附加标签"""
    module, captured_output = reload_module(monkeypatch)

    ctx = module.ContextLogger(module.logger, {})
    ctx.warning("plain message")

    output = captured_output.getvalue()
    assert "plain message" in output
    assert "[" not in output.split("plain message")[0].split("WARNING")[-1]


# ==================== truncate 测试 ====================


def test_truncate_short_string(monkeypatch):
    """短于 max_len 的字符串原样返回"""
    module, _ = reload_module(monkeypatch)

    result = module.truncate("hello", max_len=500)
    assert result == "hello"


def test_truncate_exact_length(monkeypatch):
    """等于 max_len 的字符串原样返回"""
    module, _ = reload_module(monkeypatch)

    s = "x" * 500
    result = module.truncate(s, max_len=500)
    assert result == s


def test_truncate_long_string(monkeypatch):
    """长字符串被截断，保留首尾并标注总长度"""
    module, _ = reload_module(monkeypatch)

    s = "A" * 200 + "B" * 400 + "C" * 200
    result = module.truncate(s, max_len=100)

    assert len(result) < len(s)
    assert result.startswith("A" * 50)
    assert result.endswith("C" * 50)
    assert "(800 chars)" in result


def test_truncate_non_string(monkeypatch):
    """非字符串值会先转为 str"""
    module, _ = reload_module(monkeypatch)

    result = module.truncate(12345)
    assert result == "12345"
