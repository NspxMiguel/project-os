"""The hash router, exercised in node against a fake window.

There is one behaviour here worth a test, and it cost the Files screen: a view
that keeps its state in the URL (Files writes ``?path=``) calls navigate() on
every load. navigate() used to re-dispatch when the hash was already the target,
so the view mounted, loaded, navigated, mounted again -- forever. The screen
never settled and nothing else could be reached, because the rewrite dragged the
URL back to Files even after you clicked away.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
// A window just real enough for router.js: a hash, a replaceState, and events.
const listeners = {};
let hash = '#/';
globalThis.HashChangeEvent = class { constructor(type) { this.type = type; } };
globalThis.window = {
  get location() {
    return {
      get hash() { return hash; },
      set hash(value) { const before = hash; hash = value; if (before !== value) fire(); },
      href: 'http://box/' + hash,
    };
  },
  history: {
    replaceState(_a, _b, url) {
      const index = String(url).indexOf('#');
      const next = index === -1 ? '' : String(url).slice(index);
      hash = next;
    },
  },
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  removeEventListener(type, fn) {
    listeners[type] = (listeners[type] || []).filter((f) => f !== fn);
  },
  dispatchEvent(event) { fire(event.type); },
};
function fire(type = 'hashchange') { (listeners[type] || []).slice().forEach((fn) => fn()); }

const {createRouter, navigate} = await import('__ROUTER__');

const out = {};
let mounts = 0;
const router = createRouter();
router.add('#/files', () => {
  mounts += 1;
  // Exactly what views/files.js does once it knows which folder it is showing.
  if (mounts < 50) navigate('#/files?path=%2Fhome', {replace: true});
});
router.add('#/services', () => { out.reachedServices = true; });
router.start();

window.location.hash = '#/files';
out.mountsAfterLandingOnFiles = mounts;

// Leaving has to work: this is the part that was impossible.
window.location.hash = '#/services';
out.hashAfterLeaving = window.location.hash;

// A deliberate re-run of the current route still works (clicking the sidebar
// item you are already on).
let reruns = 0;
router.add('#/logs', () => { reruns += 1; });
window.location.hash = '#/logs';
navigate('#/logs');
out.rerunsAfterNavigatingToTheSameRoute = reruns;

console.log(JSON.stringify(out));
"""


def _run() -> dict:
    router = (ROOT / "web" / "lib" / "router.js").as_uri()
    script = HARNESS.replace("__ROUTER__", router)
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return json.loads(result.stdout.decode("utf-8").strip().splitlines()[-1])


def test_a_view_that_rewrites_its_own_url_does_not_loop() -> None:
    assert _run()["mountsAfterLandingOnFiles"] == 1


def test_you_can_leave_a_view_that_rewrites_its_own_url() -> None:
    result = _run()
    assert result["hashAfterLeaving"] == "#/services"
    assert result["reachedServices"] is True


def test_navigating_to_the_route_you_are_on_still_reruns_it() -> None:
    assert _run()["rerunsAfterNavigatingToTheSameRoute"] == 2
