import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

// NODE_OPTIONS reaches Vitest's workers as well as its main process.
// jsdom provides browser storage; Node's native storage must not shadow it.
const result = spawnSync(process.execPath, [
  fileURLToPath(new URL('../node_modules/vitest/vitest.mjs', import.meta.url)),
  'run',
  ...process.argv.slice(2),
], {
  stdio: 'inherit',
  env: {
    ...process.env,
    NODE_OPTIONS: `${process.env.NODE_OPTIONS ?? ''} --no-experimental-webstorage`.trim(),
  },
})

if (result.error) console.error(result.error.message)
process.exit(result.status ?? 1)
