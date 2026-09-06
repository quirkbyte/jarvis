/*
 * The three panels and the header, all drawn.
 *
 * Labels are texture rather than information: small, uppercase, widely spaced,
 * and deliberately hard to read at a glance. The transcript is the exception —
 * it is the one thing here meant to be read, so it is sentence case at 20px and
 * gets a typewriter reveal timed to arrive with the speech.
 *
 * The activity column fades entries with age rather than deleting them
 * abruptly, because something vanishing draws the eye and something fading
 * does not.
 */
(function (global) {
  'use strict';

  var TYPE_RATE = 45;          // characters per second, roughly speech rate
  var FADE_SECONDS = 30;       // activity entries fade to 0.25 over this
  var MAX_ACTIVITY = 12;
  var SPARK_SAMPLES = 60;

  function label(ctx, text, x, y, palette, colour) {
    ctx.save();
    ctx.font = '600 10px ' + palette.font;
    ctx.fillStyle = colour || palette.textDim;
    ctx.textBaseline = 'middle';
    var spaced = String(text).split('').join(' ');   // letter-spacing, by hand
    ctx.fillText(spaced, x, y);
    ctx.restore();
    return ctx.measureText(spaced).width;
  }

  function frame(ctx, x, y, w, h, palette, progress) {
    // Panel frames draw themselves in during boot as animated 1px strokes.
    var p = progress === undefined ? 1 : progress;
    ctx.save();
    ctx.strokeStyle = palette.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x + w * p, y);
    ctx.moveTo(x, y + h);
    ctx.lineTo(x + w * p, y + h);
    ctx.moveTo(x, y);
    ctx.lineTo(x, y + h * p);
    ctx.moveTo(x + w, y);
    ctx.lineTo(x + w, y + h * p);
    ctx.stroke();
    ctx.restore();
  }

  function sparkline(ctx, samples, x, y, w, h, palette, colour) {
    if (samples.length < 2) { return; }
    // Right-aligned: the newest sample is always at the right edge, so a
    // half-filled history reads as "not much yet" rather than as a broken chart.
    var step = w / (SPARK_SAMPLES - 1);
    x = x + w - (samples.length - 1) * step;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(x, y + h);
    for (var i = 0; i < samples.length; i++) {
      ctx.lineTo(x + i * step, y + h - samples[i] * h);
    }
    ctx.lineTo(x + (samples.length - 1) * step, y + h);
    ctx.closePath();
    ctx.globalAlpha = 0.15;
    ctx.fillStyle = colour;
    ctx.fill();

    ctx.globalAlpha = 0.9;
    ctx.beginPath();
    for (var j = 0; j < samples.length; j++) {
      var px = x + j * step;
      var py = y + h - samples[j] * h;
      if (j === 0) { ctx.moveTo(px, py); } else { ctx.lineTo(px, py); }
    }
    ctx.strokeStyle = colour;
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.restore();
  }

  function ringGauge(ctx, x, y, radius, value, palette) {
    var colour = value < 0.2 ? palette.amber : palette.cyan;
    ctx.save();
    ctx.translate(x, y);
    ctx.lineWidth = 3;
    ctx.strokeStyle = palette.grid;
    ctx.beginPath();
    ctx.arc(0, 0, radius, 0, Math.PI * 2);
    ctx.stroke();
    ctx.strokeStyle = colour;
    ctx.shadowColor = colour;
    ctx.shadowBlur = 8;
    ctx.beginPath();
    ctx.arc(0, 0, radius, -Math.PI / 2, -Math.PI / 2 + value * Math.PI * 2);
    ctx.stroke();
    ctx.shadowBlur = 0;
    ctx.font = '500 13px ' + palette.font;
    ctx.fillStyle = palette.text;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(Math.round(value * 100) + '%', 0, 0);
    ctx.restore();
  }

  function bytes(rate) {
    if (rate >= 1e6) { return (rate / 1e6).toFixed(1) + 'M'; }
    if (rate >= 1e3) { return Math.round(rate / 1e3) + 'K'; }
    return Math.round(rate) + 'B';
  }

  var Panels = {
    history: { cpu: [], mem: [], disk: [], net: [] },

    record: function (telemetry) {
      var self = this;
      [['cpu', telemetry.cpu], ['mem', telemetry.mem], ['disk', telemetry.disk],
       ['net', Math.min(1, (telemetry.net ? telemetry.net.down : 0) / 2e6)]
      ].forEach(function (pair) {
        var series = self.history[pair[0]];
        series.push(pair[1] || 0);
        if (series.length > SPARK_SAMPLES) { series.shift(); }
      });
    },

    header: function (ctx, view, palette) {
      var w = view.width;
      ctx.save();
      ctx.font = '600 15px ' + palette.font;
      ctx.fillStyle = palette.cyan;
      ctx.textBaseline = 'middle';
      ctx.shadowColor = palette.cyanGlow;
      ctx.shadowBlur = 12;
      ctx.fillText('J.A.R.V.I.S.', 28, 28);
      ctx.shadowBlur = 0;

      var linked = view.link;
      ctx.fillStyle = linked ? palette.cyan : palette.red;
      ctx.beginPath();
      ctx.arc(168, 28, 3.5, 0, Math.PI * 2);
      ctx.fill();
      label(ctx, linked ? 'LINK' : 'LINK LOST', 180, 28, palette,
            linked ? palette.textDim : palette.red);

      ctx.textAlign = 'center';
      ctx.font = '500 12px ' + palette.font;
      ctx.fillStyle = view.state === 'error' ? palette.red : palette.cyanPale;
      ctx.fillText(view.statusText.split('').join(' '), w / 2, 28);

      ctx.textAlign = 'right';
      ctx.font = '500 13px ' + palette.font;
      ctx.fillStyle = palette.textDim;
      var now = new Date();
      var clock = [now.getHours(), now.getMinutes(), now.getSeconds()]
        .map(function (n) { return String(n).padStart(2, '0'); }).join(':');
      var date = now.toLocaleDateString('en-GB',
        { day: '2-digit', month: 'short', year: '2-digit' }).toUpperCase();
      ctx.fillText(clock + '   ' + date, w - 28, 28);

      ctx.strokeStyle = palette.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, 56);
      ctx.lineTo(w, 56);
      ctx.stroke();
      ctx.restore();
    },

    telemetry: function (ctx, view, palette) {
      var t = view.telemetry;
      var x = 28;
      var y = 96;
      var w = 204;
      frame(ctx, 16, 72, 244, view.height - 72 - 156, palette, view.panelDraw);
      if (view.panelDraw < 1) { return; }
      label(ctx, 'TELEMETRY', x, y, palette, palette.cyan);
      y += 28;

      var rows = [
        ['CPU', t.cpu, this.history.cpu, Math.round((t.cpu || 0) * 100) + '%'],
        ['MEM', t.mem, this.history.mem, Math.round((t.mem || 0) * 100) + '%'],
        ['DISK', t.disk, this.history.disk, Math.round((t.disk || 0) * 100) + '%'],
        ['NET', 0, this.history.net,
         t.net ? '↓' + bytes(t.net.down) : '—']
      ];
      rows.forEach(function (row) {
        label(ctx, row[0], x, y, palette);
        ctx.save();
        ctx.font = '500 13px ' + palette.font;
        ctx.fillStyle = palette.text;
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ctx.fillText(row[3], x + w, y);
        ctx.restore();
        sparkline(ctx, row[2], x, y + 10, w, 40, palette, palette.cyan);
        y += 68;
      });

      label(ctx, 'PWR', x, y + 14, palette);
      if (t.battery) {
        ringGauge(ctx, x + w - 24, y + 14, 22, t.battery.pct, palette);
        label(ctx, t.battery.charging ? 'CHARGING' : 'ON BATTERY', x, y + 40, palette);
      } else {
        // A Mac mini has no battery. Saying so beats an empty gauge.
        ringGauge(ctx, x + w - 24, y + 14, 22, 1, palette);
        label(ctx, 'MAINS', x, y + 40, palette);
      }
    },

    activity: function (ctx, view, palette) {
      var w = 284;
      var x = view.width - w - 16;
      frame(ctx, x, 72, w, view.height - 72 - 156, palette, view.panelDraw);
      if (view.panelDraw < 1) { return; }
      label(ctx, 'ACTIVITY', x + 12, 96, palette, palette.cyan);

      var y = 128;
      view.activity.slice(0, MAX_ACTIVITY).forEach(function (entry) {
        var age = view.clock - entry.at;
        var alpha = Math.max(0.25, 1 - age / FADE_SECONDS * 0.75);
        ctx.save();
        ctx.globalAlpha = alpha;
        var glyphAlpha = entry.running
          ? 0.4 + 0.6 * Math.abs(Math.sin(view.clock * Math.PI))
          : 1;
        ctx.fillStyle = entry.ok === false ? palette.red : palette.cyan;
        ctx.globalAlpha = alpha * glyphAlpha;
        ctx.font = '500 12px ' + palette.font;
        ctx.textBaseline = 'middle';
        ctx.fillText('▸', x + 12, y);
        ctx.globalAlpha = alpha;
        label(ctx, entry.label, x + 28, y, palette, palette.text);
        if (entry.detail) {
          ctx.font = '300 12px ' + palette.font;
          ctx.fillStyle = palette.textDim;
          var detail = entry.detail.length > 30
            ? entry.detail.slice(0, 29) + '…' : entry.detail;
          ctx.fillText(detail, x + 28, y + 16);
        }
        ctx.restore();
        y += entry.detail ? 40 : 26;
      });
    },

    transcript: function (ctx, view, palette) {
      var top = view.height - 140;
      ctx.save();
      ctx.strokeStyle = palette.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, top);
      ctx.lineTo(view.width, top);
      ctx.stroke();
      ctx.restore();

      var lines = view.transcript.slice(-2);
      var y = top + 46;
      lines.forEach(function (line, index) {
        var fading = lines.length > 1 && index === 0;
        var shown = line.role === 'jarvis'
          ? line.text.slice(0, Math.floor((view.clock - line.at) * TYPE_RATE))
          : line.text;
        ctx.save();
        ctx.globalAlpha = fading ? 0.35 : 1;
        ctx.font = '500 20px ' + palette.font;
        ctx.textBaseline = 'middle';
        ctx.fillStyle = line.role === 'user' ? palette.amber : palette.cyanPale;
        ctx.fillText(line.role === 'user' ? '▸' : '◈', 28, y);
        ctx.fillText(shown, 56, y);
        ctx.restore();
        y += 44;
      });
    },

    notice: function (ctx, view, palette) {
      if (!view.notice) { return; }
      var age = view.clock - view.notice.at;
      if (age > 8) { return; }
      var colour = view.notice.level === 'error' ? palette.red
        : view.notice.level === 'warn' ? palette.amber : palette.textDim;
      ctx.save();
      ctx.globalAlpha = Math.min(1, 8 - age);
      ctx.textAlign = 'center';
      label(ctx, view.notice.text, view.width / 2, view.height - 158, palette, colour);
      ctx.restore();
    },

    TYPE_RATE: TYPE_RATE,
    MAX_ACTIVITY: MAX_ACTIVITY
  };

  global.Panels = Panels;
}(window));
