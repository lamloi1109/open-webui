"""
GroupLightragConfig Model
Links O-UI Groups to LightRAG workspaces for knowledge base management.
"""

import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, ForeignKey, Text
from sqlalchemy.orm import Session

from open_webui.internal.db import Base, get_db_context

log = logging.getLogger(__name__)


####################
# GroupLightragConfig DB Schema
####################


class GroupLightragConfig(Base):
    __tablename__ = "group_lightrag_config"

    id = Column(Text, primary_key=True, unique=True)
    group_id = Column(
        Text,
        ForeignKey("group.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    lightrag_url = Column(Text, nullable=True)  # Nullable for graceful migration
    lightrag_workspace_name = Column(Text, nullable=True)  # Nullable for graceful migration
    knowledge_base_id = Column(Text, nullable=True)  # Link to auto-created KB
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class GroupLightragConfigModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    group_id: str
    lightrag_url: Optional[str] = None
    lightrag_workspace_name: Optional[str] = None
    knowledge_base_id: Optional[str] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


####################
# Forms
####################


class GroupLightragConfigForm(BaseModel):
    lightrag_url: Optional[str] = None
    lightrag_workspace_name: Optional[str] = None
    knowledge_base_id: Optional[str] = None


class GroupLightragConfigUpdateForm(BaseModel):
    lightrag_url: Optional[str] = None
    lightrag_workspace_name: Optional[str] = None
    knowledge_base_id: Optional[str] = None


####################
# Table Operations
####################


class GroupLightragConfigTable:
    def insert_config(
        self,
        group_id: str,
        form_data: GroupLightragConfigForm,
        db: Optional[Session] = None,
    ) -> Optional[GroupLightragConfigModel]:
        with get_db_context(db) as db:
            config = GroupLightragConfig(
                id=str(uuid.uuid4()),
                group_id=group_id,
                lightrag_url=form_data.lightrag_url,
                lightrag_workspace_name=form_data.lightrag_workspace_name,
                knowledge_base_id=form_data.knowledge_base_id,
                created_at=int(time.time()),
                updated_at=int(time.time()),
            )
            db.add(config)
            db.commit()
            db.refresh(config)
            return GroupLightragConfigModel.model_validate(config)

    def get_config_by_group_id(
        self, group_id: str, db: Optional[Session] = None
    ) -> Optional[GroupLightragConfigModel]:
        with get_db_context(db) as db:
            config = (
                db.query(GroupLightragConfig)
                .filter(GroupLightragConfig.group_id == group_id)
                .first()
            )
            if config:
                return GroupLightragConfigModel.model_validate(config)
            return None

    def update_config_by_group_id(
        self,
        group_id: str,
        form_data: GroupLightragConfigUpdateForm,
        db: Optional[Session] = None,
    ) -> Optional[GroupLightragConfigModel]:
        with get_db_context(db) as db:
            config = (
                db.query(GroupLightragConfig)
                .filter(GroupLightragConfig.group_id == group_id)
                .first()
            )
            if config:
                if form_data.lightrag_url is not None:
                    config.lightrag_url = form_data.lightrag_url
                if form_data.lightrag_workspace_name is not None:
                    config.lightrag_workspace_name = form_data.lightrag_workspace_name
                if form_data.knowledge_base_id is not None:
                    config.knowledge_base_id = form_data.knowledge_base_id
                config.updated_at = int(time.time())
                db.commit()
                db.refresh(config)
                return GroupLightragConfigModel.model_validate(config)
            return None

    def delete_config_by_group_id(
        self, group_id: str, db: Optional[Session] = None
    ) -> bool:
        with get_db_context(db) as db:
            result = (
                db.query(GroupLightragConfig)
                .filter(GroupLightragConfig.group_id == group_id)
                .delete()
            )
            db.commit()
            return result > 0

    def is_group_linked(
        self, group_id: str, db: Optional[Session] = None
    ) -> bool:
        """Check if a group has a valid LightRAG configuration."""
        config = self.get_config_by_group_id(group_id, db)
        if config is None:
            return False
        return bool(config.lightrag_url and config.lightrag_workspace_name)

    def get_config_by_knowledge_base_id(
        self, knowledge_base_id: str, db: Optional[Session] = None
    ) -> Optional[GroupLightragConfigModel]:
        """Get config by the auto-created Knowledge Base ID."""
        with get_db_context(db) as db:
            config = (
                db.query(GroupLightragConfig)
                .filter(GroupLightragConfig.knowledge_base_id == knowledge_base_id)
                .first()
            )
            if config:
                return GroupLightragConfigModel.model_validate(config)
            return None


GroupLightragConfigs = GroupLightragConfigTable()
