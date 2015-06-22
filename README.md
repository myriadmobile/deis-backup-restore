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
docker run -it myriadmobile/deis-backup-restore \
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

## Backing Up a Cluster

Note, this section assumes that you can access the cluster via SSH or console and the Deis is running normally.

### Clone the Repository

Log in to the cluster and clone the deis-backup-restore repository:

```bash
https://github.com/myriadmobile/deis-backup-restore.git
```

### Edit the Unit File

Edit `contrib/deis-backup.service` and replace the `REPLACE_ME` with your S3 API key, API Secret, and bucket name respectively.
See the full usage above for additional configuration parameters.

Be default, the cluster will be backed up nightly at 12:00am UTC. This can be changed by editing `contrib/deis-backup.timer`.
See http://www.freedesktop.org/software/systemd/man/systemd.timer.html for more information on timer files.

### Install the Units

Next we need to submit the unit files to the cluster using fleetctl.

```bash
cd contrib
fleetctl submit deis-backup.service
fleetctl submit deis-backup.timer
```

Now, start the timer:

```bash
fleetctl start deis-backup.timer
```

Note, this will only start the timer, which will start the backup service at the defined time. You can always manually start a backup:
 
```bash
fleetctl start deis-backup.service
```

### Verifying a Backup Completed

There are two ways to verify a backup:

1. Use `fleetctl journal -u deis-backup.service` to view the output from the backup.
2. Look for a `success` file in the root of the remote backup directory. This file contains some basic information about the backup procedure.


## Restoring a Backup

### Provision a New Cluster

Although it is possible to restore on top of an old cluster, it is recommended that you deploy a new cluster to promote consistency.

### Install Deis

Login to the cluster and install Deis normally using:

```bash
deisctl install platform
```

**Do not start Deis!**

### Start the Deis Store Components

Start the Deis store components and a router to access the gateway.

```bash
deisctl start store-monitor
deisctl start store-daemon
deisctl start store-metadata
deisctl start store-gateway@1
deisctl start store-volume
deisctl start router@1
```

### Restore the Data

Once the store components and router are up and running, you can restore the data:

```bash
source /etc/environment
docker run -it myriadmobile/deis-backup-restore \
	--key S3_ACCESS_KEY_ID \
	--secret S3_SECRET_KEY \
	--bucket S3_BUCKET_NAME \
	--etcd-host $COREOS_PRIVATE_IPV4
	restore [Date Directory Name]
```

### Start the Platform

After the data is restored place, start the cluster normally:

```bash
deisctl start platform
```

### Restore Controller Data

The controller stores information about keys, apps, domains, etc. in etcd. To restore, log in to the machine running deis-controller. You can find
the ip address of the machine `fleetctl list-units`.

Login to the controller:

```bash
nse deis-controller
```

Restore the data:
Note: `export ETCD=[HOST IP ADDRESS]:4001`

```bash
cd /app
export ETCD=172.17.8.100:4001
./manage.py shell <<EOF
from api.models import *
[k.save() for k in Key.objects.all()]
[a.save() for a in App.objects.all()]
[d.save() for d in Domain.objects.all()]
[c.save() for c in Certificate.objects.all()]
EOF
exit
```

The cluster should now be fully restored!