# Contacts

Utility scripts for managing contacts.

## ts-node

These scripts run with ts-node. Install using npm:

```sh
npm install -g typescript
npm install -g ts-node
```

## Scripts

**merge-contacts.ts** will merge multiple vcf files into one:

```sh
ts-node ./src/merge-contacts.ts contacts1.vcf contacts2.vcf
# outputs to merged-contacts.vcf
```

**prune-contacts.ts** will remove contacts missing email or phone numbers:

```sh
ts-node ./src/prune-contacts.ts contacts.vcf
# outputs to pruned-contacts.vcf
```
