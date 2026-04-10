import logging
import os
import uuid
import json
from pathlib import Path
from typing import Optional
from urllib.parse import quote
import asyncio

from fastapi import (
    BackgroundTasks,
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
    Query,
)

from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from open_webui.internal.db import get_session, SessionLocal

from open_webui.constants import ERROR_MESSAGES
from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT

from open_webui.models.channels import Channels
from open_webui.models.users import Users
from open_webui.models.files import (
    FileForm,
    FileModel,
    FileModelResponse,
    Files,
)
from open_webui.models.chats import Chats
from open_webui.models.knowledge import Knowledges
from open_webui.models.groups import Groups
from open_webui.utils.lightrag_client import LightRAGClient, LightRAGClientError
from open_webui.models.group_lightrag_config import GroupLightragConfigs

from open_webui.routers.retrieval import ProcessFileForm, process_file
from open_webui.routers.audio import transcribe

from open_webui.storage.provider import Storage


from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.access_control import has_access
from open_webui.utils.misc import strict_match_mime_type
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter()


############################
# Check if the current user has access to a file through any knowledge bases the user may be in.
############################


# TODO: Optimize this function to use the knowledge_file table for faster lookups.
def has_access_to_file(
    file_id: Optional[str],
    access_type: str,
    user=Depends(get_verified_user),
    db: Optional[Session] = None,
) -> bool:
    file = Files.get_file_by_id(file_id, db=db)
    log.debug(f"Checking if user has {access_type} access to file")
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    # Check if the file is associated with any knowledge bases the user has access to
    knowledge_bases = Knowledges.get_knowledges_by_file_id(file_id, db=db)
    user_group_ids = {
        group.id for group in Groups.get_groups_by_member_id(user.id, db=db)
    }
    for knowledge_base in knowledge_bases:
        if knowledge_base.user_id == user.id or has_access(
            user.id, access_type, knowledge_base.access_control, user_group_ids, db=db
        ):
            return True

    knowledge_base_id = file.meta.get("collection_name") if file.meta else None
    if knowledge_base_id:
        knowledge_bases = Knowledges.get_knowledge_bases_by_user_id(
            user.id, access_type, db=db
        )
        for knowledge_base in knowledge_bases:
            if knowledge_base.id == knowledge_base_id:
                return True

    # Check if the file is associated with any channels the user has access to
    channels = Channels.get_channels_by_file_id_and_user_id(file_id, user.id, db=db)
    if access_type == "read" and channels:
        return True

    # Check if the file is associated with any chats the user has access to
    # TODO: Granular access control for chats
    chats = Chats.get_shared_chats_by_file_id(file_id, db=db)
    if chats:
        return True

    return False


############################
# Upload File
############################


def process_uploaded_file(
    request,
    file,
    file_path,
    file_item,
    file_metadata,
    user,
    db: Optional[Session] = None,
):
    def _process_handler(db_session):
        try:
            if file.content_type:
                stt_supported_content_types = getattr(
                    request.app.state.config, "STT_SUPPORTED_CONTENT_TYPES", []
                )

                if strict_match_mime_type(
                    stt_supported_content_types, file.content_type
                ):
                    file_path_processed = Storage.get_file(file_path)
                    result = transcribe(
                        request, file_path_processed, file_metadata, user
                    )

                    process_file(
                        request,
                        ProcessFileForm(
                            file_id=file_item.id, content=result.get("text", "")
                        ),
                        user=user,
                        db=db_session,
                    )
                elif (not file.content_type.startswith(("image/", "video/"))) or (
                    request.app.state.config.CONTENT_EXTRACTION_ENGINE == "external"
                ):
                    process_file(
                        request,
                        ProcessFileForm(file_id=file_item.id),
                        user=user,
                        db=db_session,
                    )
                else:
                    raise Exception(
                        f"File type {file.content_type} is not supported for processing"
                    )
            else:
                log.info(
                    f"File type {file.content_type} is not provided, but trying to process anyway"
                )
                process_file(
                    request,
                    ProcessFileForm(file_id=file_item.id),
                    user=user,
                    db=db_session,
                )

        except Exception as e:
            log.error(f"Error processing file: {file_item.id}")
            Files.update_file_data_by_id(
                file_item.id,
                {
                    "status": "failed",
                    "error": str(e.detail) if hasattr(e, "detail") else str(e),
                },
                db=db_session,
            )

    if db:
        _process_handler(db)
    else:
        with SessionLocal() as db_session:
            _process_handler(db_session)


@router.post("/", response_model=FileModelResponse)
def upload_file(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    metadata: Optional[dict | str] = Form(None),
    process: bool = Query(True),
    process_in_background: bool = Query(True),
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    return upload_file_handler(
        request,
        file=file,
        metadata=metadata,
        process=process,
        process_in_background=process_in_background,
        user=user,
        background_tasks=background_tasks,
        db=db,
    )


def upload_file_handler(
    request: Request,
    file: UploadFile = File(...),
    metadata: Optional[dict | str] = Form(None),
    process: bool = Query(True),
    process_in_background: bool = Query(True),
    user=Depends(get_verified_user),
    background_tasks: Optional[BackgroundTasks] = None,
    db: Optional[Session] = None,
):
    log.info(f"file.content_type: {file.content_type} {process}")

    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Invalid metadata format"),
            )
    file_metadata = metadata if metadata else {}

    try:
        unsanitized_filename = file.filename
        filename = os.path.basename(unsanitized_filename)

        file_extension = os.path.splitext(filename)[1]
        # Remove the leading dot from the file extension
        file_extension = file_extension[1:] if file_extension else ""
        knowledge_base_id = file_metadata.get("knowledge_id")

        if process and request.app.state.config.ALLOWED_FILE_EXTENSIONS:
            request.app.state.config.ALLOWED_FILE_EXTENSIONS = [
                ext for ext in request.app.state.config.ALLOWED_FILE_EXTENSIONS if ext
            ]

            if file_extension not in request.app.state.config.ALLOWED_FILE_EXTENSIONS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT(
                        f"File type {file_extension} is not allowed"
                    ),
                )

        # replace filename with uuid
        id = str(uuid.uuid4())
        name = filename
        filename = f"{id}_{filename}"
        contents, file_path = Storage.upload_file(
            file.file,
            filename,
            {
                "OpenWebUI-User-Email": user.email,
                "OpenWebUI-User-Id": user.id,
                "OpenWebUI-User-Name": user.name,
                "OpenWebUI-File-Id": id,
            },
        )

        file_item = Files.insert_new_file(
            user.id,
            FileForm(
                **{
                    "id": id,
                    "filename": name,
                    "path": file_path,
                    "data": {
                        **({"status": "pending"} if process else {}),
                    },
                    "meta": {
                        "name": name,
                        "content_type": file.content_type,
                        "size": len(contents),
                        "data": file_metadata,
                    },
                }
            ),
            db=db,
        )

        if "channel_id" in file_metadata:
            channel = Channels.get_channel_by_id_and_user_id(
                file_metadata["channel_id"], user.id, db=db
            )
            if channel:
                Channels.add_file_to_channel_by_id(
                    channel.id, file_item.id, user.id, db=db
                )

        # EARLY BINDING: Link to Knowledge Base immediately if knowledge_id is provided
        if knowledge_base_id:
            log.info(f"Early-binding file {file_item.id} to knowledge base {knowledge_base_id}")
            try:
                from open_webui.models.knowledge import Knowledges
                
                # Set initial rag_status to 'processing' so it appears in Processing tab
                Files.update_file_data_by_id(
                    file_item.id,
                    {
                        "rag_status": "processing",  # Mark as processing for UI
                        "knowledge_id": knowledge_base_id  # Store KB association
                    },
                    db=db
                )
                
                Knowledges.add_file_to_knowledge_by_id(
                    knowledge_id=knowledge_base_id,
                    file_id=file_item.id,
                    user_id=user.id,
                    db=db
                )
            except Exception as e:
                # If early binding fails, fail the upload entirely to prevent orphans
                log.error(f"Failed to early-bind file to KB: {e}")
                Files.delete_file_by_id(file_item.id, db=db)
                Storage.delete_file(file_path)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to link file to Knowledge Base: {e}"
                )

        if knowledge_base_id:
            log.info(f"Checking if KB '{knowledge_base_id}' is linked to LightRAG")
            lightrag_config = GroupLightragConfigs.get_config_by_knowledge_base_id(
                knowledge_base_id, db=db
            )
        
        # LightRAG Blocking Upload Path
        if lightrag_config and lightrag_config.lightrag_url and lightrag_config.lightrag_workspace_name:
            log.info(
                f"[LightRAG] KB '{knowledge_base_id}' is linked to LightRAG. "
                f"Starting blocking upload workflow for file '{name}'"
            )
            
            try:
                # Initialize LightRAG client
                lightrag_client = LightRAGClient(
                    base_url=lightrag_config.lightrag_url,
                    workspace=lightrag_config.lightrag_workspace_name,
                )
                
                # Blocking upload and poll (this will wait until processing completes)
                # This is an async call in a sync function, so we need to run it
                import asyncio
                
                result = asyncio.run(
                    lightrag_client.upload_and_poll(
                        file_content=contents,
                        filename=name,
                        content_type=file.content_type or "application/octet-stream",
                        timeout_seconds=300,
                        poll_interval=2.0,
                    )
                )
                
                # Success! Update file metadata with RAG status
                log.info(
                    f"[LightRAG] File processed successfully. doc_id={result.get('doc_id')}, "
                    f"processing_time={result.get('processing_time'):.1f}s"
                )
                
                # Extract full text content for UI display
                try:
                    from open_webui.retrieval.loaders.main import Loader
                    
                    loader = Loader(
                        engine=request.app.state.config.CONTENT_EXTRACTION_ENGINE,
                        TIKA_SERVER_URL=request.app.state.config.TIKA_SERVER_URL,
                        DATALAB_MARKER_API_KEY=request.app.state.config.DATALAB_MARKER_API_KEY,
                        DATALAB_MARKER_API_BASE_URL=request.app.state.config.DATALAB_MARKER_API_BASE_URL,
                        DATALAB_MARKER_ADDITIONAL_CONFIG=request.app.state.config.DATALAB_MARKER_ADDITIONAL_CONFIG,
                        DATALAB_MARKER_SKIP_CACHE=request.app.state.config.DATALAB_MARKER_SKIP_CACHE,
                        DATALAB_MARKER_FORCE_OCR=request.app.state.config.DATALAB_MARKER_FORCE_OCR,
                        DATALAB_MARKER_PAGINATE=request.app.state.config.DATALAB_MARKER_PAGINATE,
                        DATALAB_MARKER_STRIP_EXISTING_OCR=request.app.state.config.DATALAB_MARKER_STRIP_EXISTING_OCR,
                        DATALAB_MARKER_DISABLE_IMAGE_EXTRACTION=request.app.state.config.DATALAB_MARKER_DISABLE_IMAGE_EXTRACTION,
                        DATALAB_MARKER_FORMAT_LINES=request.app.state.config.DATALAB_MARKER_FORMAT_LINES,
                        DATALAB_MARKER_USE_LLM=request.app.state.config.DATALAB_MARKER_USE_LLM,
                        DATALAB_MARKER_OUTPUT_FORMAT=request.app.state.config.DATALAB_MARKER_OUTPUT_FORMAT,
                        EXTERNAL_DOCUMENT_LOADER_URL=request.app.state.config.EXTERNAL_DOCUMENT_LOADER_URL,
                        EXTERNAL_DOCUMENT_LOADER_API_KEY=request.app.state.config.EXTERNAL_DOCUMENT_LOADER_API_KEY,
                        DOCLING_SERVER_URL=request.app.state.config.DOCLING_SERVER_URL,
                        DOCLING_API_KEY=request.app.state.config.DOCLING_API_KEY,
                        DOCLING_PARAMS=request.app.state.config.DOCLING_PARAMS,
                        DOCUMENT_INTELLIGENCE_ENDPOINT=request.app.state.config.DOCUMENT_INTELLIGENCE_ENDPOINT,
                        DOCUMENT_INTELLIGENCE_KEY=request.app.state.config.DOCUMENT_INTELLIGENCE_KEY,
                        DOCUMENT_INTELLIGENCE_MODEL=request.app.state.config.DOCUMENT_INTELLIGENCE_MODEL,
                        MINERU_API_MODE=request.app.state.config.MINERU_API_MODE,
                        MINERU_API_URL=request.app.state.config.MINERU_API_URL,
                        MINERU_API_KEY=request.app.state.config.MINERU_API_KEY,
                        MINERU_API_TIMEOUT=request.app.state.config.MINERU_API_TIMEOUT,
                        MINERU_PARAMS=request.app.state.config.MINERU_PARAMS,
                        MISTRAL_OCR_API_BASE_URL=request.app.state.config.MISTRAL_OCR_API_BASE_URL,
                        MISTRAL_OCR_API_KEY=request.app.state.config.MISTRAL_OCR_API_KEY,
                        PDF_EXTRACT_IMAGES=request.app.state.config.PDF_EXTRACT_IMAGES,
                        user=user,
                    )
                    
                    # Extract text from file
                    docs = loader.load(
                        filename=name,
                        file_content_type=file.content_type or "application/octet-stream",
                        file_path=Storage.get_file(file_path)
                    )
                    
                    # Combine all extracted text
                    extracted_content = "\n\n".join([doc.page_content for doc in docs])
                    log.info(f"[LightRAG] Extracted {len(extracted_content)} characters of text content")
                    
                except Exception as e:
                    log.warning(f"[LightRAG] Failed to extract text content: {e}")
                    # Fallback to summary or default message
                    extracted_content = result.get("summary") or "Processed by LightRAG. Content indexed in Graph."
                
                Files.update_file_data_by_id(
                    file_item.id,
                    {
                        "status": "completed",
                        "rag_status": "processed",
                        "rag_id": result.get("doc_id"),
                        "track_id": result.get("track_id"),
                        "processing_time": result.get("processing_time"),
                        "content": extracted_content,  # Store full extracted text for UI display
                    },
                    db=db,
                )
                
                # Note: We don't need to link here anymore because we did it via Early Binding
                # The `knowledge_id` parameter is used for early binding, and LightRAG handles its own internal linking.
                
                # Return success immediately (no background processing needed)
                return {
                    "status": True,
                    "lightrag_processed": True,
                    **file_item.model_dump()
                }
                
            except LightRAGClientError as e:
                # LightRAG processing failed - ROLLBACK
                log.error(
                    f"[LightRAG] Processing failed for file '{name}': {e}. "
                    f"Rolling back (deleting file from DB and disk)"
                )
                
                try:
                    # Delete from database
                    Files.delete_file_by_id(file_item.id, db=db)
                    log.info(f"[LightRAG Rollback] Deleted file from database: {file_item.id}")
                    
                    # Delete from disk
                    Storage.delete_file(file_path)
                    log.info(f"[LightRAG Rollback] Deleted file from disk: {file_path}")

                    # If early-bound, remove from KB
                    if knowledge_id:
                        from open_webui.models.knowledge import Knowledges
                        Knowledges.remove_file_from_knowledge_by_id(knowledge_id, file_item.id, db=db)
                        log.info(f"[LightRAG Rollback] Removed file {file_item.id} from KB {knowledge_id}")
                    
                except Exception as rollback_error:
                    log.error(f"[LightRAG Rollback] Error during rollback: {rollback_error}")
                
                # Return error to user
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"LightRAG processing failed: {str(e)}. File has been removed.",
                )
            
            except Exception as e:
                # Unexpected error (e.g., timeout, network) - ROLLBACK
                log.error(
                    f"[LightRAG] Unexpected error during upload for '{name}': {e}. "
                    f"Rolling back (deleting file)"
                )
                
                try:
                    Files.delete_file_by_id(file_item.id, db=db)
                    Storage.delete_file(file_path)
                    log.info(f"[LightRAG Rollback] Deleted file: {file_item.id}")

                    # If early-bound, remove from KB
                    if knowledge_id:
                        from open_webui.models.knowledge import Knowledges
                        Knowledges.remove_file_from_knowledge_by_id(knowledge_id, file_item.id, db=db)
                        log.info(f"[LightRAG Rollback] Removed file {file_item.id} from KB {knowledge_id}")

                except Exception as rollback_error:
                    log.error(f"[LightRAG Rollback] Error during rollback: {rollback_error}")
                
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Upload failed: {str(e)}. File has been removed.",
                )

        if process:
            if background_tasks and process_in_background:
                background_tasks.add_task(
                    process_uploaded_file,
                    request,
                    file,
                    file_path,
                    file_item,
                    file_metadata,
                    user,
                )
                return {"status": True, **file_item.model_dump()}
            else:
                process_uploaded_file(
                    request,
                    file,
                    file_path,
                    file_item,
                    file_metadata,
                    user,
                    db=db,
                )
                return {"status": True, **file_item.model_dump()}
        else:
            if file_item:
                return file_item
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT("Error uploading file"),
                )

    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Error uploading file"),
        )


############################
# List Files
############################


@router.get("/", response_model=list[FileModelResponse])
async def list_files(
    user=Depends(get_verified_user),
    content: bool = Query(True),
    db: Session = Depends(get_session),
):
    if user.role == "admin":
        files = Files.get_files(db=db)
    else:
        files = Files.get_files_by_user_id(user.id, db=db)

    if not content:
        for file in files:
            if "content" in file.data:
                del file.data["content"]

    return files


############################
# Search Files
############################


@router.get("/search", response_model=list[FileModelResponse])
async def search_files(
    filename: str = Query(
        ...,
        description="Filename pattern to search for. Supports wildcards such as '*.txt'",
    ),
    content: bool = Query(True),
    skip: int = Query(0, ge=0, description="Number of files to skip"),
    limit: int = Query(
        100, ge=1, le=1000, description="Maximum number of files to return"
    ),
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    """
    Search for files by filename with support for wildcard patterns.
    Uses SQL-based filtering with pagination for better performance.
    """
    # Determine user_id: null for admin (search all), user.id for regular users
    user_id = None if user.role == "admin" else user.id

    # Use optimized database query with pagination
    files = Files.search_files(
        user_id=user_id,
        filename=filename,
        skip=skip,
        limit=limit,
        db=db,
    )

    if not files:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No files found matching the pattern.",
        )

    if not content:
        for file in files:
            if file.data and "content" in file.data:
                del file.data["content"]

    return files


############################
# Delete All Files
############################


@router.delete("/all")
async def delete_all_files(
    user=Depends(get_admin_user), db: Session = Depends(get_session)
):
    result = Files.delete_all_files(db=db)
    if result:
        try:
            Storage.delete_all_files()
            VECTOR_DB_CLIENT.reset()
        except Exception as e:
            log.exception(e)
            log.error("Error deleting files")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error deleting files"),
            )
        return {"message": "All files deleted successfully"}
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Error deleting files"),
        )


############################
# Get File By Id
############################


@router.get("/{id}", response_model=Optional[FileModel])
async def get_file_by_id(
    id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    file = Files.get_file_by_id(id, db=db)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        file.user_id == user.id
        or user.role == "admin"
        or has_access_to_file(id, "read", user, db=db)
    ):
        return file
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


@router.get("/{id}/process/status")
async def get_file_process_status(
    id: str,
    stream: bool = Query(False),
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    file = Files.get_file_by_id(id, db=db)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        file.user_id == user.id
        or user.role == "admin"
        or has_access_to_file(id, "read", user, db=db)
    ):
        if stream:
            MAX_FILE_PROCESSING_DURATION = 3600 * 2

            async def event_stream(file_id):
                # NOTE: We intentionally do NOT capture the request's db session here.
                # Each poll creates its own short-lived session to avoid holding a
                # connection for hours. A WebSocket push would be more efficient.
                for _ in range(MAX_FILE_PROCESSING_DURATION):
                    file_item = Files.get_file_by_id(file_id)  # Creates own session
                    if file_item:
                        data = file_item.model_dump().get("data", {})
                        status = data.get("status")

                        if status:
                            event = {"status": status}
                            if status == "failed":
                                event["error"] = data.get("error")

                            yield f"data: {json.dumps(event)}\n\n"
                            if status in ("completed", "failed"):
                                break
                        else:
                            # Legacy
                            break
                    else:
                        yield f"data: {json.dumps({'status': 'not_found'})}\n\n"
                        break

                    await asyncio.sleep(1)

            return StreamingResponse(
                event_stream(file.id),
                media_type="text/event-stream",
            )
        else:
            return {"status": file.data.get("status", "pending")}
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# Get File Data Content By Id
############################


@router.get("/{id}/data/content")
async def get_file_data_content_by_id(
    id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    file = Files.get_file_by_id(id, db=db)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        file.user_id == user.id
        or user.role == "admin"
        or has_access_to_file(id, "read", user, db=db)
    ):
        return {"content": file.data.get("content", "")}
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# Update File Data Content By Id
############################


class ContentForm(BaseModel):
    content: str


@router.post("/{id}/data/content/update")
async def update_file_data_content_by_id(
    request: Request,
    id: str,
    form_data: ContentForm,
    user=Depends(get_verified_user),
    db: Session = Depends(get_session),
):
    file = Files.get_file_by_id(id, db=db)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        file.user_id == user.id
        or user.role == "admin"
        or has_access_to_file(id, "write", user, db=db)
    ):
        try:
            process_file(
                request,
                ProcessFileForm(file_id=id, content=form_data.content),
                user=user,
            )
            file = Files.get_file_by_id(id=id, db=db)
        except Exception as e:
            log.exception(e)
            log.error(f"Error processing file: {file.id}")

        return {"content": file.data.get("content", "")}
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# Get File Content By Id
############################


@router.get("/{id}/content")
async def get_file_content_by_id(
    id: str,
    user=Depends(get_verified_user),
    attachment: bool = Query(False),
    db: Session = Depends(get_session),
):
    file = Files.get_file_by_id(id, db=db)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        file.user_id == user.id
        or user.role == "admin"
        or has_access_to_file(id, "read", user, db=db)
    ):
        try:
            file_path = Storage.get_file(file.path)
            file_path = Path(file_path)

            # Check if the file already exists in the cache
            if file_path.is_file():
                # Handle Unicode filenames
                filename = file.meta.get("name", file.filename)
                encoded_filename = quote(filename)  # RFC5987 encoding

                content_type = file.meta.get("content_type")
                filename = file.meta.get("name", file.filename)
                encoded_filename = quote(filename)
                headers = {}

                if attachment:
                    headers["Content-Disposition"] = (
                        f"attachment; filename*=UTF-8''{encoded_filename}"
                    )
                else:
                    if content_type == "application/pdf" or filename.lower().endswith(
                        ".pdf"
                    ):
                        headers["Content-Disposition"] = (
                            f"inline; filename*=UTF-8''{encoded_filename}"
                        )
                        content_type = "application/pdf"
                    elif content_type != "text/plain":
                        headers["Content-Disposition"] = (
                            f"attachment; filename*=UTF-8''{encoded_filename}"
                        )

                return FileResponse(file_path, headers=headers, media_type=content_type)

            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=ERROR_MESSAGES.NOT_FOUND,
                )
        except Exception as e:
            log.exception(e)
            log.error("Error getting file content")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error getting file content"),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


@router.get("/{id}/content/html")
async def get_html_file_content_by_id(
    id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    file = Files.get_file_by_id(id, db=db)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    file_user = Users.get_user_by_id(file.user_id, db=db)
    if not file_user.role == "admin":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        file.user_id == user.id
        or user.role == "admin"
        or has_access_to_file(id, "read", user, db=db)
    ):
        try:
            file_path = Storage.get_file(file.path)
            file_path = Path(file_path)

            # Check if the file already exists in the cache
            if file_path.is_file():
                log.info(f"file_path: {file_path}")
                return FileResponse(file_path)
            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=ERROR_MESSAGES.NOT_FOUND,
                )
        except Exception as e:
            log.exception(e)
            log.error("Error getting file content")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error getting file content"),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


@router.get("/{id}/content/{file_name}")
async def get_file_content_by_id(
    id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    file = Files.get_file_by_id(id, db=db)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        file.user_id == user.id
        or user.role == "admin"
        or has_access_to_file(id, "read", user, db=db)
    ):
        file_path = file.path

        # Handle Unicode filenames
        filename = file.meta.get("name", file.filename)
        encoded_filename = quote(filename)  # RFC5987 encoding
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        }

        if file_path:
            file_path = Storage.get_file(file_path)
            file_path = Path(file_path)

            # Check if the file already exists in the cache
            if file_path.is_file():
                return FileResponse(file_path, headers=headers)
            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=ERROR_MESSAGES.NOT_FOUND,
                )
        else:
            # File path doesn’t exist, return the content as .txt if possible
            file_content = file.content.get("content", "")
            file_name = file.filename

            # Create a generator that encodes the file content
            def generator():
                yield file_content.encode("utf-8")

            return StreamingResponse(
                generator(),
                media_type="text/plain",
                headers=headers,
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# Delete File By Id
############################


@router.delete("/{id}")
async def delete_file_by_id(
    id: str, user=Depends(get_verified_user), db: Session = Depends(get_session)
):
    file = Files.get_file_by_id(id, db=db)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        file.user_id == user.id
        or user.role == "admin"
        or has_access_to_file(id, "write", user, db=db)
    ):
        # Check if this file was processed by LightRAG
        is_lightrag_file = False
        lightrag_doc_id = None
        
        if file.data:
            lightrag_doc_id = file.data.get("rag_id")
            is_lightrag_file = lightrag_doc_id is not None
        log.info(f"[File Delete] Detected LightRAG file: file_id={id}, file={file}")
        # Delete from LightRAG if applicable
        if is_lightrag_file:
            log.info(f"[File Delete] Detected LightRAG file: file_id={id}, rag_id={lightrag_doc_id}")
            
            try:
                # Get Knowledge Base configuration
                knowledge_base_id = file.meta.get("data", {}).get("knowledge_id") if file.meta else None
                
                if knowledge_base_id:
                    lightrag_config = GroupLightragConfigs.get_config_by_knowledge_base_id(
                        knowledge_base_id, db=db
                    )
                    
                    if lightrag_config and lightrag_config.lightrag_url:
                        # Initialize LightRAG client
                        lightrag_client = LightRAGClient(
                            base_url=lightrag_config.lightrag_url,
                            workspace=lightrag_config.lightrag_workspace_name,
                        )
                        
                        # Delete from LightRAG (best-effort, non-blocking)
                        delete_success = await lightrag_client.delete_document(
                            doc_id=lightrag_doc_id,
                            delete_llm_cache=True,
                        )
                        
                        if not delete_success:
                            log.warning(
                                f"[File Delete] Failed to delete from LightRAG "
                                f"(file_id={id}, rag_id={lightrag_doc_id}). "
                                f"Continuing with local deletion."
                            )
                        else:
                            log.info(
                                f"[File Delete] Successfully deleted from LightRAG "
                                f"(file_id={id}, rag_id={lightrag_doc_id})"
                            )
                    else:
                        log.warning(
                            f"[File Delete] No LightRAG config found for knowledge_base_id={knowledge_base_id}"
                        )
                else:
                    log.warning(f"[File Delete] No knowledge_base_id found in file metadata")
                    
            except Exception as e:
                # Log error but proceed with local deletion (best-effort)
                log.error(
                    f"[File Delete] Error while deleting from LightRAG "
                    f"(file_id={id}, rag_id={lightrag_doc_id}): {e}"
                )
        
        result = Files.delete_file_by_id(id, db=db)
        if result:
            try:
                Storage.delete_file(file.path)
                VECTOR_DB_CLIENT.delete(collection_name=f"file-{id}")
            except Exception as e:
                log.exception(e)
                log.error("Error deleting files")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT("Error deleting files"),
                )
            return {"message": "File deleted successfully"}
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error deleting file"),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
