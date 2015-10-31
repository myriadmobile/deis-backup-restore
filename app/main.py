#!/usr/bin/env python
import Queue
import argparse
from datetime import datetime
import fnmatch
import time
import json
import subprocess
import tempfile
import sys
import threading
import traceback

import boto
from boto.s3.connection import OrdinaryCallingFormat
from boto.s3.key import Key
import etcd
import os


class DeisBackupRestore:
    _etcd_connection = None
    _base_directory = None

    _data_path = '/data/'

    _max_spool_bytes = 1000000

    def __init__(self, aws_access_key_id='', aws_secret_access_key='', host='s3.amazonaws.com', port=443,
                 is_secure=True, bucket_name='deis-backup', etcd_host='127.0.0.1', etcd_port=4001, no_data=False, dry_run=False):
        self._remote_access_key = aws_access_key_id
        self._remote_secret_key = aws_secret_access_key
        self._remote_host = host
        self._remote_port = port
        self._remote_is_secure = is_secure
        self._remote_bucket_name = bucket_name
        self._etcd_host = etcd_host
        self._etcd_port = etcd_port
        self._no_data = no_data
        self._dry_run = dry_run

    def get_base_directory(self):
        if self._base_directory is None:
            self._base_directory = datetime.now().strftime("%Y-%m-%d-%H:%M")
        return self._base_directory

    def get_etcd_connection(self):
        if self._etcd_connection is None:
            self._etcd_connection = etcd.Client(host=self._etcd_host, port=self._etcd_port)
        return self._etcd_connection

    def get_etcd_value(self, key, default=None):
        try:
            return self.get_etcd_connection().read(key).value
        except:
            return default

    def set_etcd_value(self, key, value, ttl=None):
        return self.get_etcd_connection().set(key, value, ttl)

    def get_deis_s3_connection_args(self):
        host = self.get_etcd_value('/deis/store/gateway/host')
        port = self.get_etcd_value('/deis/store/gateway/port')
        aws_access_key_id = self.get_etcd_value('/deis/store/gateway/accessKey')
        aws_secret_access_key = self.get_etcd_value('/deis/store/gateway/secretKey')
        return {
            'aws_access_key_id': aws_access_key_id,
            'aws_secret_access_key': aws_secret_access_key,
            'host': host,
            'port': int(port),
            'is_secure': False,
            'calling_format': OrdinaryCallingFormat()
        }

    def get_remote_s3_connection_args(self):
        return {
            'aws_access_key_id': self._remote_access_key,
            'aws_secret_access_key': self._remote_secret_key,
            'host': self._remote_host,
            'port': self._remote_port,
            'is_secure': self._remote_is_secure,
            'calling_format': OrdinaryCallingFormat()
        }

    def get_deis_s3_connection(self):
        return boto.connect_s3(**self.get_deis_s3_connection_args())

    def get_remote_s3_connection(self):
        return boto.connect_s3(**self.get_remote_s3_connection_args())

    def get_deis_s3_bucket(self, bucket_name):
        return self.get_deis_s3_connection().get_bucket(bucket_name)

    def get_remote_s3_bucket(self):
        return self.get_remote_s3_connection().get_bucket(self._remote_bucket_name)

    def get_deis_key_name(self, deis_bucket_name, remote_key_name):
        return remote_key_name.replace(self.get_base_directory() + '/' + deis_bucket_name + '/', '')

    def get_remote_key_name(self, deis_bucket_name, deis_key_name):
        return '/' + self.get_base_directory() + '/' + deis_bucket_name + '/' + deis_key_name

    def backup(self):
        started_at = datetime.utcnow()
        try:
            self.backup_etcd()
            self.backup_database_sql()
            self.backup_database_wal()
            self.backup_registry()
            if not self._no_data:
                self.backup_data()
        except:
            ex_type, ex, tb = sys.exc_info()
            try:
                self.write_failure_file(traceback.format_exc())
            except:
                pass
            raise ex_type, ex, tb
        else:
            self.write_success_file(started_at, datetime.utcnow())

    def restore(self, base_directory):
        self._base_directory = base_directory
        self.set_router_body_size('2048m')
        self.restore_database_wal()
        self.restore_registry()
        self.restore_etcd()
        if not self._no_data:
            self.restore_data()

    def write_success_file(self, started_at, ended_at):
        bucket = self.get_remote_s3_bucket()
        key = Key(bucket, '/' + self.get_base_directory() + '/success')
        message = 'Started at ' + started_at.isoformat() + '\n'
        message += 'Ended at ' + ended_at.isoformat() + '\n'
        message += 'Took ' + str(ended_at - started_at)
        self.set_contents_from_string(key, message)

    def write_failure_file(self, message=''):
        bucket = self.get_remote_s3_bucket()
        key = Key(bucket, '/' + self.get_base_directory() + '/failure')
        self.set_contents_from_string(key, message)

    def set_router_body_size(self, size='1m'):
        key = '/deis/router/bodySize'
        self.set_etcd_value(key, size)

    def backup_database_sql(self):
        print('backing up database sql')
        env = os.environ.copy()
        env['PGHOST'] = self.get_etcd_value('/deis/database/host')
        env['PGPORT'] = self.get_etcd_value('/deis/database/port')
        env['PGUSER'] = self.get_etcd_value('/deis/database/adminUser')
        env['PGPASSWORD'] = self.get_etcd_value('/deis/database/adminPass')
        proc = subprocess.Popen('pg_dumpall', env=env, stdout=subprocess.PIPE)
        (output, err) = proc.communicate()

        bucket = self.get_remote_s3_bucket()
        key = Key(bucket, self.get_remote_key_name('other', 'database.sql'))
        self.set_contents_from_string(key, output)
        key.close()

    def backup_bucket(self, deis_bucket_name):
        print('backing up bucket ' + deis_bucket_name)

        pool = BucketThreadPool(4, self.get_deis_s3_bucket, deis_bucket_name, self.get_remote_s3_bucket)

        deis_bucket = self.get_deis_s3_bucket(deis_bucket_name)
        for key in deis_bucket.list():
            pool.add_task(self.backup_file, *(deis_bucket_name, key.key))

        pool.wait_and_shutdown()

    def backup_file(self, deis_bucket_name, key_name, deis_bucket=None, remote_bucket=None):
        key = Key(deis_bucket, key_name)
        remote_key = Key(remote_bucket, self.get_remote_key_name(deis_bucket_name, key_name))

        temp = tempfile.SpooledTemporaryFile(self._max_spool_bytes)
        key.get_contents_to_file(temp)
        self.set_contents_from_file(remote_key, temp)
        temp.close()

        key.close()
        remote_key.close()

    def restore_bucket(self, deis_bucket_name):
        print('restoring bucket ' + deis_bucket_name)

        try:
            print('creating bucket...')
            self.get_deis_s3_connection().create_bucket(deis_bucket_name)
        except:
            print('bucket already exists')

        print 'emptying bucket...'
        bucket = self.get_deis_s3_bucket(deis_bucket_name)
        bucket_list = bucket.list()
        bucket.delete_keys([key.name for key in bucket_list])

        pool = BucketThreadPool(4, self.get_deis_s3_bucket, deis_bucket_name, self.get_remote_s3_bucket)

        remote_bucket = self.get_remote_s3_bucket()
        for key in remote_bucket.list(prefix=self.get_base_directory() + '/' + deis_bucket_name + '/'):
            pool.add_task(self.restore_file, *(deis_bucket_name, key.key))

        pool.wait_and_shutdown()

    def restore_file(self, deis_bucket_name, key_name, deis_bucket=None, remote_bucket=None):
        key = Key(remote_bucket, key_name)
        deis_key = Key(deis_bucket, self.get_deis_key_name(deis_bucket_name, key.key))

        temp = tempfile.SpooledTemporaryFile(self._max_spool_bytes)
        key.get_contents_to_file(temp)
        self.set_contents_from_file(deis_key, temp)
        temp.close()

        key.close()
        deis_key.close()

    def set_contents_from_string(self, key, string):
        if self._dry_run:
            print('dry-run: not uploading ' + key.key)
        else:
            print('uploading ' + key.key)
            key.set_contents_from_string(string)

    def set_contents_from_file(self, key, file):
        if self._dry_run:
            print('dry-run: not uploading ' + key.key)
        else:
            print('uploading ' + key.key)
            key.set_contents_from_file(file, rewind=True)

    def get_contents_to_file(self, key, file):
        if self._dry_run:
            print('dry-run: not getting ' + key.key + ' to ' + file.name)
        else:
            print('getting ' + key.key + ' to ' + file.name)
            key.get_contents_to_file(file)

    def backup_database_wal(self):
        bucket_name = self.get_etcd_value('/deis/database/bucketName', 'db_wal')
        self.backup_bucket(bucket_name)

    def backup_registry(self):
        bucket_name = self.get_etcd_value('/deis/registry/bucketName', 'registry')
        self.backup_bucket(bucket_name)

    def restore_database_wal(self):
        bucket_name = self.get_etcd_value('/deis/database/bucketName', 'db_wal')
        self.restore_bucket(bucket_name)

    def restore_registry(self):
        bucket_name = self.get_etcd_value('/deis/registry/bucketName', 'registry')
        self.restore_bucket(bucket_name)

    def backup_etcd(self):
        print ('backing up etcd...')

        root_dir = self.get_etcd_connection().read('/', recursive=True)
        d = {}
        for entry in root_dir.children:
            d[entry.modifiedIndex] = {
                'key': entry.key,
                'value': entry.value,
                'ttl': entry.ttl,
                'dir': entry.dir,
                'index': entry.modifiedIndex
            }

        indexes = sorted(d.keys())
        dumplist = []
        for idx in indexes:
            dumplist.append(d[idx])

        bucket = self.get_remote_s3_bucket()
        key = Key(bucket, self.get_remote_key_name('other', 'etcd.json'))
        self.set_contents_from_string(key, json.dumps(dumplist))

    def restore_etcd(self):
        print ('restoring etcd...')
        bucket = self.get_remote_s3_bucket()
        key = Key(bucket, self.get_remote_key_name('other', 'etcd.json'))
        data = json.loads(key.get_contents_as_string(encoding='utf-8'))

        blacklist = [
            '/coreos.com/*',
            '/deis/builder/users/*',
            '/deis/cache/*',
            '/deis/certs/*',
            '/deis/config/*',
            '/deis/database/initId',
            '/deis/domains/*',
            '/deis/platform/domain',
            '/deis/platform/sshPrivateKey',
            '/deis/registry/hosts/*'
            '/deis/registry/masterLock',
            '/deis/router/hosts/*',
            '/deis/services/*',
            '/deis/store/*',
            '/registry/*'
        ]

        for entry in data:
            entry_key = entry['key'].encode('utf-8')
            blacklisted = False
            for blacklisted_pattern in blacklist:
                if fnmatch.fnmatch(entry_key, blacklisted_pattern):
                    blacklisted = True
            if not blacklisted:
                self.restore_etcd_value(entry)

    def restore_etcd_value(self, entry):
        if self._dry_run:
            print('dry-run: not setting ' + entry['key'].encode('utf-8') + ' => ' + entry['value'].encode('utf-8'))
        else:
            self.get_etcd_connection().write(entry['key'].encode('utf-8'), entry['value'].encode('utf-8'),
                                             ttl=entry['ttl'], dir=entry['dir'])

    def backup_data(self):
        print('backing up data...')

        pool = BucketThreadPool(6, None, None, self.get_remote_s3_bucket)

        for root, dirnames, filenames in os.walk(self._data_path):
            for filename in filenames:
                file_path = os.path.join(root, filename)
                key_name = file_path[len(self._data_path):]
                remote_key_name = self.get_remote_key_name('data', key_name)
                pool.add_task(self.backup_data_file, *(file_path, remote_key_name))

        pool.wait_and_shutdown()

    def backup_data_file(self, file_path, remote_key_name, remote_bucket=None):
        stat = os.stat(file_path)
        fp = file(file_path)
        key = Key(remote_bucket, remote_key_name)
        key.metadata = {'uid': stat.st_uid, 'gid': stat.st_gid}
        self.set_contents_from_file(key, fp)
        key.close()
        fp.close()

    def restore_data(self):
        print('restoring data...')

        pool = BucketThreadPool(6, None, None, self.get_remote_s3_bucket)

        remote_bucket = self.get_remote_s3_bucket()
        prefix = self.get_base_directory() + '/data/'
        for key in remote_bucket.list(prefix=prefix):
            file_path = os.path.join(self._data_path, key.key[len(prefix):])
            dir_name = os.path.dirname(file_path)
            if not os.path.exists(dir_name):
                os.makedirs(dir_name)
            pool.add_task(self.restore_data_file, *(file_path, key.key))

        pool.wait_and_shutdown()

    def restore_data_file(self, file_path, remote_key_name, remote_bucket=None):
        fp = file(file_path, 'w')
        key = Key(remote_bucket, remote_key_name)
        self.get_contents_to_file(key, fp)
        try:
            uid = int(key.get_metadata('uid'))
            gid = int(key.get_metadata('gid'))
            os.chown(file_path, uid, gid)
        except:
            pass

        key.close()
        fp.close()


class ThreadPool:
    _workers = []

    def __init__(self, num_threads, **worker_args):
        self._tasks = Queue.Queue(num_threads)
        self._worker_args = worker_args

        for _ in range(num_threads):
            self.create_worker(self._tasks, self._worker_args)

    def create_worker(self, tasks, **worker_args):
        self._workers.append(ThreadPoolWorker(tasks, worker_args))

    def add_task(self, func, *args):
        self._tasks.put((func, args))

    def wait_completion(self):
        self._tasks.join()

    def shutdown(self):
        for worker in self._workers:
            worker.stop()

    def wait_and_shutdown(self):
        self.wait_completion()
        self.shutdown()


class BucketThreadPool(ThreadPool):
    _deis_bucket = None
    _remote_bucket = None

    def __init__(self, num_threads, deis_bucket_fn, deis_bucket_name, remote_bucket_fn, **worker_args):
        if deis_bucket_fn is not None:
            self._deis_bucket = deis_bucket_fn(deis_bucket_name)

        if remote_bucket_fn is not None:
            self._remote_bucket = remote_bucket_fn()

        ThreadPool.__init__(self, num_threads, **worker_args)

    def create_worker(self, tasks, deis_bucket_fn=None, deis_bucket_name=None, remote_bucket_fn=None, **worker_args):
        if self._deis_bucket is not None:
            worker_args['deis_bucket'] = self._deis_bucket
        if self._remote_bucket is not None:
            worker_args['remote_bucket'] = self._remote_bucket
        self._workers.append(ThreadPoolWorker(tasks, **worker_args))


class ThreadPoolWorker(threading.Thread):
    def __init__(self, tasks, **args):
        threading.Thread.__init__(self)
        self._stop = threading.Event()
        self._tasks = tasks
        self._args = args
        self.daemon = False
        self.start()

    def stop(self):
        self._stop.set()

    def stopped(self):
        return self._stop.isSet()

    def run(self):
        while not self.stopped():
            try:
                func, args = self._tasks.get_nowait()
                func(*args, **self._args)
                self._tasks.task_done()
            except Queue.Empty:
                time.sleep(1)
                pass
            except:
                print traceback.format_exc()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backup and restore Deis data')
    parser.add_argument('--key', required=True, dest='aws_access_key_id', help='s3 key id')
    parser.add_argument('--secret', required=True, dest='aws_secret_access_key', help='s3 secret key')
    parser.add_argument('--bucket', required=True, dest='bucket_name', help='s3 backup bucket')
    parser.add_argument('--host', dest='host', default='s3.amazonaws.com', help='s3 host')
    parser.add_argument('--port', dest='port', default=443, type=int, help='s3 port')
    parser.add_argument('--insecure', dest='is_secure', action='store_false', help='s3 use ssl connection')
    parser.add_argument('--dry-run', dest='dry_run', action='store_true', help='dry run')
    parser.add_argument('--etcd-host', dest='etcd_host', default='127.0.0.1', help='etcd host')
    parser.add_argument('--etcd-port', dest='etcd_port', default=4001, type=bool, help='etcd port')
    parser.add_argument('--no-data', dest='no_data', action='store_true', help='don\'t include store data')

    subparsers = parser.add_subparsers(help='sub-command help')
    parser_backup = subparsers.add_parser('backup', help='backup help')
    parser_backup.set_defaults(cmd='backup')

    parser_restore = subparsers.add_parser('restore', help='restore help')
    parser_restore.add_argument('directory', help='directory on remote s3 to restore')
    parser_restore.set_defaults(cmd='restore')

    args = vars(parser.parse_args())
    cmd = args.pop('cmd')
    directory = args.pop('directory', None)
    object = DeisBackupRestore(**args)

    if cmd is 'backup':
        object.backup()
    elif cmd is 'restore':
        object.restore(directory)
