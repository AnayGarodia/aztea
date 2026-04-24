#!/usr/bin/env node
'use strict';

const { spawnSync } = require('child_process');
const { join } = require('path');
const fs = require('fs');

// Try to find aztea-tui in PATH first (if already installed via pip/pipx/uv)
function findInPath(name) {
  const dirs = (process.env.PATH || '').split(':');
  for (const dir of dirs) {
    const full = join(dir, name);
    if (fs.existsSync(full)) return full;
  }
  return null;
}

const cmd = findInPath('aztea-tui');
if (cmd) {
  const result = spawnSync(cmd, process.argv.slice(2), { stdio: 'inherit' });
  process.exit(result.status ?? 0);
}

// Fall back: invoke as Python module
const pythons = ['python3', 'python'];
for (const py of pythons) {
  const result = spawnSync(py, ['-m', 'aztea_tui', ...process.argv.slice(2)], {
    stdio: 'inherit',
  });
  if (result.status !== null) {
    process.exit(result.status);
  }
}

console.error(
  'aztea-tui: could not find Python 3.11+. Install it from https://python.org'
);
process.exit(1);
