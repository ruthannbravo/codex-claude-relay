// Call before every live model call in your test runner. Inside a relay session RELAY_CALL_BUDGET points at a
// shared ledger; each call reserves one slot with an atomic mkdir, so parallel or repeated runs share one cap.
// Outside the relay (variable unset) it does nothing and your runner's own limits apply.
import {mkdirSync, readFileSync} from 'node:fs'
import {join} from 'node:path'

export function reserveRelayCall(directory = process.env.RELAY_CALL_BUDGET) {
  if (directory === undefined) return
  if (!directory) throw Error('RELAY_CALL_BUDGET is set but empty')
  const {limit} = JSON.parse(readFileSync(join(directory, 'budget.json'), 'utf8'))
  if (!Number.isSafeInteger(limit) || limit < 0) throw Error('Invalid relay call budget')
  for (let i = 0; i < limit; i++) {
    try {mkdirSync(join(directory, `call-${i}`)); return}
    catch (error) {if (error.code !== 'EEXIST') throw error}
  }
  throw Error('Relay call budget used up; no call made')
}
