'use strict';

const { spawnSync } = require('child_process');

console.log('aztea-tui: installing Python package…');

const pythons = ['python3', 'python'];
for (const py of pythons) {
  const check = spawnSync(py, ['-c', 'import sys; assert sys.version_info >= (3,11)'], {
    stdio: 'pipe',
  });
  if (check.status !== 0) continue;

  // Try pipx first, then uv, then pip
  const installers = [
    ['pipx', ['install', 'aztea-tui', '--force']],
    ['uv',   ['tool', 'install', 'aztea-tui']],
    [py,     ['-m', 'pip', 'install', '--user', '--upgrade', 'aztea-tui']],
  ];

  for (const [cmd, args] of installers) {
    const r = spawnSync(cmd, args, { stdio: 'inherit' });
    if (r.status === 0) {
      console.log('aztea-tui: Python package installed successfully.');
      process.exit(0);
    }
  }
}

console.warn(
  'aztea-tui: could not auto-install the Python package.\n' +
  'Run manually:  pip install aztea-tui'
);
