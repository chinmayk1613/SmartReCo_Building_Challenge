(() => {
  const root = document.querySelector('[data-observability-root]');
  if (!root) return;
  const selectedPageDate = root.dataset.selectedDate || '';
  const selectedPageStartDate = root.dataset.selectedStartDate || selectedPageDate;
  const selectedPageEndDate = root.dataset.selectedEndDate || selectedPageDate;
  const timeline = root.querySelector('[data-observability-timeline]');
  const updated = root.querySelector('[data-observability-updated]');
  const dialog = root.querySelector('[data-kpi-dialog]');
  const detailLoading = dialog?.querySelector('[data-kpi-loading]');
  const detailRangeControl = dialog?.querySelector('[data-kpi-range-control]');
  const detailStartDate = dialog?.querySelector('[data-kpi-start-date]');
  const detailEndDate = dialog?.querySelector('[data-kpi-end-date]');
  let polling = false;
  let detailPolling = false;
  let activeMetric = '';
  let activeDetail = null;
  const chartSelections = new Map();
  const detailRangeSelections = new Map();
  const NUMBER_LOCALE = 'en-IN';
  const TIME_ZONE = 'UTC';

  const formatUtcDateTime = (value, includeDate = true) => new Intl.DateTimeFormat(NUMBER_LOCALE, {
    ...(includeDate ? {year: 'numeric', month: 'short', day: '2-digit'} : {}),
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    timeZone: TIME_ZONE, timeZoneName: 'short',
  }).format(new Date(value));

  const setMetric = (name, value) => {
    const node = root.querySelector(`[data-observe-metric="${name}"]`);
    if (node) node.textContent = value;
  };

  const formatValue = (value, format = 'integer') => {
    if (format === 'text') return value || 'None';
    const number = Number(value || 0);
    if (format === 'currency') return `$${number.toFixed(6)}`;
    if (format === 'milliseconds') return `${Math.round(number).toLocaleString(NUMBER_LOCALE)} ms`;
    if (format === 'percent') return `${number.toFixed(1)}%`;
    if (format === 'decimal') return number.toLocaleString(NUMBER_LOCALE, {maximumFractionDigits: 1});
    return Math.round(number).toLocaleString(NUMBER_LOCALE);
  };

  const renderChips = (container, values, emptyText, uppercase = false) => {
    if (!container) return;
    container.replaceChildren();
    const entries = Object.entries(values || {});
    if (!entries.length) {
      const empty = document.createElement('p'); empty.className = 'muted'; empty.textContent = emptyText; container.append(empty); return;
    }
    entries.forEach(([name, count]) => {
      const chip = document.createElement('span');
      const strong = document.createElement('strong'); strong.textContent = String(count);
      chip.append(strong, document.createTextNode(uppercase ? name.toUpperCase() : name)); container.append(chip);
    });
  };

  const appendCell = (row, primary, secondary = '') => {
    const cell = document.createElement('td'); cell.textContent = primary;
    if (secondary) {
      const small = document.createElement('small'); small.className = 'table-sub'; small.textContent = secondary; cell.append(small);
    }
    row.append(cell);
  };

  const renderTimeline = (items) => {
    if (!timeline) return;
    timeline.replaceChildren();
    if (!items.length) {
      const row = document.createElement('tr'); const cell = document.createElement('td'); cell.colSpan = 9;
      cell.textContent = 'No service invocations match this filter yet. Browse as a learner to create evidence.'; row.append(cell); timeline.append(row); return;
    }
    items.forEach((item) => {
      const row = document.createElement('tr');
      appendCell(row, formatUtcDateTime(item.started_at));
      appendCell(row, item.user_name, item.user_id ? `(${item.user_id.slice(0, 8)})` : '');
      appendCell(row, item.service.toUpperCase(), item.operation.replaceAll('_', ' '));
      appendCell(row, item.model || '—');
      appendCell(row, String(item.input_tokens + item.output_tokens), `${item.input_tokens} in / ${item.output_tokens} out`);
      appendCell(row, item.estimated_cost === null ? 'Unpriced' : `$${Number(item.estimated_cost).toFixed(6)}`);
      appendCell(row, item.latency_ms === null ? '—' : `${item.latency_ms} ms`);
      const outcome = item.failure_scope
        ? item.failure_scope.replaceAll('_', ' ')
        : item.attempt ? `attempt ${item.attempt} selected` : 'completed';
      const outcomeDetail = item.error_code
        ? `${item.error_code}${item.try_next_model ? ' · trying next model' : ' · failover stopped'}`
        : (item.provider_receipt || item.request_id) ? `Mesh receipt ${item.provider_receipt || item.request_id}` : '';
      const exportStatus = item.langsmith_export_status === 'legacy' ? 'not correlation-enabled' : (item.langsmith_export_status || 'not correlation-enabled').replaceAll('_', ' ');
      const exportDetail = item.service === 'llm' ? `LangSmith: ${exportStatus}` : '';
      appendCell(row, outcome, [outcomeDetail, exportDetail].filter(Boolean).join(' / '));
      const statusCell = document.createElement('td'); const status = document.createElement('span');
      status.className = `status ${item.status === 'succeeded' ? 'good' : item.status === 'failed' ? 'bad' : 'warn'}`;
      status.textContent = item.status; statusCell.append(status); row.append(statusCell); timeline.append(row);
    });
  };

  const summaryFields = (metric) => ({
    llm_calls: [['calls', 'Provider attempts', 'integer'], ['langsmith_matched', 'Matched spans', 'integer'], ['export_coverage', 'Export coverage', 'percent'], ['export_delayed', 'Delayed exports', 'integer']],
    total_tokens: [['total_tokens', 'Total tokens', 'integer'], ['input_tokens', 'Input tokens', 'integer'], ['output_tokens', 'Output tokens', 'integer'], ['tokens_per_call', 'Tokens / call', 'decimal']],
    estimated_cost: [['estimated_cost', 'Estimated cost', 'currency'], ['cost_per_call', 'Cost / call', 'currency'], ['priced_calls', 'Priced calls', 'integer'], ['unpriced_calls', 'Unpriced calls', 'integer']],
    rag_calls: [['calls', 'Retrievals', 'integer'], ['success_rate', 'Success rate', 'percent'], ['avg_latency', 'Avg latency', 'milliseconds'], ['p95_latency', 'P95 latency', 'milliseconds']],
    mcp_calls: [['calls', 'Tool calls', 'integer'], ['success_rate', 'Success rate', 'percent'], ['avg_latency', 'Avg latency', 'milliseconds'], ['p95_latency', 'P95 latency', 'milliseconds']],
    graph_runs: [['calls', 'Workflow runs', 'integer'], ['success_rate', 'Completion rate', 'percent'], ['avg_latency', 'Avg runtime', 'milliseconds'], ['p95_latency', 'P95 runtime', 'milliseconds']],
    average_latency: [['avg_latency', 'Average', 'milliseconds'], ['p95_latency', 'P95', 'milliseconds'], ['min_latency', 'Fastest', 'milliseconds'], ['max_latency', 'Slowest', 'milliseconds']],
    failures: [['failures', 'Failures', 'integer'], ['failure_rate', 'Failure rate', 'percent'], ['calls', 'Observed calls', 'integer'], ['top_error', 'Most common error', 'text']],
  }[metric] || []);

  const renderSummary = (data) => {
    const container = dialog.querySelector('[data-kpi-summary]'); container.replaceChildren();
    summaryFields(data.metric).forEach(([key, label, format]) => {
      const card = document.createElement('article');
      const small = document.createElement('span'); small.textContent = label;
      const strong = document.createElement('strong'); strong.textContent = formatValue(data.summary[key], format);
      card.append(small, strong); container.append(card);
    });
  };

  const renderTable = (head, body, rows, columns, leading) => {
    head.replaceChildren(); body.replaceChildren();
    const headerRow = document.createElement('tr');
    const leadingHeader = document.createElement('th'); leadingHeader.textContent = leading.label; headerRow.append(leadingHeader);
    columns.forEach((column) => { const cell = document.createElement('th'); cell.textContent = column.label; headerRow.append(cell); });
    head.append(headerRow);
    if (!rows.length) {
      const row = document.createElement('tr'); const cell = document.createElement('td'); cell.colSpan = columns.length + 1;
      cell.textContent = 'No matching evidence for this selection.'; row.append(cell); body.append(row); return;
    }
    rows.forEach((item) => {
      const row = document.createElement('tr');
      const first = document.createElement('td'); const strong = document.createElement('strong'); strong.textContent = item[leading.key] || 'System'; first.append(strong);
      if (leading.secondary && item[leading.secondary]) {
        const small = document.createElement('small'); small.className = 'table-sub'; small.textContent = `(${String(item[leading.secondary]).slice(0, 8)})`; first.append(small);
      }
      row.append(first);
      columns.forEach((column) => appendCell(row, formatValue(item[column.key], column.format)));
      body.append(row);
    });
  };

  const renderTrend = (data) => {
    const trend = dialog.querySelector('[data-kpi-trend]'); trend.replaceChildren();
    if (!data.daily.length) {
      const empty = document.createElement('p'); empty.className = 'kpi-empty'; empty.textContent = 'No daily trend is available yet.'; trend.append(empty); return;
    }
    const values = data.daily.slice(0, 14).map((row) => Number(row[data.primary_field] || 0));
    const maximum = Math.max(...values, 1);
    data.daily.slice(0, 14).forEach((item) => {
      const row = document.createElement('div'); row.className = 'kpi-trend-row';
      const date = document.createElement('time'); date.dateTime = item.date; date.textContent = item.date;
      const track = document.createElement('span'); const bar = document.createElement('i');
      bar.style.width = `${Math.max(3, Number(item[data.primary_field] || 0) / maximum * 100)}%`; track.append(bar);
      const value = document.createElement('strong'); value.textContent = formatValue(item[data.primary_field], data.primary_format);
      const health = document.createElement('b'); health.className = item.failure_rate > 10 ? 'critical' : item.failure_rate > 2 ? 'watch' : 'good';
      health.title = `${item.failure_rate}% failure rate`;
      row.append(date, track, value, health); trend.append(row);
    });
  };

  const renderUserView = () => {
    if (!activeDetail) return;
    const rows = activeDetail.users;
    renderTable(
      dialog.querySelector('[data-kpi-user-head]'), dialog.querySelector('[data-kpi-user-body]'),
      rows, activeDetail.columns, {key: 'user_name', secondary: 'user_id', label: 'Learner'},
    );
    const range = detailRangeSelections.get(activeMetric);
    dialog.querySelector('[data-kpi-user-scope]').textContent = activeMetric === 'llm_calls' && (range?.start || range?.end)
      ? `${range.start || 'Beginning'} to ${range.end || 'Latest'}`
      : 'All dates';
  };

  const svgElement = (name, attributes = {}) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  };

  const axisScale = (value, format) => {
    if (value <= 0) return {maximum: 1, ticks: [0, 1]};
    const roughStep = value / 5;
    const magnitude = 10 ** Math.floor(Math.log10(roughStep));
    const normalized = roughStep / magnitude;
    const multiplier = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
    let step = multiplier * magnitude;
    if (format === 'integer') step = Math.max(1, Math.ceil(step));
    const maximum = Math.ceil(value / step) * step;
    const ticks = [];
    for (let tick = 0; tick <= maximum + step / 1000; tick += step) ticks.push(tick);
    return {maximum, ticks};
  };

  const formatAxisValue = (value, format) => {
    if (format === 'percent') return `${Number(value).toFixed(value < 10 && value % 1 ? 1 : 0)}%`;
    if (format === 'milliseconds') {
      if (value >= 1000) return `${(value / 1000).toLocaleString(NUMBER_LOCALE, {maximumFractionDigits: 1})}k ms`;
      return `${Math.round(value).toLocaleString(NUMBER_LOCALE)} ms`;
    }
    if (value >= 1000000) return `${(value / 1000000).toLocaleString(NUMBER_LOCALE, {maximumFractionDigits: 1})}M`;
    if (value >= 1000) return `${(value / 1000).toLocaleString(NUMBER_LOCALE, {maximumFractionDigits: 1})}k`;
    if (format === 'integer') return Math.round(value).toLocaleString(NUMBER_LOCALE);
    return Number(value).toLocaleString(NUMBER_LOCALE, {maximumFractionDigits: 1});
  };

  const formatXAxisValue = (value) => {
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      return new Intl.DateTimeFormat(NUMBER_LOCALE, {day: '2-digit', month: 'short', timeZone: TIME_ZONE})
        .format(new Date(`${value}T00:00:00Z`));
    }
    if (/^\d{2}:\d{2}$/.test(value)) return `${value} UTC`;
    return value.length > 18 ? `${value.slice(0, 16)}…` : value;
  };

  const renderSeriesChart = (host, chart, suppliedSeries = null) => {
    host.replaceChildren();
    const series = (suppliedSeries || chart.series || [])
      .map((item) => ({...item, total: item.points.reduce((sum, point) => sum + Number(point.y || 0), 0)}))
      .sort((a, b) => b.total - a.total).slice(0, 4);
    if (!series.length || !series.some((item) => item.points.length)) {
      const empty = document.createElement('p'); empty.className = 'kpi-empty'; empty.textContent = 'No chart evidence is available yet.'; host.append(empty); return;
    }
    const labels = [...new Set(series.flatMap((item) => item.points.map((point) => point.x)))].sort();
    const maximum = Math.max(1, ...series.flatMap((item) => item.points.map((point) => Number(point.y || 0))));
    const scale = axisScale(maximum, chart.format);
    const axisMaximum = scale.maximum;
    const width = 680; const height = 230; const left = 72; const right = 20; const top = 16; const bottom = 58;
    const plotWidth = width - left - right; const plotHeight = height - top - bottom;
    const x = (label) => left + (labels.length === 1 ? plotWidth / 2 : labels.indexOf(label) / (labels.length - 1) * plotWidth);
    const y = (value) => top + plotHeight - (Number(value || 0) / axisMaximum * plotHeight);
    const colors = ['#5365ec', '#9146d5', '#1f9c78', '#e28636'];
    const svg = svgElement('svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': chart.title});
    scale.ticks.forEach((value) => {
      const yPosition = y(value);
      svg.append(svgElement('line', {x1: left, y1: yPosition, x2: width - right, y2: yPosition, class: 'chart-grid-line'}));
      const label = svgElement('text', {x: left - 10, y: yPosition + 4, class: 'chart-y-label', 'text-anchor': 'end'});
      label.textContent = formatAxisValue(value, chart.format); svg.append(label);
    });
    svg.append(svgElement('line', {x1: left, y1: top, x2: left, y2: top + plotHeight, class: 'chart-axis-line'}));
    svg.append(svgElement('line', {x1: left, y1: top + plotHeight, x2: width - right, y2: top + plotHeight, class: 'chart-axis-line'}));
    const tickStep = Math.max(1, Math.ceil(labels.length / 7));
    labels.forEach((labelValue, index) => {
      if (index % tickStep !== 0 && index !== labels.length - 1) return;
      const xPosition = x(labelValue);
      svg.append(svgElement('line', {x1: xPosition, y1: top + plotHeight, x2: xPosition, y2: top + plotHeight + 5, class: 'chart-axis-line'}));
      const label = svgElement('text', {x: xPosition, y: top + plotHeight + 21, class: 'chart-x-label', 'text-anchor': 'middle'});
      label.textContent = formatXAxisValue(labelValue); svg.append(label);
    });
    series.forEach((item, index) => {
      const ordered = [...item.points].sort((a, b) => a.x.localeCompare(b.x));
      const path = ordered.map((point, pointIndex) => `${pointIndex ? 'L' : 'M'} ${x(point.x)} ${y(point.y)}`).join(' ');
      svg.append(svgElement('path', {d: path, fill: 'none', stroke: colors[index], 'stroke-width': 3, 'stroke-linecap': 'round', 'stroke-linejoin': 'round'}));
      ordered.forEach((point) => {
        const circle = svgElement('circle', {cx: x(point.x), cy: y(point.y), r: 4, fill: colors[index]});
        const title = svgElement('title'); title.textContent = `${item.name}: ${formatValue(point.y, chart.format)} at ${formatXAxisValue(point.x)}`; circle.append(title); svg.append(circle);
      });
    });
    host.append(svg);
    const legend = document.createElement('div'); legend.className = 'chart-legend';
    series.forEach((item, index) => { const label = document.createElement('span'); const dot = document.createElement('i'); dot.style.background = colors[index]; label.append(dot, document.createTextNode(item.name.replaceAll('_', ' '))); legend.append(label); });
    host.append(legend);
  };

  const renderOperations = (container, title, rows) => {
    if (!rows?.length) return;
    const section = document.createElement('section'); section.className = 'insight-operations';
    const heading = document.createElement('div'); heading.className = 'insight-section-heading';
    const h3 = document.createElement('h3'); h3.textContent = title;
    const note = document.createElement('small'); note.textContent = 'Purpose and health'; heading.append(h3, note); section.append(heading);
    const tableWrap = document.createElement('div'); tableWrap.className = 'table-wrap kpi-detail-table';
    const table = document.createElement('table'); const head = document.createElement('thead'); const body = document.createElement('tbody');
    const headRow = document.createElement('tr'); ['Service', 'Purpose', 'Calls', 'Avg latency', 'Failures'].forEach((label) => { const th = document.createElement('th'); th.textContent = label; headRow.append(th); }); head.append(headRow);
    rows.slice(0, 8).forEach((item) => { const row = document.createElement('tr'); appendCell(row, item.operation, item.model || item.service); appendCell(row, item.purpose); appendCell(row, String(item.calls)); appendCell(row, formatValue(item.avg_latency, 'milliseconds')); appendCell(row, String(item.failures)); body.append(row); });
    table.append(head, body); tableWrap.append(table); section.append(tableWrap); container.append(section);
  };

  const renderReconciliation = (container, data) => {
    if (!data.reconciliation || !['llm_calls', 'total_tokens', 'average_latency'].includes(data.metric)) return;
    const value = data.reconciliation;
    const section = document.createElement('section'); section.className = 'reconciliation-detail';
    const heading = document.createElement('div'); heading.className = 'insight-section-heading';
    const copy = document.createElement('div'); const title = document.createElement('h3');
    title.textContent = data.metric === 'total_tokens'
      ? 'LangSmith token-consumption reconciliation'
      : data.metric === 'average_latency'
        ? 'LangSmith LLM-latency reconciliation'
        : 'LangSmith LLM-call reconciliation';
    const note = document.createElement('p'); note.textContent = value.message;
    const explanation = document.createElement('p'); explanation.className = 'reconciliation-explanation';
    explanation.textContent = data.metric === 'total_tokens'
      ? 'The full local token history remains visible, while the alignment delta compares only spans that carry both a durable correlation ID and LangSmith token usage. Historical spans without usage are never treated as zero-token matches.'
      : data.metric === 'average_latency'
        ? 'This compares local provider wall-clock latency with the duration of the exact matching LangSmith LLM span. A small positive local delta is expected because local timing includes telemetry and persistence overhead.'
        : value.explanation;
    copy.append(title, note, explanation);
    const badge = document.createElement('span'); badge.className = `status ${value.status === 'healthy' ? 'good' : ['pending', 'neutral'].includes(value.status) ? 'warn' : 'bad'}`; badge.textContent = value.status;
    heading.append(copy, badge); section.append(heading);
    const grid = document.createElement('div'); grid.className = 'reconciliation-detail-grid';
    const cards = data.metric === 'total_tokens' ? [
      {label: 'All local input', value: value.local_history_input_tokens, detail: 'Every locally recorded input token in the selected UTC date scope.'},
      {label: 'Local matched input', value: value.local_input_tokens, detail: 'Input tokens in the correlation-enabled comparison cohort.'},
      {label: 'LangSmith input', value: value.langsmith_input_tokens, detail: 'Prompt tokens reported by the exact matching LangSmith spans.'},
      {label: 'Input-token delta', value: value.input_token_delta, detail: 'Matched local input minus LangSmith input; zero means aligned.'},
      {label: 'All local output', value: value.local_history_output_tokens, detail: 'Every locally recorded output token in the selected UTC date scope.'},
      {label: 'Local matched output', value: value.local_output_tokens, detail: 'Output tokens in the correlation-enabled comparison cohort.'},
      {label: 'LangSmith output', value: value.langsmith_output_tokens, detail: 'Completion tokens reported by the exact matching LangSmith spans.'},
      {label: 'Output-token delta', value: value.output_token_delta, detail: 'Matched local output minus LangSmith output; zero means aligned.'},
    ] : data.metric === 'average_latency' ? [
      {label: 'Comparable LLM spans', value: value.latency_comparable_spans, detail: 'Provider attempts with both local timing and a matching LangSmith duration.'},
      {label: 'Local average', value: value.local_average_latency_ms, format: 'milliseconds', detail: 'Average local wall-clock time for the matched cohort.'},
      {label: 'LangSmith average', value: value.langsmith_average_latency_ms, format: 'milliseconds', detail: 'Average duration of the exact matching LangSmith LLM spans.'},
      {label: 'Average delta', value: value.average_latency_delta_ms, format: 'milliseconds', detail: 'Local average minus LangSmith average; small overhead is expected.'},
      {label: 'Local P95', value: value.local_p95_latency_ms, format: 'milliseconds', detail: 'Local 95th-percentile latency for matched provider attempts.'},
      {label: 'LangSmith P95', value: value.langsmith_p95_latency_ms, format: 'milliseconds', detail: 'LangSmith 95th-percentile duration for matching LLM spans.'},
    ] : [
      {label: 'Provider attempts', value: value.provider_attempts, detail: 'Every local LLM call in this date and learner selection, including demo history.'},
      {label: 'Correlation-enabled', value: value.correlated_attempts, detail: 'Attempts created with durable local and LangSmith run IDs.'},
      {label: 'Matched spans', value: value.matched_spans, detail: 'Correlation-enabled attempts confirmed in LangSmith.'},
      {label: 'Pending ingestion', value: value.pending_attempts, detail: 'Recently exported and still inside the normal ingestion window.'},
      {label: 'Delayed / missing', value: value.delayed_attempts, detail: 'Not found in LangSmith after the allowed export window.'},
      {label: 'Historical backfills', value: value.backfilled_attempts, detail: 'Missing spans restored from durable local provider evidence after connectivity returned.'},
      {label: 'Correlation coverage', value: value.coverage, format: 'percent', detail: 'Matched spans divided by correlation-enabled attempts.'},
    ];
    cards.forEach((item) => {
      const card = document.createElement('article'); const strong = document.createElement('strong'); strong.textContent = formatValue(item.value, item.format || 'integer');
      const label = document.createElement('span'); label.textContent = item.label;
      const detail = document.createElement('small'); detail.textContent = item.detail; card.append(strong, label, detail); grid.append(card);
    });
    section.append(grid);
    const foot = document.createElement('small');
    const dateScope = data.selected_start_date || data.selected_end_date
      ? `${data.selected_start_date || 'beginning'} through ${data.selected_end_date || 'latest'} UTC`
      : data.selected_date ? `${data.selected_date} UTC` : 'all recorded UTC dates';
    foot.textContent = data.metric === 'total_tokens'
      ? `${value.token_comparable_spans || 0} comparable span(s) / ${value.token_uncomparable_attempts || 0} local attempt(s) outside the usage-aware cohort / ${dateScope}`
      : data.metric === 'average_latency'
        ? `${value.latency_comparable_spans || 0} comparable LLM span(s) / ${dateScope} / Project ${value.project}`
        : `Project ${value.project} / filter span name: ${value.span_name} / ${dateScope}`;
    section.append(foot);
    container.append(section);
  };

  const renderGraphTopology = (container, graph) => {
    if (!graph) return;
    const section = document.createElement('section'); section.className = 'graph-insight';
    const heading = document.createElement('div'); heading.className = 'insight-section-heading';
    const h3 = document.createElement('h3'); h3.textContent = 'SmartReco recommendation flow';
    const note = document.createElement('small'); note.textContent = 'LangSmith-traced nodes and edges'; heading.append(h3, note); section.append(heading);
    const flow = document.createElement('div'); flow.className = 'graph-flow';
    graph.nodes.forEach((item, index) => {
      const node = document.createElement('article'); const badge = document.createElement('span'); badge.textContent = String(index + 1).padStart(2, '0');
      const copy = document.createElement('div'); const strong = document.createElement('strong'); strong.textContent = item.label; const small = document.createElement('small'); small.textContent = item.purpose; copy.append(strong, small); node.append(badge, copy); flow.append(node);
      if (index < graph.nodes.length - 1) { const edge = document.createElement('b'); edge.textContent = '→'; edge.title = graph.edges[index]; flow.append(edge); }
    });
    section.append(flow); container.append(section);
  };

  const renderInsights = (data) => {
    const container = dialog.querySelector('[data-kpi-insights]'); container.replaceChildren();
    const charts = document.createElement('div'); charts.className = 'insight-chart-grid';
    (data.insights?.charts || []).forEach((chart, chartIndex) => {
      const card = document.createElement('section'); card.className = 'insight-chart-card';
      const heading = document.createElement('div'); heading.className = 'insight-chart-heading';
      const copy = document.createElement('div'); const title = document.createElement('h3'); title.textContent = chart.title; const subtitle = document.createElement('p'); subtitle.textContent = chart.subtitle; copy.append(title, subtitle); heading.append(copy);
      const plot = document.createElement('div'); plot.className = 'line-chart';
      const selectionKey = `${data.metric}:${chartIndex}:${chart.title}`;
      let selectedSeries = chart.series;
      if (chart.drilldown && Object.keys(chart.drilldown).length) {
        const control = document.createElement('div'); control.className = 'chart-filter-control';
        const select = document.createElement('select'); const all = document.createElement('option'); all.value = ''; all.textContent = 'Daily overview'; select.append(all);
        Object.keys(chart.drilldown).sort().reverse().forEach((date) => { const option = document.createElement('option'); option.value = date; option.textContent = date; select.append(option); });
        const previousSelection = chartSelections.get(selectionKey) || '';
        if ([...select.options].some((option) => option.value === previousSelection)) {
          select.value = previousSelection;
          selectedSeries = previousSelection ? chart.drilldown[previousSelection] : chart.series;
        }
        select.addEventListener('change', () => {
          chartSelections.set(selectionKey, select.value);
          renderSeriesChart(plot, chart, select.value ? chart.drilldown[select.value] : chart.series);
        });
        const hint = document.createElement('small'); hint.textContent = chart.drilldown_hint || 'Select a date to see the detailed hourly view.';
        control.append(select, hint); heading.append(control);
      }
      card.append(heading, plot); charts.append(card); renderSeriesChart(plot, chart, selectedSeries);
    });
    if (charts.children.length) container.append(charts);
    renderReconciliation(container, data);
    renderGraphTopology(container, data.insights?.graph);
    if (['rag_calls', 'mcp_calls', 'average_latency'].includes(data.metric)) renderOperations(container, 'Services and purpose', data.insights?.operations);
    if (data.metric === 'graph_runs') {
      renderOperations(container, 'Workflow types', data.insights?.graph?.flows);
      renderOperations(container, 'Runtime bottlenecks', data.insights?.graph?.bottlenecks);
    }
    if (data.metric === 'failures') {
      renderOperations(container, 'Failure locations', data.insights?.operations);
      renderOperations(container, 'LLM endpoints affected', data.insights?.llm_failures);
    }
  };

  const renderDetail = (data) => {
    activeDetail = data;
    dialog.querySelector('[data-kpi-title]').textContent = data.title;
    dialog.querySelector('[data-kpi-subtitle]').textContent = data.subtitle;
    dialog.querySelector('[data-kpi-grain]').textContent = data.date_grain;
    const health = dialog.querySelector('[data-kpi-health]'); health.className = `kpi-health ${data.health.status}`;
    dialog.querySelector('[data-kpi-health-label]').textContent = data.health.label;
    dialog.querySelector('[data-kpi-health-message]').textContent = data.health.message;
    renderSummary(data); renderInsights(data); renderTrend(data);
    renderTable(
      dialog.querySelector('[data-kpi-date-head]'), dialog.querySelector('[data-kpi-date-body]'),
      data.daily, data.columns, {key: 'date', label: 'UTC date'},
    );
    const usesRange = data.metric === 'llm_calls';
    detailRangeControl.hidden = !usesRange;
    if (usesRange) {
      const previousRange = detailRangeSelections.get(data.metric) || {};
      detailStartDate.value = data.selected_start_date || previousRange.start || selectedPageStartDate;
      detailEndDate.value = data.selected_end_date || previousRange.end || selectedPageEndDate;
      detailRangeSelections.set(data.metric, {start: detailStartDate.value, end: detailEndDate.value});
    }
    renderUserView();
    dialog.querySelector('[data-kpi-updated]').textContent = formatUtcDateTime(data.generated_at, false);
  };

  const loadDetail = async (metric, showLoading = true) => {
    if (!metric || detailPolling) return;
    detailPolling = true;
    if (showLoading) detailLoading.hidden = false;
    try {
      const params = new URLSearchParams({metric});
      if (metric === 'llm_calls') {
        const range = detailRangeSelections.get(metric) || {start: detailStartDate.value, end: detailEndDate.value};
        if (range.start) params.set('start_date', range.start);
        if (range.end) params.set('end_date', range.end);
      }
      const response = await fetch(`/api/admin/observability/details?${params}`, {headers: {'Accept': 'application/json'}, cache: 'no-store'});
      if (!response.ok) throw new Error('KPI details unavailable');
      renderDetail(await response.json());
    } catch {
      dialog.querySelector('[data-kpi-health]').className = 'kpi-health critical';
      dialog.querySelector('[data-kpi-health-label]').textContent = 'Unable to load';
      dialog.querySelector('[data-kpi-health-message]').textContent = 'The detail service will retry while this window remains open.';
    } finally {
      detailLoading.hidden = true; detailPolling = false;
    }
  };

  const refresh = async () => {
    if (polling) return;
    polling = true;
    try {
      const params = new URLSearchParams();
      if (selectedPageStartDate) params.set('start_date', selectedPageStartDate);
      if (selectedPageEndDate) params.set('end_date', selectedPageEndDate);
      const query = params.size ? `?${params}` : '';
      const response = await fetch(`/api/admin/observability${query}`, {headers: {'Accept': 'application/json'}, cache: 'no-store'});
      if (!response.ok) return;
      const data = await response.json(); const metrics = data.metrics;
      ['llm_calls', 'total_tokens', 'rag_calls', 'mcp_calls', 'graph_runs', 'failures'].forEach((name) => setMetric(name, formatValue(metrics[name])));
      setMetric('estimated_cost', `$${Number(metrics.estimated_cost).toFixed(6)}`);
      setMetric('average_latency', formatValue(metrics.average_latency, 'milliseconds'));
      const tokenDetail = root.querySelector('[data-observe-token-detail]');
      if (tokenDetail) tokenDetail.textContent = `${metrics.input_tokens} in · ${metrics.output_tokens} out`;
      const reconciliation = data.reconciliation || {};
      const llmDetail = root.querySelector('[data-observe-llm-detail]');
      if (llmDetail) llmDetail.textContent = `${reconciliation.matched_spans || 0} matched spans / ${(reconciliation.pending_attempts || 0) + (reconciliation.delayed_attempts || 0)} awaiting`;
      const reconciliationStatus = root.querySelector('[data-reconciliation-status]');
      if (reconciliationStatus) { reconciliationStatus.textContent = reconciliation.status || 'not run'; reconciliationStatus.className = `status ${reconciliation.status === 'healthy' ? 'good' : ['pending', 'neutral'].includes(reconciliation.status) ? 'warn' : 'bad'}`; }
      const reconciliationMessage = root.querySelector('[data-reconciliation-message]'); if (reconciliationMessage) reconciliationMessage.textContent = reconciliation.message || '';
      [['attempts', 'provider_attempts'], ['matched', 'matched_spans'], ['pending', 'pending_attempts'], ['delayed', 'delayed_attempts']].forEach(([target, key]) => { const node = root.querySelector(`[data-reconciliation-${target}]`); if (node) node.textContent = String(reconciliation[key] || 0); });
      const reconciliationChecked = root.querySelector('[data-reconciliation-checked]'); if (reconciliationChecked) reconciliationChecked.textContent = reconciliation.last_checked_at ? formatUtcDateTime(reconciliation.last_checked_at) : 'not yet';
      renderChips(root.querySelector('[data-service-counts]'), data.service_counts, 'No calls recorded yet.', true);
      renderChips(root.querySelector('[data-model-counts]'), data.model_counts, 'No model calls recorded yet.');
      renderTimeline(data.items || []);
      if (updated) updated.textContent = formatUtcDateTime(data.refreshed_at, false);
    } catch { /* The next one-second poll retries automatically. */ }
    finally { polling = false; }
  };

  root.querySelectorAll('[data-kpi-detail]').forEach((button) => {
    button.addEventListener('click', () => {
      activeMetric = button.dataset.kpiDetail;
      if (activeMetric === 'llm_calls') {
        const range = detailRangeSelections.get(activeMetric) || {start: selectedPageStartDate, end: selectedPageEndDate};
        detailStartDate.value = range.start || '';
        detailEndDate.value = range.end || '';
        detailRangeSelections.set(activeMetric, {start: detailStartDate.value, end: detailEndDate.value});
      }
      if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
      loadDetail(activeMetric, true);
    });
  });
  dialog?.querySelector('[data-kpi-close]')?.addEventListener('click', () => dialog.close());
  dialog?.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
  dialog?.querySelectorAll('[data-kpi-view]').forEach((button) => {
    button.addEventListener('click', () => {
      const userView = button.dataset.kpiView === 'user';
      dialog.querySelectorAll('[data-kpi-view]').forEach((item) => item.classList.toggle('active', item === button));
      dialog.querySelector('[data-kpi-date-view]').hidden = userView;
      dialog.querySelector('[data-kpi-user-view]').hidden = !userView;
      if (userView) renderUserView();
    });
  });
  [detailStartDate, detailEndDate].forEach((control) => control?.addEventListener('change', () => {
    if (activeMetric !== 'llm_calls') return;
    detailRangeSelections.set(activeMetric, {start: detailStartDate.value, end: detailEndDate.value});
    loadDetail(activeMetric, true);
  }));

  refresh();
  setInterval(refresh, 1000);
  setInterval(() => { if (dialog?.open && activeMetric) loadDetail(activeMetric, false); }, 5000);
  document.addEventListener('visibilitychange', refresh);
})();
