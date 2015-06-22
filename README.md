# Deis Backup/Restore

Deis Backup/Restore provides a simple method of backing up and restoring the gateway and etcd keys used by Deis.

## Features
- backup and restore etcd
- backup and restore the registry and database
- backup and restore store data (logs)
- highly configurable
- works with any s3 compatible stores
- multi-threaded for performance
- low memory and storage requirements

## Basic Usage

```bash
docker run -it myriadmobile/deis-backup-restore:v1.0.4 \
	--key S3_ACCESS_KEY_ID \
	--secret S3_SECRET_KEY \
	--bucket S3_BUCKET_NAME \
	--etcd-host $COREOS_PRIVATE_IPV4
	{backup|restore}
```

## Configuration
```bash
usage: main.py [-h] --key AWS_ACCESS_KEY_ID --secret AWS_SECRET_ACCESS_KEY
               --bucket BUCKET_NAME [--host HOST] [--port PORT] [--insecure]
               [--dry-run] [--etcd-host ETCD_HOST] [--etcd-port ETCD_PORT]
               [--no-data]
               {backup,restore} ...

Backup and restore Deis data

positional arguments:
  {backup,restore}      sub-command help
    backup              backup help
    restore             restore help

optional arguments:
  -h, --help            show this help message and exit
  --key AWS_ACCESS_KEY_ID
                        s3 key id
  --secret AWS_SECRET_ACCESS_KEY
                        s3 secret key
  --bucket BUCKET_NAME  s3 backup bucket
  --host HOST           s3 host
  --port PORT           s3 port
  --insecure            s3 use ssl connection
  --dry-run             dry run
  --etcd-host ETCD_HOST
                        etcd host
  --etcd-port ETCD_PORT
                        etcd port
  --no-data             don't include store data

```

## More Docs Coming Soon!