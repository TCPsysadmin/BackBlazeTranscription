"""Webhook callback client"""
import asyncio
import logging
import httpx

logger = logging.getLogger(__name__)


class WebhookClient:
    """Handles webhook callbacks with retry logic"""
    
    MAX_RETRIES = 3
    
    async def send_callback(self, callback_url: str, payload: dict):
        """Send webhook callback with retry logic"""
        retry_count = 0
        
        while retry_count < self.MAX_RETRIES:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(callback_url, json=payload)
                    response.raise_for_status()
                    
                logger.info(f"Webhook sent successfully to {callback_url}")
                return
                
            except Exception as e:
                retry_count += 1
                logger.warning(f"Webhook failed (attempt {retry_count}/{self.MAX_RETRIES}): {e}")
                
                if retry_count < self.MAX_RETRIES:
                    await asyncio.sleep(2 ** retry_count)
                else:
                    logger.error(f"Webhook failed after {self.MAX_RETRIES} attempts")
                    raise
