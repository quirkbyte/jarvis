/*
 * The main loop, the socket, and the state the other modules draw from.
 *
 * One canvas, one requestAnimationFrame. Every module exposes draw(ctx, view,
 * palette) and this calls them in order — giving each component its own loop or
 * its own canvas is how a 60fps interface becomes a 30fps one.
 *
 * The socket reconnects forever with jitter and never shows an error page. A
 * HUD that cannot reach the core is a HUD with a LINK indicator off, not a
 * broken web page; the core is very probably still talking.
 */
(function (global) {
  'use strict';

  var STATUS = {
    boot: 'INITIALISING', idle: 'STANDBY', listening: 'LISTENING',
    thinking: 'PROCESSING', speaking: 'RESPONDING', error: 'FAULT'
  };
  var SPIN = { boot: 1, idle: 1, listening: 1.6, thinking: 3, speaking: 1.2, error: 1 };
  var TRANSITION = 0.24;                 // seconds, everything interpolates

  var canvas = document.getElementById('hud');
  var ctx = canvas.getContext('2d', { alpha: false });
  var entry = document.getElementById('entry');
  var reduced = global.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var palette = {};
  var view = {
    state: 'boot', previous: 'boot', changedAt: 0, clock: 0,
    width: 0, height: 0, scale: 1, dpr: 1, centre: { x: 0, y: 0 },
    statusText: STATUS.boot, spin: 1, reactorAlpha: 1,
    envelope: 0, waveformSource: 'jarvis', link: false,
    telemetry: {}, activity: [], transcript: [], notice: null,
    flyIn: {}, ignition: 1, flash: 0, panelDraw: 1,
    fps: 0, debug: false, events: [],
    themeFlash: '', themeFlashUntil: 0
  };

  function readPalette() {
    var css = getComputedStyle(document.documentElement);
    function get(name) { return css.getPropertyValue(name).trim(); }
    palette = {
      bgVoid: get('--bg-void'), bgDeep: get('--bg-deep'),
      grid: get('--grid'), gridMajor: get('--grid-major'),
      cyan: get('--cyan'), cyanDim: get('--cyan-dim'),
      cyanGlow: get('--cyan-glow'), cyanPale: get('--cyan-pale'),
      amber: get('--amber'), amberDim: get('--amber-dim'), red: get('--red'),
      accent2: get('--accent2'), accent2Dim: get('--accent2-dim'), accent3: get('--accent3'),
      text: get('--text'), textDim: get('--text-dim'), textFaint: get('--text-faint'),
      // Read live rather than hardcoded, or every theme's panels keep
      // rendering in Rajdhani regardless of which font.css actually loaded.
      font: get('--font') || "'Rajdhani', 'Eurostile', 'DIN Alternate', system-ui, sans-serif"
    };
  }

  function resize() {
    var dpr = global.devicePixelRatio || 1;
    var rect = canvas.getBoundingClientRect();
    // Size the backing store to rect * dpr, then scale the context. Skip this
    // and every line is soft on a Retina display.
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
    view.width = rect.width;
    view.height = rect.height;
    view.dpr = dpr;
    // Everything scales from the smaller dimension, so it works on any display.
    view.scale = Math.min(rect.width, rect.height) / 900 * 0.92;
    view.centre = { x: rect.width / 2, y: 56 + (rect.height - 56 - 140) / 2 };
  }

  /* ---- background: a grid receding to a vanishing point ------------------ */
  function drawGrid(t) {
    var drift = view.state === 'idle' ? 3 : 6;             // px/s
    var horizon = view.centre.y;
    ctx.save();
    ctx.fillStyle = palette.bgVoid;
    ctx.fillRect(0, 0, view.width, view.height);

    var glow = ctx.createRadialGradient(
      view.centre.x, horizon, 0, view.centre.x, horizon, view.height * 0.7);
    glow.addColorStop(0, palette.bgDeep);
    glow.addColorStop(1, palette.bgVoid);
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, view.width, view.height);

    ctx.strokeStyle = palette.grid;
    ctx.lineWidth = 1;
    var offset = reduced ? 0 : (t * drift) % 60;
    for (var y = view.height; y > horizon; y -= 60) {
      var py = y - offset;
      ctx.globalAlpha = Math.max(0, (py - horizon) / (view.height - horizon));
      ctx.beginPath();
      ctx.moveTo(0, py);
      ctx.lineTo(view.width, py);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    for (var i = -14; i <= 14; i++) {
      ctx.beginPath();
      ctx.moveTo(view.centre.x + i * 140, view.height);
      ctx.lineTo(view.centre.x + i * 8, horizon);
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawVignette() {
    if (view.state !== 'error') { return; }
    var age = view.clock - view.changedAt;
    var alpha = Math.max(0, 0.15 * (1 - age / 2));
    if (alpha <= 0) { return; }
    var gradient = ctx.createRadialGradient(
      view.width / 2, view.height / 2, view.height * 0.3,
      view.width / 2, view.height / 2, view.height * 0.8);
    gradient.addColorStop(0, 'rgba(255, 77, 94, 0)');
    gradient.addColorStop(1, 'rgba(255, 77, 94, ' + alpha + ')');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, view.width, view.height);
  }

  function drawDebug() {
    if (!view.debug) { return; }
    ctx.save();
    ctx.font = '500 11px monospace';
    ctx.fillStyle = palette.amber;
    ctx.textBaseline = 'top';
    ctx.fillText(Math.round(view.fps) + ' fps   ' + view.state +
                 '   clients ' + (view.link ? 'linked' : 'offline'), 24, 68);
    view.events.slice(-10).forEach(function (line, i) {
      ctx.fillStyle = palette.textDim;
      ctx.fillText(line, 24, 84 + i * 13);
    });
    ctx.restore();
  }

  /* ---- eased state transitions ------------------------------------------ */
  function ease(from, to, progress) { return from + (to - from) * progress; }

  function updateView(dt) {
    var since = view.clock - view.changedAt;
    var progress = Math.min(1, since / TRANSITION);
    var eased = progress * progress * (3 - 2 * progress);        // smoothstep
    view.statusText = (view.clock < view.themeFlashUntil)
      ? view.themeFlash
      : (STATUS[view.state] || view.state.toUpperCase());
    view.spin = reduced ? 0 : ease(SPIN[view.previous] || 1, SPIN[view.state] || 1, eased);
    view.reactorAlpha = ease(
      view.previous === 'idle' ? 0.6 : 1, view.state === 'idle' ? 0.6 : 1, eased);
    view.waveformSource = view.state === 'listening' ? 'user' : 'jarvis';
    view.envelope = Math.max(0, view.envelope - dt * 2.2);

    if (view.state === 'boot' || !global.Boot.done) {
      view.flyIn = global.Boot.flyIn(view.clock);
      view.ignition = global.Boot.ignition(view.clock);
      view.flash = global.Boot.flash(view.clock);
      view.panelDraw = global.Boot.panelDraw(view.clock);
    } else {
      view.flyIn = {};
      view.ignition = 1;
      view.flash = 0;
      view.panelDraw = 1;
    }

    var theme = global.Themes.active();
    if (view.state === 'idle' || view.state === 'thinking') {
      theme.idle(reduced ? 0 : view.clock);
    }
    theme.step();
    global.Waveform.prunePulses(view.clock);
  }

  /* ---- the loop ---------------------------------------------------------- */
  var last = performance.now();
  var frames = 0;
  var fpsAt = last;
  var lastFrameError = null;

  function frame(now) {
    var dt = Math.min(0.1, (now - last) / 1000);
    last = now;
    view.clock += dt;
    frames++;
    if (now - fpsAt > 500) {
      view.fps = frames * 1000 / (now - fpsAt);
      frames = 0;
      fpsAt = now;
    }

    // One bad frame must never take the whole render loop down with it —
    // a thrown exception here would otherwise stop requestAnimationFrame
    // from ever being called again, which is a permanent black screen with
    // no way to recover short of reloading. Logged once per distinct error
    // so a real problem is still visible, not swallowed.
    try {
      updateView(dt);
      var activeTheme = global.Themes.active();
      // A "layer" theme is a DOM/CSS skin (skins.js) — it owns the whole
      // screen, so the canvas has nothing to draw and would just be paying
      // for it. Its own per-frame sync still runs, for the caption typewriter.
      if (!activeTheme.layer) {
        if (activeTheme.background) { activeTheme.background(ctx, view, palette); }
        else { drawGrid(view.clock); }
        global.Panels.header(ctx, view, palette);
        global.Panels.telemetry(ctx, view, palette);
        global.Panels.activity(ctx, view, palette);
        activeTheme.draw(ctx, view, palette);
        global.Panels.transcript(ctx, view, palette);
        global.Panels.notice(ctx, view, palette);
        global.Boot.draw(ctx, view, palette);
        drawVignette();
        drawDebug();
      }
      global.Skins.sync(view, palette);
    } catch (err) {
      if (lastFrameError !== err.message) {
        console.error('HUD frame error (rendering continues):', err);
        lastFrameError = err.message;
      }
    }
    requestAnimationFrame(frame);
  }

  /* ---- events from the core ---------------------------------------------- */
  function setState(next) {
    if (next === view.state) { return; }
    view.previous = view.state;
    view.state = next;
    view.changedAt = view.clock;
    if (next === 'boot') { global.Boot.reset(view.clock); }
    if (next === 'speaking') { global.Themes.active().pulse(view.clock); }
    global.Skins.applyState(next);
  }

  function handle(event) {
    view.events.push(event.type + ' ' + (event.state || event.label || event.text || ''));
    if (view.events.length > 40) { view.events.shift(); }

    switch (event.type) {
      case 'state':
        setState(event.state);
        break;
      case 'level':
        global.Themes.active().feed(event.rms || 0, event.bands);
        view.envelope = Math.max(view.envelope, event.rms || 0);
        global.Skins.feedVoice(event.rms || 0);
        break;
      case 'transcript':
        onTranscript(event);
        break;
      case 'tool':
        onTool(event);
        break;
      case 'telemetry':
        view.telemetry = event;
        global.Panels.record(event);
        break;
      case 'notice':
        view.notice = { text: event.text, level: event.level, at: view.clock };
        break;
      case 'boot':
        global.Boot.event(event.step, event.progress, view.clock);
        break;
      default:
        break;
    }
  }

  function onTranscript(event) {
    var lines = view.transcript;
    var last = lines[lines.length - 1];
    if (!event.text) {
      if (last && last.role === 'jarvis') { last.closed = true; }
      return;
    }
    if (event.role === 'jarvis' && last && last.role === 'jarvis' && !last.closed) {
      last.text += (last.text.endsWith(' ') ? '' : ' ') + event.text;
    } else {
      lines.push({ role: event.role, text: event.text, at: view.clock, closed: event.final });
      global.Themes.active().pulse(view.clock);
    }
    if (lines.length > 8) { lines.shift(); }
  }

  function find(id) {
    for (var i = 0; i < view.activity.length; i++) {
      if (view.activity[i].id === id) { return i; }
    }
    return -1;
  }

  function onTool(event) {
    if (event.phase === 'start') {
      var existing = find(event.id);
      if (existing !== -1) {
        view.activity[existing].at = view.clock;     // a repeat is an update
        view.activity[existing].running = true;
        return;
      }
      view.activity.unshift({
        id: event.id, label: event.label, detail: null, at: view.clock, running: true
      });
      view.activity = view.activity.slice(0, global.Panels.MAX_ACTIVITY);
      return;
    }
    for (var i = 0; i < view.activity.length; i++) {
      if (view.activity[i].id === event.id) {
        view.activity[i].running = false;
        view.activity[i].ok = event.ok;
        view.activity[i].detail = event.detail || null;
        return;
      }
    }
  }

  /* ---- the socket -------------------------------------------------------- */
  var socket = null;
  var backoff = 1000;

  function connect() {
    var url = 'ws://' + location.host + '/ws';
    socket = new WebSocket(url);
    socket.onopen = function () {
      view.link = true;
      backoff = 1000;
      socket.send(JSON.stringify({ type: 'hello', client: 'hud', version: 1 }));
    };
    socket.onmessage = function (message) {
      try { handle(JSON.parse(message.data)); } catch (err) { /* a bad frame is not fatal */ }
    };
    socket.onclose = function () {
      view.link = false;
      socket = null;
      // Retry forever, with jitter. Never an error page.
      setTimeout(connect, backoff + Math.random() * 400);
      backoff = Math.min(4000, backoff * 1.4);
    };
    socket.onerror = function () { if (socket) { socket.close(); } };
  }

  function send(message) {
    if (socket && socket.readyState === 1) { socket.send(JSON.stringify(message)); }
  }

  /* ---- keys -------------------------------------------------------------- */
  var talking = false;

  document.addEventListener('keydown', function (e) {
    if (document.activeElement === entry) {
      if (e.key === 'Enter') {
        send({ type: 'text', text: entry.value });
        entry.value = '';
        entry.hidden = true;
        entry.blur();
      } else if (e.key === 'Escape') {
        entry.value = '';
        entry.hidden = true;
        entry.blur();
      }
      return;
    }
    if (e.code === 'Space' && !talking && !e.repeat) {
      talking = true;
      send({ type: 'ptt', down: true });
      e.preventDefault();
    } else if (e.key === 'Escape') {
      send({ type: 'interrupt' });
    } else if (e.key === '/') {
      entry.hidden = false;
      entry.focus();
      e.preventDefault();
    } else if (e.key === 'f' || e.key === 'F') {
      if (document.fullscreenElement) { document.exitFullscreen(); }
      else { document.documentElement.requestFullscreen(); }
    } else if (e.key === 'd' || e.key === 'D') {
      view.debug = !view.debug;
    } else if (e.key === 't' || e.key === 'T') {
      var next = global.Themes.cycle();
      readPalette();
      view.themeFlash = next.name;
      view.themeFlashUntil = view.clock + 1.4;
    }
  });

  document.addEventListener('keyup', function (e) {
    if (e.code === 'Space' && talking) {
      talking = false;
      send({ type: 'ptt', down: false });
    }
  });

  global.addEventListener('resize', resize);

  // Same reasoning as the frame() try/catch: an exception here would abort
  // every line after it, including the socket connect and the first
  // requestAnimationFrame — nothing would ever be drawn. If theme/skin init
  // fails, still bring the socket up and start the loop.
  try {
    global.Themes.init();
    global.Skins.applyState(view.state);
  } catch (err) {
    console.error('HUD init error (starting anyway):', err);
  }
  readPalette();
  resize();
  connect();
  requestAnimationFrame(frame);

  global.HUD = { view: view, handle: handle, palette: function () { return palette; } };
}(window));
