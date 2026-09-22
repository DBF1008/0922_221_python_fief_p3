#!/usr/bin/env bash
#
# 手动回归测试脚本：用户角色集合原子同步
# （PUT /users/{id}/roles + UserRolesService.sync_roles）
#
# 用法：
#   ./test.sh              # 依次运行全部 3 个阶段
#   ./test.sh service      # 只跑服务层单元测试
#   ./test.sh api          # 只跑 API 层回归测试
#   ./test.sh all          # 只跑全量测试套件
#
# 依赖：已安装项目依赖的环境（hatch 或自行安装的 pytest）。
# 默认使用 SQLITE 数据库，无需外部服务。

set -euo pipefail
cd "$(dirname "$0")"

# 与 CI（.github/actions/test）保持一致的环境变量
export ENVIRONMENT=development
export TELEMETRY_ENABLED=0
export SECRET=ThisShouldBeChangedInProduction
export ENCRYPTION_KEY=uSieBJ_695D2NA7bOPUJqFGCS2_qI8G4aI6L42WhjjM=
export GENERATED_JWK_SIZE=1024
export DATABASE_TYPE="${DATABASE_TYPE:-SQLITE}"
export ALLOW_ORIGIN_REGEX=http://localhost:3000
export FIEF_CLIENT_ID=FIEF_CLIENT_ID
export FIEF_CLIENT_SECRET=FIEF_CLIENT_SECRET

if command -v hatch >/dev/null 2>&1; then
    PYTEST=(hatch run pytest)
else
    PYTEST=(python -m pytest)
fi

run_service() {
    echo "==> [服务层] UserRolesService.sync_roles 单元测试（幂等/部分变更/异常回滚）"
    "${PYTEST[@]}" tests/test_services_user_roles.py -v
}

run_api() {
    echo "==> [API 层] 用户角色回归测试（含 PUT /users/{id}/roles 同步）"
    "${PYTEST[@]}" tests/test_apps_api_users.py -v -k "UserRole"
}

run_all() {
    echo "==> [全量] 完整单元测试套件"
    "${PYTEST[@]}" tests/
}

case "${1:-}" in
    service) run_service ;;
    api) run_api ;;
    all) run_all ;;
    "")
        run_service
        run_api
        run_all
        ;;
    *)
        echo "未知参数：$1（可选：service | api | all）" >&2
        exit 1
        ;;
esac

echo "==> 全部通过 ✅"
