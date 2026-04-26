
import os
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

# Initialize Object Storage client
try:
    from replit.object_storage import Client
    storage_client = Client()
    logger.info("✅ Object Storage client initialized")
except Exception as e:
    logger.error(f"❌ Failed to initialize Object Storage: {e}")
    storage_client = None

# ImgBB API configuration
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY", "")
IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"

def upload_screenshot(file_data: bytes, filename: str, submission_id: str) -> Optional[str]:
    """Upload screenshot to Object Storage"""
    if not storage_client:
        logger.error("❌ Object Storage client not available")
        return None
    
    try:
        # Create unique filename with submission_id
        # Clean filename to avoid issues
        import re
        clean_filename = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
        storage_filename = f"community_screenshots/{submission_id}_{clean_filename}"
        
        logger.info(f"📤 Uploading screenshot: {storage_filename} ({len(file_data)} bytes)")
        
        # Upload to Object Storage
        storage_client.upload_from_bytes(storage_filename, file_data)
        
        logger.info(f"✅ Uploaded screenshot: {storage_filename}")
        return storage_filename
        
    except Exception as e:
        logger.error(f"❌ Error uploading screenshot: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return None

def download_screenshot(filename: str) -> Optional[bytes]:
    """Download screenshot from Object Storage"""
    if not storage_client:
        logger.error("❌ Object Storage client not available")
        return None
    
    try:
        logger.info(f"📥 Downloading screenshot: {filename}")
        
        # Download from Object Storage
        file_data = storage_client.download_as_bytes(filename)
        
        logger.info(f"✅ Downloaded screenshot: {filename} ({len(file_data)} bytes)")
        return file_data
        
    except Exception as e:
        logger.error(f"❌ Error downloading screenshot: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return None

def get_screenshot_url(filename: str) -> Optional[str]:
    """Get public URL for screenshot"""
    if not storage_client:
        return None
    
    try:
        # For now, we'll serve through Flask endpoint
        # You can also use signed URLs if needed
        return f"/api/screenshot/{filename}"
    except Exception as e:
        logger.error(f"❌ Error getting screenshot URL: {e}")
        return None

def delete_screenshot(filename: str) -> bool:
    """Delete screenshot from Object Storage"""
    if not storage_client:
        return False
    
    try:
        storage_client.delete(filename)
        logger.info(f"✅ Deleted screenshot: {filename}")
        return True
    except Exception as e:
        logger.error(f"❌ Error deleting screenshot: {e}")
        return False

def download_screenshot(filename: str) -> Optional[bytes]:
    """Download screenshot from Object Storage"""
    if not storage_client:
        return None
    
    try:
        data = storage_client.download_as_bytes(filename)
        return data
    except Exception as e:
        logger.error(f"❌ Error downloading screenshot: {e}")
        return None

def upload_to_imgbb(file) -> dict:
    """
    Upload image to ImgBB and return the URL
    
    Args:
        file: FileStorage object from Flask request.files
        
    Returns:
        dict: {'success': bool, 'url': str, 'error': str}
    """
    if not IMGBB_API_KEY:
        logger.error("❌ IMGBB_API_KEY not configured")
        return {
            'success': False,
            'error': 'ImgBB API key not configured. Please set IMGBB_API_KEY in Secrets.'
        }
    
    try:
        # Validate file object
        if not file or not hasattr(file, 'read'):
            logger.error("❌ Invalid file object")
            return {
                'success': False,
                'error': 'Invalid file object provided'
            }
        
        # Reset file pointer and read data
        file.seek(0)
        file_data = file.read()
        
        # Validate file data
        if not file_data or len(file_data) == 0:
            logger.error("❌ File data is empty after read")
            return {
                'success': False,
                'error': 'File data is empty - please select a valid image file'
            }
        
        # Validate file size (max 32MB for ImgBB)
        if len(file_data) > 32 * 1024 * 1024:
            logger.error(f"❌ File too large: {len(file_data)} bytes")
            return {
                'success': False,
                'error': 'File size exceeds 32MB limit'
            }
        
        logger.info(f"📤 Uploading to ImgBB: {file.filename} ({len(file_data)} bytes)")
        
        # Encode to base64
        import base64
        encoded_image = base64.b64encode(file_data).decode('utf-8')
        
        # Prepare payload
        payload = {
            'key': IMGBB_API_KEY,
            'image': encoded_image,
            'name': file.filename or 'news_image'
        }
        
        # Upload to ImgBB with retries
        max_retries = 3
        last_error = "Unknown error"
        
        for attempt in range(max_retries):
            try:
                logger.info(f"📤 Uploading to ImgBB (Attempt {attempt + 1}/{max_retries}): {file.filename} ({len(file_data)} bytes)")
                
                # Use a slightly longer timeout and better connection handling
                response = requests.post(IMGBB_UPLOAD_URL, data=payload, timeout=60)
                
                logger.info(f"📥 ImgBB Response: {response.status_code}")
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get('success'):
                        image_url = result['data']['url']
                        logger.info(f"✅ Image uploaded: {image_url}")
                        return {
                            'success': True,
                            'url': image_url,
                            'delete_url': result['data'].get('delete_url'),
                            'display_url': result['data'].get('display_url')
                        }
                    else:
                        last_error = result.get('error', {}).get('message', 'Unknown API error')
                        logger.error(f"❌ ImgBB API error: {last_error}")
                else:
                    last_error = f"HTTP {response.status_code}"
                    logger.error(f"❌ ImgBB HTTP error: {last_error}")
                    
            except requests.exceptions.Timeout:
                last_error = "Upload timeout"
                logger.warning(f"⚠️ Attempt {attempt + 1} timed out")
            except Exception as e:
                last_error = str(e)
                logger.error(f"❌ Attempt {attempt + 1} failed: {e}")
            
            # Brief wait before retry if not the last attempt
            if attempt < max_retries - 1:
                import time
                time.sleep(2)
        
        return {
            'success': False,
            'error': f'Upload failed after {max_retries} attempts: {last_error}'
        }
    except Exception as e:
        logger.error(f"❌ Upload error: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return {
            'success': False,
            'error': f'Upload failed: {str(e)}'
        }
