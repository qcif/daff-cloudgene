# Azure resource creation

## App registration, roles and certs

```sh
. .env.azure

az login
SUB=$(az account show --query id -o tsv)

# Create the blob container if it doesn't exist yet
az storage container create \
  --name "$CONTAINER_NAME" \
  --account-name "$STORAGE_ACCOUNT_STD" \
  --resource-group "$RESOURCE_GROUP"

# Create the app registration and cert
az ad sp create-for-rbac \
  --name "$APP_NAME" \
  --create-cert \
  --years 3

# Write these to .env.azure based on output from the above:
APP_ID=xxx
TENANT_ID=xxx

# Set this for the session only
CERT_PATH=xxx

# Move the cert to the web server, owned by the service user.
# 0600 www-data:www-data — only the service can read it. The uploader
# refuses to start if the PEM is world-readable or unreadable.
scp $CERT_PATH cloudgene:/etc/cloudgene-uploader/azure-cert.pem
# then, on the server:
#   chown www-data:www-data /etc/cloudgene-uploader/azure-cert.pem
#   chmod 600               /etc/cloudgene-uploader/azure-cert.pem

# The uploader service reads it via this variable, set in /etc/uploads.env
# (see uploader/deploy/uploads.service):
#   AZURE_CLIENT_CERTIFICATE_PATH=/etc/cloudgene-uploader/azure-cert.pem

# Delegator role at account scope
az role assignment create \
  --assignee "$APP_ID" \
  --role "Storage Blob Delegator" \
  --scope "/subscriptions/$SUB/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$STORAGE_ACCOUNT_STD"

# Data role at container scope
az role assignment create \
  --assignee "$APP_ID" \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/$SUB/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$STORAGE_ACCOUNT_STD/blobServices/default/containers/$CONTAINER_NAME"
```

`Storage Blob Data Contributor` is exercised for **delete** as well as read
and list: `DELETE /uploads/api/files` removes a blob server-side under this
principal, because the SAS handed to the browser is create-only and must
stay that way. Narrowing this assignment to a reader role would break that
route. Nothing extra needs granting — `.../blobs/delete` is already in it.

Deletion through that route is permanent unless blob soft delete is enabled
on the account, which nothing here does. To check:

```sh
az storage account blob-service-properties show \
  --account-name "$STORAGE_ACCOUNT_STD" \
  --query deleteRetentionPolicy
```

Verify that it worked

```sh
az login --service-principal \
  --username "$APP_ID" \
  --tenant "$TENANT_ID" \
  --certificate "$CERT_PATH"

az storage blob list \
  --account-name "$STORAGE_ACCOUNT_STD" \
  --container-name "$CONTAINER_NAME" \
  --auth-mode login \
  --output table

az storage blob generate-sas \
  --account-name "$STORAGE_ACCOUNT_STD" \
  --container-name "$CONTAINER_NAME" \
  --name test.txt \
  --permissions cw \
  --expiry "$(date -u -d '1 hour' '+%Y-%m-%dT%H:%MZ')" \
  --auth-mode login \
  --as-user
```

To rotate the cert (must be done before 3 years):

```sh
az ad app credential reset --id "$APP_ID" --create-cert --years 3 --append
az ad app credential list --id "$APP_ID"   # find the old keyId
az ad app credential delete --id "$APP_ID" --key-id "<old-keyId>"
```

To revoke:

```sh
# cut the app's access
az role assignment delete --assignee "$APP_ID" \
  --scope "/subscriptions/$SUB/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$STORAGE_ACCOUNT_STD"

# kill every outstanding SAS immediately
az storage account revoke-delegation-keys \
  --name "$STORAGE_ACCOUNT_STD" \
  --resource-group "$RESOURCE_GROUP"
```

---

## CORS designation

Configured on the **storage account**, not in FastAPI — FastAPI's CORS
middleware has no bearing on requests the browser makes to
`*.blob.core.windows.net`. A failed preflight surfaces in the browser as an
opaque network error with no useful detail, so verify it in isolation
(bottom of this section) before debugging any upload logic.

**Seven allowed headers, not four.** The SDK the client bundles
(`@azure/storage-blob` `BlockBlobClient`) sends `x-ms-version`,
`x-ms-client-request-id` and `x-ms-useragent` on every request, and lists
all three in the preflight's `Access-Control-Request-Headers`. None was in
the original list; any one of them missing fails every upload. The first two
were found by measuring the SDK's outgoing calls, the third only by driving
a real preflight in a real browser — see the escalation in
[`spec/tasks/completed/2_azure_resources.md`](spec/tasks/completed/2_azure_resources.md)
§4.

```sh
az storage cors add \
  --account-name "$STORAGE_ACCOUNT_STD" \
  --services b \
  --methods PUT OPTIONS \
  --origins "https://cloudgene.qcif.edu.au" \
  --allowed-headers \
      x-ms-blob-type \
      x-ms-blob-content-type \
      content-type \
      content-length \
      x-ms-version \
      x-ms-client-request-id \
      x-ms-useragent \
  --exposed-headers etag x-ms-request-id \
  --max-age 3600 \
  --auth-mode login
```

`--auth-mode login` needs **Storage Account Contributor** on the account —
the data-plane roles used elsewhere in this file are not sufficient for
setting service properties. Add `--account-key` instead if shared key access
is still enabled and the role is not available.

Note `cors add` **appends**. Re-running it stacks duplicate rules rather
than updating in place, so to change the rule later, clear first:

```sh
az storage cors clear --account-name "$STORAGE_ACCOUNT_STD" --services b \
  --auth-mode login
```

Then confirm what is actually applied:

```sh
az storage cors list --account-name "$STORAGE_ACCOUNT_STD" --services b \
  --auth-mode login --output json
```

<details>
<summary>Declarative alternative via the ARM API</summary>

Equivalent, and idempotent where `cors add` is not — but this is a **`PUT`
on the whole blob service properties resource**, so anything else configured
there (soft delete retention, versioning, change feed) is reset to default
unless it is included in the body. `GET` the current properties first and
merge, or prefer `az storage cors add` above.

```sh
az rest --method put \
  --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$STORAGE_ACCOUNT_STD/blobServices/default?api-version=2023-01-01" \
  --headers "Content-Type=application/json" \
  --body '{
    "properties": {
      "cors": {
        "corsRules": [
          {
            "allowedOrigins": ["https://cloudgene.qcif.edu.au"],
            "allowedMethods": ["PUT", "OPTIONS"],
            "allowedHeaders": ["x-ms-blob-type", "x-ms-blob-content-type", "content-type", "content-length", "x-ms-version", "x-ms-client-request-id", "x-ms-useragent"],
            "exposedHeaders": ["etag", "x-ms-request-id"],
            "maxAgeInSeconds": 3600
          }
        ]
      }
    }
  }'
```

</details>

### Verifying the rule without the app

A preflight is a plain `OPTIONS` request and needs no credentials, so this
checks the rule in isolation. A `200` with an
`access-control-allow-origin` header means the rule is right; a `403` means
it is not, and the body names the reason.

```sh
curl -sS -i -X OPTIONS \
  "https://${STORAGE_ACCOUNT_STD}.blob.core.windows.net/${CONTAINER_NAME}/probe.txt" \
  -H "Origin: https://cloudgene.qcif.edu.au" \
  -H "Access-Control-Request-Method: PUT" \
  -H "Access-Control-Request-Headers: x-ms-blob-type,x-ms-blob-content-type,content-type,content-length,x-ms-version,x-ms-client-request-id,x-ms-useragent"
```

Drop one header from that list and re-run it to see the failure the missing
headers would have caused.
