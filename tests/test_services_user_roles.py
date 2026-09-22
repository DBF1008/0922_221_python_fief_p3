import uuid
from unittest.mock import MagicMock

import pytest

from fief.db import AsyncSession
from fief.logger import AuditLogger, logger
from fief.models import UserRole
from fief.repositories import (
    RoleRepository,
    UserPermissionRepository,
    UserRoleRepository,
)
from fief.services.user_roles import UserRolesService, UserRolesSyncUnknownRoles
from fief.services.webhooks.models import UserRoleCreated, UserRoleDeleted
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
    async def _get_user_role_ids(
        self, main_session: AsyncSession, user_id: uuid.UUID
    ) -> set[uuid.UUID]:
        user_role_repository = UserRoleRepository(main_session)
        user_roles = await user_role_repository.list(
            user_role_repository.get_by_user_statement(user_id)
        )
        return {user_role.role_id for user_role in user_roles}

    async def test_idempotent_no_changes(
        self,
        main_session: AsyncSession,
        test_data: TestData,
        user_roles_service: UserRolesService,
        trigger_webhooks_mock: MagicMock,
        send_task_mock: MagicMock,
    ) -> None:
        user = test_data["users"]["regular"]
        role = test_data["roles"]["castles_visitor"]

        added, deleted = await user_roles_service.sync_roles(user, [role.id])

        assert added == []
        assert deleted == []
        assert await self._get_user_role_ids(main_session, user.id) == {role.id}
        trigger_webhooks_mock.assert_not_called()
        send_task_mock.assert_not_called()

    async def test_partial_changes(
        self,
        main_session: AsyncSession,
        test_data: TestData,
        user_roles_service: UserRolesService,
        trigger_webhooks_mock: MagicMock,
        send_task_mock: MagicMock,
    ) -> None:
        user = test_data["users"]["regular"]
        visitor = test_data["roles"]["castles_visitor"]
        manager = test_data["roles"]["castles_manager"]

        added, deleted = await user_roles_service.sync_roles(
            user, [visitor.id, manager.id], run_in_worker=False
        )

        assert [user_role.role_id for user_role in added] == [manager.id]
        assert deleted == []
        assert await self._get_user_role_ids(main_session, user.id) == {
            visitor.id,
            manager.id,
        }

        user_permission_repository = UserPermissionRepository(main_session)
        user_permissions = await user_permission_repository.list(
            user_permission_repository.get_by_user_statement(user.id)
        )
        assert {
            user_permission.permission_id for user_permission in user_permissions
        } == {
            test_data["permissions"]["castles:read"].id,
            test_data["permissions"]["castles:create"].id,
            test_data["permissions"]["castles:update"].id,
            test_data["permissions"]["castles:delete"].id,
        }

        assert trigger_webhooks_mock.call_count == 1
        assert trigger_webhooks_mock.call_args[0][0] is UserRoleCreated
        send_task_mock.assert_called_once_with(
            on_user_role_created, str(user.id), str(manager.id)
        )

    async def test_add_and_delete_together(
        self,
        main_session: AsyncSession,
        test_data: TestData,
        user_roles_service: UserRolesService,
        trigger_webhooks_mock: MagicMock,
        send_task_mock: MagicMock,
    ) -> None:
        user = test_data["users"]["regular"]
        visitor = test_data["roles"]["castles_visitor"]
        manager = test_data["roles"]["castles_manager"]

        added, deleted = await user_roles_service.sync_roles(user, [manager.id])

        assert [user_role.role_id for user_role in added] == [manager.id]
        assert [user_role.role_id for user_role in deleted] == [visitor.id]
        assert await self._get_user_role_ids(main_session, user.id) == {manager.id}

        assert trigger_webhooks_mock.call_count == 2
        triggered_events = [
            call[0][0] for call in trigger_webhooks_mock.call_args_list
        ]
        assert triggered_events == [UserRoleDeleted, UserRoleCreated]

        assert send_task_mock.call_count == 2
        send_task_mock.assert_any_call(
            on_user_role_deleted, str(user.id), str(visitor.id)
        )
        send_task_mock.assert_any_call(
            on_user_role_created, str(user.id), str(manager.id)
        )

    async def test_empty_target_removes_all_roles(
        self,
        main_session: AsyncSession,
        test_data: TestData,
        user_roles_service: UserRolesService,
    ) -> None:
        user = test_data["users"]["regular"]
        visitor = test_data["roles"]["castles_visitor"]

        added, deleted = await user_roles_service.sync_roles(user, [])

        assert added == []
        assert [user_role.role_id for user_role in deleted] == [visitor.id]
        assert await self._get_user_role_ids(main_session, user.id) == set()

    async def test_unknown_roles(
        self,
        main_session: AsyncSession,
        test_data: TestData,
        user_roles_service: UserRolesService,
        trigger_webhooks_mock: MagicMock,
        send_task_mock: MagicMock,
        not_existing_uuid: uuid.UUID,
    ) -> None:
        user = test_data["users"]["regular"]
        visitor = test_data["roles"]["castles_visitor"]

        with pytest.raises(UserRolesSyncUnknownRoles) as excinfo:
            await user_roles_service.sync_roles(
                user, [visitor.id, not_existing_uuid]
            )

        assert excinfo.value.unknown_role_ids == {not_existing_uuid}
        assert await self._get_user_role_ids(main_session, user.id) == {visitor.id}
        trigger_webhooks_mock.assert_not_called()
        send_task_mock.assert_not_called()

    async def test_commit_failure_rolls_back(
        self,
        main_session: AsyncSession,
        test_data: TestData,
        user_roles_service: UserRolesService,
        trigger_webhooks_mock: MagicMock,
        send_task_mock: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user = test_data["users"]["regular"]
        visitor = test_data["roles"]["castles_visitor"]
        manager = test_data["roles"]["castles_manager"]

        async def failing_commit() -> None:
            raise RuntimeError("Database error")

        monkeypatch.setattr(main_session, "commit", failing_commit)

        with pytest.raises(RuntimeError):
            await user_roles_service.sync_roles(user, [manager.id])

        monkeypatch.undo()

        assert await self._get_user_role_ids(main_session, user.id) == {visitor.id}
        trigger_webhooks_mock.assert_not_called()
        send_task_mock.assert_not_called()

        # Session is still usable after the rollback
        added, deleted = await user_roles_service.sync_roles(user, [manager.id])
        assert len(added) == 1
        assert len(deleted) == 1
        assert await self._get_user_role_ids(main_session, user.id) == {manager.id}
