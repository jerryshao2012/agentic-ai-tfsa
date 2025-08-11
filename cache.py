# cache.py
import logging
import os
import pickle
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

CACHE_PATH = "cache"

thread_locks = {}


# Cache class to store data in a pickle file for later retrieval.
class Cache(object):
    # Global lock for each cache prevents multiple threads from updating the same cache
    cache_manager = {}
    cache_dict = {}
    CACHE_ENABLED = True  # Default to enabled

    def __init__(self):
        raise RuntimeError('Call Cache.instance(cache_name) instead')

    @classmethod
    def instance(cls, cache_name):

        # Create single instance of Cache for each cache_name
        if cache_name not in thread_locks:
            # Need global lock for each cache
            thread_locks[cache_name] = threading.Lock()

            # Acquire thread lock to prevent updates to dictionary when multiple threads using same cache()
            with thread_locks[cache_name]:
                cache = cls.__new__(cls)
                cache._init_cache(cache_name)
                cls.cache_manager[cache_name] = cache

        with thread_locks[cache_name]:
            return cls.cache_manager[cache_name]

    # private method.  Should not be called directly
    def _init_cache(self, cache_name: str):

        self.cache_name = cache_name
        if not os.path.exists(CACHE_PATH):
            os.makedirs(CACHE_PATH)
        self.cache_file_path = os.path.join(CACHE_PATH, f"{cache_name}.pkl")
        if os.path.exists(self.cache_file_path):
            with open(self.cache_file_path, 'rb') as handle:
                self.cache_dict = pickle.load(handle)
        else:
            self.cache_dict = {}
        logging.info(f"{cache_name} cache loaded with {len(self.cache_dict)} items")

    @classmethod
    def set_enabled(cls, enabled: bool):
        """Set cache enabled state"""
        cls.CACHE_ENABLED = enabled

    @classmethod
    def is_enabled(cls):
        """Check if cache is enabled"""
        return cls.CACHE_ENABLED

    def contains(self, unique_key):
        # Acquire thread lock to prevent updates to dictionary when multiple threads using same cache()
        if not self.CACHE_ENABLED:
            return False
        with thread_locks[self.cache_name]:
            if os.path.exists(self.cache_file_path):
                with open(self.cache_file_path, 'rb') as handle:
                    self.cache_dict = pickle.load(handle)
                    return unique_key in self.cache_dict
            return False

    def cache(self, unique_key, data_to_cache, timeout=18000, metadata=None):
        if not self.CACHE_ENABLED:
            return
        if metadata is None:
            metadata = {}

        with thread_locks[self.cache_name]:
            if os.path.exists(self.cache_file_path):
                with open(self.cache_file_path, 'rb') as handle:
                    self.cache_dict = pickle.load(handle)
            else:
                self.cache_dict = {}

            # Create cache item with metadata
            cache_item = {
                "value": data_to_cache,
                "expires_at": time.time() + timeout,
                "metadata": metadata
            }
            self.cache_dict[unique_key] = cache_item

            with open(self.cache_file_path, 'wb+') as handle:
                pickle.dump(self.cache_dict, handle, protocol=pickle.HIGHEST_PROTOCOL)

    # Call the contains() method prior to calling this method to ensure the key/value exists
    def load_from_cache(self, unique_key):
        if not self.CACHE_ENABLED:
            logging.warning("Cache is disabled")
            return None
        self.remove_expired()
        with thread_locks[self.cache_name]:
            if os.path.exists(self.cache_file_path):
                with open(self.cache_file_path, 'rb') as handle:
                    self.cache_dict = pickle.load(handle)
                    if unique_key in self.cache_dict:
                        return self.cache_dict[unique_key]
            raise Exception(
                f"Error: {unique_key} not found. Call Cache.contains() prior to calling Cache.load_from_cache()")

    # Add these methods to the Cache class
    def get_all(self) -> dict:
        """Return entire cache dictionary"""
        if not self.CACHE_ENABLED:
            return {}
        self.remove_expired()
        with thread_locks[self.cache_name]:
            if os.path.exists(self.cache_file_path):
                with open(self.cache_file_path, 'rb') as handle:
                    cache_dict = pickle.load(handle)
                # Handle old format items (without metadata)
                for key, item in cache_dict.items():
                    if not isinstance(item, dict) or 'expires_at' not in item:
                        cache_dict[key] = {
                            "value": item,
                            "expires_at": 0,  # Mark as expired
                            "metadata": {}
                        }
                return cache_dict
            return {}

    def delete(self, unique_key: str) -> bool:
        """Remove an item from the cache"""
        if not self.CACHE_ENABLED:
            return False
        with thread_locks[self.cache_name]:
            if os.path.exists(self.cache_file_path):
                with open(self.cache_file_path, 'rb') as handle:
                    self.cache_dict = pickle.load(handle)
                    if unique_key in self.cache_dict:
                        del self.cache_dict[unique_key]
                        with open(self.cache_file_path, 'wb+') as handle1:
                            pickle.dump(self.cache_dict, handle1, protocol=pickle.HIGHEST_PROTOCOL)
                        return True
            return False

    def remove_expired(self):
        """Remove expired items from cache and return remaining items"""
        if not self.CACHE_ENABLED:
            return {}

        current_time = time.time()
        with thread_locks[self.cache_name]:
            if os.path.exists(self.cache_file_path):
                with open(self.cache_file_path, 'rb') as handle:
                    self.cache_dict = pickle.load(handle)
            else:
                return {}

            # Handle old format items
            updated = False
            for key, item in list(self.cache_dict.items()):
                if not isinstance(item, dict) or 'expires_at' not in item:
                    # Convert to new format
                    self.cache_dict[key] = {
                        "value": item,
                        "expires_at": 0,
                        "metadata": {}
                    }
                    updated = True

                # Remove expired items
                expires_at = self.cache_dict[key]['expires_at']
                if 0 < expires_at < current_time:
                    del self.cache_dict[key]
                    updated = True

            if updated:
                with open(self.cache_file_path, 'wb+') as handle:
                    pickle.dump(self.cache_dict, handle, protocol=pickle.HIGHEST_PROTOCOL)

            return self.cache_dict
