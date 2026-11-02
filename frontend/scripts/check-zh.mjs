import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'
import { resolve } from 'node:path'
import { parse } from '@formatjs/icu-messageformat-parser'

const catalogDirectory = [resolve('messages'), resolve('../messages')].find((path) =>
  existsSync(resolve(path, 'zh.json'))
)
assert.ok(catalogDirectory, 'Translation catalogs must be available')
const english = JSON.parse(readFileSync(resolve(catalogDirectory, 'en.json'), 'utf8'))
const chinese = JSON.parse(readFileSync(resolve(catalogDirectory, 'zh.json'), 'utf8'))

function flatten(value, prefix = '', result = {}) {
  if (typeof value === 'string') result[prefix] = value
  else for (const [key, child] of Object.entries(value)) {
    flatten(child, prefix ? `${prefix}.${key}` : key, result)
  }
  return result
}

function argumentsOf(nodes, result = new Set()) {
  for (const node of nodes) {
    if ([1, 2, 3, 5, 6, 8].includes(node.type)) result.add(`${node.type}:${node.value}`)
    if (node.children) argumentsOf(node.children, result)
    if (node.options) for (const option of Object.values(node.options)) {
      argumentsOf(option.value, result)
    }
  }
  return [...result].sort()
}

const source = flatten(english)
const translated = flatten(chinese)
assert.deepEqual(Object.keys(translated).sort(), Object.keys(source).sort(), 'Catalog keys differ')
for (const [key, value] of Object.entries(source)) {
  assert.ok(translated[key].trim(), `Empty translation: ${key}`)
  assert.deepEqual(
    argumentsOf(parse(translated[key])),
    argumentsOf(parse(value)),
    `ICU arguments or rich-text tags changed: ${key}`
  )
}
assert.equal(chinese.common.appName, 'FreeLingo')
assert.deepEqual(chinese.landing.languageGreetings, english.landing.languageGreetings)
assert.equal(chinese.auth.login.title, '登录')
assert.equal(chinese.settings.uiLocale, '界面语言')
console.log(`Validated ${Object.keys(source).length} Chinese messages, ICU arguments and learning examples`)
