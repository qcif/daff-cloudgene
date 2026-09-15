import { execSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { afterAll, describe, expect, it } from 'vitest';

// §9 of tasks/5_build_client.md: the dev-only escape hatch that reads
// import.meta.env.VITE_DEV_TOKEN must be impossible to ship in a production
// build. A unit test can only prove the *logic* is DEV-gated (see
// auth.test.js); this proves the *build* actually strips it, by running a
// real production build and grepping the emitted bundle.
//
// Two traps, both of which this test has already fallen into once:
//
// 1. NODE_ENV must be forced to 'production'. Vitest sets NODE_ENV=test, a
//    child `vite build` inherits it, and Vite then resolves isProduction
//    to false and *keeps* every import.meta.env.DEV branch — so the build
//    under test would not be a production build at all.
// 2. Grepping only for the env var name is not sufficient on its own. Vite
//    statically replaces `import.meta.env.VITE_DEV_TOKEN` with its literal
//    value (`undefined` when unset), so that identifier vanishes whether or
//    not the surrounding branch was eliminated. The dev-only help text is
//    the part that actually proves the branch is gone, so assert on both.
const OUT_DIR = 'dist-build-check';

// A distinctive fragment of the dev-only message in src/auth.js. If dead
// code elimination stops working, this is what survives.
const DEV_ONLY_TEXT = 'restart Vite';

describe('production build', () => {
  afterAll(() => {
    rmSync(OUT_DIR, { recursive: true, force: true });
  });

  it('strips the dev-only token escape hatch entirely', () => {
    execSync(`npx vite build --outDir ${OUT_DIR} --logLevel silent`, {
      cwd: process.cwd(),
      env: { ...process.env, NODE_ENV: 'production' },
    });

    const assetsDir = join(OUT_DIR, 'assets');
    expect(existsSync(assetsDir)).toBe(true);

    const jsFiles = readdirSync(assetsDir).filter((f) => f.endsWith('.js'));
    expect(jsFiles.length).toBeGreaterThan(0);

    for (const file of jsFiles) {
      const contents = readFileSync(join(assetsDir, file), 'utf8');
      expect(contents).not.toContain('VITE_DEV_TOKEN');
      expect(contents).not.toContain(DEV_ONLY_TEXT);
    }
  }, 30000);
});
