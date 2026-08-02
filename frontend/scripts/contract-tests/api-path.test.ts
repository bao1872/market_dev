import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import test from 'node:test'

const ROOT = resolve(import.meta.dirname, '../..')
const SRC = join(ROOT, 'src')

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return sourceFiles(path)
    return /\.(ts|tsx)$/.test(name) && !path.includes('/__tests__/') ? [path] : []
  })
}

test('API clients use baseURL /api and versioned /v1 endpoints', () => {
  const client = readFileSync(join(SRC, 'api/client.ts'), 'utf8')
  assert.equal((client.match(/baseURL:\s*['"]\/api['"]/g) ?? []).length, 3)

  const endpointPattern =
    /(?:apiClient|publicApiClient|captureClient)\.(?:get|post|put|patch|delete)(?:<[^;]+?>)?\(\s*([`'"])(\/[^`'"]*)\1/gms
  const violations: string[] = []
  for (const path of sourceFiles(SRC)) {
    const source = readFileSync(path, 'utf8')
    for (const match of source.matchAll(endpointPattern)) {
      if (!match[2].startsWith('/v1/')) violations.push(`${path}: ${match[2]}`)
    }
    assert.ok(!source.includes('/api/api/v1'), `${path} 恢复了双 /api 路径`)
  }
  assert.deepEqual(violations, [])
})

test('Vite and Nginx each remove the gateway prefix exactly once', () => {
  const vite = readFileSync(join(ROOT, 'vite.config.ts'), 'utf8')
  const nginx = readFileSync(join(ROOT, 'nginx.conf'), 'utf8')
  assert.equal((vite.match(/path\.replace\(\/\^\\\/api\//g) ?? []).length, 2)
  assert.equal((nginx.match(/rewrite \^\/api\/\(\.\*\) \/\$1 break;/g) ?? []).length, 1)
  assert.ok(!nginx.includes('location = /api/v1/health'))
})
