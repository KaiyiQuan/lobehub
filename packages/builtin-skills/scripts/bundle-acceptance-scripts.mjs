// Keep raw executable resources portable across all application bundlers.
// Regenerate with: pnpm --filter @lobechat/builtin-skills bundle:acceptance-scripts
import { readdirSync, readFileSync, writeFileSync } from 'node:fs';

const directory = new URL('../src/acceptance/scripts/', import.meta.url);
const resources = Object.fromEntries(
  readdirSync(directory)
    .sort()
    .map((name) => [`scripts/${name}`, readFileSync(new URL(name, directory), 'utf8')]),
);
writeFileSync(
  new URL('../src/acceptance/scriptResources.generated.json', import.meta.url),
  JSON.stringify(resources, null, 2) + '\n',
);
