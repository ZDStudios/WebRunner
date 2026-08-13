/* WebRunner — shared front-end runtime. No build step, no dependencies. */

const WR = {
  csrf: document.querySelector('meta[name="csrf-token"]')?.content || '',

  /* ------------------------------------------------------------- fetch */

  async api(path, options = {}) {
    const opts = { headers: {}, ...options };
    if (opts.body !== undefined && typeof opts.body !== 'string') {
      opts.body = JSON.stringify(opts.body);
      opts.headers['Content-Type'] = 'application/json';
    }
    if (opts.method && opts.method !== 'GET') opts.headers['X-CSRF-Token'] = WR.csrf;
    const response = await fetch(path, opts);
    if (response.status === 401) {
      window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname);
      throw new Error('Not authenticated');
    }
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : null; } catch { payload = { detail: text }; }
    if (!response.ok) {
      throw new Error(payload?.detail || `${response.status} ${response.statusText}`);
    }
    return payload;
  },

  get: (p) => WR.api(p),
  post: (p, body) => WR.api(p, { method: 'POST', body: body ?? {} }),
  put: (p, body) => WR.api(p, { method: 'PUT', body }),
  del: (p) => WR.api(p, { method: 'DELETE' }),

  /* ------------------------------------------------------------ toasts */

  toast(message, kind = 'info', title = '') {
    let host = document.querySelector('.toasts');
    if (!host) {
      host = document.createElement('div');
      host.className = 'toasts';
      document.body.appendChild(host);
    }
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.innerHTML = (title ? `<div class="toast-title">${WR.escape(title)}</div>` : '') +
                   `<div>${WR.escape(message)}</div>`;
    host.appendChild(el);
    setTimeout(() => {
      el.style.transition = 'opacity .25s, transform .25s';
      el.style.opacity = '0';
      el.style.transform = 'translateX(14px)';
      setTimeout(() => el.remove(), 250);
    }, kind === 'error' ? 7000 : 3800);
  },

  error(err) { WR.toast(err?.message || String(err), 'error', 'Something went wrong'); },

  /* ------------------------------------------------------------ helpers */

  escape(value) {
    return String(value ?? '').replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  },

  el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
      else node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  },

  when(epoch, fallback = '—') {
    if (!epoch) return fallback;
    const date = new Date(epoch * 1000);
    const today = new Date();
    const sameDay = date.toDateString() === today.toDateString();
    const time = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    if (sameDay) return time;
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' +
           date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  },

  relative(epoch) {
    if (!epoch) return '—';
    let delta = Date.now() / 1000 - epoch;
    const future = delta < 0;
    delta = Math.abs(delta);
    const units = [[86400, 'd'], [3600, 'h'], [60, 'm'], [1, 's']];
    for (const [size, label] of units) {
      if (delta >= size) {
        const n = Math.floor(delta / size);
        return future ? `in ${n}${label}` : `${n}${label} ago`;
      }
    }
    return future ? 'now' : 'just now';
  },

  duration(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    const s = Number(seconds);
    if (s < 1) return `${Math.round(s * 1000)}ms`;
    if (s < 60) return `${s.toFixed(1)}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${Math.round(s % 60)}s`;
    return `${Math.floor(m / 60)}h ${m % 60}m`;
  },

  interval(seconds) {
    const s = Number(seconds) || 0;
    if (!s) return '—';
    const units = [[86400, 'd'], [3600, 'h'], [60, 'm'], [1, 's']];
    const parts = [];
    let left = Math.floor(s);
    for (const [size, label] of units) {
      if (left >= size) { parts.push(`${Math.floor(left / size)}${label}`); left %= size; }
    }
    return parts.slice(0, 2).join(' ');
  },

  pill(status) {
    const label = status || 'never run';
    return `<span class="pill ${WR.escape(status || '')}">` +
           (status === 'running' ? '<span class="dot pulse"></span>' : '') +
           WR.escape(label) + '</span>';
  },

  /* --------------------------------------------------------- ANSI → HTML */

  ansi(text) {
    let html = '';
    let open = 0;
    const parts = String(text).split(/\x1b\[([0-9;]*)m/);
    for (let i = 0; i < parts.length; i++) {
      if (i % 2 === 0) { html += WR.escape(parts[i]); continue; }
      const codes = parts[i].split(';').filter(Boolean);
      if (!codes.length || codes.includes('0')) {
        html += '</span>'.repeat(open);
        open = 0;
        continue;
      }
      const classes = codes.map((c) => `ansi-${c}`).join(' ');
      html += `<span class="${classes}">`;
      open++;
    }
    return html + '</span>'.repeat(open);
  },

  /* ---------------------------------------------------------- websocket */

  socket(path, onMessage, { onOpen, onClose } = {}) {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    let ws = null;
    let closed = false;
    let retry = 1000;

    const connect = () => {
      if (closed) return;
      ws = new WebSocket(`${scheme}://${window.location.host}${path}`);
      ws.onopen = () => { retry = 1000; onOpen?.(); };
      ws.onmessage = (event) => {
        try { onMessage(JSON.parse(event.data)); } catch { /* ignore malformed frames */ }
      };
      ws.onclose = (event) => {
        onClose?.(event);
        if (closed || event.code === 4401) return;
        setTimeout(connect, retry);
        retry = Math.min(retry * 1.8, 15000);
      };
      ws.onerror = () => ws?.close();
    };
    connect();
    return { close() { closed = true; ws?.close(); } };
  },

  /* -------------------------------------------------------------- modal */

  modal({ title, body, actions = [], size = '', onClose }) {
    const overlay = WR.el('div', { class: 'overlay' });
    const close = () => { overlay.remove(); document.removeEventListener('keydown', onKey); onClose?.(); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);

    const foot = WR.el('div', { class: 'modal-foot' });
    for (const action of actions) {
      foot.append(WR.el('button', {
        class: action.class || 'btn',
        onClick: () => action.onClick?.(close),
      }, action.label));
    }
    const modal = WR.el('div', { class: `modal ${size}` },
      WR.el('div', { class: 'modal-head' },
        WR.el('h2', {}, title),
        WR.el('button', { class: 'btn-ghost btn-icon', onClick: close, title: 'Close' }, '✕')),
      WR.el('div', { class: 'modal-body' }, body),
      actions.length ? foot : null,
    );
    overlay.append(modal);
    overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) close(); });
    document.body.append(overlay);
    modal.querySelector('input, textarea, select, button')?.focus();
    return { close, modal };
  },

  confirm(message, { title = 'Are you sure?', confirmLabel = 'Confirm', danger = true } = {}) {
    return new Promise((resolve) => {
      let decided = false;
      const { close } = WR.modal({
        title,
        size: 'narrow',
        body: WR.el('p', { class: 'muted', style: { margin: '0' } }, message),
        actions: [
          { label: 'Cancel', onClick: (c) => { decided = true; c(); resolve(false); } },
          {
            label: confirmLabel,
            class: danger ? 'btn-danger' : 'btn-primary',
            onClick: (c) => { decided = true; c(); resolve(true); },
          },
        ],
        onClose: () => { if (!decided) resolve(false); },
      });
      void close;
    });
  },

  /* ------------------------------------------------------- run log view */

  showRun(runId) {
    const output = WR.el('pre', { class: 'terminal', style: { maxHeight: '54vh' } });
    const meta = WR.el('div', { class: 'row small muted mb' }, `Run #${runId}`);
    const cancelBtn = WR.el('button', {
      class: 'btn-danger btn-sm',
      onClick: async () => {
        try {
          await WR.post(`/api/runs/${runId}/cancel`);
          WR.toast('Cancellation requested', 'warn');
        } catch (err) { WR.error(err); }
      },
    }, 'Cancel run');
    cancelBtn.style.display = 'none';

    const body = WR.el('div', {}, meta, output);
    let follow = true;
    output.addEventListener('scroll', () => {
      follow = output.scrollHeight - output.scrollTop - output.clientHeight < 40;
    });

    const socket = WR.socket(`/ws/runs/${runId}`, (msg) => {
      if (msg.type === 'snapshot') {
        output.innerHTML = WR.ansi(msg.text || '');
        meta.innerHTML = `Run #${runId} ${WR.pill(msg.status)}`;
        if (msg.status === 'running') { meta.append(cancelBtn); cancelBtn.style.display = ''; }
      } else if (msg.type === 'output') {
        output.insertAdjacentHTML('beforeend', WR.ansi(msg.text));
      } else if (msg.type === 'finished') {
        meta.innerHTML = `Run #${runId} ${WR.pill(msg.status)} ` +
          `<span class="faint">· ${WR.duration(msg.duration)} · exit ${msg.exit_code ?? '—'}</span>`;
        cancelBtn.style.display = 'none';
      }
      if (follow) output.scrollTop = output.scrollHeight;
    });

    WR.modal({
      title: `Run output`,
      size: 'wide',
      body,
      onClose: () => socket.close(),
      actions: [{ label: 'Close', onClick: (c) => c() }],
    });
  },
};

/* --------------------------------------------------------------- theme */

(function theme() {
  const saved = localStorage.getItem('wr-theme');
  if (saved) document.documentElement.dataset.theme = saved;
  window.WRtoggleTheme = () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('wr-theme', next);
  };
})();

/* -------------------------------------------------- global event stream */

WR.events = (() => {
  const handlers = new Set();
  let socket = null;
  return {
    on(handler) {
      handlers.add(handler);
      if (!socket) {
        socket = WR.socket('/ws/events', (msg) => handlers.forEach((h) => h(msg)));
      }
      return () => handlers.delete(handler);
    },
  };
})();

window.WR = WR;
