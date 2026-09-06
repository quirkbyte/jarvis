/*
 * The theme registry and switcher. Every theme — including the default
 * JARVIS look — implements the same small interface:
 *
 *   feed(rms, bands)   fold a level event into whatever data the theme needs
 *   idle(clock)        what to feed while nothing is listening or speaking
 *   step()             advance one frame's worth of easing
 *   pulse(clock)       something worth marking happened (a phrase boundary)
 *   draw(ctx, view, palette)   draw the whole centrepiece, this frame
 *
 * `feed`/`idle`/`step`/`pulse` are almost always the same few lines calling
 * straight into `Waveform`'s shared data layer — that part is not
 * theme-specific, it is just turning a stream of audio levels into eased
 * numbers. `draw` is where each theme actually differs.
 *
 * The active theme is persisted in localStorage so it survives a reload —
 * per-viewer, the same as everything else this HUD keeps in the browser.
 */
(function (global) {
  'use strict';

  var STORAGE_KEY = 'jarvis-hud-theme';

  var ReactorTheme = {
    id: 'reactor',
    name: 'JARVIS',
    feed: function (rms, bands) { global.Waveform.feed(rms, bands); },
    idle: function (clock) { global.Waveform.idle(clock); },
    step: function () { global.Waveform.step(); },
    pulse: function (clock) { global.Waveform.pulse(clock); },
    draw: function (ctx, view, palette) {
      global.Reactor.draw(ctx, view, palette);
      global.Waveform.drawPulses(ctx, view, palette);
    }
  };

  // This build ships only the default canvas look. The DOM/CSS skins
  // (skin-*.js/skin-*.css, per handoff/HUD-STYLE.md) are a separate add-on
  // and aren't part of this repo.
  var ORDER = ['reactor'];

  function registry() {
    return {
      reactor: ReactorTheme
    };
  }

  function stored() {
    try { return global.localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
  }

  function persist(id) {
    try { global.localStorage.setItem(STORAGE_KEY, id); } catch (e) { /* private mode, etc. */ }
  }

  var current = ORDER.indexOf(stored());
  if (current < 0) { current = 0; }

  function apply() {
    var id = ORDER[current];
    global.document.documentElement.dataset.theme = id;
    var theme = registry()[id];
    if (theme && global.Waveform) { global.Waveform.reset(); }
    if (global.Skins) { global.Skins.activate(!!(theme && theme.layer), id); }
    return theme;
  }

  var Themes = {
    active: function () { return registry()[ORDER[current]] || ReactorTheme; },

    id: function () { return ORDER[current]; },

    cycle: function () {
      current = (current + 1) % ORDER.length;
      persist(ORDER[current]);
      return apply();
    },

    init: function () { return apply(); }
  };

  global.Themes = Themes;
}(window));
