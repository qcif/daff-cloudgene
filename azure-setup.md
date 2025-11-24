# Azure batch setup

First set up a billing account "DASS development", and subscription "DAFF Biosecurity" to contain these resources.

After creating the subscription, it takes a little while for the necessary resources to spawn. To wait for them to come online:

```sh
# Wait until this shows "Registered" or you will get "Subscription not found" error when you continue
watch -n 30 'az provider show -n Microsoft.Storage --subscription "DAFF Biosecurity" --query registrationState'
```

## Resources

- subscription: DAFF Biosecurity
- region: australiaeast
- resource group: daff-biosecurity
- storage accounts: daffpremium, daffstandard
- storage container: refdata, workdata
- batch account: daffbatch

```sh
az account set --subscription "DAFF Biosecurity"
az group create -n daff-biosecurity -l australiaeast
az storage account create \
  -n daffpremium \
  -g daff-biosecurity \
  -l australiaeast \
  --sku Premium_LRS \
  --kind BlockBlobStorage
az storage container create \
  --name refdata \
  --account-name daffpremium
az storage account create \
  -n daffstandard \
  -g daff-biosecurity \
  -l australiaeast \
  --sku Standard_LRS
az storage container create \
  --name workdata \
  --account-name daffstandard
az batch account create \
  --name daffbatch \
  --resource-group daff-biosecurity \
  --location australiaeast \
  --storage-account daffstandard
```

Now get credentials required for NF config:

```sh
az batch account login \
  --name daffbatch \
  --resource-group daff-biosecurity \
  --shared-key-auth

# These have been saved in 1Password/DevOps:
az storage account keys list -g daff-biosecurity -n daffstandard
az storage account keys list -g daff-biosecurity -n daffpremium
az batch account keys list -g daff-biosecurity -n daffbatch
```
