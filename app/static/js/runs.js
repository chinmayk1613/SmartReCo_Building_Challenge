(() => {
  const body = document.querySelector('[data-agent-runs-body]');
  const refreshed = document.querySelector('[data-runs-refreshed]');
  if (!body) return;

  const nodeLabels = {
    queued: 'Queued', graph_started: 'Graph started', load_context: 'Load behavior + course',
    retrieve_and_rank: 'RAG · retrieve and rank', verify_with_mcp: 'MCP · verify catalog',
    generate_copy: 'LLM · generate grounded copy', generate_copy_fallback: 'Safe provider fallback',
    validate_output: 'Validate LLM output', safe_fallback: 'Validation fallback',
    persist_recommendation: 'Persist recommendation', complete: 'Complete'
  };
  const cell = (value, className) => {
    const td = document.createElement('td');
    if (className) td.className = className;
    td.textContent = value ?? '—';
    return td;
  };
  const sub = (parent, value) => {
    const small = document.createElement('small'); small.className = 'table-sub'; small.textContent = value; parent.append(small);
  };
  const statusCell = (item) => {
    const td = document.createElement('td'); const span = document.createElement('span');
    span.className = `status ${item.status === 'succeeded' ? 'good' : item.status === 'failed' ? 'bad' : 'warn'}`;
    span.textContent = item.status; td.append(span); return td;
  };
  const row = (item) => {
    const tr = document.createElement('tr'); tr.dataset.runId = item.id;
    const started = document.createElement('td'); const time = document.createElement('time');
    time.dateTime = item.created_at; time.textContent = new Intl.DateTimeFormat('en-IN', {dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC'}).format(new Date(item.created_at)); started.append(time);
    const learner = document.createElement('td'); const name = document.createElement('strong'); name.textContent = item.user_name; learner.append(name); sub(learner, `(${item.user_id.slice(0, 8)})`); learner.title = item.user_id;
    const scope = document.createElement('td'); const scopeName = document.createElement('strong'); scopeName.textContent = item.scope; scope.append(scopeName); if (item.context_product_title) sub(scope, item.context_product_title);
    const error = document.createElement('td');
    if (item.error_code) { const code = document.createElement('strong'); code.textContent = item.error_code; error.append(code); sub(error, item.error_detail || 'No additional provider detail was returned.'); } else error.textContent = '—';
    tr.append(started, learner, scope, cell(item.trigger_reason), statusCell(item), cell(nodeLabels[item.current_node] || item.current_node), cell(item.model), cell(item.tokens), error);
    return tr;
  };
  const refresh = async () => {
    try {
      const response = await fetch('/api/admin/runs', {headers: {'Accept': 'application/json'}, cache: 'no-store'});
      if (!response.ok) return;
      const data = await response.json(); body.replaceChildren();
      if (!data.items.length) { const tr = document.createElement('tr'); const td = cell('No recommendation runs yet.'); td.colSpan = 9; tr.append(td); body.append(tr); }
      else data.items.forEach(item => body.append(row(item)));
      if (refreshed) refreshed.textContent = `Updated ${new Intl.DateTimeFormat('en-IN', {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: 'UTC', timeZoneName: 'short'}).format(new Date(data.refreshed_at))}`;
    } catch { if (refreshed) refreshed.textContent = 'Reconnecting…'; }
  };
  document.querySelectorAll('[data-agent-runs-body] time[datetime]').forEach(time => { time.textContent = new Intl.DateTimeFormat('en-IN', {dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC'}).format(new Date(time.dateTime)); });
  refresh(); setInterval(refresh, 1000);
})();
