/**
 * IntentConsole — the A6 free-text intent console (fixed shell chrome).
 *
 * One textarea + one POST: the user's free text goes to the frozen A6
 * endpoint contract (`POST /api/app/a2ui/intent`, body `{"text": ...}`),
 * and the answer renders as STRUCTURED truth:
 *
 *  - the four executable kinds → a structured intent echo card
 *    (updates / goal+constraints / checkpoint_label) with its rationale;
 *  - `clarify` → a question card that executes NOTHING — the honest
 *    "could not map this to a governance command" verdict;
 *  - 4xx / network failure → an error surface, verbatim.
 *
 * The console is FIXED React chrome (never model-generated, never
 * hidden); it lives inside the governance shell, outside the dynamic
 * task region. GUI-only: it renders labels/values the server echoed —
 * never ids, never deep links.
 */
import { useId, useState } from 'react';
import {
  realIntentTransport,
  type IntentResponse,
  type IntentTransport,
} from './intentTransport';

const KIND_LABELS: Record<string, string> = {
  local_patch: '修改变量',
  goal_patch: '修订目标',
  checkpoint: '设置检查点',
  rollback: '回退到检查点',
};

type ConsolePhase =
  | { phase: 'idle' }
  | { phase: 'pending'; text: string }
  | { phase: 'done'; text: string; response: IntentResponse }
  | { phase: 'failed'; text: string; error: string };

export interface IntentConsoleProps {
  /** injectable transport — mock-first in tests, the real POST by default */
  transport?: IntentTransport;
  /** notified after an ok:true parse — the parent echoes it honestly */
  onSubmit?: (response: IntentResponse, text: string) => void;
}

function formatValue(v: unknown): string {
  if (v === null) return 'null';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

function Chips({ title, items }: { title: string; items?: string[] }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="intent-card__chips">
      <span className="intent-card__chip-title">{title}</span>
      <div className="intent-chips">
        {items.map((it) => (
          <span key={it} className="intent-chip">
            {it}
          </span>
        ))}
      </div>
    </div>
  );
}

export function IntentConsole({
  transport = realIntentTransport,
  onSubmit,
}: IntentConsoleProps) {
  const [text, setText] = useState('');
  const [state, setState] = useState<ConsolePhase>({ phase: 'idle' });
  const inputId = useId();

  const pending = state.phase === 'pending';
  const canSubmit = text.trim().length > 0 && !pending;

  const submit = async () => {
    const trimmed = text.trim();
    if (!trimmed || pending) return;
    setState({ phase: 'pending', text: trimmed });
    // a transport-level rejection (network down, etc.) is an honest
    // failure surface — never an unhandled crash, never a fake success
    const response = await transport.postIntent(trimmed).catch(
      (e): IntentResponse => ({
        ok: false,
        error: e instanceof Error ? e.message : String(e),
      }),
    );
    setState((s) => {
      // a newer submit superseded this answer — drop the stale frame
      if (s.phase !== 'pending' || s.text !== trimmed) return s;
      return response.ok
        ? { phase: 'done', text: trimmed, response }
        : { phase: 'failed', text: trimmed, error: response.error };
    });
    if (response.ok) onSubmit?.(response, trimmed);
  };

  return (
    <section className="intent-console" data-testid="intent-console">
      <h2 className="plane__section-title">自由指令</h2>
      <p className="intent-console__hint">
        用一句话描述你想改什么，任务会解析成结构化意图后经治理端口执行。
      </p>
      <div className="intent-console__row">
        <label className="sr-only" htmlFor={inputId}>
          自由文本指令
        </label>
        <textarea
          id={inputId}
          data-testid="intent-input"
          rows={2}
          placeholder="例如：把发布日期改到 8 月 30 日"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              void submit();
            }
          }}
        />
        <button
          type="button"
          className="gov-btn gov-btn--primary"
          data-testid="intent-submit"
          onClick={() => void submit()}
          disabled={!canSubmit}
        >
          解析
        </button>
      </div>

      {pending && (
        <p className="intent-console__pending" data-testid="intent-pending">
          解析中…
        </p>
      )}

      {state.phase === 'failed' && (
        <div
          className="intent-console__error"
          data-testid="intent-error"
          role="alert"
        >
          意图解析失败：{state.error}
        </div>
      )}

      {state.phase === 'done' && state.response.ok && (
        <IntentAnswerCard response={state.response} />
      )}
    </section>
  );
}

function IntentAnswerCard({ response }: { response: IntentResponse }) {
  if (!response.ok) return null;

  if (response.kind === 'clarify') {
    return (
      <div
        className="intent-card intent-card--clarify"
        data-testid="intent-clarify"
      >
        <header className="intent-card__head">
          <span aria-hidden>❓</span> 需要澄清
        </header>
        <p className="intent-card__question">{response.question}</p>
        <p className="intent-card__note">该问题不会执行任何变更</p>
        {response.intent.rationale && (
          <p className="intent-card__rationale">{response.intent.rationale}</p>
        )}
      </div>
    );
  }

  const payload = response.intent;
  const kindLabel = KIND_LABELS[response.kind] ?? response.kind;
  return (
    <div
      className="intent-card"
      data-testid="intent-result"
      data-kind={response.kind}
    >
      <header className="intent-card__head">
        <span aria-hidden>✅</span> {kindLabel} · 已执行
      </header>

      {response.kind === 'local_patch' && payload.updates && (
        <ul className="intent-updates">
          {Object.entries(payload.updates).map(([key, value]) => (
            <li key={key}>
              <code>{key}</code>
              <span aria-hidden>→</span>
              <strong>{formatValue(value)}</strong>
            </li>
          ))}
        </ul>
      )}

      {response.kind === 'goal_patch' && (
        <>
          {payload.goal && <p className="intent-card__goal">{payload.goal}</p>}
          <Chips title="约束" items={payload.constraints} />
          <Chips title="范围" items={payload.scope} />
          <Chips title="成功标准" items={payload.success_criteria} />
        </>
      )}

      {(response.kind === 'checkpoint' || response.kind === 'rollback') && (
        <p className="intent-card__checkpoint">
          <span aria-hidden>🚩</span> {payload.checkpoint_label ?? ''}
        </p>
      )}

      {payload.rationale && (
        <p className="intent-card__rationale">{payload.rationale}</p>
      )}
    </div>
  );
}
