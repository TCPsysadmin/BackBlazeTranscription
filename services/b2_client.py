"""Backblaze B2 client"""
import asyncio
import logging
import os
from b2sdk.v2 import B2Api, InMemoryAccountInfo
from b2sdk.exception import NonExistentBucket, FileNotPresent, B2Error

logger = logging.getLogger(__name__)


class B2DownloadError(Exception):
    """Custom exception for B2 download errors"""
    pass


class B2UploadError(Exception):
    """Custom exception for B2 upload errors"""
    pass


class B2Client:
    """Client for downloading and uploading files to Backblaze B2"""
    
    def __init__(self, key_id: str, app_key: str):
        self.key_id = key_id
        self.app_key = app_key
        self.api = None
    
    def _get_api(self):
        """Get or create B2 API instance"""
        if not self.api:
            info = InMemoryAccountInfo()
            self.api = B2Api(info)
            self.api.authorize_account("production", self.key_id, self.app_key)
        return self.api
    
    async def download_file(self, bucket_name: str, file_path: str, local_path: str, max_retries: int = 3):
        """Download a file from B2 to local path with retry logic"""
        retry_count = 0
        last_error = None
        
        while retry_count < max_retries:
            try:
                await self._download_file_once(bucket_name, file_path, local_path)
                return  # Success
            except B2DownloadError as e:
                last_error = e
                error_msg = str(e).lower()
                
                # Don't retry on certain errors
                if "bucket_not_found" in error_msg or "file_not_found" in error_msg:
                    logger.error(f"Non-retryable error downloading {file_path}: {e}")
                    raise
                
                retry_count += 1
                if retry_count < max_retries:
                    wait_time = 2 ** retry_count
                    logger.warning(
                        f"Download failed (attempt {retry_count}/{max_retries}), "
                        f"retrying in {wait_time}s: {e}"
                    )
                    await asyncio.sleep(wait_time)
                    
                    # Clean up partial download if it exists
                    try:
                        if os.path.exists(local_path):
                            os.remove(local_path)
                            logger.info(f"Removed partial download: {local_path}")
                    except Exception as cleanup_error:
                        logger.warning(f"Failed to clean up partial download: {cleanup_error}")
                else:
                    logger.error(f"Download failed after {max_retries} attempts: {last_error}")
                    raise last_error
    
    async def _download_file_once(self, bucket_name: str, file_path: str, local_path: str):
        """Download a file from B2 to local path (single attempt)"""
        def _download():
            try:
                api = self._get_api()
                bucket = api.get_bucket_by_name(bucket_name)
                
                # Download file
                downloaded_file = bucket.download_file_by_name(file_path)
                downloaded_file.save_to(local_path)
                
                logger.info(f"Downloaded {file_path} from bucket {bucket_name}")
                
            except NonExistentBucket as e:
                logger.error(f"Bucket not found: {bucket_name}")
                raise B2DownloadError(f"bucket_not_found: {bucket_name}")
            except FileNotPresent as e:
                logger.error(f"File not found: {file_path} in bucket {bucket_name}")
                raise B2DownloadError(f"file_not_found: {file_path}")
            except B2Error as e:
                logger.error(f"B2 error: {e}")
                raise B2DownloadError(f"b2_error: {str(e)}")
            except Exception as e:
                logger.error(f"Unexpected error downloading from B2: {e}")
                raise B2DownloadError(f"download_error: {str(e)}")
        
        # Run in thread pool to avoid blocking
        await asyncio.get_event_loop().run_in_executor(None, _download)
    
    async def upload_file(self, bucket_name: str, local_path: str, remote_path: str):
        """Upload a file from local path to B2"""
        def _upload():
            try:
                api = self._get_api()
                bucket = api.get_bucket_by_name(bucket_name)
                
                # Upload file
                bucket.upload_local_file(
                    local_file=local_path,
                    file_name=remote_path
                )
                
                logger.info(f"Uploaded {local_path} to {remote_path} in bucket {bucket_name}")
                
            except NonExistentBucket as e:
                logger.error(f"Bucket not found: {bucket_name}")
                raise B2UploadError(f"bucket_not_found: {bucket_name}")
            except B2Error as e:
                logger.error(f"B2 error: {e}")
                raise B2UploadError(f"b2_error: {str(e)}")
            except Exception as e:
                logger.error(f"Unexpected error uploading to B2: {e}")
                raise B2UploadError(f"upload_error: {str(e)}")
        
        # Run in thread pool to avoid blocking
        await asyncio.get_event_loop().run_in_executor(None, _upload)
