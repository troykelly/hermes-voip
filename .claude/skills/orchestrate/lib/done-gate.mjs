/**
 * Objective exit gate. Pure, no I/O, no model judgement.
 *
 * DONE is a computed fact, never a feeling. Every invalid input fails SAFE
 * (NOT done), because "I could not confirm" must never read as "finished".
 *
 * K is floored: a caller cannot argue the loop into a one-cycle exit.
 */

export const MIN_DRY_CYCLES = 2;

const isCount = (n) => Number.isInteger(n) && n >= 0;

/**
 * @param {object} input
 *   buildableOpenIssues  - open, unblocked, unassigned issues an agent could start
 *   openOrchestratorPRs  - PRs this loop authored that are still open
 *   consecutiveDryCycles - cycles in a row that produced no MATERIAL finding
 *   requiredDryCycles    - K, floored at MIN_DRY_CYCLES
 *   blockedOnHuman       - items that only a human can unblock
 */
export function evaluateDone(input = {}) {
  const reasons = [];

  if (!isCount(input.buildableOpenIssues)) reasons.push('invalid buildable-issue count — cannot confirm empty');
  if (!isCount(input.openOrchestratorPRs)) reasons.push('invalid open-PR count — cannot confirm empty');
  if (!isCount(input.consecutiveDryCycles)) reasons.push('invalid dry-cycle count — cannot confirm convergence');

  if (reasons.length) return { done: false, reasons, k: MIN_DRY_CYCLES };

  const k = isCount(input.requiredDryCycles) && input.requiredDryCycles >= MIN_DRY_CYCLES
    ? input.requiredDryCycles
    : MIN_DRY_CYCLES;

  if (input.buildableOpenIssues > 0) reasons.push(`${input.buildableOpenIssues} buildable issue(s) remain`);
  if (input.openOrchestratorPRs > 0) reasons.push(`${input.openOrchestratorPRs} orchestrator PR(s) still open`);
  if (input.consecutiveDryCycles < k) {
    reasons.push(`${input.consecutiveDryCycles}/${k} consecutive dry cycles`);
  }

  if (reasons.length === 0) {
    // Blocked-on-human items are not "buildable", so a clean frontier can still
    // have work outstanding. Converged either way, but say which it is.
    const parked = isCount(input.blockedOnHuman) && input.blockedOnHuman > 0;
    return {
      done: true,
      ...(parked ? { parked: true } : {}),
      reasons: [parked
        ? `converged; ${input.blockedOnHuman} item(s) blocked on a human`
        : 'frontier empty, no open PRs, convergence reached'],
      k,
    };
  }

  return { done: false, reasons, k };
}

/**
 * A finding resets the dry-cycle counter ONLY if it is material. Without this
 * floor a sufficiently pedantic review always names something and the loop can
 * never terminate.
 */
export const MATERIAL = new Set(['defect', 'security', 'privacy', 'data-loss', 'missing-spec-feature', 'broken-doc', 'user-visible']);

export function isMaterial(finding) {
  return !!finding && MATERIAL.has(finding.kind) && finding.confirmed === true;
}

export function nextDryCycles(prev, findings) {
  const p = isCount(prev) ? prev : 0;
  if (!Array.isArray(findings)) return p; // could not evaluate: do not advance, do not reset
  return findings.some(isMaterial) ? 0 : p + 1;
}
