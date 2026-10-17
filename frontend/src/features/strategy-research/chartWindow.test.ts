import test from 'node:test'
import assert from 'node:assert/strict'
import {chartWindow} from './chartWindow'
test('initial and focused windows preserve 90 real bars including either boundary',()=>{
  for (const focus of [undefined,0,2,49,250,499]) {
    const r=chartWindow(500,focus)!
    assert.equal(r.to-r.from+1,90)
    assert.ok(r.from>=0 && r.to<500)
    if(focus!=null)assert.ok(r.from<=focus&&focus<=r.to)
  }
})
test('short history remains truthful and small requested viewports still preserve 90',()=>{
  assert.deepEqual(chartWindow(45),{from:0,to:44})
  const r=chartWindow(150,undefined,22)!
  assert.equal(r.to-r.from+1,90)
  assert.equal(chartWindow(0),null)
})
