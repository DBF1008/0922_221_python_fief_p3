from unittest.mock import MagicMock

import pytest

from fief.db import AsyncSession
from fief.logger import AuditLogger, logger
from fief.repositories import (
    RoleRepository,
    UserPermissionRepository,
    UserRoleRepository,
)
from fief.services.user_roles import UserRolesService
from fief.tasks import on_user_role_created, on_user_role_deleted
from tests.data import TestData


@pytest.fixture
def trigger_webhooks_mock() -> MagicMock:
    return MagicMock()


@pytest.fixture
def user_roles_service(
    main_session: AsyncSession,
    trigger_webhooks_mock: MagicMock,
    send_task_mock: MagicMock,
) -> UserRolesService:
    return UserRolesService(
        UserRoleRepository(main_session),
        UserPermissionRepository(main_session),
        RoleRepository(main_session),
        AuditLogger(logger),
        trigger_webhooks_mock,
        send_task_mock,
    )


@pytest.mark.asyncio
async def test_add_default_roles(
    main_session: AsyncSession,
    test_data: TestData,
    user_roles_service: UserRolesService,
) -> None:
    user = test_data["users"]["regular_secondary"]

    await user_roles_service.add_default_roles(user, run_in_worker=False)

    user_role_repository = UserRoleRepository(main_session)
    user_roles = await user_role_repository.list(
        user_role_repository.get_by_user_statement(user.id)
    )
    assert len(user_roles) == 1

    user_permission_repository = UserPermissionRepository(main_session)
    user_permissions = await user_permission_repository.list(
        user_permission_repository.get_by_user_statement(user.id)
    )
    assert len(user_permissions) == 1


@pytest.mark.asyncio
class TestSyncRoles:
    async def test_idempotent_no_change(
        self,
        main_session: AsyncSession,
        test_data: TestData,
        user_roles_service: UserRolesService,
        trigger_webhooks_mock: MagicMock,
        send_task_mock: MagicMock,
    ) -> None:
        user = test_data["users"]["regular"]
        role_repository = RoleRepository(main_session)
        castles_visitor = await role_repository.get_by_id(
            test_data["roles"]["castles_visitor"].id
        )
        assert castles_visitor is not None

        added, deleted = await user_roles_service.sync_roles(
            user, [castles_visitor], run_in_worker=False
        )

        assert added == []
        assert deleted == []

        user_role_repository = UserRoleRepository(main_session)
        user_roles = await user_role_repository.list(
            user_role_repository.get_by_user_statement(user.id)
        )
        assert len(user_roles) == 1
        assert user_roles[0].role_id == castles_visitor.id

        trigger_webhooks_mock.assert_not_called()
        send_task_mock.assert_not_called()

    async def test_partial_change(
        self,
        main_session: AsyncSession,
        test_data: TestData,
        user_roles_service: UserRolesService,
        trigger_webhooks_mock: MagicMock,
        send_task_mock: MagicMock,
    ) -> None:
        user = test_data["users"]["regular"]
        role_repository = RoleRepository(main_session)
        castles_manager = await role_repository.get_by_id(
            test_data["roles"]["castles_manager"].id
        )
        assert castles_manager is not None

        added, deleted = await user_roles_service.sync_roles(
            user, [castles_manager], run_in_worker=False
        )

        assert len(added) == 1
        assert added[0].role_id == castles_manager.id
        assert len(deleted) == 1
        assert deleted[0].role_id == test_data["roles"]["castles_visitor"].id

        user_role_repository = UserRoleRepository(main_session)
        user_roles = await user_role_repository.list(
            user_role_repository.get_by_user_statement(user.id)
        )
        assert len(user_roles) == 1
        assert user_roles[0].role_id == castles_manager.id

        user_permission_repository = UserPermissionRepository(main_session)
        user_permissions = await user_permission_repository.list(
            user_permission_repository.get_by_user_statement(user.id)
        )
        # 4 permissions from castles_manager + 1 direct castles:delete
        assert len(user_permissions) == 5
        assert {
            user_permission.permission.codename
            for user_permission in user_permissions
            if user_permission.from_role_id == castles_manager.id
        } == {"castles:read", "castles:create", "castles:update", "castles:delete"}

        assert trigger_webhooks_mock.call_count == 2
        send_task_mock.assert_not_called()

    async def test_partial_change_run_in_worker(
        self,
        main_session: AsyncSession,
        test_data: TestData,
        user_roles_service: UserRolesService,
        send_task_mock: MagicMock,
    ) -> None:
        user = test_data["users"]["regular"]
        role_repository = RoleRepository(main_session)
        castles_manager = await role_repository.get_by_id(
            test_data["roles"]["castles_manager"].id
        )
        assert castles_manager is not None

        await user_roles_service.sync_roles(user, [castles_manager])

        assert send_task_mock.call_count == 2
        send_task_mock.assert_any_call(
            on_user_role_created, str(user.id), str(castles_manager.id)
        )
        send_task_mock.assert_any_call(
            on_user_role_deleted,
            str(user.id),
            str(test_data["roles"]["castles_visitor"].id),
        )

    async def test_empty_target_removes_all_roles(
        self,
        main_session: AsyncSession,
        test_data: TestData,
        user_roles_service: UserRolesService,
    ) -> None:
        user = test_data["users"]["regular"]

        added, deleted = await user_roles_service.sync_roles(
            user, [], run_in_worker=False
        )

        assert added == []
        assert len(deleted) == 1

        user_role_repository = UserRoleRepository(main_session)
        user_roles = await user_role_repository.list(
            user_role_repository.get_by_user_statement(user.id)
        )
        assert len(user_roles) == 0

    async def test_commit_error_rolls_back(
        self,
        main_session: AsyncSession,
        test_data: TestData,
        user_roles_service: UserRolesService,
        trigger_webhooks_mock: MagicMock,
        send_task_mock: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user = test_data["users"]["regular"]
        role_repository = RoleRepository(main_session)
        castles_manager = await role_repository.get_by_id(
            test_data["roles"]["castles_manager"].id
        )
        assert castles_manager is not None

        async def failing_commit() -> None:
            raise RuntimeError("Database error")

        monkeypatch.setattr(main_session, "commit", failing_commit)

        with pytest.raises(RuntimeError):
            await user_roles_service.sync_roles(
                user, [castles_manager], run_in_worker=False
            )

        monkeypatch.undo()

        user_role_repository = UserRoleRepository(main_session)
        user_roles = await user_role_repository.list(
            user_role_repository.get_by_user_statement(user.id)
        )
        assert len(user_roles) == 1
        assert user_roles[0].role_id == test_data["roles"]["castles_visitor"].id

        trigger_webhooks_mock.assert_not_called()
        send_task_mock.assert_not_called()
