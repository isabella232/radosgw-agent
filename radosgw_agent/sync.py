import logging
import multiprocessing
import time

from radosgw_agent import worker
from radosgw_agent import client

log = logging.getLogger(__name__)

# the replica log api only supports one entry, and updating it
# requires sending a daemon id that matches the existing one. This
# doesn't make a whole lot of sense with the current structure of
# radosgw-agent, so just use a constant value for the daemon id.
DAEMON_ID = 'radosgw-agent'

class Syncer(object):
    def __init__(self, src, dest, max_entries, *args, **kwargs):
        self.src = src
        self.dest = dest
        self.src_conn = client.connection(src)
        self.dest_conn = client.connection(dest)
        self.daemon_id = DAEMON_ID
        self.worker_cls = None # filled in by subclass constructor
        self.num_shards = None
        self.max_entries = max_entries
        self.object_sync_timeout = kwargs.get('object_sync_timeout')

    def init_num_shards(self):
        if self.num_shards is not None:
            return
        try:
            self.num_shards = client.num_log_shards(self.src_conn, self.type)
            log.debug('%d shards to check', self.num_shards)
        except Exception as e:
            log.error('finding number of shards failed: %s', e)
            raise

    def shard_num_for_key(self, key):
        key = key.encode('utf8')
        hash_val = 0
        for char in key:
            c = ord(char)
            hash_val = (hash_val + (c << 4) + (c >> 4)) * 11
        return hash_val % self.num_shards

    def prepare(self):
        """Setup any state required before syncing starts.

        This must be called before sync().
        """
        pass

    def generate_work(self):
        """Generate items to be place in a queue or processing"""
        pass

    def wait_until_ready(self):
        pass

    def complete_item(self, shard_num, retries):
        """Called when syncing a single item completes successfully"""
        marker = self.shard_info.get(shard_num)
        if not marker:
            return
        try:
            data = [dict(name=retry, time=worker.DEFAULT_TIME)
                    for retry in retries]
            client.set_worker_bound(self.dest_conn,
                                    self.type,
                                    marker,
                                    worker.DEFAULT_TIME,
                                    self.daemon_id,
                                    shard_num,
                                    data)
        except Exception as e:
            log.warn('could not set worker bounds, may repeat some work: %s', e)

    def sync(self, num_workers, log_lock_time, max_entries=None):
        workQueue = multiprocessing.Queue()
        resultQueue = multiprocessing.Queue()

        processes = [self.worker_cls(workQueue,
                                     resultQueue,
                                     log_lock_time,
                                     self.src,
                                     self.dest,
                                     daemon_id=self.daemon_id,
                                     max_entries=max_entries,
                                     object_sync_timeout=self.object_sync_timeout,
                                     )
                     for i in xrange(num_workers)]
        for process in processes:
            process.daemon = True
            process.start()

        self.wait_until_ready()

        log.info('Starting sync')
        # enqueue the shards to be synced
        num_items = 0
        for item in self.generate_work():
            num_items += 1
            workQueue.put(item)

        # add a poison pill for each worker
        for i in xrange(num_workers):
            workQueue.put(None)

        # pull the results out as they are produced
        retries = {}
        for i in xrange(num_items):
            result, item = resultQueue.get()
            shard_num, retries = item
            if result == worker.RESULT_SUCCESS:
                log.debug('synced item %r successfully', item)
                self.complete_item(shard_num, retries)
            else:
                log.error('error syncing shard %d', shard_num)
                retries.append(shard_num)

            log.info('%d/%d items processed', i + 1, num_items)
        if retries:
            log.error('Encountered errors syncing these %d shards: %r',
                      len(retries), retries)


class IncrementalSyncer(Syncer):

    def get_worker_bound(self, shard_num):
        try:
            marker, timestamp, retries = client.get_worker_bound(
                self.dest_conn,
                self.type,
                shard_num)
            log.debug('oldest marker and time for shard %d are: %r %r',
                      shard_num, marker, timestamp)
            log.debug('%d items to retrie are: %r', len(retries), retries)
        except client.NotFound:
            # if no worker bounds have been set, start from the beginning
            marker, retries = '', []
        return marker, retries

    def get_log_entries(self, shard_num, marker):
        try:
            log_entries = client.get_log(self.src_conn, self.type,
                                         marker, self.max_entries,
                                         shard_num)
            if len(log_entries) == self.max_entries:
                log.warn('shard %d log has fallen behind - log length >= %d',
                         shard_num)
        except client.NotFound:
            # no entries past this marker yet, but we my have retries
            log_entries = []
        return log_entries

    def prepare(self):
        self.init_num_shards()

        self.shard_info = {}
        self.shard_work = {}
        for shard_num in xrange(self.num_shards):
            marker, retries = self.get_worker_bound(shard_num)
            log_entries = self.get_log_entries(shard_num, marker)
            self.shard_info[shard_num] = marker
            self.shard_work[shard_num] = log_entries, retries

        self.prepared_at = time.time()

    def generate_work(self):
        return self.shard_work.iteritems()



class MetaSyncerInc(IncrementalSyncer):

    def __init__(self, *args, **kwargs):
        super(MetaSyncerInc, self).__init__(*args, **kwargs)
        self.worker_cls = worker.MetadataWorkerIncremental
        self.type = 'metadata'


class DataSyncerInc(IncrementalSyncer):

    def __init__(self, *args, **kwargs):
        super(DataSyncerInc, self).__init__(*args, **kwargs)
        self.worker_cls = worker.DataWorkerIncremental
        self.type = 'data'
        self.rgw_data_log_window = kwargs.get('rgw_data_log_window', 30)

    def wait_until_ready(self):
        log.info('waiting to make sure bucket log is consistent')
        while time.time() < self.prepared_at + self.rgw_data_log_window:
            time.sleep(1)


class DataSyncerFull(Syncer):

    def __init__(self, *args, **kwargs):
        super(DataSyncerFull, self).__init__(*args, **kwargs)
        self.worker_cls = worker.DataWorkerFull
        self.type = 'data'
        self.rgw_data_log_window = kwargs.get('rgw_data_log_window', 30)

    def prepare(self):
        self.init_num_shards()

        # save data log markers for each shard
        self.shard_info = {}
        for shard in xrange(self.num_shards):
            info = client.get_log_info(self.src_conn, 'data', shard)
            # setting an empty marker returns an error
            if info['marker']:
                self.shard_info[shard] = info['marker']

        # get list of buckets after getting any markers to avoid skipping
        # entries added before we got the marker info
        buckets = client.get_bucket_list(self.src_conn)

        self.prepared_at = time.time()

        self.buckets_by_shard = {}
        for bucket in buckets:
            shard = self.shard_num_for_key(bucket)
            self.buckets_by_shard.setdefault(shard, [])
            self.buckets_by_shard[shard].append(bucket)

    def generate_work(self):
        return self.buckets_by_shard.iteritems()

    def wait_until_ready(self):
        log.info('waiting to make sure bucket log is consistent')
        while time.time() < self.prepared_at + self.rgw_data_log_window:
            time.sleep(1)


class MetaSyncerFull(Syncer):
    def __init__(self, *args, **kwargs):
        super(MetaSyncerFull, self).__init__(*args, **kwargs)
        self.worker_cls = worker.MetadataWorkerFull
        self.type = 'metadata'

    def prepare(self):
        try:
            self.sections = client.get_metadata_sections(self.src_conn)
        except client.HttpError as e:
            log.error('Error listing metadata sections: %s', e)
            raise

        # grab the lastest shard markers and timestamps before we sync
        self.shard_info = {}
        self.init_num_shards()
        for shard_num in xrange(self.num_shards):
            info = client.get_log_info(self.src_conn, 'metadata', shard_num)
            # setting an empty marker returns an error
            if info['marker']:
                self.shard_info[shard_num] = info['marker']

        self.metadata_by_shard = {}
        for section in self.sections:
            try:
                for key in client.list_metadata_keys(self.src_conn, section):
                    shard = self.shard_num_for_key(section + ':' + key)
                    self.metadata_by_shard.setdefault(shard, [])
                    self.metadata_by_shard[shard].append((section, key))
            except client.NotFound:
                # no keys of this type exist
                continue
            except client.HttpError as e:
                log.error('Error listing metadata for section %s: %s',
                          section, e)
                raise

    def generate_work(self):
        return self.metadata_by_shard.iteritems()
