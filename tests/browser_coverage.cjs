const fs = require("node:fs");
const path = require("node:path");
const v8ToIstanbul = require("v8-to-istanbul");
const { createCoverageMap } = require("istanbul-lib-coverage");

async function main() {
  const [input, output] = process.argv.slice(2);
  if (!input || !output) {
    throw new Error("Usage: node tests/browser_coverage.cjs INPUT.json OUTPUT.json");
  }
  const scripts = JSON.parse(fs.readFileSync(input, "utf8"));
  if (!Array.isArray(scripts) || scripts.length === 0) {
    throw new Error("No dashboard browser coverage was captured.");
  }
  const coverage = createCoverageMap({});
  const source = scripts[0].source;
  const filename = path.resolve("bin/dashboard-inline.js");
  for (const script of scripts) {
    if (script.source !== source) {
      throw new Error("Browser coverage includes different dashboard versions.");
    }
    const converter = v8ToIstanbul(filename, 0, { source });
    await converter.load();
    converter.applyCoverage(script.functions);
    coverage.merge(converter.toIstanbul());
  }
  fs.writeFileSync(output, JSON.stringify({ source, coverage: coverage.toJSON() }, null, 2));
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
