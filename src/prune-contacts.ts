import vCard = require("vcf");
import fs = require("fs");
import yargs = require("yargs");

const argv = yargs
  .usage("$0 contacts.vcf")
  .option("output", {
    alias: "o",
    type: "string",
    description: "output filename",
    default: "pruned-contacts.vcf",
  })
  .help()
  .alias("help", "h").argv;

const path = argv._[0];
const fileBuffer = fs.readFileSync(path);
const contacts = vCard.parse(fileBuffer);
console.log(`Read ${contacts.length} contacts from ${path}.`);

const filteredContacts = contacts.filter(
  (contact) => contact.data.email && contact.data.tel
);

console.log(`Writing ${filteredContacts.length} contacts to ${argv.output}.`);
fs.writeFileSync(
  argv.output,
  filteredContacts.map((contact) => contact.toString()).join("\n")
);
