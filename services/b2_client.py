"""Backblaze B2 client"""
import asyncio
import logging
from b2sdk.v2 import B2Api, InMemoryAccountInfo

logger = logging.getLogger(__name__)


class B2Client:
    """Client for downloading files from Backblaze B2"""
    
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
    
    async def download_file(self, bucket_name: str, file_path: str, local_path: str):
        """Download a file from B2 to local path"""
        def _download():
            api = self._get_api()
            bucket = api.get_bucket_by_name(bucket_name)
            
            # Download file
            downloaded_file = bucket.download_file_by_name(file_path)
            downloaded_file.save_to(local_path)
            
            logger.info(f"Downloaded {file_path} from bucket {bucket_name}")
        
        # Run in thread pool to avoid blocking
        await asyncio.get_event_loop().run_in_executor(None, _download)
