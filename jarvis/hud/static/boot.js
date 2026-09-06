/*
 * The startup sequence: 2.4 seconds, driven by real boot events.
 *
 * The progress is not faked. Each line corresponds to something that actually
 * happened — a model loading, a connection opening — which is why the sequence
 * is satisfying rather than decorative. A progress bar that is lying to you is
 * obvious even when you cannot say why.
 */
(function (global) {
  'use strict';

  var LINES = [
    { match: 'ACOUSTIC', text: 'ACOUSTIC MODEL' },
    { match: 'AGENT', text: 'AGENT LINK' },
    { match: 'READY', text: 'SYSTEM CONTROL' }
  ];

  var Boot = {
    started: 0,
    steps: [],       // {text, at}
    // Done until told otherwise. A HUD opened mid-conversation never sees a
    // boot event, and must render the running system rather than waiting
    // forever for a sequence that finished before it arrived.
    done: true,
    progress: 1,

    reset: function (clock) {
      this.started = clock;
      this.steps = [];
      this.done = false;
      this.progress = 0;
    },

    /* A boot event from the core. Progress drives the rings flying in. */
    event: function (step, progress, clock) {
      if (!this.started) { this.reset(clock); }
      var upper = String(step).toUpperCase();
      LINES.forEach(function (line) {
        if (upper.indexOf(line.match) !== -1) {
          Boot.steps.push({ text: line.text, at: clock });
        }
      });
      this.progress = progress;
      if (progress >= 1) { this.done = true; this.finishedAt = clock; }
    },

    elapsed: function (clock) {
      // No boot event seen: behave as though the sequence is long finished.
      return this.started ? clock - this.started : 999;
    },

    /* 0 to 1 per ring, staggered 80ms apart, outermost first. */
    flyIn: function (clock) {
      var order = ['r6', 'r5', 'r4', 'r3', 'r1'];
      var age = this.elapsed(clock);
      var out = {};
      order.forEach(function (key, index) {
        var start = 0.4 + index * 0.08;
        out[key] = Math.max(0, Math.min(1, (age - start) / 0.5));
      });
      return out;
    },

    ignition: function (clock) {
      var age = this.elapsed(clock);
      return Math.max(0, Math.min(1, (age - 1.0) / 0.4));
    },

    flash: function (clock) {
      var age = this.elapsed(clock);
      return age > 1.0 && age < 1.2 ? 1 - (age - 1.0) / 0.2 : 0;
    },

    panelDraw: function (clock) {
      var age = this.elapsed(clock);
      return Math.max(0, Math.min(1, (age - 1.2) / 0.4));
    },

    draw: function (ctx, view, palette) {
      var age = this.elapsed(view.clock);
      if (age > 2.2 && this.done) { return; }
      var y = view.height / 2 + 200 * view.scale;
      ctx.save();
      ctx.font = '500 12px ' + palette.font;
      ctx.fillStyle = palette.textDim;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      this.steps.forEach(function (line, index) {
        var shown = view.clock - line.at;
        if (shown < 0) { return; }
        var dots = '.'.repeat(Math.max(0, 22 - line.text.length));
        var full = line.text + ' ' + dots + ' OK';
        var chars = Math.min(full.length, Math.floor(shown * 60));
        ctx.globalAlpha = Math.max(0, Math.min(1, 1 - (age - 2.0) / 0.2));
        ctx.fillText(full.slice(0, chars), view.width / 2, y + index * 20);
      });
      ctx.restore();
    }
  };

  global.Boot = Boot;
}(window));
