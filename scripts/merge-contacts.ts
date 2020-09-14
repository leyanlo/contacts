import vCard = require("vcf");
import fs = require("fs");
import yargs = require("yargs");

const argv = yargs
  .usage("$0 contacts1.vcf contacts2.vcf ...")
  .option("output", {
    alias: "o",
    type: "string",
    description: "output filename",
    default: "merged-contacts.vcf",
  })
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
