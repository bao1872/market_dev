// Compile the EXISTING chart owner for Node; never maintain a Python ZigZag copy.
import {createRequire} from 'node:module'
import {readFileSync, writeFileSync} from 'node:fs'
import {createHash} from 'node:crypto'
import {resolve} from 'node:path'
const require = createRequire(resolve('frontend/package.json'))
const {buildSync} = require('esbuild')
const source = 'frontend/src/components/zigzag.ts'
const output = 'backend/app/strategy_integration/tv_zigzag_core.cjs'
const manifest = 'backend/app/strategy_integration/tv_zigzag_core.json'
const sha = x => createHash('sha256').update(x).digest('hex')
const bytes = buildSync({entryPoints:[source],bundle:true,format:'cjs',platform:'node',target:'node18',
  write:false,legalComments:'none',banner:{js:'// GENERATED from frontend/src/components/zigzag.ts; use tools/build_strategy_zigzag_bridge.mjs'}}).outputFiles[0].contents
const record = Buffer.from(JSON.stringify({source,source_sha256:sha(readFileSync(source)),bundle_sha256:sha(bytes)},null,2)+'\n')
if (process.argv.includes('--check')) {
  if (!Buffer.from(bytes).equals(readFileSync(output)) || !record.equals(readFileSync(manifest))) throw Error('TV_ZIGZAG_BRIDGE_STALE')
} else {
  writeFileSync(output,bytes);writeFileSync(manifest,record)
}
