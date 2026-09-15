import assert from 'node:assert/strict';
import test from 'node:test';
import { messagesForModel, runExperiment } from './bridge.mjs';

const tool = name => ({ name, description: 'Test tool', parameters: {
  type: 'object', properties: { value: { type: 'string' } }, required: ['value'], additionalProperties: false,
} });
const initial = { prompt: 'test', systemPrompt: 'test', tools: [tool('prepare'), tool('read')] };

test('actual Pi loop feeds tool observations into the next model turn', async () => {
  let models = 0;
  const result = await runExperiment(initial, async (type, payload) => {
    if (type === 'tool') return { observations: [{ status: 'ok', value: 'observed' }] };
    models++;
    if (models === 1) return { tool_calls: [{ name: 'read', arguments: { value: 'query' } }] };
    assert.equal(payload.messages.at(-1).role, 'toolResult');
    assert.match(JSON.stringify(payload.messages.at(-1)), /observed/);
    return { content: 'finished', tool_calls: [] };
  });
  assert.equal(models, 2);
  assert.equal(result.calls, 1);
  assert.equal(result.finalText, 'finished');
});

test('a question is returned verbatim even without tools', async () => {
  const history = [{ role: 'user', content: [{ type: 'text', text: 'earlier user message' }], timestamp: 0 }];
  const result = await runExperiment({ ...initial, history }, async (_type, payload) => {
    assert.equal(payload.messages[0].content[0].text, 'earlier user message');
    return { content: 'Which friend?', tool_calls: [] };
  });
  assert.equal(result.finalText, 'Which friend?');
  assert.equal(result.rounds, 1);
});

test('a provider timeout retains its code', async () => {
  const result = await runExperiment(initial, async () => ({ error: 'pi_provider_timeout' }));
  assert.equal(result.error, 'pi_provider_timeout');
  assert.equal(result.calls, 0);
});

test('a prepared confirmation stops remaining calls in the same batch', async () => {
  const executed = [];
  const result = await runExperiment(initial, async (type, payload) => {
    if (type === 'model') return { tool_calls: ['prepare', 'read'].map(name => ({ name, arguments: { value: 'x' } })) };
    executed.push(payload.name);
    return { observations: [{ pending_confirmation: true }], stop: true };
  });
  assert.deepEqual(executed, ['prepare']);
  assert.equal(result.rounds, 1);
  assert.equal(result.stopped, true);
});

test('repeated invalid tool schema falls back to one bounded prose-only recovery', async () => {
  let models = 0;
  const result = await runExperiment(initial, async (type, payload) => {
    assert.equal(type, 'model');
    models += 1;
    if (models === 3) {
      assert.deepEqual(payload.toolNames, []);
      assert.match(JSON.stringify(payload.messages), /明確指出未完成的需求/);
      return { content: '部分查詢完成，但排程尚未建立。', tool_calls: [] };
    }
    return { tool_calls: [{ name: 'prepare', arguments: { user_id: 'forged' } }] };
  });
  assert.equal(result.calls, 0);
  assert.equal(result.rounds, 3);
  assert.equal(result.error, null);
  assert.equal(result.finalText, '部分查詢完成，但排程尚未建立。');
  assert.equal(result.schema_recovery_attempted, true);
  assert.deepEqual(result.toolFailures.map(item => item.attempt), [1, 2]);
  assert.deepEqual(result.toolFailures[0].argumentFields, ['<unknown>']);
  assert.equal(result.budget_exhausted, false);
});

test('a tool-requested recovery disables tools before the next model turn', async () => {
  let models = 0;
  const result = await runExperiment(initial, async (type, payload) => {
    if (type === 'tool') return {
      observations: [{ status: 'failed', error_code: 'location_not_found' }],
      disableTools: true,
      recoveryPrompt: '請根據成功結果回答，並說明後續地點搜尋未完成。',
    };
    models += 1;
    if (models === 1) {
      return { tool_calls: [{ name: 'read', arguments: { value: 'place' } }] };
    }
    assert.deepEqual(payload.toolNames, []);
    assert.match(JSON.stringify(payload.messages), /後續地點搜尋未完成/);
    return { content: '已找到餐廳，但附近飲料店尚未找到。', tool_calls: [] };
  });
  assert.equal(result.finalText, '已找到餐廳，但附近飲料店尚未找到。');
  assert.equal(result.error, null);
  assert.equal(result.rounds, 2);
});

test('model-only timestamps survive tool rounds without changing history', async () => {
  const history = [{
    role: 'user', content: [{ type: 'text', text: '下週四去駁二' }], timestamp: 0,
    messageTime: { sent_at: '2026-09-14T15:53:05+08:00', timezone: 'Asia/Taipei' },
  }];
  const original = structuredClone(history);
  let models = 0;
  const result = await runExperiment({ ...initial, history }, async (type, payload) => {
    if (type === 'tool') return { observations: [{ status: 'ok' }] };
    models++;
    const text = payload.messages[0].content.map(item => item.text).join('');
    assert.equal(text, '[訊息時間：2026-09-14T15:53:05+08:00；時區：Asia/Taipei]\n下週四去駁二');
    assert.equal('messageTime' in payload.messages[0], false);
    if (models === 1) return { tool_calls: [{ name: 'read', arguments: { value: 'query' } }] };
    assert.equal(payload.messages.at(-1).role, 'toolResult');
    return { content: '要安排下午五點嗎？', tool_calls: [] };
  });
  assert.deepEqual(history, original);
  assert.equal(result.finalText, '要安排下午五點嗎？');
  assert.equal(models, 2);
});

test('time projection preserves tool identities and excludes invalid metadata', () => {
  const messages = [
    { role: 'assistant', content: [{ type: 'toolCall', id: 'call1', name: 'read', arguments: {} }], timestamp: 0 },
    { role: 'toolResult', toolCallId: 'call1', toolName: 'read', content: [{ type: 'text', text: 'ok' }], timestamp: 0 },
    { role: 'user', content: [{ type: 'text', text: '明天' }], timestamp: 0,
      messageTime: { sent_at: 'not a timestamp', timezone: 'Asia/Taipei' } },
  ];
  const projected = messagesForModel(messages);
  assert.deepEqual(projected.slice(0, 2), messages.slice(0, 2));
  assert.deepEqual(projected[2].content, messages[2].content);
  assert.equal('messageTime' in projected[2], false);
});

test('the backend-selected fixed tool set is visible without keyword enablement', async () => {
  const web = tool('web.search');
  const result = await runExperiment({
    ...initial,
    tools: [web, tool('places.search_nearby')],
    initialToolNames: ['web.search', 'places.search_nearby'],
  }, async (type, payload) => {
    assert.equal(type, 'model');
    assert.deepEqual(new Set(payload.toolNames), new Set(['web.search', 'places.search_nearby']));
    return { content: 'ready', tool_calls: [] };
  });
  assert.equal(result.finalText, 'ready');
});

test('one invalid final can be repaired through the same Agent follow-up queue', async () => {
  let models = 0;
  let validations = 0;
  const result = await runExperiment({ ...initial, validateFinal: true }, async (type, payload) => {
    if (type === 'model') {
      models += 1;
      if (models === 1) return { content: '[確認卡｜已確認]', tool_calls: [] };
      assert.match(JSON.stringify(payload.messages), /上一個答案/);
      assert.deepEqual(payload.toolNames, []);
      return { content: '請告訴我出發時間。', tool_calls: [] };
    }
    assert.equal(type, 'validate_final');
    validations += 1;
    return validations === 1
      ? { accept: false, repairPrompt: '上一個答案無效，請重新回答。', disableTools: true }
      : { accept: true, text: payload.text };
  });
  assert.equal(result.finalText, '請告訴我出發時間。');
  assert.equal(result.repair_attempted, true);
  assert.equal(models, 2);
});

test('a valid final on the last allowed round is accepted before budget stop', async () => {
  const result = await runExperiment({ ...initial, maxRounds: 1, validateFinal: true }, async (type, payload) => {
    if (type === 'model') return { content: '最後一輪有效答案', tool_calls: [] };
    assert.equal(type, 'validate_final');
    return { accept: true, text: payload.text };
  });
  assert.equal(result.finalText, '最後一輪有效答案');
  assert.equal(result.budget_exhausted, false);
});
