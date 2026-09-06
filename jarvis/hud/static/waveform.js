/*
 * The radial spectrum: 128 bars around the R2 ring.
 *
 * Two details carry it. The colour says who is talking — amber when the source
 * is the microphone, cyan when it is JARVIS — so you can tell from across the
 * room without reading anything. And the decay is asymmetric: bars jump to a
 * new peak instantly and fall back slowly, which is what makes it feel
 * responsive rather than soupy. Symmetric smoothing looks like jelly.
 *
 * The feed carries eight bands. They are interpolated across 128 bars with a
 * cubic so it reads as a continuous spectrum rather than as eight fat blocks.
 */
(function (global) {
  'use strict';

  var BARS = 128;
  var BASELINE = 4;       // px, at reference scale
  var REACH = 56;         // px of travel above the baseline
  var FALL = 3.5;         // units per frame, downward only

  var levels = new Float32Array(BARS);   // what is drawn
  var targets = new Float32Array(BARS);  // what the feed asked for

  /* Catmull-Rom through the band values, wrapped around the circle. */
  function sampleBands(bands, position) {
    var n = bands.length;
    if (n === 0) { return 0; }
    if (n === 1) { return bands[0]; }
    var x = position * n;
    var i = Math.floor(x);
    var t = x - i;
    var p0 = bands[(i - 1 + n) % n];
    var p1 = bands[i % n];
    var p2 = bands[(i + 1) % n];
    var p3 = bands[(i + 2) % n];
    return 0.5 * ((2 * p1) +
      (-p0 + p2) * t +
      (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t +
      (-p0 + 3 * p1 - 3 * p2 + p3) * t * t * t);
  }

  var Waveform = {
    /* The eased 0..1 magnitudes themselves, for a theme that wants to draw
       its own interpretation of "the audio right now" rather than reactor.js's
       radial bars — a live reference into the same array `step()` updates, so
       reading it costs nothing and never goes stale. */
    levels: levels,

    /* Fold the incoming feed onto the 128 bars. Mirrored, so the spectrum
       reads outward from the top in both directions rather than sweeping. */
    feed: function (rms, bands) {
      var source = (bands && bands.length) ? bands : null;
      for (var i = 0; i < BARS; i++) {
        var mirrored = i < BARS / 2 ? i / (BARS / 2) : (BARS - i) / (BARS / 2);
        var value = source ? sampleBands(source, mirrored) : rms;
        targets[i] = Math.max(0, Math.min(1, value));
      }
    },

    idle: function (clock) {
      // A slow wave travelling round the ring: alive, but plainly not listening.
      for (var i = 0; i < BARS; i++) {
        var phase = (i / BARS) * Math.PI * 2 - clock / 6 * Math.PI * 2;
        targets[i] = Math.max(0, Math.sin(phase)) * 0.05;
      }
    },

    step: function () {
      for (var i = 0; i < BARS; i++) {
        if (targets[i] >= levels[i]) {
          levels[i] = targets[i];                     // rising is instant
        } else {
          levels[i] = Math.max(targets[i], levels[i] - FALL / 100);
        }
      }
    },

    draw: function (ctx, view, palette, radius) {
      var scale = view.scale;
      var flat = view.state === 'thinking' || view.state === 'error';
      // hud-design.md: error is flat red, not whatever colour the source would
      // otherwise imply — a fault reads as a fault regardless of who was
      // talking when it happened.
      var colour = view.state === 'error' ? palette.red
        : view.waveformSource === 'user' ? palette.amber : palette.cyan;

      ctx.save();
      ctx.strokeStyle = colour;
      ctx.shadowColor = colour;
      ctx.shadowBlur = 8;
      ctx.lineWidth = 2 * scale;
      ctx.lineCap = 'butt';
      ctx.globalAlpha = view.reactorAlpha;

      for (var i = 0; i < BARS; i++) {
        var angle = (i / BARS) * Math.PI * 2 - Math.PI / 2;
        var level = flat ? 0.02 + Math.sin(view.clock * 3 + i) * 0.01 : levels[i];
        var length = (BASELINE + level * REACH) * scale;
        var cos = Math.cos(angle);
        var sin = Math.sin(angle);
        ctx.beginPath();
        ctx.moveTo(cos * radius, sin * radius);
        ctx.lineTo(cos * (radius + length), sin * (radius + length));
        ctx.stroke();
      }

      // The ring the bars stand on.
      ctx.globalAlpha = view.reactorAlpha * 0.4;
      ctx.shadowBlur = 0;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(0, 0, radius, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    },

    /* A phrase boundary, while speaking — one timestamp per event. Shared
       across every theme so each can render its own take on "something just
       happened" (a ring, a burst, a shatter) without re-tracking timing itself.
       Whoever owns the array (this module) prunes it; readers never mutate it. */
    pulses: [],
    pulseLife: 1.2,

    pulse: function (clock) { this.pulses.push(clock); },

    prunePulses: function (clock) {
      var life = this.pulseLife;
      this.pulses = this.pulses.filter(function (born) { return clock - born <= life; });
    },

    /* The reactor theme's own rendering of a pulse: a plain outward ring.
       Other themes read `Waveform.pulses` directly and draw their own shape. */
    drawPulses: function (ctx, view, palette) {
      var self = this;
      ctx.save();
      ctx.translate(view.centre.x, view.centre.y);
      this.pulses.forEach(function (born) {
        var progress = (view.clock - born) / self.pulseLife;
        ctx.globalAlpha = (1 - progress) * 0.4 * view.reactorAlpha;
        ctx.strokeStyle = palette.cyan;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(0, 0, (120 + progress * 180) * view.scale, 0, Math.PI * 2);
        ctx.stroke();
      });
      ctx.restore();
      return self.pulses.length;
    },

    reset: function () {
      levels.fill(0);
      targets.fill(0);
      this.pulses = [];
    }
  };

  global.Waveform = Waveform;
}(window));
