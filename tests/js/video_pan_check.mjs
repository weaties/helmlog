// Behavioural check for the pure view math in static/video_pan.js (#844).
//
// Run standalone: `node tests/js/video_pan_check.mjs`
// It prints "OK <n> checks" and exits 0, or prints the failing check and exits
// 1. The pytest wrapper (tests/test_video_pan.py) shells out to this and skips
// if node is unavailable. DOM wiring (pointer events, YT player calls) is not
// covered here — the repo has no jsdom dependency.

import { createRequire } from 'module';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const pan = require(join(here, '..', '..', 'src', 'helmlog', 'static', 'video_pan.js'));

let n = 0;
function check(name, cond, detail) {
  n++;
  if (!cond) {
    console.log('FAIL ' + name + (detail !== undefined ? ': ' + JSON.stringify(detail) : ''));
    process.exit(1);
  }
}
const near = (a, b) => Math.abs(a - b) < 1e-9;

const start = { yaw: 10, pitch: 0, roll: 0, fov: 100 };

// The scene follows the pointer (grab-the-scene). YouTube: +yaw turns the
// view left, so the scene slides right; +pitch looks up, so the scene slides
// down. A full-width drag rotates by one field of view.
let v = pan.panFromDrag(start, 500, 0, 500);
check('drag right increases yaw', near(v.yaw, 110), v);
check('drag right keeps pitch', near(v.pitch, 0), v);
check('drag keeps fov', near(v.fov, 100), v);
check('drag keeps roll', near(v.roll, 0), v);

v = pan.panFromDrag(start, -50, 0, 500);
check('drag left decreases yaw', near(v.yaw, 0), v);

// Yaw wraps into [0, 360).
v = pan.panFromDrag(start, -100, 0, 500);
check('yaw wraps below 0', near(v.yaw, 350), v);
v = pan.panFromDrag({ ...start, yaw: 350 }, 100, 0, 500);
check('yaw wraps above 360', near(v.yaw, 10), v);

// Drag down pulls the scene down, i.e. looks up; pitch clamps at the poles.
v = pan.panFromDrag(start, 0, 100, 500);
check('drag down looks up', near(v.pitch, 20), v);
v = pan.panFromDrag(start, 0, -100, 500);
check('drag up looks down', near(v.pitch, -20), v);
v = pan.panFromDrag(start, 0, 5000, 500);
check('pitch clamps at +90', near(v.pitch, 90), v);
v = pan.panFromDrag(start, 0, -5000, 500);
check('pitch clamps at -90', near(v.pitch, -90), v);

// Zero-width layer (hidden / not laid out) must not produce NaN.
v = pan.panFromDrag(start, 50, 50, 0);
check('zero width is a no-op', near(v.yaw, 10) && near(v.pitch, 0), v);

// isSpherical: YT returns {} for flat videos, a populated object for 360.
check('spherical props', pan.isSpherical({ yaw: 0, pitch: 0, roll: 0, fov: 100 }) === true);
check('empty props', pan.isSpherical({}) === false);
check('null props', pan.isSpherical(null) === false);
check('undefined props', pan.isSpherical(undefined) === false);

// isClick: tiny movement counts as a click (play/pause), not a pan.
check('no move is click', pan.isClick(0, 0) === true);
check('jitter is click', pan.isClick(3, -2) === true);
check('real drag is not click', pan.isClick(12, 0) === false);

console.log('OK ' + n + ' checks');
