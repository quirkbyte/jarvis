/*
 * The arc reactor. Concentric rings, each turning at its own rate, and a core
 * that breathes.
 *
 * The counter-rotation is the whole trick: adjacent rings turning opposite ways
 * at different speeds read as mechanism, while everything turning together
 * reads as a loading spinner. And the breath does more for the illusion than
 * anything else here — while idle it is a slow sine, and while speaking it is
 * driven by the audio envelope, so the core swells on each syllable.
 *
 * The tick marks and dots are traced once into an offscreen canvas and then
 * rotated as a whole. Re-tracing 120 ticks every frame is affordable at 60fps
 * and still the wrong thing to do.
 */
(function (global) {
  'use strict';

  // radius, rotation in degrees/second. From hud-design.md, at 520px reference.
  var RINGS = {
    r6: { radius: 260, speed: 0.8 },
    r5: { radius: 232, speed: -2.2 },
    r4: { radius: 200, speed: 1.4 },
    r3: { radius: 168, speed: -0.6 },
    r2: { radius: 120, speed: 0 },
    r1: { radius: 84, speed: 4.0 },
    core: { radius: 56 }
  };

  var MARKERS = [
    { angle: 45, label: 'SYS' },
    { angle: 135, label: 'NET' },
    { angle: 225, label: 'AUD' },
    { angle: 315, label: 'MEM' }
  ];

  var REFERENCE = 520;   // the diameter the radii above are quoted at
  var cache = null;      // offscreen rings, keyed by scale and dpr

  function rad(deg) { return deg * Math.PI / 180; }

  /* Ring geometry that never changes, drawn once. */
  function buildCache(scale, dpr, palette) {
    var size = Math.ceil(RINGS.r6.radius * 2 * scale + 40);
    var layer = {};
    ['r6', 'r5', 'r4', 'r3', 'r1'].forEach(function (key) {
      var canvas = document.createElement('canvas');
      canvas.width = canvas.height = size * dpr;
      var ctx = canvas.getContext('2d');
      ctx.scale(dpr, dpr);
      ctx.translate(size / 2, size / 2);
      drawRing(ctx, key, RINGS[key].radius * scale, palette);
      layer[key] = canvas;
    });
    return { size: size, scale: scale, dpr: dpr, layers: layer };
  }

  function drawRing(ctx, key, radius, palette) {
    ctx.strokeStyle = palette.cyanDim;
    ctx.fillStyle = palette.cyanDim;
    ctx.lineCap = 'butt';

    if (key === 'r6') {                       // 120 ticks, every tenth longer
      for (var i = 0; i < 120; i++) {
        var major = i % 10 === 0;
        var length = major ? 14 : 6;
        ctx.save();
        ctx.rotate(rad(i * 3));
        ctx.lineWidth = major ? 1.6 : 1;
        ctx.globalAlpha = major ? 0.9 : 0.5;
        ctx.beginPath();
        ctx.moveTo(radius - length, 0);
        ctx.lineTo(radius, 0);
        ctx.stroke();
        ctx.restore();
      }
    } else if (key === 'r5') {                // three gapped 40 degree arcs
      ctx.lineWidth = 3;
      for (var s = 0; s < 3; s++) {
        var start = rad(s * 120);
        ctx.beginPath();
        ctx.arc(0, 0, radius, start, start + rad(40));
        ctx.stroke();
      }
    } else if (key === 'r4') {                // 72 dots
      for (var d = 0; d < 72; d++) {
        var a = rad(d * 5);
        ctx.beginPath();
        ctx.arc(Math.cos(a) * radius, Math.sin(a) * radius, 1.4, 0, Math.PI * 2);
        ctx.fill();
      }
    } else if (key === 'r3') {                // ring plus 12 spokes
      ctx.lineWidth = 1;
      ctx.globalAlpha = 0.55;
      ctx.beginPath();
      ctx.arc(0, 0, radius, 0, Math.PI * 2);
      ctx.stroke();
      for (var k = 0; k < 12; k++) {
        ctx.save();
        ctx.rotate(rad(k * 30));
        ctx.beginPath();
        ctx.moveTo(radius - 18, 0);
        ctx.lineTo(radius + 10, 0);
        ctx.stroke();
        ctx.restore();
      }
    } else if (key === 'r1') {                // thick 300 degree arc
      ctx.lineWidth = 6;
      ctx.globalAlpha = 0.8;
      ctx.beginPath();
      ctx.arc(0, 0, radius, rad(30), rad(330));
      ctx.stroke();
    }
  }

  function blit(ctx, canvas, size, angle, alpha) {
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.rotate(angle);
    ctx.drawImage(canvas, -size / 2, -size / 2, size, size);
    ctx.restore();
  }

  /* The breath. Idle is a 4-second sine at 6%; speaking follows the voice. */
  function breath(view, t) {
    if (view.state === 'speaking' || view.state === 'listening') {
      return 1 + view.envelope * 0.22;
    }
    return 1 + Math.sin(t / 4 * Math.PI * 2) * 0.06;
  }

  function drawCore(ctx, radius, view, t, palette) {
    var scale = breath(view, t) * (view.state === 'listening' ? 1.15 : 1);
    var r = radius * scale * view.ignition;
    if (r <= 0.5) { return; }
    var colour = view.state === 'error' ? palette.red : palette.cyan;

    var gradient = ctx.createRadialGradient(0, 0, 0, 0, 0, r);
    gradient.addColorStop(0, view.flash > 0.02 ? '#ffffff' : colour);
    gradient.addColorStop(0.35, colour);
    gradient.addColorStop(1, 'rgba(0, 217, 255, 0)');
    ctx.save();
    ctx.globalAlpha = 0.9;
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.arc(0, 0, r, 0, Math.PI * 2);
    ctx.fill();

    ctx.globalAlpha = 1;
    ctx.shadowColor = colour;
    ctx.shadowBlur = 24;
    ctx.fillStyle = view.flash > 0.02 ? '#ffffff' : colour;
    ctx.beginPath();
    ctx.arc(0, 0, r * 0.34, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  /* The detail people notice: a line sweeping the full radius while thinking. */
  function drawScanLine(ctx, radius, view, palette) {
    if (view.state !== 'thinking') { return; }
    var angle = (view.clock % 0.9) / 0.9 * Math.PI * 2;
    ctx.save();
    ctx.rotate(angle);
    // A 12 degree trail, drawn as sixteen steps so it reads as a smear rather
    // than as a handful of separate lines.
    for (var trail = 0; trail < 16; trail++) {
      ctx.save();
      ctx.rotate(rad(-trail * 0.8));
      ctx.globalAlpha = 0.45 * Math.pow(1 - trail / 16, 1.6);
      ctx.strokeStyle = palette.cyan;
      ctx.shadowColor = palette.cyan;
      ctx.shadowBlur = trail === 0 ? 20 : 0;
      ctx.lineWidth = trail === 0 ? 2 : 1.4;
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.lineTo(radius, 0);
      ctx.stroke();
      ctx.restore();
    }
    ctx.restore();
  }

  function drawMarkers(ctx, radius, angle, palette, alpha) {
    ctx.save();
    ctx.globalAlpha = alpha * 0.8;
    ctx.font = '600 9px ' + palette.font;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    MARKERS.forEach(function (marker) {
      var a = rad(marker.angle) + angle;
      var x = Math.cos(a) * radius;
      var y = Math.sin(a) * radius;
      ctx.strokeStyle = palette.cyanDim;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.cos(a) * (radius - 8), Math.sin(a) * (radius - 8));
      ctx.lineTo(x, y);
      ctx.stroke();
      ctx.fillStyle = palette.cyan;
      // Counter-rotated so the labels stay upright as the ring turns.
      ctx.fillText(marker.label, Math.cos(a) * (radius + 14), Math.sin(a) * (radius + 14));
    });
    ctx.restore();
  }

  var Reactor = {
    radii: RINGS,

    draw: function (ctx, view, palette) {
      var scale = view.scale;
      var dpr = view.dpr;
      if (!cache || cache.scale !== scale || cache.dpr !== dpr) {
        cache = buildCache(scale, dpr, palette);
      }

      var t = view.clock;
      var spin = view.spin;                 // state multiplier, eased
      var alpha = view.reactorAlpha;
      var size = cache.size;

      ctx.save();
      ctx.translate(view.centre.x, view.centre.y);

      if (view.state === 'listening') {     // soft amber halo
        var halo = ctx.createRadialGradient(0, 0, 0, 0, 0, 300 * scale);
        halo.addColorStop(0, 'rgba(255, 181, 69, 0.12)');
        halo.addColorStop(1, 'rgba(255, 181, 69, 0)');
        ctx.fillStyle = halo;
        ctx.beginPath();
        ctx.arc(0, 0, 300 * scale, 0, Math.PI * 2);
        ctx.fill();
      }

      // The judder: 2px of random offset at 4Hz, faults only.
      if (view.state === 'error' && Math.floor(t * 4) % 1 === 0) {
        ctx.translate((Math.random() - 0.5) * 2, (Math.random() - 0.5) * 2);
      }

      ['r6', 'r5', 'r4', 'r3', 'r1'].forEach(function (key) {
        var fly = view.flyIn[key] === undefined ? 1 : view.flyIn[key];
        if (fly <= 0) { return; }
        ctx.save();
        // Rings fly inward from outside the viewport during boot.
        ctx.scale(1 + (1 - fly) * 2.2, 1 + (1 - fly) * 2.2);
        blit(ctx, cache.layers[key], size, rad(RINGS[key].speed * spin * t), alpha * fly);
        ctx.restore();
      });

      drawMarkers(ctx, RINGS.r5.radius * scale, rad(RINGS.r5.speed * spin * t), palette, alpha);
      drawScanLine(ctx, RINGS.r6.radius * scale, view, palette);
      global.Waveform.draw(ctx, view, palette, RINGS.r2.radius * scale);
      drawCore(ctx, RINGS.core.radius * scale, view, t, palette);
      ctx.restore();
    }
  };

  global.Reactor = Reactor;
  global.Reactor.REFERENCE = REFERENCE;
}(window));
