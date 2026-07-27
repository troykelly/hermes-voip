/**
 * Deterministic wave partitioning. Pure, no I/O, no model judgement.
 *
 * Collisions cannot surface at merge if colliding lanes are never assigned at
 * once. Asking a model "are these safe to run together?" is how you land two
 * PRs that are green alone and red together.
 *
 * Input candidates: { number, lane, blockedBy: [{ number, satisfied }], scope? }
 * Config: { singleWriterLanes: string[], scopedLanes: string[], maxWave: number }
 */

export const DEFAULT_MAX_WAVE = 6;

/** Unknown lanes coerce to "unknown", which is always single-writer. */
export function normaliseLane(lane, knownLanes) {
  if (typeof lane !== 'string' || !lane.trim()) return 'unknown';
  return knownLanes.includes(lane) ? lane : 'unknown';
}

export function partitionWave(candidates, config = {}) {
  const knownLanes = Array.isArray(config.knownLanes) ? config.knownLanes : [];
  const singleWriter = new Set([...(config.singleWriterLanes || []), 'unknown']);
  const scopedLanes = new Set(config.scopedLanes || []);
  const maxWave = Number.isInteger(config.maxWave) && config.maxWave > 0 ? config.maxWave : DEFAULT_MAX_WAVE;

  const wave = [];
  const deferred = [];

  if (!Array.isArray(candidates)) {
    return { wave: [], deferred: [], error: 'candidates is not an array — refusing to dispatch' };
  }

  // Stable order: lowest issue number first. Determinism matters more than cleverness.
  const ordered = [...candidates]
    .filter((c) => c && Number.isInteger(c.number))
    .sort((a, b) => a.number - b.number);

  const laneTaken = new Set();
  const scopeTaken = new Set();

  for (const c of ordered) {
    const lane = normaliseLane(c.lane, knownLanes);

    if (wave.length >= maxWave) {
      deferred.push({ number: c.number, reason: `wave cap ${maxWave} reached` });
      continue;
    }

    const blockers = Array.isArray(c.blockedBy) ? c.blockedBy : [];
    const unmet = blockers.filter((b) => b && b.satisfied !== true).map((b) => b.number);
    if (unmet.length) {
      deferred.push({ number: c.number, reason: `blocked by unmerged ${unmet.join(', ')}` });
      continue;
    }

    if (singleWriter.has(lane)) {
      if (laneTaken.has(lane)) {
        deferred.push({ number: c.number, reason: `single-writer lane "${lane}" already taken this wave` });
        continue;
      }
      laneTaken.add(lane);
    } else if (scopedLanes.has(lane)) {
      // One item per (lane, scope). No scope declared => conservative, treat as single-writer.
      const scope = typeof c.scope === 'string' && c.scope.trim() ? c.scope : null;
      const key = `${lane}::${scope ?? '__unscoped__'}`;
      if (scope === null) {
        if (laneTaken.has(lane)) {
          deferred.push({ number: c.number, reason: `lane "${lane}" item declares no scope — treated as single-writer` });
          continue;
        }
        laneTaken.add(lane);
      } else if (scopeTaken.has(key)) {
        deferred.push({ number: c.number, reason: `scope "${scope}" in lane "${lane}" already taken this wave` });
        continue;
      }
      scopeTaken.add(key);
    }
    // lanes that are neither single-writer nor scoped are disjoint by construction

    wave.push(c.number);
  }

  return { wave, deferred };
}
