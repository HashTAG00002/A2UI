/**
 * mockMessages — a recorded A2UI v0.9 message stream shaped EXACTLY like
 * what the production backend emits (`taskvm/genui` protocol.py +
 * data_model.py): server-owned createSurface/updateDataModel, decoder-
 * owned updateComponents with correct `{"path": ...}` bindings, and the
 * single allowed action `taskvm.local_patch`.
 *
 * Wave A5 drives the island from these mocks; the SSE transport replaces
 * the source of the same messages in the next wave without any component
 * changes (the island only ever sees `A2uiMessage[]`).
 */
import {
  createSurfaceMessage,
  surfaceIdForSession,
  updateComponentsMessage,
  updateDataModelMessage,
  type A2uiMessage,
} from './protocol';

export const MOCK_SESSION_ID = 'mock-demo-01';
export const MOCK_SURFACE_ID = surfaceIdForSession(MOCK_SESSION_ID);

/** Matches the backend's TaskDataModelProjector output shape. */
export const mockDataModel = {
  task: {
    goal: '把发布会日期改到 8 月底并通知所有参会人',
    status: 'ready',
  },
  variables: {
    release_date: {
      label: '发布日期',
      value_type: 'date',
      observed: '2026-08-01',
      desired: '2026-08-30',
      mutability: 'editable',
      status: 'diverged',
      confidence: 0.95,
    },
    notify_list: {
      label: '通知名单',
      value_type: 'string',
      observed: '3 人',
      desired: '5 人',
      mutability: 'readonly',
      status: 'diverged',
      confidence: 1.0,
    },
    budget: {
      label: '预算',
      value_type: 'number',
      observed: 2000,
      desired: 2000,
      mutability: 'editable',
      status: 'synced',
      confidence: 1.0,
    },
  },
  workflow: { has_plan: true, nodes: [] },
  checkpoints: [],
  conflicts: [],
};

/** A decoder-style component tree using ONLY correct v0.9 bindings. */
const mockComponents = [
  {
    id: 'root',
    component: 'Column',
    children: ['title', 'date_field', 'budget_field', 'status_text', 'submit', 'submit_label'],
  },
  { id: 'title', component: 'Text', text: '任务变量', variant: 'h2' },
  {
    id: 'date_field',
    component: 'TextField',
    label: '发布日期',
    value: { path: '/variables/release_date/desired' },
  },
  {
    id: 'budget_field',
    component: 'TextField',
    label: '预算',
    value: { path: '/variables/budget/desired' },
    variant: 'number',
  },
  { id: 'status_text', component: 'Text', text: { path: '/task/status' } },
  {
    id: 'submit',
    component: 'Button',
    child: 'submit_label',
    variant: 'primary',
    action: {
      event: {
        name: 'taskvm.local_patch',
        context: { semanticKey: 'release_date' },
      },
    },
  },
  { id: 'submit_label', component: 'Text', text: '更新日期' },
];

export const mockA2uiMessages: A2uiMessage[] = [
  createSurfaceMessage(MOCK_SURFACE_ID),
  updateComponentsMessage(MOCK_SURFACE_ID, mockComponents),
  updateDataModelMessage(MOCK_SURFACE_ID, mockDataModel),
];
