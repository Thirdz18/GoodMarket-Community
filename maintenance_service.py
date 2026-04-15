from datetime import datetime
import logging
import json
import time
from supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

_MAINTENANCE_CACHE_TTL = 60  # seconds

class MaintenanceService:
    """Service for managing maintenance mode"""

    def __init__(self):
        self.supabase = get_supabase_client()
        self._status_cache: dict = {}
        self._all_settings_cache: tuple = (None, 0)
        logger.info("🔧 Maintenance Service initialized")

    def _invalidate_cache(self):
        """Invalidate all internal caches (call after any write operation)"""
        self._status_cache.clear()
        self._all_settings_cache = (None, 0)

    def get_maintenance_status(self, feature_name: str) -> dict:
        """Get maintenance status for a feature (cached for 60s)"""
        try:
            now = time.time()
            cached_entry = self._status_cache.get(feature_name)
            if cached_entry and now < cached_entry[1]:
                return cached_entry[0]

            if not self.supabase:
                self.supabase = get_supabase_client()
            
            if not self.supabase:
                return {'success': True, 'is_maintenance': False, 'message': ''}

            result = self.supabase.table('maintenance_settings')\
                .select('*')\
                .eq('feature_name', feature_name)\
                .execute()

            if result.data and len(result.data) > 0:
                data = {
                    'success': True,
                    'is_maintenance': result.data[0].get('is_maintenance', False),
                    'message': result.data[0].get('maintenance_message', 'Feature under maintenance')
                }
            else:
                data = {
                    'success': True,
                    'is_maintenance': False,
                    'message': ''
                }

            self._status_cache[feature_name] = (data, now + _MAINTENANCE_CACHE_TTL)
            return data
        except Exception as e:
            logger.error(f"❌ Error getting maintenance status: {e}")
            return {'success': False, 'is_maintenance': False, 'message': ''}

    def set_maintenance_status(self, feature_name: str, is_maintenance: bool, message: str, admin_wallet: str) -> dict:
        """Set maintenance status for a feature"""
        try:
            if not self.supabase:
                self.supabase = get_supabase_client()
            
            if not self.supabase:
                return {'success': False, 'error': 'Database connection not available'}

            # First, check if the record exists
            check = self.supabase.table('maintenance_settings')\
                .select('id')\
                .eq('feature_name', feature_name)\
                .execute()
            
            if not check.data:
                # Insert if not exists
                result = self.supabase.table('maintenance_settings')\
                    .insert({
                        'feature_name': feature_name,
                        'is_maintenance': is_maintenance,
                        'maintenance_message': message,
                        'updated_by': admin_wallet
                    })\
                    .execute()
            else:
                # Update if exists
                result = self.supabase.table('maintenance_settings')\
                    .update({
                        'is_maintenance': is_maintenance,
                        'maintenance_message': message,
                        'updated_by': admin_wallet,
                        'updated_at': datetime.now().isoformat()
                    }) \
                .eq('feature_name', feature_name) \
                .execute()

            if result.data:
                self._invalidate_cache()
                logger.info(f"✅ Maintenance mode {'enabled' if is_maintenance else 'disabled'} for {feature_name}")
                return {
                    'success': True,
                    'message': f"Maintenance mode {'enabled' if is_maintenance else 'disabled'} successfully"
                }
            else:
                return {
                    'success': False,
                    'error': 'Failed to update maintenance status'
                }
        except Exception as e:
            logger.error(f"❌ Error setting maintenance status: {e}")
            return {'success': False, 'error': str(e)}

    def get_all_maintenance_settings(self) -> dict:
        """Get all maintenance settings (cached for 60s)"""
        try:
            now = time.time()
            cached_data, expires_at = self._all_settings_cache
            if cached_data is not None and now < expires_at:
                return cached_data

            if not self.supabase:
                self.supabase = get_supabase_client()
                
            if not self.supabase:
                return {'success': False, 'settings': []}

            result = self.supabase.table('maintenance_settings')\
                .select('*')\
                .execute()

            data = {
                'success': True,
                'settings': result.data or []
            }
            self._all_settings_cache = (data, now + _MAINTENANCE_CACHE_TTL)
            return data
        except Exception as e:
            logger.error(f"❌ Error getting all maintenance settings: {e}")
            return {'success': False, 'settings': []}

# Global instance
maintenance_service = MaintenanceService()
