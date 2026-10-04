import { expect, test } from 'claude-code/testing'

import { wrapText } from './register'

test('wrapText keeps short lines and existing newlines', () => {
  expect(wrapText('привет', 40)).toBe('привет')
  expect(wrapText('a\n\nb', 40)).toBe('a\n\nb')
})

test('wrapText breaks at word boundaries', () => {
  expect(wrapText('один два три', 8)).toBe('один два\nтри')
})

test('wrapText hard-breaks a word longer than the whole width', () => {
  expect(wrapText('https://example.com/very/long/path', 10)).toBe(
    'https://ex\nample.com/\nvery/long/\npath',
  )
})
