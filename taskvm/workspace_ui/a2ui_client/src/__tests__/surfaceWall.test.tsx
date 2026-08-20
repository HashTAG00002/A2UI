/**
 * SurfaceWall — the A9.2 screenshot-wall contract:
 *
 *  - one card per world surface, names from the server feed;
 *  - the thumbnail's object URL stays STABLE while the hash is
 *    unchanged (zero loss: idle screens never flicker);
 *  - executing lanes pulse 开工中; ≥2 concurrent lanes show the
 *    fan-out badge (fan-out/fan-in is kernel-truth driven, pure
 *    front-end);
 *  - clicking a card opens the modal whose image is the FULL shot,
 *    lazy-loaded ONLY at that moment (no full-size fetch before).
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { SurfaceWall, type WallLane } from '../wall/SurfaceWall';

const entry = (hash: string) => ({
  name: '微信', role: 'foreground' as const, hash, seq: 1,
  thumbUrl: `/t/${hash}`,
  fullUrl: `/f/${hash}`,
});

const lanes: WallLane[] = [
  { label: '修改日期', kind: 'step', status: 'waiting' },
  { label: '通知成员', kind: 'step', status: 'waiting' },
  { label: '校验', kind: 'verification', status: 'waiting' },
];

describe('SurfaceWall — cards', () => {
  it('renders one card per surface with the decoded thumbnail + hash', () => {
    render(
      <SurfaceWall entry={entry('h1')} thumbUrl="blob:thumb-1"
        loading={false} surfaces={[{ name: '微信', role: 'foreground' }]}
        lanes={[]} />,
    );
    const cards = screen.getAllByTestId('surface-card');
    expect(cards).toHaveLength(1);
    const img = screen.getByTestId('surface-thumb');
    expect(img.getAttribute('src')).toBe('blob:thumb-1');
    expect(img.dataset.hash).toBe('h1');
    expect(screen.getByText('微信')).toBeInTheDocument();
  });

  it('zero loss: an unchanged hash keeps the previous frame (same src)', () => {
    const { rerender } = render(
      <SurfaceWall entry={entry('h1')} thumbUrl="blob:thumb-1"
        loading={false} surfaces={[{ name: '微信', role: 'foreground' }]}
        lanes={[]} />,
    );
    // the feed refreshed (seq moved) but the hash is the same — the
    // wall keeps the decoded frame, no swap, no flicker
    rerender(
      <SurfaceWall
        entry={{ ...entry('h1'), seq: 2 }} thumbUrl="blob:thumb-1"
        loading={false} surfaces={[{ name: '微信', role: 'foreground' }]}
        lanes={[]} />,
    );
    expect(screen.getByTestId('surface-thumb').getAttribute('src'))
      .toBe('blob:thumb-1');
  });

  it('first frame in flight renders the honest empty state (同步中…)', () => {
    render(
      <SurfaceWall entry={null} thumbUrl={null} loading
        surfaces={[]} lanes={[]} />,
    );
    expect(screen.getByTestId('surface-wall').dataset.empty).toBe('true');
    expect(screen.getByText('截图同步中…')).toBeInTheDocument();
  });
});

describe('SurfaceWall — lanes (kernel-truth driven pulses)', () => {
  it('a single executing lane pulses 开工中 without the fan-out badge', () => {
    render(
      <SurfaceWall entry={entry('h1')} thumbUrl="blob:thumb-1"
        loading={false} surfaces={[{ name: '微信', role: 'foreground' }]}
        lanes={[{ ...lanes[0], status: 'executing' }]} />,
    );
    expect(screen.getByText('开工中')).toBeInTheDocument();
    expect(screen.getByTestId('wall-lanes').dataset.fanout).toBe('false');
    expect(screen.queryByTestId('fanout-badge')).toBeNull();
  });

  it('≥2 concurrent executing lanes show the fan-out badge', () => {
    render(
      <SurfaceWall entry={entry('h1')} thumbUrl="blob:thumb-1"
        loading={false} surfaces={[{ name: '微信', role: 'foreground' }]}
        lanes={[
          { ...lanes[0], status: 'executing' },
          { ...lanes[1], status: 'executing' },
        ]} />,
    );
    expect(screen.getByTestId('wall-lanes').dataset.fanout).toBe('true');
    expect(screen.getByTestId('fanout-badge').textContent)
      .toContain('fan-out · 2 个分支开工中');
  });

  it('no lanes ⇒ no lane panel (nothing fabricated)', () => {
    const { container } = render(
      <SurfaceWall entry={entry('h1')} thumbUrl="blob:thumb-1"
        loading={false} surfaces={[{ name: '微信', role: 'foreground' }]}
        lanes={[]} />,
    );
    expect(container.querySelector('[data-testid="wall-lanes"]')).toBeNull();
  });
});

describe('SurfaceWall — click-to-zoom (lazy full shot)', () => {
  it('the modal mounts ONLY after the click and carries the FULL url', async () => {
    const user = userEvent.setup();
    render(
      <SurfaceWall entry={entry('h1')} thumbUrl="blob:thumb-1"
        loading={false} surfaces={[{ name: '微信', role: 'foreground' }]}
        lanes={[]} />,
    );
    // before the click there is no modal (the ~2MB shot is NOT loaded)
    expect(screen.queryByTestId('shot-modal')).toBeNull();
    await user.click(screen.getByTestId('surface-card'));
    const modal = screen.getByTestId('shot-modal');
    expect(modal).toBeInTheDocument();
    expect(screen.getByTestId('shot-modal-img').getAttribute('src'))
      .toBe('/f/h1');
  });

  it('Escape / 关闭 unmounts the modal', async () => {
    const user = userEvent.setup();
    render(
      <SurfaceWall entry={entry('h1')} thumbUrl="blob:thumb-1"
        loading={false} surfaces={[{ name: '微信', role: 'foreground' }]}
        lanes={[]} />,
    );
    await user.click(screen.getByTestId('surface-card'));
    await user.click(screen.getByTestId('shot-modal-close'));
    expect(screen.queryByTestId('shot-modal')).toBeNull();
  });
});
