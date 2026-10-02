import { expect, test } from 'claude-code/testing'

import { parseValidProfiles } from './register'

test('parseValidProfiles picks only ✓ ok lines', () => {
  expect(parseValidProfiles('alpha ✓ ok\nbeta ✗ broken\nNo profiles yet — run: tg-messenger --profile NAME login\n')).toEqual([
    'alpha',
  ])
})

test('parseValidProfiles keeps several, empty on junk', () => {
  expect(parseValidProfiles('a ✓ ok\nb ✓ ok\n')).toEqual(['a', 'b'])
  expect(parseValidProfiles('')).toEqual([])
})
