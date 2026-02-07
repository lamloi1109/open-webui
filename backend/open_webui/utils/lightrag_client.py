"""
LightRAG API Client
HTTP client for interacting with LightRAG server.
"""

import os
import logging
from typing import Optional, Any
import httpx

log = logging.getLogger(__name__)


class LightRAGClientError(Exception):
    """Raised when LightRAG API call fails."""
    pass


class LightRAGClient:
    """
    Async HTTP client for LightRAG API.
    
    All requests include the LIGHTRAG-WORKSPACE header for workspace isolation.
    """

    def __init__(
        self,
        base_url: str,
        workspace: str,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.workspace = workspace
        self.api_key = api_key or os.getenv("LIGHTRAG_API_KEY", "")
        self.timeout = timeout
        
        # Debug: print API key status (not the actual key)
        if self.api_key:
            print(f"[LightRAG] API key loaded: {self.api_key[:10]}...")
        else:
            print("[LightRAG] WARNING: No API key found in environment!")

    def _get_headers(self) -> dict:
        """Build request headers with workspace and auth."""
        headers = {}
        
        # Add workspace header if needed by your LightRAG version
        # headers["LIGHTRAG-WORKSPACE"] = self.workspace
        
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def check_health(self) -> bool:
        """
        Verify LightRAG server is reachable and workspace is accessible.
        
        Returns:
            True if server responds successfully.
            
        Raises:
            LightRAGClientError if connection fails.
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/health",
                    headers=self._get_headers(),
                )
                return response.status_code == 200
        except httpx.RequestError as e:
            log.error(f"LightRAG health check failed: {e}")
            raise LightRAGClientError(f"Failed to connect to LightRAG: {e}")

    async def upload_document(
        self,
        file_content: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> dict:
        """
        Upload a document to LightRAG.
        
        Args:
            file_content: Raw bytes of the file
            filename: Name of the file
            content_type: MIME type of the file
            
        Returns:
            Response JSON with status and track_id
        """
        try:
            log.info(f"Uploading {filename} ({len(file_content)} bytes) to {self.base_url}")
            
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # Try different endpoint formats for compatibility
                upload_endpoints = [
                    f"{self.base_url}/documents/upload",
                    f"{self.base_url}/api/v1/documents/upload",
                    f"{self.base_url}/upload",
                ]
                
                last_error = None
                for endpoint in upload_endpoints:
                    try:
                        files = {
                            "file": (filename, file_content, content_type),
                        }
                        response = await client.post(
                            endpoint,
                            headers=self._get_headers(),
                            files=files,
                        )
                        
                        log.info(f"Upload response from {endpoint}: status={response.status_code}")
                        
                        if response.status_code == 404:
                            continue  # Try next endpoint
                        
                        response.raise_for_status()
                        result = response.json()
                        
                        log.info(f"Upload result: {result}")
                        
                        # Handle different response formats from LightRAG
                        # Normalize response to always have track_id
                        if "track_id" not in result:
                            # Some versions use different keys
                            if "tracking_id" in result:
                                result["track_id"] = result["tracking_id"]
                            elif "id" in result:
                                result["track_id"] = result["id"]
                            elif "document_id" in result:
                                result["track_id"] = result["document_id"]
                        
                        return result
                        
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 404:
                            continue  # Try next endpoint
                        raise
                
                # If we get here, no endpoint worked
                raise LightRAGClientError(
                    f"Upload failed: No valid endpoint found. Tried: {', '.join(upload_endpoints)}"
                )
                        
        except httpx.HTTPStatusError as e:
            error_text = e.response.text if e.response else str(e)
            log.error(f"LightRAG upload failed: {error_text}")
            raise LightRAGClientError(f"Upload failed: {error_text}")
        except httpx.RequestError as e:
            log.error(f"LightRAG upload request failed: {e}")
            raise LightRAGClientError(f"Upload request failed: {e}")
        except Exception as e:
            log.exception(f"Unexpected error in upload: {e}")
            raise LightRAGClientError(f"Upload error: {e}")

    async def get_track_status(self, track_id: str) -> dict:
        """
        Get processing status for a track ID.
        
        Args:
            track_id: The tracking ID returned from upload
            
        Returns:
            Status response with documents and status_summary
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/documents/track_status/{track_id}",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            log.error(f"LightRAG track status failed: {e.response.text}")
            raise LightRAGClientError(f"Track status failed: {e.response.text}")
        except httpx.RequestError as e:
            log.error(f"LightRAG track status request failed: {e}")
            raise LightRAGClientError(f"Track status request failed: {e}")

    async def list_documents(
        self,
        page: int = 1,
        page_size: int = 50,
        status_filter: Optional[str] = None,
        sort_field: str = "updated_at",
        sort_direction: str = "desc",
    ) -> dict:
        """
        List documents with pagination.
        
        Args:
            page: Page number (1-based)
            page_size: Items per page
            status_filter: Filter by status (PENDING, PROCESSING, PROCESSED, FAILED)
            sort_field: Field to sort by
            sort_direction: asc or desc
            
        Returns:
            Paginated response with documents and pagination info
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                payload = {
                    "page": page,
                    "page_size": page_size,
                    "sort_field": sort_field,
                    "sort_direction": sort_direction,
                    "status_filter": "processed",
                }

                log.info(f"LightRAG list documents payload: {payload}, {self.base_url}") 

                response = await client.post(
                    f"{self.base_url}/documents/paginated",
                    headers={**self._get_headers(), "Content-Type": "application/json"},
                    json=payload,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            log.error(f"LightRAG list documents failed: {e.response.text}")
            raise LightRAGClientError(f"List documents failed: {e.response.text}")
        except httpx.RequestError as e:
            log.error(f"LightRAG list documents request failed: {e}")
            raise LightRAGClientError(f"List documents request failed: {e}")

    async def delete_documents(
        self,
        doc_ids: list[str],
        delete_file: bool = True,
        delete_llm_cache: bool = True,
    ) -> dict:
        """
        Delete documents by IDs.
        
        Args:
            doc_ids: List of document IDs to delete
            delete_file: Also delete the uploaded file
            delete_llm_cache: Also delete LLM cache for these docs
            
        Returns:
            Deletion result
        """
        try:

            log.info(f"LightRAG delete documents payload: {doc_ids}, {self.base_url}")  

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                payload = {
                    "doc_ids": doc_ids,
                    "delete_file": delete_file,
                    "delete_llm_cache": delete_llm_cache,
                }
                response = await client.request(
                    "DELETE",
                    f"{self.base_url}/documents/delete_document",
                    headers={**self._get_headers(), "Content-Type": "application/json"},
                    json=payload,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            log.error(f"LightRAG delete documents failed: {e.response.text}")
            raise LightRAGClientError(f"Delete documents failed: {e.response.text}")
        except httpx.RequestError as e:
            log.error(f"LightRAG delete documents request failed: {e}")
            raise LightRAGClientError(f"Delete documents request failed: {e}")

    async def clear_workspace(self) -> bool:
        """
        Clear all documents in the workspace.
        Used when deleting a group.
        
        Returns:
            True if successful
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/documents/clear",
                    headers={**self._get_headers(), "Content-Type": "application/json"},
                    json={},
                )
                response.raise_for_status()
                return True
        except httpx.HTTPStatusError as e:
            log.error(f"LightRAG clear workspace failed: {e.response.text}")
            raise LightRAGClientError(f"Clear workspace failed: {e.response.text}")
        except httpx.RequestError as e:
            log.error(f"LightRAG clear workspace request failed: {e}")
            raise LightRAGClientError(f"Clear workspace request failed: {e}")

    async def get_status_counts(self) -> dict:
        """
        Get document status counts for the workspace.
        
        Returns:
            Dict with status counts
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/documents/status_counts",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            log.error(f"LightRAG status counts failed: {e.response.text}")
            raise LightRAGClientError(f"Status counts failed: {e.response.text}")
        except httpx.RequestError as e:
            log.error(f"LightRAG status counts request failed: {e}")
            raise LightRAGClientError(f"Status counts request failed: {e}")

    async def cleanup_source_file(self, doc_id: str) -> bool:
        """
        Delete the physical source file from LightRAG while preserving the knowledge index.
        
        This optimization saves storage space on the LightRAG server since OpenWebUI
        maintains the master file copy. Only the processed knowledge graph/vectors are retained.
        
        Args:
            doc_id: The LightRAG document ID to cleanup
            
        Returns:
            True if cleanup succeeded, False otherwise (non-fatal)
        """
        try:
            log.info(f"Cleaning up source file for LightRAG doc_id: {doc_id}")
            
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                payload = {
                    "doc_ids": [doc_id],
                    "delete_file": True,          # Delete physical file (save storage)
                    "delete_llm_cache": False,    # Keep knowledge index (preserve intelligence)
                }
                
                response = await client.request(
                    "DELETE",
                    f"{self.base_url}/documents/delete_document",
                    headers={**self._get_headers(), "Content-Type": "application/json"},
                    json=payload,
                )
                response.raise_for_status()
                
                log.info(f"Successfully cleaned up source file for doc_id: {doc_id}")
                return True
                
        except Exception as e:
            # Cleanup failure is non-fatal - log warning but don't fail the upload
            log.warning(f"Failed to cleanup source file for doc_id {doc_id}: {e}")
            return False

    async def delete_document(
        self,
        doc_id: str,
        delete_llm_cache: bool = True,
    ) -> bool:
        """
        Delete a document from LightRAG (best-effort deletion).
        
        This is a best-effort operation that logs errors but does NOT raise
        exceptions to avoid blocking the local file deletion process.
        
        Args:
            doc_id: The LightRAG document ID to delete
            delete_llm_cache: If True, completely removes all associated data including
                              the knowledge graph (entities, relationships, vectors).
                              If False, keeps LLM cache which allows partial rebuilds.
                              Default: True for complete cleanup.
        
        Returns:
            bool: True if deletion succeeded, False if it failed (logged internally)
        """
        try:
            log.info(f"[LightRAG Delete] Deleting document: doc_id={doc_id}, delete_llm_cache={delete_llm_cache}")
            
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                payload = {
                    "doc_ids": [doc_id],
                    "delete_file": True,
                    "delete_llm_cache": delete_llm_cache,
                }
                
                response = await client.request(
                    "DELETE",
                    f"{self.base_url}/documents/delete_document",
                    headers={**self._get_headers(), "Content-Type": "application/json"},
                    json=payload,
                )
                
                response.raise_for_status()
                result = response.json()
                
                log.info(f"[LightRAG Delete] Successfully deleted doc_id={doc_id}")
                return True
                
        except httpx.HTTPStatusError as e:
            log.error(
                f"[LightRAG Delete] HTTP error deleting doc_id={doc_id}: "
                f"status={e.response.status_code}, response={e.response.text}"
            )
            return False
            
        except httpx.RequestError as e:
            log.error(f"[LightRAG Delete] Network error deleting doc_id={doc_id}: {e}")
            return False
            
        except Exception as e:
            log.exception(f"[LightRAG Delete] Unexpected error deleting doc_id={doc_id}: {e}")
            return False

    async def upload_and_poll(
        self,
        file_content: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
        timeout_seconds: int = 300,
        poll_interval: float = 2.0,
    ) -> dict:
        """
        Upload a document to LightRAG and poll until processing completes.
        
        This is a **blocking** operation that orchestrates:
        1. Upload file to LightRAG
        2. Poll status every 2 seconds until PROCESSED/FAILED
        3. Cleanup source file from LightRAG (keep index only)
        4. Return metadata
        
        Args:
            file_content: Raw bytes of the file
            filename: Name of the file
            content_type: MIME type of the file
            timeout_seconds: Maximum time to wait for processing (default: 300s = 5min)
            poll_interval: Time between status checks in seconds (default: 2s)
            
        Returns:
            LightRAG document metadata with processed status
            
        Raises:
            LightRAGClientError: If upload fails, processing fails, or timeout occurs
        """
        import asyncio
        import time
        
        # Step 1: Upload file to LightRAG
        log.info(f"[LightRAG Upload] Starting upload for {filename} ({len(file_content)} bytes)")
        upload_result = await self.upload_document(file_content, filename, content_type)
        
        track_id = upload_result.get("track_id")
        if not track_id:
            raise LightRAGClientError(
                f"Upload response missing track_id. Response: {upload_result}"
            )
        
        log.info(f"[LightRAG Upload] File uploaded successfully. track_id={track_id}")
        
        # Step 2: Poll for processing completion
        log.info(f"[LightRAG Poll] Starting polling loop (timeout={timeout_seconds}s)")
        start_time = time.time()
        poll_count = 0
        
        while True:
            elapsed = time.time() - start_time
            
            # Check timeout
            if elapsed > timeout_seconds:
                raise LightRAGClientError(
                    f"Timeout waiting for file processing. Waited {elapsed:.1f}s for track_id={track_id}"
                )
            
            # Get current status
            try:
                status_response = await self.get_track_status(track_id)
            except Exception as e:
                log.error(f"[LightRAG Poll] Failed to get status: {e}")
                raise LightRAGClientError(f"Failed to get track status: {e}")
            
            poll_count += 1
            log.info(f"[LightRAG Poll] Status Response: {status_response}")
            documents = status_response.get("documents", [])
            summary = status_response.get("status_summary", {})
            # log.info(f"[LightRAG Poll] Documents: {documents}")
            # log.info(f"[LightRAG Poll] Summary: {summary}")

            # Log progress every 5 polls
            if poll_count % 5 == 0:
                log.info(
                    f"[LightRAG Poll] Poll #{poll_count} ({elapsed:.1f}s): "
                    f"Summary={summary}"
                )
            
            # Check if all documents are no longer pending/processing
            pending_count = summary.get("pending", 0) + summary.get("processing", 0)
            
            if pending_count == 0:
                # Processing complete - check for success or failure
                processed_count = summary.get("processed", 0)
                failed_count = summary.get("failed", 0)
                
                if failed_count > 0:
                    # Find the failed document for error details
                    failed_docs = [
                        doc for doc in documents 
                        if doc.get("status", "").upper() == "FAILED"
                    ]
                    error_msg = "Unknown error"
                    if failed_docs:
                        error_msg = failed_docs[0].get("error", error_msg)
                    
                    raise LightRAGClientError(
                        f"LightRAG processing failed: {error_msg}"
                    )
                
                if processed_count > 0:
                    # Success! Find the processed document
                    processed_docs = [
                        doc for doc in documents 
                        if doc.get("status", "").upper() == "PROCESSED"
                    ]
                    
                    if not processed_docs:
                        raise LightRAGClientError(
                            "Status summary shows processed but no processed documents found"
                        )
                    
                    doc = processed_docs[0]
                    doc_id = doc.get("id")
                    
                    log.info(
                        f"[LightRAG Poll] Processing completed in {elapsed:.1f}s "
                        f"(polls={poll_count}, doc_id={doc_id})"
                    )
                    
                    # # Step 3: Cleanup source file (non-blocking, best-effort)
                    # if doc_id:
                    #     await self.cleanup_source_file(doc_id)
                    # else:
                    #     log.warning("[LightRAG Cleanup] No doc_id found, skipping cleanup")
                    
                    # Return metadata
                    return {
                        "track_id": track_id,
                        "doc_id": doc_id,
                        "status": "processed",
                        "document": doc,
                        "processing_time": elapsed,
                    }
                
                # # No processed, no failed - unexpected state
                # raise LightRAGClientError(
                #     f"Unexpected status summary: {summary}. No files processed or failed."
                # )
            
            # Still processing - wait before next poll
            await asyncio.sleep(poll_interval)



def get_lightrag_client(base_url: str, workspace: str) -> LightRAGClient:
    """Factory function to create a LightRAG client."""
    return LightRAGClient(base_url=base_url, workspace=workspace)
