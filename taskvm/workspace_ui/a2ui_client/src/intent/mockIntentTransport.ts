/**
 * mockIntentTransport — recorded fixtures shaped EXACTLY like the real
 * endpoint's answers (a2ui_transport.apply_intent + ParsedIntent
 * .to_payload). Same discipline as mockMessages.ts: the island only ever
 * sees `IntentResponse`s; the mock swaps the source, never the shape.
 */
import type { IntentResponse, IntentTransport } from './intentTransport';

export const MOCK_INTENT_RESPONSES: Record<
  'local_patch' | 'goal_patch' | 'checkpoint' | 'rollback' | 'clarify' | 'error',
  IntentResponse
> = {
  local_patch: {
    ok: true,
    kind: 'local_patch',
    result: { status: 'patched', revision: 3 },
    intent: {
      kind: 'local_patch',
      source: 'model',
      updates: { release_date: '2026-08-30' },
      rationale: '用户要求把发布日期推迟到 8 月底，release_date 是可编辑变量',
    },
  },
  goal_patch: {
    ok: true,
    kind: 'goal_patch',
    result: { status: 'recomposed', revision: 4 },
    intent: {
      kind: 'goal_patch',
      source: 'model',
      goal: '把发布会日期改到 8 月底并通知所有参会人，控制在预算内',
      constraints: ['预算不超过 5000 元'],
      scope: ['发布', '通知'],
      success_criteria: ['所有参会人收到新日期通知'],
      rationale: '用户补充了预算约束，需要修订目标',
    },
  },
  checkpoint: {
    ok: true,
    kind: 'checkpoint',
    result: { status: 'checkpointed', revision: 5 },
    intent: {
      kind: 'checkpoint',
      source: 'model',
      checkpoint_label: '日期已确认',
      rationale: '日期变更已落盘，用户要求在此处打检查点',
    },
  },
  rollback: {
    ok: true,
    kind: 'rollback',
    result: { status: 'rolled_back', revision: 6 },
    intent: {
      kind: 'rollback',
      source: 'model',
      checkpoint_label: '日期已确认',
      rationale: '用户要求回到日期已确认的状态',
    },
  },
  clarify: {
    ok: true,
    kind: 'clarify',
    question: '您想把发布日期改到哪一天？8 月 28 日还是 8 月 31 日？',
    intent: {
      kind: 'clarify',
      source: 'clarify',
      question: '您想把发布日期改到哪一天？8 月 28 日还是 8 月 31 日？',
    },
  },
  error: {
    ok: false,
    error: 'readonly variable: budget（该变量不可编辑）',
  },
};

export interface MockIntentTransport extends IntentTransport {
  /** every text the console submitted, in order (assertion surface) */
  calls: string[];
  /** queue the next responses; the last entry repeats */
  script(next: IntentResponse[]): void;
}

/** A scriptable mock transport — tests drive the five kinds + failures. */
export function createMockIntentTransport(
  initial: IntentResponse[] = [],
): MockIntentTransport {
  const calls: string[] = [];
  let queue = [...initial];
  return {
    calls,
    script(next: IntentResponse[]) {
      queue = [...next];
    },
    async postIntent(text: string) {
      calls.push(text);
      const head = queue.length > 1 ? queue.shift() : queue[0];
      if (head === undefined) {
        return { ok: false, error: 'mock transport exhausted' };
      }
      return head;
    },
  };
}
