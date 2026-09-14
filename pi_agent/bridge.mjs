// Pi owns the agent loop. Python owns model credentials, context and all tools.
// The child receives only bounded projections over anonymous stdin/stdout pipes.
import { Agent } from '@earendil-works/pi-agent-core';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';

// The source timestamp is app metadata, not conversation prose. Only the LLM
// projection receives the annotation; stored AgentMessages remain unchanged.
export function messagesForModel(messages) {
  return messages.map(message => {
    const { messageTime, ...projected } = message;
    if (!messageTime || !['user', 'assistant'].includes(message.role)) return projected;
    const sentAt = messageTime.sent_at;
    const timezone = messageTime.timezone;
    if (typeof sentAt !== 'string' || !/^\d{4}-\d{2}-\d{2}T[0-9:.+Z-]+$/.test(sentAt)
      || sentAt.length > 40 || typeof timezone !== 'string'
      || !/^[A-Za-z0-9_+./:-]{1,64}$/.test(timezone)) return projected;
    const annotation = `[訊息時間：${sentAt}；時區：${timezone}]\n`;
    const content = typeof projected.content === 'string'
      ? [{ type: 'text', text: projected.content }]
      : Array.isArray(projected.content) ? projected.content : [];
    return { ...projected, content: [{ type: 'text', text: annotation }, ...content] };
  });
}

function argumentFieldPaths(value, prefix = '', output = []) {
  if (output.length >= 24) return output;
  if (Array.isArray(value)) {
    if (value.length) argumentFieldPaths(value[0], `${prefix}[]`, output);
    return output;
  }
  if (!value || typeof value !== 'object') return output;
  for (const [key, child] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key;
    output.push(path.slice(0, 120));
    argumentFieldPaths(child, path, output);
    if (output.length >= 24) break;
  }
  return output;
}

function schemaFieldPaths(schema, prefix = '', output = new Set()) {
  if (!schema || typeof schema !== 'object') return output;
  for (const [key, child] of Object.entries(schema.properties ?? {})) {
    const path = prefix ? `${prefix}.${key}` : key;
    output.add(path);
    schemaFieldPaths(child, path, output);
  }
  if (schema.items && typeof schema.items === 'object') schemaFieldPaths(schema.items, `${prefix}[]`, output);
  for (const branch of schema.anyOf ?? schema.oneOf ?? []) schemaFieldPaths(branch, prefix, output);
  return output;
}

export async function runExperiment(initial, request, onAgent = undefined) {
  let stopped = false;
  let rounds = 0;
  let calls = 0;
  let finalText = '';
  const toolFailures = [];
  const toolFailureCounts = new Map();
  const toolArgumentFields = new Map();
  const allowedArgumentFields = new Map(
    initial.tools.map(tool => [tool.name, schemaFieldPaths(tool.parameters)]),
  );
  let repeatedToolFailure = false;
  let repairAttempted = false;
  let proseRepairMode = false;
  let pendingToolRefresh = false;
  let maxRounds = Math.max(1, Math.min(initial.maxRounds ?? 6, 10));
  let maxToolCalls = Math.max(1, Math.min(initial.maxToolCalls ?? 8, 16));
  const model = {
    id: 'ayue-configured-provider', name: 'Ayue configured provider',
    api: 'openai-completions', provider: 'ayue', baseUrl: '', reasoning: false,
    input: ['text'], contextWindow: 64000, maxTokens: 4096,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  };
  const provider = (_model, context) => {
    const stream = createAssistantMessageEventStream();
    const message = {
      role: 'assistant', content: [], api: model.api, provider: model.provider,
      model: model.id, stopReason: 'stop', timestamp: Date.now(),
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    };
    void (async () => {
      try {
        const result = await request('model', {
          messages: context.messages,
          toolNames: (context.tools ?? []).map(tool => tool.name),
        });
        if (result.error) {
          message.errorMessage = [
            'pi_provider_timeout', 'pi_provider_error', 'pi_model_budget_exhausted',
          ].includes(result.error)
            ? result.error : 'pi_provider_error';
          throw new Error('provider_unavailable');
        }
        if (result.content) message.content.push({ type: 'text', text: result.content });
        for (const [index, call] of (result.tool_calls ?? []).slice(0, 4).entries()) {
          message.content.push({ type: 'toolCall', id: `call_${rounds}_${index}`,
            name: call.name, arguments: call.arguments ?? {} });
        }
        message.usage.input = result.input_tokens ?? 0;
        message.usage.output = result.output_tokens ?? 0;
        message.usage.totalTokens = message.usage.input + message.usage.output;
        message.stopReason = message.content.some(c => c.type === 'toolCall') ? 'toolUse' : 'stop';
        stream.push({ type: 'start', partial: message });
        stream.push({ type: 'done', reason: message.stopReason, message });
      } catch {
        message.stopReason = 'error';
        message.errorMessage ||= 'pi_provider_error';
        stream.push({ type: 'error', reason: 'error', error: message });
      } finally { stream.end(message); }
    })();
    return stream;
  };
  let enabledToolNames = new Set(
    Array.isArray(initial.initialToolNames)
      ? initial.initialToolNames.filter(name => typeof name === 'string')
      : initial.tools.map(tool => tool.name),
  );
  const allTools = initial.tools.map(tool => ({
    ...tool, label: tool.name, executionMode: 'sequential',
    execute: async (_id, args) => {
      calls += 1;
      const result = await request('tool', { name: tool.name, arguments: args });
      if (result.upgradeBudget === true) {
        maxRounds = 9;
        maxToolCalls = 16;
      }
      if (result.stop) stopped = true;
      for (const name of result.enableTools ?? []) {
        if (allTools.some(candidate => candidate.name === name)) enabledToolNames.add(name);
      }
      if (Array.isArray(result.enableTools) && result.enableTools.length) pendingToolRefresh = true;
      return { content: [{ type: 'text', text: JSON.stringify(result.observations ?? []) }],
        details: {}, addedToolNames: result.enableTools ?? [], terminate: stopped };
    },
  }));
  const activeTools = () => allTools.filter(tool => enabledToolNames.has(tool.name));
  let agent;
  agent = new Agent({
    initialState: {
      systemPrompt: initial.systemPrompt,
      model,
      thinkingLevel: 'off',
      messages: initial.history ?? [],
      tools: activeTools(),
    },
    streamFn: provider,
    convertToLlm: messagesForModel,
    toolExecution: 'sequential',
    beforeToolCall: async ({ toolCall }) => {
      if (proseRepairMode) {
        return { block: true, reason: 'reply_repair_has_no_tools', terminate: false };
      }
      if (!enabledToolNames.has(toolCall.name)) {
        return { block: true, reason: 'tool_not_enabled', terminate: false };
      }
      if (stopped || calls >= maxToolCalls) {
        return { block: true, reason: 'confirmation_or_budget_stop', terminate: true };
      }
      return undefined;
    },
    afterToolCall: async ({ result, isError }) => ({
      content: result.content,
      details: result.details,
      isError,
      terminate: stopped || result.terminate === true,
    }),
    transformContext: async messages => {
      if (messages.length <= 40) return messages;
      return [...messages.slice(0, 8), ...messages.slice(-32)];
    },
    prepareNextTurnWithContext: ({ context }) => {
      if (!pendingToolRefresh) return undefined;
      pendingToolRefresh = false;
      return { context: { ...context, tools: activeTools() } };
    },
    shouldStopAfterTurn: async ({ message }) => {
      if (stopped || repeatedToolFailure) return true;
      if (message.role !== 'assistant' || message.stopReason !== 'stop'
        || message.content.some(item => item.type === 'toolCall')) return rounds >= maxRounds;
      const candidate = message.content.filter(item => item.type === 'text')
        .map(item => item.text).join('');
      if (!initial.validateFinal) {
        finalText = candidate;
        return true;
      }
      const verdict = await request('validate_final', { text: candidate, repairAttempted });
      if (verdict.accept) {
        finalText = typeof verdict.text === 'string' ? verdict.text : candidate;
        return true;
      }
      if (!repairAttempted && typeof verdict.repairPrompt === 'string'
        && verdict.repairPrompt && rounds < maxRounds) {
        repairAttempted = true;
        if (verdict.disableTools === true) {
          proseRepairMode = true;
          enabledToolNames.clear();
          pendingToolRefresh = true;
        }
        agent.followUp({ role: 'user', content: [{ type: 'text', text: verdict.repairPrompt }], timestamp: Date.now() });
        return false;
      }
      error = typeof verdict.error === 'string' ? verdict.error : 'pi_reply_invalid';
      return true;
    },
  });
  let error = null;
  agent.subscribe(async event => {
    if (event.type === 'turn_start') rounds += 1;
    if (event.type === 'message_end' && event.message.stopReason === 'error') {
      error = event.message.errorMessage;
    }
    if (event.type === 'message_end' && event.message.role === 'assistant') {
      for (const item of event.message.content ?? []) {
        if (item.type === 'toolCall') {
          const allowed = allowedArgumentFields.get(item.name) ?? new Set();
          toolArgumentFields.set(item.id, argumentFieldPaths(item.arguments)
            .map(path => allowed.has(path) ? path : '<unknown>'));
        }
      }
    }
    if (event.type === 'tool_execution_end' && event.isError) {
      const tool = String(event.toolName ?? '').slice(0, 100);
      const attempt = (toolFailureCounts.get(tool) ?? 0) + 1;
      toolFailureCounts.set(tool, attempt);
      toolFailures.push({ tool, code: 'tool_schema_invalid', attempt,
        argumentFields: toolArgumentFields.get(event.toolCallId) ?? [] });
      if (attempt >= 2) repeatedToolFailure = true;
    }
  });
  if (onAgent) onAgent(agent);
  try {
    await agent.prompt(initial.prompt);
    await agent.waitForIdle();
  } finally {
    if (onAgent) onAgent(null);
  }
  if (repeatedToolFailure && !error) error = 'pi_tool_schema_invalid';
  return { rounds, calls, stopped, error, finalText, toolFailures: toolFailures.slice(0, 4),
    repair_attempted: repairAttempted,
    budget_exhausted: !stopped && !finalText && rounds >= maxRounds };
}

async function main() {
  if (process.argv.includes('--check')) {
    process.stdout.write('Pi agent-core ready\n');
    return;
  }
  const lines = createInterface({ input: process.stdin });
  const pending = new Map();
  let sequence = 0;
  let started = false;
  let activeAgent = null;
  const send = value => process.stdout.write(`${JSON.stringify(value)}\n`);
  const request = (type, payload) => new Promise((resolve, reject) => {
    const id = String(++sequence);
    pending.set(id, { resolve, reject });
    send({ type, id, ...payload });
  });
  lines.on('line', line => {
    if (line.length > 1000000) { process.exitCode = 1; lines.close(); return; }
    let data;
    try { data = JSON.parse(line); } catch { return; }
    if (data.type === 'start' && !started) {
      started = true;
      void runExperiment(data, request, value => { activeAgent = value; })
        .then(result => send({ type: 'done', ...result }))
        .catch(() => send({ type: 'done', error: 'pi_loop_failed' }))
        .finally(() => { lines.close(); process.stdin.destroy(); });
    } else if (data.type === 'response' && pending.has(data.id)) {
      pending.get(data.id).resolve(data.result);
      pending.delete(data.id);
    }
  });
  lines.on('close', () => {
    activeAgent?.abort();
    for (const item of pending.values()) item.reject(new Error('parent_disconnected'));
    pending.clear();
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
