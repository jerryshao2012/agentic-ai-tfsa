import logging
import os
import pickle
import threading

logger = logging.getLogger("sle")

CACHE_PATH = "cache"

thread_locks = {}


# Cache class to store data in a pickle file for later retrieval.
class Cache(object):
    # Global lock for each cache prevents multiple threads from updating the same cache
    cache_manager = {}
    cache_dict = {}

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
        logger.info(f"{cache_name} cache loaded with {len(self.cache_dict)} items")

    def contains(self, unique_key):
        # Acquire thread lock to prevent updates to dictionary when multiple threads using same cache()
        with thread_locks[self.cache_name]:
            if os.path.exists(self.cache_file_path):
                with open(self.cache_file_path, 'rb') as handle:
                    self.cache_dict = pickle.load(handle)
                    return unique_key in self.cache_dict
            return False

    def cache(self, unique_key, data_to_cache):
        with thread_locks[self.cache_name]:
            if os.path.exists(self.cache_file_path):
                with open(self.cache_file_path, 'rb') as handle:
                    self.cache_dict = pickle.load(handle)
                    self.cache_dict[unique_key] = data_to_cache
            else:
                self.cache_dict[unique_key] = data_to_cache
            with open(self.cache_file_path, 'wb+') as handle:
                pickle.dump(self.cache_dict, handle, protocol=pickle.HIGHEST_PROTOCOL)

    # Call the contains() method prior to calling this method to ensure the key/value exists
    def load_from_cache(self, unique_key):
        with thread_locks[self.cache_name]:
            if os.path.exists(self.cache_file_path):
                with open(self.cache_file_path, 'rb') as handle:
                    self.cache_dict = pickle.load(handle)
                    if unique_key in self.cache_dict:
                        return self.cache_dict[unique_key]
            raise Exception(
                f"Error: {unique_key} not found. Call Cache.contains() prior to calling Cache.load_from_cache()")
