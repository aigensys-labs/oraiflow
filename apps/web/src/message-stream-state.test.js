import test from 'node:test'
import assert from 'node:assert/strict'
import {
  clearMessageRevealStates,
  getMessageRevealState,
  saveMessageRevealState,
} from './message-stream-state.js'

test('completed messages remain complete after their panel remounts', () => {
  clearMessageRevealStates()
  const text = 'A long assistant message that has already finished streaming.'
  saveMessageRevealState('message-1', { sourceText: text, visibleText: text, completed: true })
  assert.deepEqual(getMessageRevealState('message-1', text), {
    visibleText: text,
    completed: true,
  })
})

test('an in-progress message resumes from its previous reveal position', () => {
  clearMessageRevealStates()
  saveMessageRevealState('message-2', {
    sourceText: 'The complete response is arriving in chunks.',
    visibleText: 'The complete response',
    completed: false,
  })
  assert.deepEqual(getMessageRevealState('message-2', 'The complete response is arriving in chunks.'), {
    visibleText: 'The complete response',
    completed: false,
  })
})

test('a growing message preserves its current visible prefix', () => {
  clearMessageRevealStates()
  saveMessageRevealState('message-3', {
    sourceText: 'The response is growing',
    visibleText: 'The response',
    completed: false,
  })
  assert.deepEqual(getMessageRevealState('message-3', 'The response is growing while the model streams.'), {
    visibleText: 'The response',
    completed: false,
  })
})

test('unrelated replacement content does not inherit another message reveal state', () => {
  clearMessageRevealStates()
  saveMessageRevealState('message-4', {
    sourceText: 'Previous answer', visibleText: 'Previous', completed: false,
  })
  assert.equal(getMessageRevealState('message-4', 'A different answer'), null)
})


test('reveal cache is bounded when many agent messages have accumulated', () => {
  clearMessageRevealStates()
  for (let index = 0; index < 520; index += 1) {
    const text = `message ${index}`
    saveMessageRevealState(`message-${index}`, { sourceText: text, visibleText: text, completed: true })
  }
  assert.equal(getMessageRevealState('message-0', 'message 0'), null)
  assert.deepEqual(getMessageRevealState('message-519', 'message 519'), {
    visibleText: 'message 519', completed: true,
  })
})
