#!/usr/bin/env node
/**
 * PreToolUse hook — stop file operations going through Bash.
 *
 * Why: measured at 3,179 Bash calls against 165 Read calls (19:1). Bash is
 * 74.6% of all tool-result bytes and ~29% of request payload. Every Bash call
 * is a full turn (~12s) whose output then rides in the cached prefix for every
 * remaining turn of the session. cache_read is 59.5% of true spend.
 *
 * Native tools return bounded, structured output and cost far less context:
 *   cat        -> Read   (offset/limit, no whole-file dump)
 *   grep / rg  -> Grep   (head_limit, output_mode, no full-file context)
 *   find / ls  -> Glob   (paths only)
 *
 * Fails OPEN: any parse error, unknown shape, or unexpected exception allows
 * the command. A hook that blocks legitimate work is worse than one that misses.
 *
 * Escape hatch: NATIVE_TOOL_HOOK=off, or prefix the command with `# raw:` when
 * you genuinely need the shell form (piping into another command, remote exec).
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

function tokenise(seg) {
  return seg.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) || [];
}

function check(cmd) {
  // explicit opt-out
  if (/^\s*#\s*raw:/.test(cmd)) return null;

  for (const seg of segments(cmd)) {
    const t = tokenise(seg);
    if (!t.length) continue;

    // strip env assignments and common prefixes
    let k = 0;
    while (k < t.length && (/^[A-Z_][A-Z0-9_]*=/.test(t[k]) || ['sudo', 'command', 'nice', 'time'].includes(t[k]))) k++;
    const bin = (t[k] || '').replace(/^.*\//, '');
    const args = t.slice(k + 1);
    const rest = args.join(' ');

    // never touch write/heredoc forms or remote execution
    if (/[><]|<<|ssh\s|docker\s|kubectl\s/.test(seg)) continue;
    // downstream of a pipe: reading stdin, which is correct usage
    if (isDownstream(cmd, seg)) continue;

    const namesFile = args.some((a) => !a.startsWith('-') && /[./]|\.\w+$/.test(a));

    if (bin === 'cat' && namesFile) {
      return {
        bin,
        use: 'Read',
        why: 'cat dumps the whole file into context permanently. Read takes offset/limit and is bounded.',
      };
    }
    if ((bin === 'grep' || bin === 'egrep' || bin === 'rg' || bin === 'ag') && namesFile) {
      return {
        bin,
        use: 'Grep',
        why: 'Grep supports output_mode and head_limit, so it returns matches instead of whole files.',
      };
    }
    if (bin === 'grep' && /-r|-R|--recursive/.test(rest)) {
      return { bin, use: 'Grep', why: 'Recursive shell grep returns unbounded output.' };
    }
    if (bin === 'find' && !/-delete|-exec|-execdir|-ok/.test(rest)) {
      return { bin, use: 'Glob', why: 'find for discovery returns unbounded paths. Glob returns matches only.' };
    }
    if (bin === 'ls' && /-R|--recursive/.test(rest)) {
      return { bin, use: 'Glob', why: 'Recursive ls enumerates the tree into context.' };
    }
    if ((bin === 'head' || bin === 'tail') && namesFile) {
      return { bin, use: 'Read', why: 'Read with offset/limit does this without a shell round-trip.' };
    }
    if (bin === 'sed' && /-n\s*['"]?\d+\s*,\s*\d+p/.test(rest) && namesFile) {
      return { bin, use: 'Read', why: 'Reading a line range is exactly Read offset/limit.' };
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
    `Blocked: \`${hit.bin}\` via Bash. Use the ${hit.use} tool instead.\n` +
    `${hit.why}\n` +
    `If you genuinely need the shell form (piping into another command), prefix the command with "# raw:".\n`
  );
  process.exit(BLOCK);
} catch {
  process.exit(ALLOW); // fail open, always
}
