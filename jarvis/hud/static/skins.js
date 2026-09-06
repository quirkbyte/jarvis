/*
 * The DOM/CSS layer-stack engine (handoff/HUD-STYLE.md §1 + handoff/STATES.md
 * §1). One job: own the mechanics every skin shares — which skin-layer is
 * visible, the three state-driven CSS variables, the four-state machine's
 * `data-state`/`data-wait` attributes, and the flash on a switch — so each
 * skin file only has to describe what it looks like.
 *
 * The spec's cursor-tilt parallax is deliberately not implemented: tried it,
 * and for a HUD you glance at rather than hold, a background that shifts
 * with the mouse read as distracting rather than immersive.
 *
 * A skin's own per-frame data sync (telemetry numbers, activity list, the
 * caption typewriter) is *not* here: it lives in that skin's own file and is
 * registered into `SkinRegistry`, because that content is different for
 * every skin while this mechanism is identical for all of them.
 */
(function (global) {
  'use strict';

  // hud.js's state names collapse onto STATES.md's four: 'speaking' reads as
  // "responding" there, and 'boot'/'error' (outside that model) fall back to
  // idle's baseline rather than leaving --spd/--vx undefined.
  var STATE_MAP = {
    boot: 'idle', idle: 'idle', listening: 'listening', thinking: 'thinking',
    speaking: 'responding', error: 'idle'
  };
  // STATES.md §1, verbatim. Thinking is the deliberate inversion: --spd goes
  // UP (the world slows down) while the thinking indicator itself speeds up —
  // every other state gets faster with energy, thinking alone goes backwards.
  var SPD = { idle: 1, listening: 0.78, thinking: 1.6, responding: 0.55 };
  var VX = { idle: 0.35, listening: 0.7, thinking: 0.15, responding: 1 };
  var IMMERSION_DEFAULT = 0.8;
  var LONG_WAIT_MS = 4000;

  var root = document.getElementById('skins-root');
  var flash = document.getElementById('skins-flash');
  var html = document.documentElement;
  var waitTimer = null;
  var voiceLevel = 0;

  global.SkinRegistry = global.SkinRegistry || {};

  function fireFlash() {
    flash.classList.remove('fire');
    // Force reflow so re-adding the class restarts the animation on a
    // second switch back to the same skin, not just the first one.
    void flash.offsetWidth;
    flash.classList.add('fire');
  }

  var Skins = {
    /* Called once per state change (see hud.js's setState), never per
       frame — a CSS animation's duration reacts to the variable the instant
       it changes, so there is nothing to drive continuously here (except
       the voice ring's own amplitude feed — see feedVoice below). */
    applyState: function (state) {
      var mapped = STATE_MAP[state] || 'idle';
      root.dataset.state = mapped;
      // Written on #skins-root itself, not html: hud-skins.css's own
      // `.hud-root { --spd/--vx/--imm: <defaults> }` rule (verbatim import)
      // re-pins those exact custom properties locally the moment
      // #skins-root carries the "hud-root" class hud-states.css's selectors
      // require — inheriting from html's inline style never even gets a
      // chance to apply. An inline style on #skins-root itself always wins
      // over that stylesheet rule, so the live value actually reaches
      // every skin's CSS instead of being silently shadowed back to the
      // hardcoded default on every frame.
      root.style.setProperty('--spd', SPD[mapped]);
      root.style.setProperty('--vx', VX[mapped]);
      root.style.setProperty('--imm', IMMERSION_DEFAULT);

      // STATES.md §1: only entering "thinking" arms the 4s long-wait timer
      // (Option B). Leaving it for any reason clears the timer and the
      // attribute — never enter B directly from idle.
      clearTimeout(waitTimer);
      if (mapped === 'thinking') {
        waitTimer = setTimeout(function () { root.dataset.wait = 'long'; }, LONG_WAIT_MS);
      } else {
        delete root.dataset.wait;
      }
    },

    /* STATES.md §5: the voice ring's one input. Fed raw amplitude from the
       same 'level' events every skin's waveform already used; only actually
       moves --vx while listening/responding — thinking/idle keep their
       fixed low baseline from applyState, since there is no voice to track. */
    feedVoice: function (raw) {
      var state = root.dataset.state;
      if (state !== 'listening' && state !== 'responding') { return; }
      var clamped = Math.min(1, Math.max(0, raw || 0));
      voiceLevel += (clamped - voiceLevel) * 0.22;
      root.style.setProperty('--vx', voiceLevel.toFixed(3));
    },

    /* Called whenever the active theme changes (themes.js). `layer` is
       whether the new theme is one of these DOM skins at all; `id` is which
       one, ignored when layer is false. */
    activate: function (layer, id) {
      html.dataset.skinMode = layer ? 'layer' : 'canvas';
      var layers = root.querySelectorAll('.skin-layer');
      for (var i = 0; i < layers.length; i++) {
        layers[i].dataset.active = String(layers[i].dataset.skin === id);
      }
      fireFlash();
    },

    /* Called every animation frame from hud.js's loop, a no-op unless a DOM
       skin is actually showing. Dispatches to that skin's own sync(). */
    sync: function (view, palette) {
      if (html.dataset.skinMode !== 'layer') { return; }
      var active = root.querySelector('.skin-layer[data-active="true"]');
      if (!active) { return; }
      var entry = global.SkinRegistry[active.dataset.skin];
      if (entry && entry.sync) { entry.sync(view, palette); }
    }
  };

  global.Skins = Skins;
}(window));
