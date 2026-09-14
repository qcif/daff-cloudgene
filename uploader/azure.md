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
# (see uploader/uploads.service):
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
            "allowedHeaders": ["x-ms-blob-type", "x-ms-blob-content-type", "content-type", "content-length"],
            "exposedHeaders": ["etag", "x-ms-request-id"],
            "maxAgeInSeconds": 3600
          }
        ]
      }
    }
  }'
```
