# cache.py
import logging
import os
import pickle
import tempfile
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Directory for the pickle cache files. Configurable via env so it can point at a
# writable location in read-only / non-root container runtimes (e.g. Bedrock
# AgentCore, where the baked-in /app/cache is root-owned and not writable).
CACHE_PATH = os.environ.get("CACHE_PATH", "cache")


def _resolve_writable_cache_dir() -> str:
    """Return CACHE_PATH if writable, otherwise fall back to a temp dir.

    Bedrock AgentCore runs the container as a non-root user while /app and its
    contents are root-owned, so writing cache/<name>.pkl raises PermissionError.
    Probing for writability (rather than just checking existence) handles the case
    where the directory exists but is read-only.
    """
    candidate = CACHE_PATH
    try:
        os.makedirs(candidate, exist_ok=True)
        probe = os.path.join(candidate, ".write_test")
        with open(probe, "w") as handle:
            handle.write("ok")
        os.remove(probe)
        return candidate
    except OSError:
        fallback = os.path.join(tempfile.gettempdir(), "tfsa_cache")
        os.makedirs(fallback, exist_ok=True)
        logging.warning(
            "Cache dir %r is not writable; falling back to %r", candidate, fallback
        )
        return fallback

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
        cache_dir = _resolve_writable_cache_dir()
        self.cache_file_path = os.path.join(cache_dir, f"{cache_name}.pkl")
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

    def cache(self, unique_key, data_to_cache, timeout=18000, metadata=None, expires_at=None):
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

            # Determine expiration time
            if expires_at is None:
                expiration_timestamp = time.time() + timeout
            else:
                expiration_timestamp = expires_at

            # Create cache item with metadata
            cache_item = {
                "value": data_to_cache,
                "expires_at": expiration_timestamp,
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

        # Remove expired items first
        self.remove_expired()

        with thread_locks[self.cache_name]:
            if os.path.exists(self.cache_file_path):
                with open(self.cache_file_path, 'rb') as handle:
                    self.cache_dict = pickle.load(handle)

                # Check if key exists in current cache
                if unique_key in self.cache_dict:
                    # Check if item is expired
                    item = self.cache_dict[unique_key]
                    expires_at = item.get('expires_at', 0)
                    if 0 < expires_at < time.time():
                        # Item is expired, remove it
                        del self.cache_dict[unique_key]
                        with open(self.cache_file_path, 'wb+') as handle:
                            pickle.dump(self.cache_dict, handle, protocol=pickle.HIGHEST_PROTOCOL)
                        return None

                    return item
            return None  # Return None instead of raising exception

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
