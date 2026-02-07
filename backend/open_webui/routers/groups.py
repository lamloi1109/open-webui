import os
from pathlib import Path
from typing import Optional
import logging

from open_webui.models.users import Users, UserInfoResponse
from open_webui.models.groups import (
    Groups,
    GroupForm,
    GroupUpdateForm,
    GroupResponse,
    UserIdsForm,
)
from open_webui.models.knowledge import Knowledges, KnowledgeForm

from open_webui.config import CACHE_DIR
from open_webui.constants import ERROR_MESSAGES
from fastapi import APIRouter, Depends, HTTPException, Request, status

from open_webui.internal.db import get_session
from sqlalchemy.orm import Session

from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.models.group_lightrag_config import (
    GroupLightragConfigs,
    GroupLightragConfigForm,
    GroupLightragConfigUpdateForm,
)
from open_webui.utils.lightrag_client import LightRAGClient, LightRAGClientError

log = logging.getLogger(__name__)

router = APIRouter()

############################
# GetFunctions
############################


@router.get("/", response_model=list[GroupResponse])
async def get_groups(
    share: Optional[bool] = None,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):

    filter = {}

    # Admins can share to all groups regardless of share setting
    if user.role != "admin":
        filter["member_id"] = user.id
        if share is not None:
            filter["share"] = share

    groups = Groups.get_groups(filter=filter, db=db)

    return groups


############################
# CreateNewGroup
############################


@router.post("/create", response_model=Optional[GroupResponse])
async def create_new_group(
    form_data: GroupForm,
    user=Depends(get_admin_user),
    db: Session = Depends(get_session),
):
    try:
        group = Groups.insert_new_group(user.id, form_data, db=db)
        if group:
            # Check if LightRAG config is provided in the form data
            lightrag_url = getattr(form_data, 'lightrag_url', None) or (form_data.data or {}).get('lightrag_url')
            lightrag_workspace_name = getattr(form_data, 'lightrag_workspace_name', None) or (form_data.data or {}).get('lightrag_workspace_name')
            
            if lightrag_url and lightrag_workspace_name:
                # Validate LightRAG connection before storing config
                try:
                    client = LightRAGClient(
                        base_url=lightrag_url,
                        workspace=lightrag_workspace_name,
                    )
                    await client.check_health()
                except LightRAGClientError as e:
                    # Rollback: delete the created group if LightRAG validation fails
                    Groups.delete_group_by_id(group.id, db=db)
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"LightRAG connection failed: {e}",
                    )
                
                # Store LightRAG config
                GroupLightragConfigs.insert_config(
                    group_id=group.id,
                    form_data=GroupLightragConfigForm(
                        lightrag_url=lightrag_url,
                        lightrag_workspace_name=lightrag_workspace_name,
                    ),
                    db=db,
                )
            
            # Auto-create Knowledge Base for this group
            try:
                kb_name = f"{group.name}_KnowledgeBase"
                kb_description = f"Auto-generated Knowledge Base for {group.name}"
                
                # Set access control to restrict to this group only
                # Both read and write access limited to the group members
                access_control = {
                    "read": {
                        "group_ids": [group.id],
                        "user_ids": []
                    },
                    "write": {
                        "group_ids": [group.id],
                        "user_ids": []
                    }
                }
                
                kb_form = KnowledgeForm(
                    name=kb_name,
                    description=kb_description,
                    access_control=access_control,
                )
                
                knowledge_base = Knowledges.insert_new_knowledge(
                    user_id=user.id,
                    form_data=kb_form,
                    db=db,
                )
                
                if knowledge_base:
                    log.info(f"Auto-created Knowledge Base '{kb_name}' (id={knowledge_base.id}) for group '{group.name}' (id={group.id})")
                    
                    # Store the KB ID in the group's LightRAG config for reference
                    if lightrag_url and lightrag_workspace_name:
                        GroupLightragConfigs.update_config_by_group_id(
                            group_id=group.id,
                            form_data=GroupLightragConfigUpdateForm(
                                lightrag_url=lightrag_url,
                                lightrag_workspace_name=lightrag_workspace_name,
                                knowledge_base_id=knowledge_base.id,
                            ),
                            db=db,
                        )
                else:
                    log.warning(f"Failed to create Knowledge Base for group '{group.name}'")
                    
            except Exception as kb_error:
                log.exception(f"Error creating Knowledge Base for group {group.name}: {kb_error}")
                # Continue even if KB creation fails - group is still valid
            
            return GroupResponse(
                **group.model_dump(),
                member_count=Groups.get_group_member_count_by_id(group.id, db=db),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error creating group"),
            )
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f"Error creating a new group: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )


############################
# GetGroupById
############################


@router.get("/id/{id}", response_model=Optional[GroupResponse])
async def get_group_by_id(
    id: str, user=Depends(get_admin_user), db: Session = Depends(get_session)
):
    group = Groups.get_group_by_id(id, db=db)
    if group:
        return GroupResponse(
            **group.model_dump(),
            member_count=Groups.get_group_member_count_by_id(group.id, db=db),
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# ExportGroupById
############################


class GroupExportResponse(GroupResponse):
    user_ids: list[str] = []
    pass


@router.get("/id/{id}/export", response_model=Optional[GroupExportResponse])
async def export_group_by_id(
    id: str, user=Depends(get_admin_user), db: Session = Depends(get_session)
):
    group = Groups.get_group_by_id(id, db=db)
    if group:
        return GroupExportResponse(
            **group.model_dump(),
            member_count=Groups.get_group_member_count_by_id(group.id, db=db),
            user_ids=Groups.get_group_user_ids_by_id(group.id, db=db),
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# GetUsersInGroupById
############################


@router.post("/id/{id}/users", response_model=list[UserInfoResponse])
async def get_users_in_group(
    id: str, user=Depends(get_admin_user), db: Session = Depends(get_session)
):
    try:
        users = Users.get_users_by_group_id(id, db=db)
        return users
    except Exception as e:
        log.exception(f"Error adding users to group {id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )


############################
# UpdateGroupById
############################


@router.post("/id/{id}/update", response_model=Optional[GroupResponse])
async def update_group_by_id(
    id: str,
    form_data: GroupUpdateForm,
    user=Depends(get_admin_user),
    db: Session = Depends(get_session),
):
    try:
        group = Groups.update_group_by_id(id, form_data, db=db)
        if group:
            return GroupResponse(
                **group.model_dump(),
                member_count=Groups.get_group_member_count_by_id(group.id, db=db),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error updating group"),
            )
    except Exception as e:
        log.exception(f"Error updating group {id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )


############################
# AddUserToGroupByUserIdAndGroupId
############################


@router.post("/id/{id}/users/add", response_model=Optional[GroupResponse])
async def add_user_to_group(
    id: str,
    form_data: UserIdsForm,
    user=Depends(get_admin_user),
    db: Session = Depends(get_session),
):
    try:
        if form_data.user_ids:
            form_data.user_ids = Users.get_valid_user_ids(form_data.user_ids, db=db)

        group = Groups.add_users_to_group(id, form_data.user_ids, db=db)
        if group:
            return GroupResponse(
                **group.model_dump(),
                member_count=Groups.get_group_member_count_by_id(group.id, db=db),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error adding users to group"),
            )
    except Exception as e:
        log.exception(f"Error adding users to group {id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )


@router.post("/id/{id}/users/remove", response_model=Optional[GroupResponse])
async def remove_users_from_group(
    id: str,
    form_data: UserIdsForm,
    user=Depends(get_admin_user),
    db: Session = Depends(get_session),
):
    try:
        group = Groups.remove_users_from_group(id, form_data.user_ids, db=db)
        if group:
            return GroupResponse(
                **group.model_dump(),
                member_count=Groups.get_group_member_count_by_id(group.id, db=db),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error removing users from group"),
            )
    except Exception as e:
        log.exception(f"Error removing users from group {id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )


############################
# DeleteGroupById
############################


@router.delete("/id/{id}/delete", response_model=bool)
async def delete_group_by_id(
    id: str, user=Depends(get_admin_user), db: Session = Depends(get_session)
):
    try:
        # Clear LightRAG workspace before deleting the group
        config = GroupLightragConfigs.get_config_by_group_id(id, db=db)
        if config and config.lightrag_url and config.lightrag_workspace_name:
            try:
                client = LightRAGClient(
                    base_url=config.lightrag_url,
                    workspace=config.lightrag_workspace_name,
                )
                await client.clear_workspace()
            except LightRAGClientError as e:
                log.warning(f"Failed to clear LightRAG workspace for group {id}: {e}")
                # Continue with deletion even if LightRAG cleanup fails
        
        # Delete LightRAG config (cascade will handle this, but explicit is safer)
        GroupLightragConfigs.delete_config_by_group_id(id, db=db)
        
        result = Groups.delete_group_by_id(id, db=db)
        if result:
            return result
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error deleting group"),
            )
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f"Error deleting group {id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )
