# Contacts

Utility scripts for managing contacts.

## Getting Started

Install dependencies

```sh
npm install
```

## Scripts

**merge-contacts** will merge multiple vcf files into one:

```sh
npm run merge-contacts contacts1.vcf contacts2.vcf
# outputs to merged-contacts.vcf
```

**prune-contacts** will remove contacts missing email or phone numbers:

```sh
npm run prune-contacts contacts.vcf
# outputs to pruned-contacts.vcf
```
