#!/usr/bin/env bash
# 文件名: clean_install_smoke.sh / File name: clean_install_smoke.sh
# 作者: JQQ / Author: JQQ
# 创建日期: 2026/09/29 / Created: 2026/09/29
# 描述: 干净安装冒烟——把**构建出的 wheel** 装进空 venv（只装制品自己声明的依赖），
#       验证制品在两种安装面上都可用。用于 tests.yml 与 publish.yml **单点复用**
#       （两处逐字复制会静默漂移，使「上传前」那道闸门失效——#224 的逃逸通道）。
#       Description: Clean-install smoke — install the BUILT WHEEL into an empty venv (declared deps
#       only) and verify the artifact on two install surfaces. Shared by tests.yml and publish.yml so
#       the two gates cannot drift apart.
#
# 用法 / Usage:
#   scripts/clean_install_smoke.sh <wheel-path> [cli|none] [venv-dir]
#     wheel-path  wheel 文件路径（只允许一个，多个即报错——见下）
#     mode        cli（默认）= 文档安装面：装 cli extra 并跑 `a2c-computer --help`；
#                 none = 纯 core 导入契约（#226）：不装任何 extra，只验证 import 与惰性错误
#     venv-dir    临时 venv 路径，默认 /tmp/a2c-smoke
#
# 背景 / Why: #224 —— `pyyaml` 从未声明为依赖，但 dev/CI 的 `uv sync --group dev` 里
#   `poethepoet` 传递带入 pyyaml，于是「源码 import 未声明的包」在 CI **永远复现不了**。
#   本脚本构造真实用户的安装面（只有制品声明的依赖），把这类坏包挡在合入/上传之前。
#
# #226 / Mode `none`: 纯 core 安装下 `import a2c_smcp.computer` 必须成功，且交互路径
#   （console / ainput_prompt）抛携带 `pip install "a2c-smcp[cli]"` 的 ImportError。
#   `a2c-computer` 入口本身需要 typer（cli extra），故 none 模式不跑它。
set -euo pipefail

WHEEL_PATH="${1:?用法: clean_install_smoke.sh <wheel-path> [cli|none] [venv-dir]}"
# 注意用 ${2-cli} 而非 ${2:-cli}：后者对**空串**也取默认值，会让下面的空值校验变成死条件
# （传 "" 时静默装 [cli]，与「显式拒绝」的语义不符）。${2-cli} 仅在未传参时取默认。
MODE="${2-cli}"
VENV_DIR="${3:-/tmp/a2c-smoke}"

if [[ -z "${MODE}" ]]; then
  echo "::error::模式不得为空——传 cli 或 none" >&2
  exit 1
fi

case "${MODE}" in
  cli)
    TARGET="${WHEEL_PATH}[cli]"
    RUN_CLI=1
    ;;
  none)
    TARGET="${WHEEL_PATH}"
    RUN_CLI=0
    ;;
  *)
    echo "::error::不支持的模式 '${MODE}'——只接受 cli 或 none" >&2
    exit 1
    ;;
esac

# rm -rf 的目标必须是个安全的自定义目录：挡掉空值、根目录与显然会被误伤的路径。
# Guard the rm -rf target: reject empty / root / the cwd itself.
case "${VENV_DIR}" in
  ""|"/"|"."|".."|"${HOME}"|"${PWD}")
    echo "::error::venv 目录不安全，拒绝 rm -rf: '${VENV_DIR}'" >&2
    exit 1
    ;;
esac
if [[ "${VENV_DIR}" != /* ]]; then
  echo "::error::venv 目录须为绝对路径（收到 '${VENV_DIR}'）—— 断言按路径归属判定，相对路径会误报" >&2
  exit 1
fi

if [[ ! -f "${WHEEL_PATH}" ]]; then
  echo "::error::wheel 不存在: ${WHEEL_PATH}" >&2
  exit 1
fi

# 只允许一个 wheel：多 wheel 时 "a.whl\nb.whl[cli]" 会拼成含换行的单参数，
# 安装报出与真实原因无关的错误（假红）。单数断言优于 `ls | head -1` 的静默取一。
# Exactly one wheel: otherwise the path list collapses into a single newline-laden argument and
# `uv pip install` fails with an unrelated error (a false RED).
WHEEL_COUNT=$(find "$(dirname "${WHEEL_PATH}")" -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')
if [[ "${WHEEL_COUNT}" != "1" ]]; then
  echo "::error::期望恰好 1 个 wheel，实际 ${WHEEL_COUNT} 个：$(ls "$(dirname "${WHEEL_PATH}")"/*.whl 2>/dev/null | tr '\n' ' ')" >&2
  exit 1
fi

rm -rf "${VENV_DIR}"
uv venv "${VENV_DIR}"
echo "Smoke-testing ${TARGET} (mode=${MODE})"
uv pip install --python "${VENV_DIR}/bin/python" "${TARGET}"

# ⚠️ 必须在**中性 cwd** 下跑：`python -c` 会把 cwd 放进 sys.path[0]，若在仓库根跑，导入到的是
#    仓库源码而非被测 wheel —— 那样即使制品是坏的，本脚本也会绿。下面的 __file__ 断言把这点钉死。
# ⚠️ Must run from a neutral cwd: `python -c` puts cwd on sys.path[0], so running at the repo root
#    would import the source tree instead of the artifact — the smoke would pass even for a broken
#    wheel. The __file__ assertion below pins this down.
cd /tmp

if [[ "${RUN_CLI}" == "1" ]]; then
  "${VENV_DIR}/bin/a2c-computer" --help > /dev/null
  echo "a2c-computer --help OK"
fi

"${VENV_DIR}/bin/python" - "${VENV_DIR}" "${MODE}" <<'PY'
import asyncio
import importlib.util
import sys
from pathlib import Path

import a2c_smcp
import a2c_smcp.computer

# 按「路径归属」判定而非子串匹配：软链（macOS /tmp → /private/tmp）与相对路径都会让子串比法误报。
# 归属比较前先 resolve()，对软链与相对路径都成立。
# Ownership check (not substring matching): symlinks (macOS /tmp → /private/tmp) and relative paths
# make substring comparison produce false alarms.
venv_dir = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
for mod in (a2c_smcp, a2c_smcp.computer):
    path = Path(mod.__file__ or "").resolve()
    assert path.is_relative_to(venv_dir), f"smoke 导入的是 {path}，不是被测制品 {venv_dir} / not the artifact"
    print(f"  {mod.__name__} -> {path}")

from a2c_smcp.computer.inputs.cli_io import ainput_prompt
from a2c_smcp.computer.utils import console as console_util


def _expect_actionable(label: str, call) -> None:
    try:
        call()
    except ImportError as exc:
        text = str(exc)
        assert 'a2c-smcp[cli]' in text, f"{label}: hint missing from {text!r}"
        assert isinstance(exc.__cause__, ModuleNotFoundError), f"{label}: cause is {exc.__cause__!r}"
    else:
        raise AssertionError(f"{label}: expected an actionable ImportError")


if mode == "none":
    # 夹具自检：纯 core 安装面必须真的没有 cli 依赖，否则下面的「惰性」断言是假绿。
    # Fixture self-check: the bare-core surface must really lack the cli deps.
    for name in ("rich", "prompt_toolkit"):
        assert importlib.util.find_spec(name) is None, f"fixture self-check failed: {name} is installed in mode=none"
    assert console_util._console is None, "console must not be constructed by importing a2c_smcp.computer"
    console_util.set_no_color(True)  # 不得触发 rich 导入
    _expect_actionable("console", lambda: console_util.console)
    _expect_actionable("ainput_prompt", lambda: asyncio.run(ainput_prompt("value: ")))
    print("core import contract OK (mode=none)")
else:
    # 正向对照：装 cli extra 时 console 必须真实可用（防 __getattr__ 过度惰性）。
    assert console_util.console is not None
    assert console_util.console is console_util.console, "console identity must be stable"
    print("artifact import OK (mode=cli)")
PY
