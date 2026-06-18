#!/usr/bin/env node
/**
 * PreToolUse hook: blocks Edit/Write/NotebookEdit calls that target the root
 * checkout. AGENTS.md rule 8: all work happens in worktree lanes under
 * .worktrees/; the root checkout is a pristine mirror of main.
 *
 * Allowed: paths under <root>/.worktrees/** and paths outside the repository
 * entirely (e.g. ~/.claude memory, /tmp scratch). Blocked: everything else
 * inside the root checkout's working tree.
 *
 * Exit codes per the hooks contract: 0 = allow, 2 = block (stderr is fed back
 * to the model). Any unexpected failure allows the call — this hook is defence
 * in depth, not the rule itself.
 */
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";

let input;
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  process.exit(0);
}

const toolInput = input.tool_input ?? {};
const target = toolInput.file_path ?? toolInput.notebook_path;
if (typeof target !== "string" || target === "") {
  process.exit(0);
}

const cwd =
  typeof input.cwd === "string" && input.cwd !== "" ? input.cwd : process.cwd();

let mainRoot;
try {
  // The common git dir is <main-root>/.git for every linked worktree.
  const commonDir = execFileSync(
    "git",
    ["rev-parse", "--path-format=absolute", "--git-common-dir"],
    { cwd, encoding: "utf8" },
  ).trim();
  mainRoot = path.dirname(commonDir);
} catch {
  process.exit(0); // not inside a git repository — nothing to enforce
}

const resolved = path.resolve(cwd, target);
const rel = path.relative(mainRoot, resolved);
const insideRoot =
  rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
const insideLane =
  rel.startsWith(`.worktrees${path.sep}`) ||
  rel.startsWith(`.claude${path.sep}worktrees${path.sep}`);

if (insideRoot && !insideLane) {
  console.error(
    `Blocked: ${resolved} is in the root checkout. AGENTS.md rule 8: never edit the ` +
      `root checkout — create a worktree lane (worktree-lane skill) and edit ` +
      `${mainRoot}/.worktrees/<lane>/... instead.`,
  );
  process.exit(2);
}

process.exit(0);
