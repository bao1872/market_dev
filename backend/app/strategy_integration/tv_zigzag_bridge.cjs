// Transport only. All pivot/projection logic is the compiled existing TS owner.
const {computeZigZag} = require('./tv_zigzag_core.cjs')
const readline = require('node:readline')
async function main() {
for await (const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})) {
  try {
    const input = JSON.parse(line)
    const result = computeZigZag(input.bars.map(([high,low,volume])=>({high,low,volume})))
    const segment = result.projected ?? result.segments.at(-1) ?? null
    process.stdout.write(JSON.stringify(input.mode==='overlay' ? result : {segment,projected:!!result.projected})+'\n')
  } catch (error) {
    process.stdout.write(JSON.stringify({error:String(error)})+'\n')
  }
}
}
main().catch(error=>{process.stderr.write(String(error));process.exitCode=1})
