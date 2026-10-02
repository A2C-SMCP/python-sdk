#!/usr/bin/env bash
# 文件名: clean_install_smoke.sh / File name: clean_install_smoke.sh
# 作者: JQQ / Author: JQQ
# 创建日期: 2026/09/29 / Created: 2026/09/29
# 描述: 干净安装冒烟——把**构建出的 wheel** 装进空 venv（只装制品自己声明的依赖），
#       验证 `a2c-computer --help` 可跑、且包能被导入。用于 tests.yml 与 publish.yml **单点复用**
#       （两处逐字复制会静默漂移，使「上传前」那道闸门失效——#224 的逃逸通道）。
#       Description: Clean-install smoke — install the BUILT WHEEL into an empty venv (declared deps
#       only), then verify `a2c-computer --help` runs and the package imports. Shared by tests.yml and
#       publish.yml so the two gates cannot drift apart.
#
# 用法 / Usage:
#   scripts/clean_install_smoke.sh <wheel-path> [extras] [venv-dir]
#     wheel-path  wheel 文件路径（只允许一个，多个即报错——见下）
#     extras      额外依赖名，逗号分隔，默认 cli
#     venv-dir    临时 venv 路径，默认 /tmp/a2c-smoke
#
#   ⚠️ 不提供「不装 extra」模式：本脚本固定运行 `a2c-computer --help`，而该入口点需要 cli extra
#      （typer / prompt_toolkit / rich），故纯 core 安装下必然红——那是**另一个**缺陷
#      （`import a2c_smcp.computer` 硬依赖 cli extra 的成员），不属本闸门的判据。
#   ⚠️ There is deliberately no "no extras" mode: this script always runs `a2c-computer --help`,
#      whose entry point needs the cli extra. Bare-core installs therefore always fail — that is a
#      separate defect, not what this gate is about.
#
# 背景 / Why: #224 —— `pyyaml` 从未声明为依赖，但 dev/CI 的 `uv sync --group dev` 里
#   `poethepoet` 传递带入 pyyaml，于是「源码 import 未声明的包」在 CI **永远复现不了**。
#   本脚本构造真实用户的安装面（只有制品声明的依赖），把这类坏包挡在合入/上传之前。
set -euo pipefail

WHEEL_PATH="${1:?用法: clean_install_smoke.sh <wheel-path> [extras] [venv-dir]}"
# 注意用 ${2-cli} 而非 ${2:-cli}：后者对**空串**也取默认值，会让下面的空值校验变成死条件
# （传 "" 时静默装 [cli]，与「显式拒绝」的语义不符）。${2-cli} 仅在未传参时取默认。
EXTRAS="${2-cli}"
VENV_DIR="${3:-/tmp/a2c-smoke}"

if [[ -z "${EXTRAS}" || "${EXTRAS}" == "none" ]]; then
  echo "::error::extras 不得为空或 none —— 本脚本固定跑 \`a2c-computer --help\`，纯 core 安装必然失败（见文件头说明）" >&2
  exit 1
fi

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

TARGET="${WHEEL_PATH}[${EXTRAS}]"

rm -rf "${VENV_DIR}"
uv venv "${VENV_DIR}"
echo "Smoke-testing ${TARGET}"
uv pip install --python "${VENV_DIR}/bin/python" "${TARGET}"

# ⚠️ 必须在**中性 cwd** 下跑：`python -c` 会把 cwd 放进 sys.path[0]，若在仓库根跑，导入到的是
#    仓库源码而非被测 wheel —— 那样即使制品是坏的，本脚本也会绿。下面的 __file__ 断言把这点钉死。
# ⚠️ Must run from a neutral cwd: `python -c` puts cwd on sys.path[0], so running at the repo root
#    would import the source tree instead of the artifact — the smoke would pass even for a broken
#    wheel. The __file__ assertion below pins this down.
cd /tmp
"${VENV_DIR}/bin/a2c-computer" --help > /dev/null
echo "a2c-computer --help OK"

"${VENV_DIR}/bin/python" - "${VENV_DIR}" <<'PY'
import sys
from pathlib import Path

import a2c_smcp
import a2c_smcp.computer

# 按「路径归属」判定而非子串匹配：软链（macOS /tmp → /private/tmp）与相对路径都会让子串比法误报。
# 归属比较前先 resolve()，对软链与相对路径都成立。
# Ownership check (not substring matching): symlinks (macOS /tmp → /private/tmp) and relative paths
# make substring comparison produce false alarms.
venv_dir = Path(sys.argv[1]).resolve()
for mod in (a2c_smcp, a2c_smcp.computer):
    path = Path(mod.__file__ or "").resolve()
    assert path.is_relative_to(venv_dir), f"smoke 导入的是 {path}，不是被测制品 {venv_dir} / not the artifact"
    print(f"  {mod.__name__} -> {path}")
print("artifact import OK")
PY
