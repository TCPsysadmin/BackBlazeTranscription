"""Webhook callback client"""
import asyncio
import logging
import httpx

logger = logging.getLogger(__name__)


class WebhookClient:
    """Handles webhook callbacks with retry logic"""
    
    MAX_RETRIES = 3
    
    async def send_callback(self, callback_url: str, payload: dict) -> bool:
        """Send webhook callback with retry logic
        
        Returns:
            bool: True if webhook was delivered successfully, False otherwise
        """
        retry_count = 0
        last_error = None
        
        while retry_count < self.MAX_RETRIES:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(callback_url, json=payload)
                    response.raise_for_status()
                    
                logger.info(f"Webhook sent successfully to {callback_url}")
                return True
                
            except httpx.HTTPStatusError as e:
                retry_count += 1
                last_error = e
                logger.warning(
                    f"Webhook failed (attempt {retry_count}/{self.MAX_RETRIES}): "
                    f"HTTP {e.response.status_code} for {callback_url}"
                )
                
                # Don't retry client errors (4xx) except 408, 429
                if 400 <= e.response.status_code < 500 and e.response.status_code not in [408, 429]:
                    logger.error(f"Webhook failed with client error {e.response.status_code}, not retrying")
                    break
                
                if retry_count < self.MAX_RETRIES:
                    await asyncio.sleep(2 ** retry_count)
                    
            except (httpx.RequestError, httpx.TimeoutException) as e:
                retry_count += 1
                last_error = e
                logger.warning(
                    f"Webhook failed (attempt {retry_count}/{self.MAX_RETRIES}): "
                    f"Network error - {type(e).__name__}: {e}"
                )
                
                if retry_count < self.MAX_RETRIES:
                    await asyncio.sleep(2 ** retry_count)
                    
            except Exception as e:
                retry_count += 1
                last_error = e
                logger.warning(
                    f"Webhook failed (attempt {retry_count}/{self.MAX_RETRIES}): "
                    f"Unexpected error - {type(e).__name__}: {e}"
                )
                
                if retry_count < self.MAX_RETRIES:
                    await asyncio.sleep(2 ** retry_count)
        
        # All retries exhausted
        logger.error(
            f"Webhook failed after {self.MAX_RETRIES} attempts to {callback_url}. "
            f"Last error: {last_error}"
        )
        return False
