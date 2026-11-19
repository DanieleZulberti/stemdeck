import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const index = readFileSync(new URL('../../static/index.html', import.meta.url), 'utf8');
const mobile = readFileSync(new URL('../../static/mobile/index.html', import.meta.url), 'utf8');
const boot = readFileSync(new URL('../../static/js/boot-diagnostics.js', import.meta.url), 'utf8');
const vars = readFileSync(new URL('../../static/css/variables.css', import.meta.url), 'utf8');

for (const html of [index, mobile]) {
  assert.equal(html.includes('fonts.googleapis.com'), false, 'Google Fonts must not be a boot dependency');
  assert.equal(html.includes('fonts.gstatic.com'), false, 'Google font assets must not be a boot dependency');
}
assert.match(vars, /--font-sans:.*system-ui/s, 'sans font stack must have a system fallback');
assert.match(vars, /--font-mono:.*SF Mono.*Menlo/s, 'mono font stack must have local fallbacks');
assert.match(boot, /bootOptional/, 'boot diagnostics must support explicitly optional resources');

console.log('boot-fonts.test.mjs: ok');
