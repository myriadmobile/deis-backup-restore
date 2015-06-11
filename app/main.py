#!/usr/bin/env python
from Queue import Queue
import argparse
from datetime import datetime
import json
import subprocess
import tempfile
from threading import Thread

import boto
from boto.s3.connection import OrdinaryCallingFormat
from boto.s3.key import Key
import etcd
import os


class DeisBackupRestore:
    _etcd_connection = None
    _base_directory = None

    _deis_s3_connection_args = None
    _remote_s3_connection_args = None

    def __init__(self, aws_access_key_id='', aws_secret_access_key='', host='s3.amazonaws.com', port=443,
                 is_secure=True, bucket_name='deis-backup', etcd_host='127.0.0.1', etcd_port=4001, dry_run=False):
        self._remote_access_key = aws_access_key_id
        self._remote_secret_key = aws_secret_access_key
        self._remote_host = host
        self._remote_port = port
        self._remote_is_secure = is_secure
        self._remote_bucket_name = bucket_name
        self._etcd_host = etcd_host
        self._etcd_port = etcd_port
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

    def get_deis_s3_connection_args(self):
        if self._deis_s3_connection_args is None:
            host = self.get_etcd_value('/deis/store/gateway/host')
            port = self.get_etcd_value('/deis/store/gateway/port')
            aws_access_key_id = self.get_etcd_value('/deis/store/gateway/accessKey')
            aws_secret_access_key = self.get_etcd_value('/deis/store/gateway/secretKey')
            self._deis_s3_connection_args = {
                'aws_access_key_id': aws_access_key_id,
                'aws_secret_access_key': aws_secret_access_key,
                'host': host,
                'port': int(port),
                'is_secure': False,
                'calling_format': OrdinaryCallingFormat()
            }
        return self._deis_s3_connection_args

    def get_remote_s3_connection_args(self):
        if self._remote_s3_connection_args is None:
            self._remote_s3_connection_args = {
                'aws_access_key_id': self._remote_access_key,
                'aws_secret_access_key': self._remote_secret_key,
                'host': self._remote_host,
                'port': self._remote_port,
                'is_secure': self._remote_is_secure,
                'calling_format': OrdinaryCallingFormat()
            }
        return self._remote_s3_connection_args

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
        self.backup_etcd()
        self.backup_database_sql()
        self.backup_database_wal()
        self.backup_registry()

    def restore(self, base_directory):
        self._base_directory = base_directory
        self.restore_etcd()
        self.restore_database_wal()
        self.restore_registry()

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

    def backup_bucket(self, deis_bucket_name):
        print('backing up bucket ' + deis_bucket_name)

        pool = BucketThreadPool(8, self.get_deis_s3_bucket, deis_bucket_name, self.get_remote_s3_bucket)

        deis_bucket = self.get_deis_s3_bucket(deis_bucket_name)
        for key in deis_bucket.list():
            pool.add_task(self.backup_file, *(deis_bucket_name, key.key))

        pool.wait_completion()

    def backup_file(self, deis_bucket_name, key_name, deis_bucket=None, remote_bucket=None):
        key = Key(deis_bucket, key_name)
        remote_key = Key(remote_bucket, self.get_remote_key_name(deis_bucket_name, key_name))
        if key.size < 16000000:  # less than 16mb, do in ram
            self.set_contents_from_string(remote_key, key.get_contents_as_string())
        else:
            temp = tempfile.SpooledTemporaryFile()
            key.get_contents_to_file(temp)
            self.set_contents_from_file(remote_key, temp)

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

        pool = BucketThreadPool(8, self.get_deis_s3_bucket, deis_bucket_name, self.get_remote_s3_bucket)

        remote_bucket = self.get_remote_s3_bucket()
        for key in remote_bucket.list(prefix=self.get_base_directory() + '/' + deis_bucket_name + '/'):
            pool.add_task(self.restore_file, *(deis_bucket_name, key.key))

        pool.wait_completion()

    def restore_file(self, deis_bucket_name, key_name, deis_bucket=None, remote_bucket=None):
        key = Key(remote_bucket, key_name)
        deis_key = Key(deis_bucket, self.get_deis_key_name(deis_bucket_name, key.key))
        if key.size < 16000000:  # less than 16mb, do in ram
            self.set_contents_from_string(deis_key, key.get_contents_as_string())
        else:
            temp = tempfile.SpooledTemporaryFile()
            key.get_contents_to_file(temp)
            self.set_contents_from_file(deis_key, temp)

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

        whitelist = [
            '/deis/builder/image',
            '/deis/cache/maxmemory',
            '/deis/cache/image',
            '/deis/controller/registrationMode',
            '/deis/controller/subdomain',
            '/deis/controller/webEnabled',
            '/deis/controller/workers',
            '/deis/controller/unitHostname',
            '/deis/controller/image',
            '/deis/controller/unitHostname',
            '/deis/controller/auth/ldap/endpoint',
            '/deis/controller/auth/ldap/bind/dn',
            '/deis/controller/auth/ldap/bind/password',
            '/deis/controller/auth/ldap/user/basedn',
            '/deis/controller/auth/ldap/user/filter',
            '/deis/controller/auth/ldap/group/basedn',
            '/deis/controller/auth/ldap/group/filter',
            '/deis/controller/auth/ldap/group/type'
            '/deis/database/image',
            '/deis/logger/image',
            '/deis/logs/image',
            '/deis/logs/drain',
            '/deis/registry/image',
            '/deis/router/affinityArg',
            '/deis/router/bodySize',
            '/deis/router/defaultTimeout',
            '/deis/router/builder/timeout/connect',
            '/deis/router/builder/timeout/tcp',
            '/deis/router/controller/timeout/connect',
            '/deis/router/controller/timeout/read',
            '/deis/router/controller/timeout/send',
            '/deis/router/enforceHTTPS',
            '/deis/router/firewall/enabled',
            '/deis/router/firewall/errorCode',
            '/deis/router/errorLogLevel',
            '/deis/router/gzip',
            '/deis/router/gzipCompLevel',
            '/deis/router/gzipDisable',
            '/deis/router/gzipHttpVersion',
            '/deis/router/gzipMinLength',
            '/deis/router/gzipProxied',
            '/deis/router/gzipTypes',
            '/deis/router/gzipVary',
            '/deis/router/gzipDisable',
            '/deis/router/gzipTypes',
            '/deis/router/maxWorkerConnections',
            '/deis/router/serverNameHashMaxSize',
            '/deis/router/serverNameHashBucketSize',
            '/deis/router/sslCert',
            '/deis/router/sslKey',
            '/deis/router/workerProcesses',
            '/deis/router/proxyProtocol',
            '/deis/router/proxyRealIpCidr',
            '/deis/router/image'
            '/deis/store/daemon/image'
            '/deis/store/gateway/image'
            '/deis/store/metadate/image'
            '/deis/store/metadate/image'
            '/deis/store/monitor/image'
        ]

        directory = self.get_etcd_connection().read('/deis/', recursive=True)
        d = {}
        for entry in directory.children:
            if entry.key in whitelist:
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

        for entry in data:
            self.set_etcd_value(entry)

    def set_etcd_value(self, entry):
        if self._dry_run:
            print('dry-run: not setting ' + entry['key'].encode('utf-8') + ' => ' + entry['value'].encode('utf-8'))
        else:
            self.get_etcd_connection().write(entry['key'].encode('utf-8'), entry['value'].encode('utf-8'),
                                             ttl=entry['ttl'], dir=entry['dir'])


class BucketWorker(Thread):
    """Thread executing tasks from a given tasks queue"""

    def __init__(self, tasks, deis_bucket, remote_bucket):
        Thread.__init__(self)
        self.deis_bucket = deis_bucket
        self.remote_bucket = remote_bucket
        self.tasks = tasks
        self.daemon = True
        self.start()

    def run(self):
        while True:
            func, args = self.tasks.get()
            try:
                func(*args, **{'deis_bucket': self.deis_bucket, 'remote_bucket': self.remote_bucket})
            except Exception, e:
                print e
            self.tasks.task_done()


class BucketThreadPool:
    """Pool of threads consuming tasks from a queue"""

    def __init__(self, num_threads, deis_bucket_fn, deis_bucket_name, remote_bucket_fn):
        self.tasks = Queue(num_threads)
        for _ in range(num_threads): BucketWorker(self.tasks, deis_bucket_fn(deis_bucket_name), remote_bucket_fn())

    def add_task(self, func, *args):
        """Add a task to the queue"""
        self.tasks.put((func, args))

    def wait_completion(self):
        """Wait for completion of all the tasks in the queue"""
        self.tasks.join()


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
