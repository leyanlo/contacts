#!/usr/bin/env node

const fs = require("fs");
const vCard = require("vcf");
const yargs = require("yargs/yargs");
const { hideBin } = require("yargs/helpers");

const argv = yargs(hideBin(process.argv))
  .usage("$0 contacts1.vcf contacts2.vcf ...")
  .option("output", {
    alias: "o",
    type: "string",
    description: "output filename",
    default: "merged-contacts.vcf",
  })
  .demandCommand(1, "Provide at least one .vcf file.")
  .help()
  .alias("help", "h").argv;

const cards = argv._.reduce((acc, path) => {
  const fileBuffer = fs.readFileSync(path);
  const contacts = vCard.parse(fileBuffer);
  console.log(`Read ${contacts.length} contacts from ${path}.`);
  return acc.concat(contacts);
}, []);

console.log(`Writing ${cards.length} contacts to ${argv.output}.`);
fs.writeFileSync(
  argv.output,
  cards.map((contact) => contact.toString()).join("\n")
);
