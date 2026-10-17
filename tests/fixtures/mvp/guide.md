# Field Guide to Resource Groups

A short guide used by the integration tests. Every section has its own heading
so the plaintext extractor yields one locator per section.

## What a resource group is

A resource group is a container that holds related resources for one
solution. Resources in a group share a lifecycle: they are deployed, updated
and deleted together.

## Naming and tags

Use a naming convention such as rg-app-env-region. Tags record the owner, the
cost centre and the environment, and they are inherited by nothing.

## Deleting a group

Deleting a resource group deletes every resource inside it. Lock production
groups with a delete lock before anyone runs a cleanup script.
