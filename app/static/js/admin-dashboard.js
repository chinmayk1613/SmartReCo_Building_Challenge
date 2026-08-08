(() => {
  const root = document.querySelector('[data-overview-root]');
  const dialog = root?.querySelector('[data-overview-dialog]');
  if (!root || !dialog) return;

  const loading = dialog.querySelector('[data-overview-loading]');
  const summary = dialog.querySelector('[data-overview-summary]');
  const head = dialog.querySelector('[data-overview-head]');
  const body = dialog.querySelector('[data-overview-body]');

  const cell = (tag, value) => {
    const node = document.createElement(tag);
    node.textContent = value ?? '?';
    return node;
  };

  const render = (data) => {
    dialog.querySelector('[data-overview-title]').textContent = data.title;
    dialog.querySelector('[data-overview-subtitle]').textContent = data.subtitle;
    dialog.querySelector('[data-overview-row-count]').textContent = `${data.rows.length} ${data.rows.length === 1 ? 'row' : 'rows'}`;
    dialog.querySelector('[data-overview-updated]').textContent = new Date(data.generated_at).toLocaleString('en-GB', {timeZone: 'UTC', hour12: false});

    summary.replaceChildren();
    data.summary.forEach((item) => {
      const article = document.createElement('article');
      article.append(cell('span', item.label), cell('strong', item.value), cell('small', item.note));
      summary.append(article);
    });

    const headerRow = document.createElement('tr');
    data.columns.forEach((column) => headerRow.append(cell('th', column)));
    head.replaceChildren(headerRow);
    body.replaceChildren();
    if (!data.rows.length) {
      const row = document.createElement('tr');
      const empty = cell('td', data.empty_message);
      empty.colSpan = Math.max(data.columns.length, 1);
      empty.className = 'overview-empty';
      row.append(empty); body.append(row);
      return;
    }
    data.rows.forEach((values) => {
      const row = document.createElement('tr');
      values.forEach((value) => row.append(cell('td', value)));
      body.append(row);
    });
  };

  const load = async (metric) => {
    loading.hidden = false;
    try {
      const response = await fetch(`/api/admin/overview/details?metric=${encodeURIComponent(metric)}`, {headers: {'Accept': 'application/json'}, cache: 'no-store'});
      if (!response.ok) throw new Error('Detail request failed');
      render(await response.json());
    } catch (_) {
      dialog.querySelector('[data-overview-title]').textContent = 'Details temporarily unavailable';
      dialog.querySelector('[data-overview-subtitle]').textContent = 'The operational evidence could not be loaded. Close this window and try again.';
      summary.replaceChildren(); head.replaceChildren(); body.replaceChildren();
    } finally {
      loading.hidden = true;
    }
  };

  root.querySelectorAll('[data-overview-detail]').forEach((button) => button.addEventListener('click', () => {
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
    load(button.dataset.overviewDetail);
  }));
  dialog.querySelector('[data-overview-close]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
})();
