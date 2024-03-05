import os
import threading
import time
import pickle

try:
    import redis
except ImportError:
    redis = None

from beaker.container import NamespaceManager
from beaker.synchronization import SynchronizerImpl
from beaker.util import SyncDict, machine_identifier
from beaker.crypto.util import sha1
from beaker._compat import string_type, PY2


class RedisClusterNamespaceManager(NamespaceManager):
    """Provides the :class:`.NamespaceManager` API over Redis cluster.

    Provided ``url`` can be both a redis connection string or
    an already existing RedisCluster instance.

    The data will be stored into redis keys, with their name
    starting with ``beaker_cache:``. So make sure you provide
    a specific database number if you don't want to mix them
    with your own data.
    """
    MAX_KEY_LENGTH = 1024

    clients = SyncDict()

    def __init__(self, namespace, url, timeout=None, **kw):
        super(RedisClusterNamespaceManager, self).__init__(namespace)
        self.lock_dir = None  # Redis uses redis itself for locking.
        self.timeout = timeout
        self.nodes = []

        if redis is None:
            raise RuntimeError('redis is not available')

        if isinstance(url, string_type):
            for url in url.split(','):
                url_options = redis.connection.parse_url(url)
                if 'db' in url_options:
                    # Argument 'db' is not possible to use in cluster mode
                    raise redis.cluster.RedisClusterException(
                        "A ``db`` querystring option can only be 0 in cluster mode"
                    )
                self.nodes.append(redis.cluster.ClusterNode(
                    host=url_options.get('host'),
                    port=url_options.get('port')
                ))
            self.client = RedisClusterNamespaceManager.clients.get(
                url, redis.RedisCluster, startup_nodes=self.nodes
            )
        else:
            self.client = url

    def _format_key(self, key):
        if not isinstance(key, str):
            key = key.decode('ascii')
        if len(key) > (self.MAX_KEY_LENGTH - len(self.namespace) - len('beaker_cache:') - 1):
            if not PY2:
                key = key.encode('utf-8')
            key = sha1(key).hexdigest()
        return 'beaker_cache:%s:%s' % (self.namespace, key)

    def get_creation_lock(self, key):
        return RedisClusterSynchronizer(self._format_key(key), self.client)

    def __getitem__(self, key):
        entry = self.client.get(self._format_key(key))
        if entry is None:
            raise KeyError(key)
        return pickle.loads(entry)

    def __contains__(self, key):
        return self.client.exists(self._format_key(key))

    def has_key(self, key):
        return key in self

    def set_value(self, key, value, expiretime=None):
        value = pickle.dumps(value)
        if expiretime is None and self.timeout is not None:
            expiretime = self.timeout
        if expiretime is not None:
            self.client.setex(self._format_key(key), int(expiretime), value)
        else:
            self.client.set(self._format_key(key), value)

    def __setitem__(self, key, value):
        self.set_value(key, value)

    def __delitem__(self, key):
        self.client.delete(self._format_key(key))

    def do_remove(self):
        for k in self.keys():
            self.client.delete(k)

    def keys(self):
        return self.client.keys('beaker_cache:%s:*' % self.namespace)


class RedisClusterSynchronizer(SynchronizerImpl):
    """Synchronizer based on redis cluster.

    Provided ``url`` can be both a redis connection string or
    an already existing RedisCluster instance.

    This Synchronizer only supports 1 reader or 1 writer at time, not concurrent readers.
    """
    # If a cache entry generation function can take a lot,
    # but 15 minutes is more than a reasonable time.
    LOCK_EXPIRATION = 900
    MACHINE_ID = machine_identifier()

    def __init__(self, identifier, url):
        super(RedisClusterSynchronizer, self).__init__()
        self.identifier = 'beaker_lock:%s' % identifier
        if isinstance(url, string_type):
            self.client = RedisClusterNamespaceManager.clients.get(url, redis.RedisCluster.from_url, url)
        else:
            self.client = url

    def _get_owner_id(self):
        return (
                '%s-%s-%s' % (self.MACHINE_ID, os.getpid(), threading.current_thread().ident)
        ).encode('ascii')

    def do_release_read_lock(self):
        self.do_release_write_lock()

    def do_acquire_read_lock(self, wait):
        self.do_acquire_write_lock(wait)

    def do_release_write_lock(self):
        identifier = self.identifier
        owner_id = self._get_owner_id()

        def execute_release(pipe):
            lock_value = pipe.get(identifier)
            if lock_value == owner_id:
                pipe.delete(identifier)

        self.client.transaction(execute_release, identifier)

    def do_acquire_write_lock(self, wait):
        owner_id = self._get_owner_id()
        while True:
            if self.client.set(self.identifier, owner_id, ex=self.LOCK_EXPIRATION, nx=True):
                return True

            if not wait:
                return False
            time.sleep(0.2)
