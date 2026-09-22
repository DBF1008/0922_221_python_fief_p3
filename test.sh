#!/usr/bin/env bash
# 手动运行单元测试脚本
# 用法:
#   ./test.sh            # 运行本次变更相关的单元测试
#   ./test.sh --all      # 运行完整测试套件
#   ./test.sh <pytest 参数>  # 透传自定义 pytest 参数
set -euo pipefail

cd "$(dirname "$0")"

if [[ "${1:-}" == "--all" ]]; then
    shift
    TEST_TARGETS=("tests/")
elif [[ $# -gt 0 ]]; then
    TEST_TARGETS=("$@")
else
    TEST_TARGETS=(
        "tests/test_services_user_roles.py"
        "tests/test_apps_api_users.py"
    )
fi

if [[ -x ".venv/bin/python" ]]; then
    PYTHON=".venv/bin/python"
elif command -v hatch >/dev/null 2>&1; then
    exec hatch run test "${TEST_TARGETS[@]}"
else
    PYTHON="python3"
fi

exec "$PYTHON" -m pytest -v "${TEST_TARGETS[@]}"
