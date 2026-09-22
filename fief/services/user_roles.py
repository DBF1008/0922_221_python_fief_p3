import uuid
from collections.abc import Sequence

from pydantic import UUID4
from sqlalchemy import select

from fief import schemas, tasks
from fief.logger import AuditLogger
from fief.models import AuditLogMessage, Role, User, UserRole
from fief.repositories import (
    RoleRepository,
    UserPermissionRepository,
    UserRoleRepository,
)
from fief.services.user_role_permissions import UserRolePermissionsService
from fief.services.webhooks.models import UserRoleCreated, UserRoleDeleted
from fief.services.webhooks.trigger import TriggerWebhooks
from fief.tasks import SendTask


class UserRolesError(Exception): ...


class UserRoleAlreadyExists(UserRolesError): ...


class UserRoleDoesNotExist(UserRolesError): ...


class UserRolesSyncUnknownRoles(UserRolesError):
    def __init__(self, unknown_role_ids: set[uuid.UUID]) -> None:
        self.unknown_role_ids = unknown_role_ids
        super().__init__(f"Unknown roles: {unknown_role_ids}")


class UserRolesService:
    def __init__(
        self,
        user_role_repository: UserRoleRepository,
        user_permission_repository: UserPermissionRepository,
        role_repository: RoleRepository,
        audit_logger: AuditLogger,
        trigger_webhooks: TriggerWebhooks,
        send_task: SendTask,
    ) -> None:
        self.user_role_repository = user_role_repository
        self.user_permission_repository = user_permission_repository
        self.role_repository = role_repository
        self.audit_logger = audit_logger
        self.trigger_webhooks = trigger_webhooks
        self.send_task = send_task
        self.user_role_permissions = UserRolePermissionsService(
            user_permission_repository
        )

    async def add_role(
        self, user: User, role: Role, *, run_in_worker: bool = True
    ) -> UserRole:
        existing_user_role = await self.user_role_repository.get_by_role_and_user(
            user.id, role.id
        )
        if existing_user_role is not None:
            raise UserRoleAlreadyExists()

        user_role = UserRole(user_id=user.id, role=role)
        await self.user_role_repository.create(user_role)
        self.audit_logger.log_object_write(
            AuditLogMessage.OBJECT_CREATED,
            user_role,
            subject_user_id=user.id,
            role_id=str(role.id),
        )
        self.trigger_webhooks(UserRoleCreated, user_role, schemas.user_role.UserRole)

        if run_in_worker:
            self.send_task(tasks.on_user_role_created, str(user.id), str(role.id))
        else:
            await self.user_role_permissions.add_role_permissions(user, role)

        return user_role

    async def delete_role(
        self, user: User, role: Role, *, run_in_worker: bool = True
    ) -> None:
        user_role = await self.user_role_repository.get_by_role_and_user(
            user.id, role.id
        )
        if user_role is None:
            raise UserRoleDoesNotExist()

        await self.user_role_repository.delete(user_role)
        self.audit_logger.log_object_write(
            AuditLogMessage.OBJECT_DELETED,
            user_role,
            subject_user_id=user.id,
            role_id=str(role.id),
        )
        self.trigger_webhooks(UserRoleDeleted, user_role, schemas.user_role.UserRole)

        self.send_task(tasks.on_user_role_deleted, str(user.id), str(role.id))

        if run_in_worker:
            self.send_task(tasks.on_user_role_deleted, str(user.id), str(role.id))
        else:
            await self.user_role_permissions.delete_role_permissions(user, role)

    async def sync_roles(
        self, user: User, role_ids: Sequence[UUID4], *, run_in_worker: bool = True
    ) -> tuple[list[UserRole], list[UserRole]]:
        target_role_ids = {uuid.UUID(str(role_id)) for role_id in role_ids}

        roles: list[Role] = []
        if target_role_ids:
            roles = await self.role_repository.list(
                select(Role).where(Role.id.in_(target_role_ids))
            )
        roles_by_id = {role.id: role for role in roles}
        unknown_role_ids = target_role_ids - roles_by_id.keys()
        if unknown_role_ids:
            raise UserRolesSyncUnknownRoles(unknown_role_ids)

        current_user_roles = await self.user_role_repository.list(
            self.user_role_repository.get_by_user_statement(user.id)
        )
        current_role_ids = {user_role.role_id for user_role in current_user_roles}

        user_roles_to_delete = [
            user_role
            for user_role in current_user_roles
            if user_role.role_id not in target_role_ids
        ]
        roles_to_add = [
            roles_by_id[role_id] for role_id in target_role_ids - current_role_ids
        ]

        session = self.user_role_repository.session
        added_user_roles: list[UserRole] = []
        try:
            for user_role in user_roles_to_delete:
                await session.delete(user_role)
            for role in roles_to_add:
                user_role = UserRole(user_id=user.id, role=role)
                session.add(user_role)
                added_user_roles.append(user_role)
            await session.commit()
        except Exception:
            await session.rollback()
            raise

        for user_role in user_roles_to_delete:
            role = user_role.role
            self.audit_logger.log_object_write(
                AuditLogMessage.OBJECT_DELETED,
                user_role,
                subject_user_id=user.id,
                role_id=str(role.id),
            )
            self.trigger_webhooks(
                UserRoleDeleted, user_role, schemas.user_role.UserRole
            )
            if run_in_worker:
                self.send_task(tasks.on_user_role_deleted, str(user.id), str(role.id))
            else:
                await self.user_role_permissions.delete_role_permissions(user, role)

        for user_role in added_user_roles:
            role = user_role.role
            self.audit_logger.log_object_write(
                AuditLogMessage.OBJECT_CREATED,
                user_role,
                subject_user_id=user.id,
                role_id=str(role.id),
            )
            self.trigger_webhooks(
                UserRoleCreated, user_role, schemas.user_role.UserRole
            )
            if run_in_worker:
                self.send_task(tasks.on_user_role_created, str(user.id), str(role.id))
            else:
                await self.user_role_permissions.add_role_permissions(user, role)

        return added_user_roles, user_roles_to_delete

    async def add_default_roles(
        self, user: User, *, run_in_worker: bool = True
    ) -> None:
        default_roles = await self.role_repository.get_granted_by_default()
        for role in default_roles:
            await self.add_role(user, role, run_in_worker=run_in_worker)
