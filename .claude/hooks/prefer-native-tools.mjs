#!/usr/bin/env node
/**
 * PreToolUse hook — stop UNBOUNDED file reads and searches going through Bash.
 *
 * Why: measured at 3,179 Bash calls against 165 Read calls (19:1). Bash is
 * 74.6% of all tool-result bytes and ~29% of request payload. Every Bash call
 * is a full turn (~12s) whose output then rides in the cached prefix for every
 * remaining turn of the session. cache_read is 59.5% of true spend.
 *
 * HARNESS NOTE — why this is not the upstream "use Grep/Glob" hook.
 * The original blocked cat/grep/find and told the agent to use the Grep and
 * Glob tools. In THIS harness those two tools do not exist; the runtime's own
 * error is "Grep is not available in this session — search file contents with
 * `grep` via the Bash tool instead." Verified here directly, and reported from
 * a subagent and a headless `claude -p` run (which is what tick.sh spawns).
 * Redirecting to a tool that is not there blocks every search with no available
 * alternative, which costs more than the burn it prevents.
 *
 * So the rules split by what the harness actually offers:
 *   cat / head / tail / sed -n 'N,Mp'  -> BLOCK, use Read   (Read DOES exist)
 *   grep / rg / ag / git grep          -> BLOCK only when UNBOUNDED
 *   find / ls -R                       -> BLOCK only when UNBOUNDED
 *
 * "Bounded" means the command cannot dump an unknown quantity into context:
 * a counting or listing flag (-l, -L on grep, -c, -q, -m N, --max-count),
 * or piping straight into head/tail/wc. Every block names a fix the agent can
 * actually apply. If a future harness exposes Grep/Glob, prefer them — they are
 * bounded by construction (head_limit / output_mode) and cost less context.
 *
 * Fails OPEN: any parse error, unknown shape, or unexpected exception allows
 * the command. A hook that blocks legitimate work is worse than one that misses.
 *
 * Escape hatch: NATIVE_TOOL_HOOK=off, or prefix the command with `# raw:` when
 * you genuinely need the unbounded form.
 *
 * Install in .claude/settings.json:
 *   "hooks": { "PreToolUse": [ { "matcher": "Bash",
 *     "hooks": [ { "type": "command", "command": "node .claude/hooks/prefer-native-tools.mjs" } ] } ] }
 */

import { readFileSync } from 'node:fs';

const ALLOW = 0;
const BLOCK = 2; // exit 2 => blocked, stderr is shown to the agent

function readStdin() {
  try {
    return readFileSync(0, 'utf8');
  } catch {
    return '';
  }
}

/** Split a command line into pipeline segments, ignoring separators inside quotes. */
function segments(cmd) {
  const out = [];
  let buf = '';
  let quote = null;
  for (let i = 0; i < cmd.length; i++) {
    const c = cmd[i];
    if (quote) {
      if (c === quote && cmd[i - 1] !== '\\') quote = null;
      buf += c;
      continue;
    }
    if (c === '"' || c === "'") { quote = c; buf += c; continue; }
    if (c === '|' || c === ';' || c === '&') {
      if (buf.trim()) out.push(buf.trim());
      buf = '';
      // consume doubled operators
      while (i + 1 < cmd.length && (cmd[i + 1] === '|' || cmd[i + 1] === '&')) i++;
      continue;
    }
    buf += c;
  }
  if (buf.trim()) out.push(buf.trim());
  return out;
}

/** True when the segment reads piped stdin rather than naming files. */
function isDownstream(cmd, seg) {
  const idx = cmd.indexOf(seg);
  if (idx <= 0) return false;
  const before = cmd.slice(0, idx);
  // last unquoted separator before this segment was a pipe
  const m = before.match(/([|;&])[^|;&]*$/);
  return !!m && m[1] === '|';
}

/**
 * True when this segment pipes DIRECTLY into a bounding consumer. `a | head` is
 * bounded; `a && b | head` is not, because head bounds b rather than a.
 */
function pipesIntoBounder(cmd, seg) {
  const idx = cmd.indexOf(seg);
  if (idx < 0) return false;
  const after = cmd.slice(idx + seg.length);
  return /^\s*\|\s*(head|tail|wc)\b/.test(after);
}

function tokenise(seg) {
  return seg.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) || [];
}

const LONG_BOUNDED = ['--files-with-matches', '--files-without-match', '--count', '--quiet', '--silent'];

/**
 * True when a grep-family invocation caps its own output. `-L` means
 * files-without-match in grep (bounded) but follow-symlinks in ripgrep
 * (unbounded), so it only counts for grep.
 */
function boundedSearch(args, bin) {
  const shortBounded = bin === 'rg' ? /[lcq]/ : /[lLcq]/;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === '--') break;
    if (!a.startsWith('-') || a === '-') continue;
    if (a.startsWith('--')) {
      const name = a.split('=')[0];
      if (LONG_BOUNDED.includes(name) || name === '--max-count') return true;
      continue;
    }
    const cluster = a.slice(1);
    if (shortBounded.test(cluster)) return true;
    const mIdx = cluster.indexOf('m');
    if (mIdx !== -1) {
      const tail = cluster.slice(mIdx + 1);
      if (/^\d+$/.test(tail)) return true;                              // -m5, -rm5
      if (tail === '' && /^\d+$/.test(args[i + 1] || '')) return true;  // -m 5
    }
  }
  return false;
}

const CAP_HINT = 'Cap the output: add -l, -c or -m N, or pipe into | head -n 50.';

function check(cmd) {
  // explicit opt-out
  if (/^\s*#\s*raw:/.test(cmd)) return null;

  for (const seg of segments(cmd)) {
    const t = tokenise(seg);
    if (!t.length) continue;

    // strip env assignments and common prefixes
    let k = 0;
    while (k < t.length && (/^[A-Z_][A-Z0-9_]*=/.test(t[k]) || ['sudo', 'command', 'nice', 'time'].includes(t[k]))) k++;
    let bin = (t[k] || '').replace(/^.*\//, '');
    let args = t.slice(k + 1);
    // `git grep` is a search like any other; bound it the same way.
    if (bin === 'git' && args[0] === 'grep') {
      bin = 'grep';
      args = args.slice(1);
    }
    const rest = args.join(' ');

    // never touch write/heredoc forms or remote execution
    if (/[><]|<<|ssh\s|docker\s|kubectl\s/.test(seg)) continue;
    // downstream of a pipe: reading stdin, which is correct usage
    if (isDownstream(cmd, seg)) continue;

    const namesFile = args.some((a) => !a.startsWith('-') && /[./]|\.\w+$/.test(a));
    const bounded = pipesIntoBounder(cmd, seg);

    // --- Read replacements. Read exists in this harness, so these stand as-is.
    if (bin === 'cat' && namesFile) {
      return {
        bin,
        why: 'cat dumps the whole file into context permanently.',
        fix: 'Use the Read tool — it takes offset/limit and is bounded.',
      };
    }
    if ((bin === 'head' || bin === 'tail') && namesFile) {
      return {
        bin,
        why: 'this is a shell round-trip for something Read already does.',
        fix: 'Use the Read tool with offset/limit.',
      };
    }
    if (bin === 'sed' && /-n\s*['"]?\d+\s*,\s*\d+p/.test(rest) && namesFile) {
      return {
        bin,
        why: 'reading a line range is exactly Read offset/limit.',
        fix: 'Use the Read tool with offset/limit.',
      };
    }

    // --- Searches. Grep/Glob do not exist here, so bound them instead.
    if (['grep', 'egrep', 'fgrep', 'rg', 'ag', 'ack'].includes(bin)) {
      if (bounded || boundedSearch(args, bin)) continue;
      return {
        bin,
        why: 'an uncapped search can dump an unknown number of matching lines into context, and they stay in the cached prefix for the rest of the session.',
        fix: CAP_HINT,
      };
    }
    if (bin === 'find' && !/-delete|-exec|-execdir|-ok|-quit/.test(rest)) {
      if (bounded) continue;
      return {
        bin,
        why: 'find for discovery returns an unbounded path list.',
        fix: 'Bound it: pipe into | head -n 50 (or narrow the -name/-path pattern).',
      };
    }
    if (bin === 'ls' && /(^|\s)-\w*R/.test(rest)) {
      if (bounded) continue;
      return {
        bin,
        why: 'recursive ls enumerates the whole tree into context.',
        fix: 'Bound it: pipe into | head -n 50, or list one directory at a time.',
      };
    }
  }
  return null;
}

try {
  if (process.env.NATIVE_TOOL_HOOK === 'off') process.exit(ALLOW);

  const raw = readStdin();
  if (!raw.trim()) process.exit(ALLOW);

  let payload;
  try { payload = JSON.parse(raw); } catch { process.exit(ALLOW); }

  if (payload?.tool_name !== 'Bash') process.exit(ALLOW);
  const cmd = payload?.tool_input?.command;
  if (typeof cmd !== 'string' || !cmd.trim()) process.exit(ALLOW);

  const hit = check(cmd);
  if (!hit) process.exit(ALLOW);

  process.stderr.write(
    `Blocked: unbounded \`${hit.bin}\` via Bash — ${hit.why}\n` +
    `${hit.fix}\n` +
    `If you genuinely need the unbounded form, prefix the command with "# raw:".\n`
  );
  process.exit(BLOCK);
} catch {
  process.exit(ALLOW); // fail open, always
}
