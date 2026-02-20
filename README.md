# Cloudgene server configuration

https://cloudgene.qcif.edu.au

This repo contains config for the above web server, which includes:

## `./config/` - Cloudgene server configuration

These files get linked to the appropriate locations to make the web server function:

```
cloudgene.service -> /etc/systemd/system/cloudgene.service
nginx-vhost.conf -> /etc/nginx/sites-available/cloudgene.qcif.edu.au.conf
```

`footer.html` - this just gets copy/pasted into the Cloudgene web admin, where you can set a custom footer.

## `./pages/` - Cloudgene webpage templates

This entire dir gets symlinked to `/mnt/data/cloudgene/pages/`

## `./sftp/` - large file uploads

This contains an ansible role for deploying a sftp server that allows users to upload files that are too large/numerous for HTTP to handle reliably (supposed to be for WF1).

## `./workflows/` - configuration for each installed workflow

These get symlinked into the repo directory for each workflow, for example:

```
# Note that this should not get committed to the taxodactyl git, this dir should be an archive, not a git clone:
taxodactyl.yml -> /mnt/data/apps/taxodactyl/v1.3.3-1/cloudgene.yml
```
