import { expect, test } from 'claude-code/testing'

import { padLines, wrapText } from './register'

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

// The engine's repaint does not clear a row's stale tail when the new row is
// shorter — fragments of older frames stay glued after the fresh text. Every
// drawn line must be padded to the full budget so a repaint overwrites the
// whole row.
test('padLines pads every line to the width, longer lines untouched', () => {
  expect(padLines('ab', 5)).toBe('ab   ')
  expect(padLines('abcde', 5)).toBe('abcde')
  expect(padLines('abcdef', 5)).toBe('abcdef')
  expect(padLines('ab\nc', 4)).toBe('ab  \nc   ')
})
