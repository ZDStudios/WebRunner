/* WebRunner — visual workflow builder.
 * A node canvas with drag-to-connect edges and a schema-driven inspector.
 * Block definitions come from /api/blocks, so the UI never needs to know
 * what a block actually does. */

const NODE_WIDTH = 214;
const GRID = 11;

// Literal placeholder delimiters, kept out of JS template-literal syntax.
const T = '{' + '{';
const U = '}' + '}';

const state = {
  blocks: [],
  byType: {},
  graph: { nodes: [], start: '' },
  selected: null,
  view: { x: 60, y: 40, scale: 1 },
  dirty: false,
  saving: false,
  jobId: window.WR_JOB_ID || 0,
  linking: null,
};

const $ = (id) => document.getElementById(id);
const canvas = $('canvas');
const world = $('world');
const nodesLayer = $('nodes');
const edgesLayer = $('edges');

/* ------------------------------------------------------------------ utils */

const uid = () => 'n' + Math.random().toString(36).slice(2, 8);

function markDirty(dirty = true) {
  state.dirty = dirty;
  $('dirty-flag').textContent = dirty ? 'unsaved changes' : '';
}

function blockDef(type) {
  return state.byType[type] || { label: type, fields: [], outputs: ['next'], color: '#8a8f98' };
}

function outputsOf(node) {
  const outs = blockDef(node.type).outputs || [];
  return outs.length ? outs : [];
}

function summarise(node) {
  const def = blockDef(node.type);
  const raw = (def.summary || '').replace(/\{(\w+)\}/g, (_, key) => {
    const value = node.config?.[key];
    if (value === null || value === undefined || value === '') return '';
    if (typeof value === 'object') {
      return Object.entries(value).map(([k, v]) => `${k}=${v}`).join(' ') || '';
    }
    return String(value);
  }).trim();
  return raw.length > 74 ? raw.slice(0, 74) + '…' : raw;
}

/* --------------------------------------------------------------- palette */

function renderPalette() {
  const groups = {};
  for (const block of state.blocks) (groups[block.category] ||= []).push(block);

  $('palette').innerHTML = Object.entries(groups).map(([category, items]) => `
    <div style="margin-bottom:16px">
      <div class="stat-label" style="margin:0 4px 7px">${WR.escape(category)}</div>
      ${items.map((block) => `
        <button class="palette-item" data-type="${block.type}" title="${WR.escape(block.description)}"
          style="width:100%;justify-content:flex-start;margin-bottom:4px;background:transparent;
                 border-color:transparent;padding:7px 8px;text-align:left">
          <span style="width:24px;height:24px;border-radius:6px;flex:none;display:grid;
                       place-items:center;background:${block.color}22;color:${block.color}">
            <svg width="14" height="14"><use href="#i-${block.icon}"/></svg>
          </span>
          <span style="font-size:12.5px;font-weight:550">${WR.escape(block.label)}</span>
        </button>`).join('')}
    </div>`).join('');

  for (const button of document.querySelectorAll('.palette-item')) {
    button.addEventListener('click', () => addNode(button.dataset.type));
  }
}

/* ----------------------------------------------------------------- nodes */

function addNode(type, position) {
  const def = blockDef(type);
  const config = {};
  for (const field of def.fields || []) {
    if (field.type === 'note') continue;
    config[field.key] = field.default !== undefined ? field.default
      : field.type === 'keyvalue' ? {}
      : field.type === 'checkbox' ? false : '';
  }

  const last = state.graph.nodes[state.graph.nodes.length - 1];
  const node = {
    id: uid(),
    type,
    name: def.label,
    config,
    edges: {},
    ignore_failure: false,
    position: position || (last
      ? { x: last.position.x, y: last.position.y + 150 }
      : { x: 140, y: 90 }),
  };

  // Wire it onto the end of the chain so a linear workflow just works.
  if (last) {
    const handle = (outputsOf(last)[0]) || 'next';
    if (!last.edges[handle]) last.edges[handle] = node.id;
  }
  state.graph.nodes.push(node);
  if (!state.graph.start) state.graph.start = node.id;

  select(node.id);
  markDirty();
  draw();
  return node;
}

function removeNode(id) {
  state.graph.nodes = state.graph.nodes.filter((n) => n.id !== id);
  for (const node of state.graph.nodes) {
    for (const [handle, target] of Object.entries(node.edges)) {
      if (target === id) delete node.edges[handle];
    }
  }
  if (state.graph.start === id) state.graph.start = state.graph.nodes[0]?.id || '';
  if (state.selected === id) state.selected = null;
  markDirty();
  draw();
  renderInspector();
}

function select(id) {
  state.selected = id;
  renderInspector();
  for (const el of nodesLayer.children) {
    el.classList.toggle('selected', el.dataset.id === state.selected);
    el.style.borderColor = el.dataset.id === state.selected ? 'var(--accent)' : 'var(--border-strong)';
    el.style.boxShadow = el.dataset.id === state.selected
      ? '0 0 0 3px var(--accent-soft), var(--shadow)' : 'var(--shadow)';
  }
}

/* ----------------------------------------------------------------- render */

function draw() {
  $('canvas-empty').style.display = state.graph.nodes.length ? 'none' : '';
  world.style.transform =
    `translate(${state.view.x}px, ${state.view.y}px) scale(${state.view.scale})`;
  $('zoom-label').textContent = Math.round(state.view.scale * 100) + '%';

  nodesLayer.innerHTML = '';
  for (const node of state.graph.nodes) nodesLayer.append(nodeElement(node));
  // Called synchronously: reading offsetLeft/offsetHeight below forces the
  // layout we need anyway, and rAF never fires in a backgrounded tab.
  drawEdges();
}

function nodeElement(node) {
  const def = blockDef(node.type);
  const outputs = outputsOf(node);
  const isStart = state.graph.start === node.id;
  const selected = state.selected === node.id;

  const el = WR.el('div', {
    class: 'wf-node',
    dataset: { id: node.id },
    style: {
      position: 'absolute',
      left: node.position.x + 'px',
      top: node.position.y + 'px',
      width: NODE_WIDTH + 'px',
      background: 'var(--panel)',
      border: `1px solid ${selected ? 'var(--accent)' : 'var(--border-strong)'}`,
      borderRadius: '9px',
      boxShadow: selected ? '0 0 0 3px var(--accent-soft), var(--shadow)' : 'var(--shadow)',
      cursor: 'grab',
      userSelect: 'none',
    },
  });

  el.innerHTML = `
    <div class="wf-head" style="display:flex;align-items:center;gap:8px;padding:8px 10px;
         border-bottom:1px solid var(--border);border-left:3px solid ${def.color};
         border-radius:8px 8px 0 0">
      <span style="width:20px;height:20px;border-radius:5px;flex:none;display:grid;place-items:center;
                   background:${def.color}22;color:${def.color}">
        <svg width="12" height="12"><use href="#i-${def.icon}"/></svg>
      </span>
      <span style="font-size:12.5px;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;
                   white-space:nowrap">${WR.escape(node.name || def.label)}</span>
      ${isStart ? '<span class="tag" style="font-size:9.5px;padding:0 5px">START</span>' : ''}
    </div>
    <div style="padding:8px 10px;font-size:11.5px;color:var(--text-faint);font-family:var(--mono);
                min-height:16px;word-break:break-all;max-height:44px;overflow:hidden">
      ${WR.escape(summarise(node)) || '<span style="opacity:.5">not configured</span>'}
    </div>
    ${node.ignore_failure ? '<div style="padding:0 10px 7px"><span class="tag" style="font-size:9.5px">ignores failure</span></div>' : ''}
    <div class="wf-ports" style="display:flex;padding:0 10px 9px;gap:6px">
      ${outputs.map((handle) => `
        <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:3px">
          <span style="font-size:9.5px;letter-spacing:.04em;text-transform:uppercase;
                       color:${portColour(handle)};font-weight:700">${handle}</span>
          <span class="wf-port" data-handle="${handle}" data-node="${node.id}"
                style="width:11px;height:11px;border-radius:50%;background:var(--panel);
                       border:2px solid ${portColour(handle)};cursor:crosshair"></span>
        </div>`).join('')}
    </div>
    <span class="wf-in" style="position:absolute;top:-6px;left:50%;transform:translateX(-50%);
          width:11px;height:11px;border-radius:50%;background:var(--panel);
          border:2px solid var(--border-strong)"></span>`;

  el.querySelector('.wf-head').addEventListener('mousedown', (e) => startNodeDrag(e, node, el));
  el.addEventListener('mousedown', (e) => {
    if (!e.target.classList.contains('wf-port')) select(node.id);
  });
  el.addEventListener('dblclick', () => select(node.id));
  for (const port of el.querySelectorAll('.wf-port')) {
    port.addEventListener('mousedown', (e) => startLink(e, node, port.dataset.handle));
  }
  return el;
}

function portColour(handle) {
  if (handle === 'success' || handle === 'true') return 'var(--green)';
  if (handle === 'failure' || handle === 'false') return 'var(--red)';
  return 'var(--accent)';
}

function nodeSize(id) {
  const el = nodesLayer.querySelector(`[data-id="${id}"]`);
  return { w: NODE_WIDTH, h: el ? el.offsetHeight : 96 };
}

function portPosition(nodeId, handle) {
  const node = state.graph.nodes.find((n) => n.id === nodeId);
  if (!node) return null;
  const el = nodesLayer.querySelector(`[data-id="${nodeId}"]`);
  const port = el?.querySelector(`.wf-port[data-handle="${handle}"]`);
  const size = nodeSize(nodeId);
  if (!port) return { x: node.position.x + size.w / 2, y: node.position.y + size.h };
  return {
    x: node.position.x + port.offsetLeft + port.offsetWidth / 2,
    y: node.position.y + port.offsetTop + port.offsetHeight / 2,
  };
}

function inputPosition(nodeId) {
  const node = state.graph.nodes.find((n) => n.id === nodeId);
  if (!node) return null;
  return { x: node.position.x + NODE_WIDTH / 2, y: node.position.y - 1 };
}

function curve(from, to) {
  const dy = to.y - from.y;
  // Forward links get a gentle S (never more than half the gap, or the
  // control points overshoot each other). Backward links need a wider bow
  // so the edge visibly loops back instead of collapsing into a line.
  const bow = dy > 0
    ? Math.min(110, Math.max(24, dy * 0.5))
    : Math.max(70, Math.abs(dy) * 0.35 + 60);
  return `M ${from.x} ${from.y} C ${from.x} ${from.y + bow}, ` +
         `${to.x} ${to.y - bow}, ${to.x} ${to.y}`;
}

function drawEdges() {
  const paths = [];
  for (const node of state.graph.nodes) {
    for (const [handle, target] of Object.entries(node.edges)) {
      if (!target) continue;
      const from = portPosition(node.id, handle);
      const to = inputPosition(target);
      if (!from || !to) continue;
      const colour = portColour(handle);
      paths.push(`
        <path d="${curve(from, to)}" stroke="${colour}" stroke-width="2" fill="none"
              opacity="0.75" marker-end="url(#arrow-${handle})"/>
        <path d="${curve(from, to)}" stroke="transparent" stroke-width="14" fill="none"
              style="pointer-events:stroke;cursor:pointer"
              data-from="${node.id}" data-handle="${handle}" class="edge-hit"/>`);
    }
  }

  const markers = ['success', 'failure', 'next', 'true', 'false'].map((handle) => `
    <marker id="arrow-${handle}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5"
            markerHeight="5" orient="auto-start-reverse">
      <path d="M 0 1 L 9 5 L 0 9 z" fill="${portColour(handle)}"/>
    </marker>`).join('');

  edgesLayer.innerHTML = `<defs>${markers}</defs>${paths.join('')}` +
    (state.linking ? `<path id="link-preview" stroke="var(--accent)" stroke-width="2"
       stroke-dasharray="5 4" fill="none" d=""/>` : '');
  edgesLayer.style.pointerEvents = 'auto';

  for (const hit of edgesLayer.querySelectorAll('.edge-hit')) {
    hit.addEventListener('click', async (event) => {
      event.stopPropagation();
      const node = state.graph.nodes.find((n) => n.id === hit.dataset.from);
      if (node && await WR.confirm('Remove this connection?',
                                   { title: 'Disconnect', confirmLabel: 'Remove' })) {
        delete node.edges[hit.dataset.handle];
        markDirty();
        draw();
      }
    });
  }
}

/* ------------------------------------------------------- canvas gestures */

function toWorld(event) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: (event.clientX - rect.left - state.view.x) / state.view.scale,
    y: (event.clientY - rect.top - state.view.y) / state.view.scale,
  };
}

function startNodeDrag(event, node, el) {
  if (event.button !== 0) return;
  event.stopPropagation();
  event.preventDefault();
  select(node.id);
  const origin = toWorld(event);
  const start = { ...node.position };
  el.style.cursor = 'grabbing';
  el.style.zIndex = '10';

  const move = (e) => {
    const now = toWorld(e);
    node.position.x = Math.round((start.x + now.x - origin.x) / GRID) * GRID;
    node.position.y = Math.round((start.y + now.y - origin.y) / GRID) * GRID;
    el.style.left = node.position.x + 'px';
    el.style.top = node.position.y + 'px';
    drawEdges();
  };
  const up = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    el.style.cursor = 'grab';
    el.style.zIndex = '';
    markDirty();
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
}

function startLink(event, node, handle) {
  event.stopPropagation();
  event.preventDefault();
  state.linking = { node, handle };
  drawEdges();
  const preview = document.getElementById('link-preview');
  const from = portPosition(node.id, handle);

  const move = (e) => {
    const to = toWorld(e);
    preview?.setAttribute('d', curve(from, to));
    for (const el of nodesLayer.children) {
      const size = nodeSize(el.dataset.id);
      const target = state.graph.nodes.find((n) => n.id === el.dataset.id);
      const over = target && target.id !== node.id &&
        to.x > target.position.x && to.x < target.position.x + size.w &&
        to.y > target.position.y && to.y < target.position.y + size.h;
      el.style.outline = over ? '2px solid var(--accent)' : '';
    }
  };

  const up = (e) => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    const drop = toWorld(e);
    for (const el of nodesLayer.children) el.style.outline = '';

    const target = state.graph.nodes.find((candidate) => {
      if (candidate.id === node.id) return false;
      const size = nodeSize(candidate.id);
      return drop.x > candidate.position.x && drop.x < candidate.position.x + size.w &&
             drop.y > candidate.position.y - 12 && drop.y < candidate.position.y + size.h;
    });

    state.linking = null;
    if (target) {
      node.edges[handle] = target.id;
      markDirty();
    } else if (Math.abs(drop.y - from.y) > 40) {
      // Dropped on empty canvas — offer to create the next block right there.
      offerBlockMenu(drop, (type) => {
        const created = addNode(type, { x: drop.x - NODE_WIDTH / 2, y: drop.y });
        node.edges[handle] = created.id;
        markDirty();
        draw();
      });
    }
    draw();
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
}

function offerBlockMenu(position, onPick) {
  const rect = canvas.getBoundingClientRect();
  const menu = WR.el('div', {
    class: 'card',
    style: {
      position: 'fixed',
      left: Math.min(rect.right - 210, position.x * state.view.scale + state.view.x + rect.left) + 'px',
      top: Math.min(rect.bottom - 260, position.y * state.view.scale + state.view.y + rect.top) + 'px',
      width: '200px', maxHeight: '260px', overflowY: 'auto', zIndex: '90',
      boxShadow: 'var(--shadow)', padding: '6px',
    },
  });
  for (const block of state.blocks) {
    menu.append(WR.el('button', {
      class: 'btn-ghost',
      style: { width: '100%', justifyContent: 'flex-start', padding: '6px 8px' },
      onClick: () => { menu.remove(); onPick(block.type); },
    }, block.label));
  }
  const dismiss = (e) => {
    if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('mousedown', dismiss); }
  };
  setTimeout(() => document.addEventListener('mousedown', dismiss), 0);
  document.body.append(menu);
}

canvas.addEventListener('mousedown', (event) => {
  if (event.target !== canvas && event.target !== world && event.target !== edgesLayer &&
      event.target !== nodesLayer && event.target.id !== 'canvas-empty') return;
  if (event.button === 2) return;
  select(null);
  const start = { x: event.clientX, y: event.clientY, vx: state.view.x, vy: state.view.y };
  canvas.style.cursor = 'grabbing';
  const move = (e) => {
    state.view.x = start.vx + (e.clientX - start.x);
    state.view.y = start.vy + (e.clientY - start.y);
    world.style.transform =
      `translate(${state.view.x}px, ${state.view.y}px) scale(${state.view.scale})`;
  };
  const up = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    canvas.style.cursor = 'grab';
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
});

canvas.addEventListener('wheel', (event) => {
  event.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const px = event.clientX - rect.left;
  const py = event.clientY - rect.top;

  if (event.ctrlKey || event.metaKey) {
    const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
    zoomAt(px, py, factor);
  } else {
    state.view.x -= event.deltaX;
    state.view.y -= event.deltaY;
    world.style.transform =
      `translate(${state.view.x}px, ${state.view.y}px) scale(${state.view.scale})`;
  }
}, { passive: false });

function zoomAt(px, py, factor) {
  const next = Math.max(0.35, Math.min(2.2, state.view.scale * factor));
  const ratio = next / state.view.scale;
  state.view.x = px - (px - state.view.x) * ratio;
  state.view.y = py - (py - state.view.y) * ratio;
  state.view.scale = next;
  world.style.transform =
    `translate(${state.view.x}px, ${state.view.y}px) scale(${state.view.scale})`;
  $('zoom-label').textContent = Math.round(next * 100) + '%';
}

function fitToView() {
  if (!state.graph.nodes.length) {
    state.view = { x: 60, y: 40, scale: 1 };
    return draw();
  }
  const xs = state.graph.nodes.map((n) => n.position.x);
  const ys = state.graph.nodes.map((n) => n.position.y);
  const width = Math.max(...xs) + NODE_WIDTH - Math.min(...xs);
  const height = Math.max(...ys) + 150 - Math.min(...ys);
  const scale = Math.max(0.4, Math.min(1.1,
    Math.min((canvas.clientWidth - 90) / width, (canvas.clientHeight - 90) / height)));
  state.view.scale = scale;
  state.view.x = (canvas.clientWidth - width * scale) / 2 - Math.min(...xs) * scale;
  state.view.y = 40 - Math.min(...ys) * scale;
  draw();
}

$('zoom-in').onclick = () => zoomAt(canvas.clientWidth / 2, canvas.clientHeight / 2, 1.15);
$('zoom-out').onclick = () => zoomAt(canvas.clientWidth / 2, canvas.clientHeight / 2, 1 / 1.15);
$('zoom-reset').onclick = fitToView;

document.addEventListener('keydown', (event) => {
  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
  if (typing) return;
  if ((event.key === 'Delete' || event.key === 'Backspace') && state.selected) {
    event.preventDefault();
    removeNode(state.selected);
  }
  if ((event.ctrlKey || event.metaKey) && event.key === 's') {
    event.preventDefault();
    save();
  }
});

/* ------------------------------------------------------------- inspector */

function renderInspector() {
  const host = $('inspector');
  const node = state.graph.nodes.find((n) => n.id === state.selected);

  if (!node) {
    host.innerHTML = `
      <div style="padding:18px">
        <div class="stat-label mb">Workflow</div>
        <p class="small muted">Select a block to configure it, or add one from the palette.</p>
        <div class="banner info small" style="margin-top:14px;display:block">
          <b>How it flows</b><br>
          The <b>START</b> block runs first, then WebRunner follows the connections.
          Blocks with <span style="color:var(--green)">success</span> and
          <span style="color:var(--red)">failure</span> outputs let you branch on errors —
          if nothing is wired to <span style="color:var(--red)">failure</span>, a failed block
          ends the run.
        </div>
        <div class="mt small">
          <div class="stat-label mb">Use values from earlier blocks</div>
          <div class="mono tiny faint" style="line-height:1.9">
            ${T} steps.&lt;id&gt;.stdout ${U}<br>
            ${T} steps.&lt;id&gt;.json.data.token ${U}<br>
            ${T} vars.myvar ${U}<br>
            ${T} secrets.API_KEY ${U}<br>
            ${T} now.date ${U} · ${T} job.name ${U}
          </div>
        </div>
      </div>`;
    return;
  }

  const def = blockDef(node.type);
  const isStart = state.graph.start === node.id;

  host.innerHTML = `
    <div style="padding:14px 16px;border-bottom:1px solid var(--border);display:flex;
                align-items:center;gap:9px;position:sticky;top:0;background:var(--bg-raised);z-index:2">
      <span style="width:26px;height:26px;border-radius:6px;flex:none;display:grid;place-items:center;
                   background:${def.color}22;color:${def.color}">
        <svg width="15" height="15"><use href="#i-${def.icon}"/></svg>
      </span>
      <div style="flex:1;min-width:0">
        <div style="font-weight:600;font-size:13px">${WR.escape(def.label)}</div>
        <div class="tiny faint mono">${node.id}</div>
      </div>
      <button class="btn-ghost btn-icon" id="delete-node" title="Delete block" style="color:var(--red)">
        <svg width="14" height="14"><use href="#i-trash"/></svg>
      </button>
    </div>
    <div style="padding:16px" id="inspector-fields"></div>`;

  const fields = $('inspector-fields');
  fields.append(textField('Block name', node.name, (value) => {
    node.name = value; markDirty(); draw();
  }, def.label));

  for (const field of def.fields || []) fields.append(fieldEditor(node, field));

  if ((def.outputs || []).includes('failure')) {
    fields.append(WR.el('label', { class: 'checkbox-row mb', style: { marginTop: '4px' } },
      WR.el('input', {
        type: 'checkbox', checked: node.ignore_failure,
        onChange: (e) => { node.ignore_failure = e.target.checked; markDirty(); draw(); },
      }),
      'Keep going if this block fails'));
  }

  const startButton = WR.el('button', {
    class: 'btn-sm',
    disabled: isStart,
    onClick: () => { state.graph.start = node.id; markDirty(); draw(); renderInspector(); },
  }, isStart ? 'This is the first block' : 'Make this the first block');

  fields.append(WR.el('div', { class: 'mt', style: { borderTop: '1px solid var(--border)', paddingTop: '14px' } },
    startButton,
    WR.el('div', { class: 'tiny faint mt' }, def.description || '')));

  $('delete-node').onclick = () => removeNode(node.id);
}

function shouldShow(node, field) {
  const condition = field.show_if;
  if (!condition) return true;
  return Object.entries(condition).every(([key, values]) =>
    values.includes(String(node.config?.[key] ?? '')));
}

function fieldEditor(node, field) {
  if (field.type === 'note') {
    return WR.el('div', { class: 'banner small mb', style: { display: 'block' } }, field.text);
  }
  if (!shouldShow(node, field)) return document.createComment('hidden');

  const wrap = WR.el('div', { class: 'field' });
  const value = node.config?.[field.key];
  const update = (next) => {
    node.config[field.key] = next;
    markDirty();
    draw();
    // A change can reveal or hide dependent fields.
    if (state.blocks.some((b) => b.fields.some((f) => f.show_if && f.show_if[field.key]))) {
      renderInspector();
    }
  };

  if (field.type === 'checkbox') {
    return WR.el('label', { class: 'checkbox-row mb' },
      WR.el('input', { type: 'checkbox', checked: !!value, onChange: (e) => update(e.target.checked) }),
      field.label);
  }

  wrap.append(WR.el('label', {}, field.label));

  let input;
  if (field.type === 'select') {
    input = WR.el('select', { onChange: (e) => update(e.target.value) });
    for (const option of field.options || []) {
      const opt = typeof option === 'string' ? { value: option, label: option } : option;
      input.append(WR.el('option', {
        value: opt.value, selected: String(value ?? '') === String(opt.value),
      }, opt.label));
    }
  } else if (field.type === 'code' || field.type === 'textarea') {
    input = WR.el('textarea', {
      class: field.type === 'code' ? 'code-input' : '',
      rows: field.rows || 4,
      placeholder: field.placeholder || '',
      onInput: (e) => update(e.target.value),
      onKeyDown: (e) => {
        if (e.key === 'Tab') {
          e.preventDefault();
          const el = e.target;
          const at = el.selectionStart;
          el.value = el.value.slice(0, at) + '  ' + el.value.slice(el.selectionEnd);
          el.selectionStart = el.selectionEnd = at + 2;
          update(el.value);
        }
      },
    });
    input.value = value ?? '';
  } else if (field.type === 'keyvalue') {
    input = keyValueEditor(value || {}, update, field);
  } else {
    input = WR.el('input', {
      type: field.type === 'password' ? 'password' : field.type === 'number' ? 'number' : 'text',
      placeholder: field.placeholder || '',
      min: field.min, max: field.max,
      autocomplete: 'off',
      onInput: (e) => update(field.type === 'number' ? Number(e.target.value) : e.target.value),
    });
    input.value = value ?? '';
    if (field.key === 'command' || field.key === 'url') input.classList.add('code-input');
  }

  wrap.append(input);
  if (field.help) wrap.append(WR.el('div', { class: 'field-help' }, field.help));
  return wrap;
}

function keyValueEditor(pairs, update, field) {
  const host = WR.el('div');
  const entries = Object.entries(pairs || {});

  const commit = () => {
    const next = {};
    for (const row of host.querySelectorAll('.kv-row')) {
      const [keyInput, valueInput] = row.querySelectorAll('input');
      if (keyInput.value.trim()) next[keyInput.value.trim()] = valueInput.value;
    }
    update(next);
  };

  const addRow = (key = '', value = '') => {
    const row = WR.el('div', { class: 'kv-row' },
      WR.el('input', {
        value: key, placeholder: field.placeholder_key || 'name',
        autocomplete: 'off', onChange: commit,
      }),
      WR.el('input', {
        value, placeholder: field.placeholder_value || 'value',
        autocomplete: 'off', onChange: commit,
      }),
      WR.el('button', {
        class: 'btn-ghost btn-icon', title: 'Remove',
        onClick: () => { row.remove(); commit(); },
      }, '✕'));
    host.insertBefore(row, host.lastChild);
    return row;
  };

  host.append(WR.el('button', {
    class: 'btn-sm', style: { width: '100%', justifyContent: 'center' },
    onClick: () => addRow(),
  }, '+ Add'));

  for (const [key, value] of entries) addRow(key, value);
  return host;
}

function textField(label, value, onChange, placeholder = '') {
  const wrap = WR.el('div', { class: 'field' }, WR.el('label', {}, label));
  const input = WR.el('input', { placeholder, autocomplete: 'off', onInput: (e) => onChange(e.target.value) });
  input.value = value ?? '';
  wrap.append(input);
  return wrap;
}

/* -------------------------------------------------------------- job form */

const envEditor = { value: {} };

function setupJobForm() {
  const host = $('job-env');
  host.innerHTML = '';
  host.append(keyValueEditor(envEditor.value, (next) => { envEditor.value = next; markDirty(); },
    { placeholder_key: 'NAME', placeholder_value: 'value' }));

  try {
    const zones = Intl.supportedValuesOf ? Intl.supportedValuesOf('timeZone') : [];
    $('tz-list').innerHTML = zones.map((z) => `<option value="${z}">`).join('');
  } catch { /* older browsers simply get a free-text field */ }

  const presets = [
    ['Every minute', '* * * * *'], ['Every 5 min', '*/5 * * * *'],
    ['Every 15 min', '*/15 * * * *'], ['Hourly', '0 * * * *'],
    ['Daily 03:00', '0 3 * * *'], ['Weekdays 09:00', '0 9 * * 1-5'],
    ['Weekly (Sun)', '0 0 * * 0'], ['Monthly 1st', '0 0 1 * *'],
  ];
  $('cron-presets').innerHTML = presets
    .map(([label, expr]) => `<button class="btn-sm" data-cron="${expr}">${label}</button>`).join('');
  for (const button of $('cron-presets').querySelectorAll('button')) {
    button.onclick = () => { $('cron').value = button.dataset.cron; markDirty(); previewSchedule(); };
  }

  $('trigger-type').addEventListener('change', () => { updateTriggerFields(); previewSchedule(); });
  for (const id of ['cron', 'interval-value', 'interval-unit', 'run-at', 'timezone',
                    'start-at', 'end-at']) {
    $(id).addEventListener('input', () => { markDirty(); previewSchedule(); });
  }
  for (const id of ['job-name', 'job-enabled', 'job-description', 'job-tags', 'job-cwd',
                    'job-run-as', 'job-timeout', 'job-retries', 'job-retry-delay',
                    'job-concurrency', 'job-catchup', 'job-notify-on', 'job-notify-url']) {
    $(id).addEventListener('input', () => markDirty());
  }

  for (const tab of document.querySelectorAll('.tabs button')) {
    tab.onclick = () => {
      for (const other of document.querySelectorAll('.tabs button')) other.classList.remove('active');
      tab.classList.add('active');
      for (const panel of document.querySelectorAll('.tab-panel')) panel.style.display = 'none';
      $('tab-' + tab.dataset.tab).style.display = tab.dataset.tab === 'workflow' ? 'flex' : 'block';
      if (tab.dataset.tab === 'schedule') previewSchedule();
    };
  }
}

function updateTriggerFields() {
  const type = $('trigger-type').value;
  $('cron-fields').style.display = type === 'cron' ? '' : 'none';
  $('interval-fields').style.display = type === 'interval' ? '' : 'none';
  $('date-fields').style.display = type === 'date' ? '' : 'none';
}

let previewTimer = null;
function previewSchedule() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    const host = $('schedule-preview');
    const payload = collectSchedule();
    if (payload.trigger_type === 'manual') {
      host.innerHTML = '<span class="muted">This job only runs when you press Run.</span>';
      return;
    }
    host.innerHTML = '<div class="row"><span class="spinner"></span> checking…</div>';
    try {
      const result = await WR.post('/api/jobs/preview', { ...payload, count: 6 });
      if (result.error) {
        host.innerHTML = `<div class="banner error small">${WR.escape(result.error)}</div>`;
        return;
      }
      host.innerHTML =
        (result.human ? `<div class="mb"><b>${WR.escape(result.human)}</b></div>` : '') +
        '<div class="stat-label mb">Next runs</div>' +
        result.runs.map((epoch) => {
          const date = new Date(epoch * 1000);
          return `<div class="row small" style="justify-content:space-between;
                    padding:5px 0;border-bottom:1px solid var(--border)">
            <span class="mono">${date.toLocaleString(undefined, {
              weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
              hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
            <span class="faint tiny">${WR.relative(epoch)}</span></div>`;
        }).join('');
    } catch (err) {
      host.innerHTML = `<div class="banner error small">${WR.escape(err.message)}</div>`;
    }
  }, 260);
}

function collectSchedule() {
  const type = $('trigger-type').value;
  return {
    trigger_type: type,
    cron: $('cron').value.trim(),
    interval_seconds: type === 'interval'
      ? Math.max(1, Number($('interval-value').value || 0)) * Number($('interval-unit').value)
      : 0,
    run_at: $('run-at').value,
    timezone: $('timezone').value.trim(),
    start_at: $('start-at').value,
    end_at: $('end-at').value,
  };
}

function collect() {
  return {
    name: $('job-name').value.trim(),
    description: $('job-description').value,
    enabled: $('job-enabled').checked,
    tags: $('job-tags').value,
    steps: state.graph,
    ...collectSchedule(),
    cwd: $('job-cwd').value.trim(),
    env: envEditor.value,
    run_as: $('job-run-as').value.trim(),
    timeout_seconds: Number($('job-timeout').value || 0),
    max_retries: Number($('job-retries').value || 0),
    retry_delay_seconds: Number($('job-retry-delay').value || 0),
    concurrency: $('job-concurrency').value,
    catch_up: $('job-catchup').checked,
    notify_on: $('job-notify-on').value,
    notify_url: $('job-notify-url').value.trim(),
  };
}

function fill(job) {
  $('job-name').value = job.name || '';
  $('job-enabled').checked = !!job.enabled;
  $('job-description').value = job.description || '';
  $('job-tags').value = job.tags || '';
  $('job-cwd').value = job.cwd || '';
  $('job-run-as').value = job.run_as || '';
  $('job-timeout').value = job.timeout_seconds || 0;
  $('job-retries').value = job.max_retries || 0;
  $('job-retry-delay').value = job.retry_delay_seconds ?? 30;
  $('job-concurrency').value = job.concurrency || 'skip';
  $('job-catchup').checked = !!job.catch_up;
  $('job-notify-on').value = job.notify_on || 'failure';
  $('job-notify-url').value = job.notify_url || '';
  $('trigger-type').value = job.trigger_type || 'cron';
  $('cron').value = job.cron || '';
  $('run-at').value = (job.run_at || '').slice(0, 16);
  $('timezone').value = job.timezone || '';
  $('start-at').value = (job.start_at || '').slice(0, 16);
  $('end-at').value = (job.end_at || '').slice(0, 16);

  const seconds = job.interval_seconds || 0;
  const unit = seconds && seconds % 86400 === 0 ? 86400
    : seconds && seconds % 3600 === 0 ? 3600
    : seconds && seconds % 60 === 0 ? 60 : 1;
  $('interval-unit').value = String(unit);
  $('interval-value').value = seconds ? seconds / unit : 5;

  envEditor.value = job.env || {};
  state.graph = job.steps && job.steps.nodes ? job.steps : { nodes: [], start: '' };
  for (const node of state.graph.nodes) {
    node.position ||= { x: 140, y: 90 };
    node.edges ||= {};
    node.config ||= {};
  }
  updateTriggerFields();
}

/* ------------------------------------------------------------------ save */

async function save({ thenRun = false } = {}) {
  if (state.saving) return null;
  const payload = collect();

  if (!payload.name) {
    WR.toast('Give the job a name first', 'error');
    $('job-name').focus();
    return null;
  }
  if (!state.graph.nodes.length) {
    WR.toast('Add at least one block to the workflow', 'error');
    return null;
  }

  state.saving = true;
  $('save-btn').disabled = true;
  $('save-btn').textContent = 'Saving…';
  try {
    const saved = state.jobId
      ? await WR.put(`/api/jobs/${state.jobId}`, payload)
      : await WR.post('/api/jobs', payload);
    state.jobId = saved.id;
    markDirty(false);
    WR.toast('Job saved', 'success');
    if (!window.location.pathname.includes(`/jobs/${saved.id}/`)) {
      window.history.replaceState({}, '', `/jobs/${saved.id}/edit`);
    }
    if (thenRun) {
      const { run_id } = await WR.post(`/api/jobs/${saved.id}/run`);
      WR.showRun(run_id);
    }
    return saved;
  } catch (err) {
    WR.error(err);
    return null;
  } finally {
    state.saving = false;
    $('save-btn').disabled = false;
    $('save-btn').textContent = 'Save job';
  }
}

$('save-btn').onclick = () => save();
$('run-btn').onclick = () => save({ thenRun: true });

window.addEventListener('beforeunload', (event) => {
  if (state.dirty) { event.preventDefault(); event.returnValue = ''; }
});

/* ------------------------------------------------------------------ boot */

(async function boot() {
  try {
    const registry = await WR.get('/api/blocks');
    state.blocks = registry.blocks;
    state.byType = Object.fromEntries(registry.blocks.map((b) => [b.type, b]));
  } catch (err) {
    return WR.error(err);
  }
  renderPalette();
  setupJobForm();

  if (state.jobId) {
    try {
      fill(await WR.get(`/api/jobs/${state.jobId}`));
    } catch (err) {
      WR.error(err);
    }
  } else {
    $('job-name').focus();
    $('cron').value = '0 3 * * *';
    updateTriggerFields();
  }

  draw();
  renderInspector();
  if (state.graph.nodes.length) fitToView();
  markDirty(false);
})();
