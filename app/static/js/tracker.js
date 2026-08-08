(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
  if (!csrf) return;
  const userNamespace = document.body.dataset.userId;
  if (!userNamespace) return;
  localStorage.removeItem('smartreco.unsent-events.v1');
  localStorage.removeItem('smartreco.session-id');
  const storageKey = `smartreco.unsent-events.v2.${userNamespace}`;
  const sessionKey = `smartreco.session-id.v2.${userNamespace}`;
  const sessionId = localStorage.getItem(sessionKey) || crypto.randomUUID();
  localStorage.setItem(sessionKey, sessionId);
  let queue = [];
  try { queue = JSON.parse(localStorage.getItem(storageKey) || '[]'); } catch { queue = []; }

  const event = (eventType, data = {}) => ({
    event_id: crypto.randomUUID(), event_type: eventType, session_id: sessionId,
    occurred_at: new Date().toISOString(), page_path: location.pathname,
    product_id: data.productId || null, category: data.category || null,
    search_query: data.searchQuery || null, duration_ms: data.durationMs || null,
    recommendation_id: data.recommendationId || null, properties: data.properties || {}
  });

  const enqueue = (eventType, data = {}) => {
    queue.push(event(eventType, data));
    localStorage.setItem(storageKey, JSON.stringify(queue));
    if (queue.length >= 10) flush();
  };

  const flush = async () => {
    if (!queue.length) return null;
    const batch = queue.splice(0, 100);
    localStorage.setItem(storageKey, JSON.stringify(queue));
    try {
      const requestHeaders = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf};
      if (document.querySelector('[data-product-page]')) requestHeaders['X-SmartReco-Context'] = 'course-visit';
      const response = await fetch('/api/events/batch', {
        method: 'POST', headers: requestHeaders,
        body: JSON.stringify({events: batch}), keepalive: true
      });
      if (!response.ok) throw new Error('event batch rejected');
      const result = await response.json();
      window.dispatchEvent(new CustomEvent('smartreco:events-synced', {detail: result}));
      return result;
    } catch {
      queue = batch.concat(queue).slice(-500);
      localStorage.setItem(storageKey, JSON.stringify(queue));
      return null;
    }
  };

  const scrollSignalsToLatest = (list) => { list.scrollTop = list.scrollHeight; };
  const optimisticSignal = (label, topic, reasonText = 'Recording this activity now…', key = '') => {
    const list = document.querySelector('[data-signal-list]');
    if (!list) return;
    list.querySelector('.signal-empty')?.remove();
    if (key) list.querySelector(`.signal-pending[data-signal-key="${key}"]`)?.remove();
    const row = document.createElement('article'); row.className = 'signal-feed-row signal-pending';
    if (key) row.dataset.signalKey = key;
    const action = document.createElement('span'); action.textContent = label;
    const detail = document.createElement('div');
    const title = document.createElement('strong'); title.textContent = topic;
    const reason = document.createElement('small'); reason.textContent = reasonText;
    detail.append(title, reason);
    const time = document.createElement('time'); time.textContent = 'Now';
    row.append(action, detail, time); list.append(row);
    while (list.children.length > 10) list.firstElementChild?.remove();
    scrollSignalsToLatest(list);
  };

  const renderSignals = (signals) => {
    const list = document.querySelector('[data-signal-list]');
    if (!list) return;
    list.replaceChildren();
    if (!signals.length) {
      const empty = document.createElement('p'); empty.className = 'signal-empty';
      empty.textContent = 'Explore or save a course and your latest activity will appear here.';
      list.append(empty); return;
    }
    signals.slice(-10).forEach((signal) => {
      const row = document.createElement('article'); row.className = 'signal-feed-row';
      const action = document.createElement('span'); action.textContent = signal.label;
      const detail = document.createElement('div');
      const title = document.createElement('strong'); title.textContent = signal.product || signal.topic;
      const reason = document.createElement('small'); reason.textContent = signal.reason || 'Recent activity';
      detail.append(title, reason);
      const time = document.createElement('time');
      time.textContent = new Intl.DateTimeFormat('en-IN', {hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC', timeZoneName: 'short'}).format(new Date(signal.observed_at));
      row.append(action, detail, time); list.append(row);
    });
    scrollSignalsToLatest(list);
  };

  const renderRecommendations = (recommendations) => {
    const grid = document.querySelector('[data-course-recommendations]');
    if (!grid) return;
    grid.replaceChildren();
    if (!recommendations.length) {
      const empty = document.createElement('p'); empty.className = 'recommendation-empty';
      empty.textContent = 'Keep exploring. Your next recommendations will appear here as your interests become clearer.';
      grid.append(empty); return;
    }
    recommendations.slice(0, 3).forEach((item, index) => {
      const link = document.createElement('a'); link.className = 'next-course-card';
      link.href = `/products/${encodeURIComponent(item.slug)}`;
      link.dataset.track = 'recommendation_clicked'; link.dataset.productId = item.product_id;
      if (item.recommendation_id) link.dataset.recommendationId = item.recommendation_id;
      const rank = document.createElement('span'); rank.className = 'next-rank'; rank.textContent = `0${index + 1}`;
      const meta = document.createElement('small'); meta.textContent = `${item.category} · ${item.level}`;
      const heading = document.createElement('h3'); heading.textContent = item.title;
      const fit = document.createElement('span'); fit.className = 'fit-badge';
      fit.textContent = `${Math.round((item.confidence_score || 0) * 100)}% fit confidence`;
      const reason = document.createElement('p'); reason.textContent = item.reason.replaceAll('_', ' ');
      const bottom = document.createElement('div');
      const price = document.createElement('strong'); price.textContent = `$${Math.round(item.price)}`;
      const view = document.createElement('b'); view.textContent = 'View course →'; bottom.append(price, view);
      link.append(rank, meta);
      if (item.confidence_score > 0) link.append(fit);
      link.append(heading, reason, bottom);
      link.addEventListener('click', () => enqueue('recommendation_clicked', {productId: item.product_id, recommendationId: item.recommendation_id}));
      grid.append(link);
    });
  };

  const nodeCopy = {
    queued: 'Preparing the recommendation workflow',
    graph_started: 'Starting the recommendation workflow',
    load_context: 'Reading your behavioral profile and this course',
    retrieve_and_rank: 'RAG is retrieving and behaviorally ranking relevant courses',
    verify_with_mcp: 'MCP is verifying the selected courses against the live catalog',
    generate_copy: 'The Mesh AI model is writing your grounded recommendation',
    generate_copy_fallback: 'Completing safe grounded recommendation copy',
    validate_output: 'Checking that the AI used only verified courses',
    safe_fallback: 'Recovering with verified recommendation copy',
    persist_recommendation: 'Saving your recommendation and course links'
  };

  const formatLocalDateTime = (value) => {
    if (!value) return 'in UTC';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'in UTC';
    return `${new Intl.DateTimeFormat('en-IN', {dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC'}).format(date)} UTC`;
  };

  const renderLifecycle = (lifecycle) => {
    const root = document.querySelector('[data-contextual-lifecycle]');
    if (!root || !lifecycle) return;
    const generating = root.querySelector('[data-recommendation-generating]');
    const current = root.querySelector('[data-recommendation-current]');
    const failed = root.querySelector('[data-recommendation-failed]');
    const progress = root.querySelector('[data-recommendation-progress]');
    root.dataset.state = lifecycle.state || 'empty';
    generating.hidden = lifecycle.state !== 'generating';
    current.hidden = lifecycle.state !== 'current';
    failed.hidden = lifecycle.state !== 'failed';
    if (progress) progress.textContent = nodeCopy[lifecycle.current_node] || 'Reading your latest course activity';
    const time = root.querySelector('[data-generated-at]');
    if (time && lifecycle.generated_at) {
      time.dateTime = lifecycle.generated_at;
      time.textContent = formatLocalDateTime(lifecycle.generated_at);
    }
    const headline = document.querySelector('[data-contextual-headline]');
    const narrative = document.querySelector('[data-contextual-narrative]');
    if (lifecycle.headline && headline) headline.textContent = lifecycle.headline;
    if (lifecycle.narrative && narrative) narrative.textContent = lifecycle.narrative;
  };

  const refreshPersonalization = async () => {
    const page = document.querySelector('[data-product-page]');
    if (!page) return;
    try {
      const response = await fetch(`/api/personalization/current?current_product_id=${encodeURIComponent(page.dataset.productId)}`, {headers: {'Accept': 'application/json'}, cache: 'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      renderSignals(data.signals || []);
      renderLifecycle(data.lifecycle);
      renderRecommendations(data.recommendations || []);
    } catch { /* A scheduled retry follows. */ }
  };
  const refreshPersonalizationSoon = () => [250, 650, 1400].forEach(delay => setTimeout(refreshPersonalization, delay));
  const setCartCount = (count) => {
    document.querySelectorAll('[data-cart-count]').forEach((node) => { node.textContent = String(Math.max(0, count)); });
  };
  const currentCartCount = () => Number(document.querySelector('[data-cart-count]')?.textContent || 0);

  window.SmartReco = {track: enqueue, flush};
  document.querySelector('form[action="/logout"]')?.addEventListener('submit', () => {
    localStorage.removeItem(storageKey);
    localStorage.removeItem(sessionKey);
  });
  document.querySelectorAll('[data-track]').forEach((element) => {
    element.addEventListener('click', async () => {
      enqueue(element.dataset.track, {productId: element.dataset.productId, category: element.dataset.category, recommendationId: element.dataset.recommendationId});
      if (element.dataset.track === 'added_to_cart' && element.tagName === 'BUTTON') {
        element.textContent = 'Added to cart'; element.disabled = true; setCartCount(currentCartCount() + 1);
        document.querySelector('[data-show-cart]')?.removeAttribute('hidden');
        optimisticSignal('Saved course', document.querySelector('[data-product-page] h1')?.textContent || 'Course', 'Saved as strong purchase intent.', 'saved-course');
        await flush(); refreshPersonalizationSoon();
      }
      if (element.dataset.track === 'removed_from_cart') {
        const item = element.closest('[data-cart-item]'); element.disabled = true; element.textContent = 'Removing…';
        await flush(); item?.remove(); setCartCount(currentCartCount() - 1);
        const total = document.querySelector('[data-cart-total]'); if (total) total.textContent = String(document.querySelectorAll('[data-cart-item]').length);
        if (!document.querySelector('[data-cart-item]')) setTimeout(() => location.reload(), 250);
      }
      if (element.dataset.track === 'recommendation_dismissed') {
        element.closest('[data-recommendation-card]')?.remove();
        await flush(); setTimeout(() => location.reload(), 900);
      }
    });
  });

  document.querySelector('[data-search-form]')?.addEventListener('submit', (submitEvent) => {
    const query = new FormData(submitEvent.currentTarget).get('q');
    if (query) enqueue('search_submitted', {searchQuery: String(query)});
  });
  document.querySelectorAll('.course-card').forEach((card) => {
    const observer = new IntersectionObserver((entries) => {
      if (entries.some(entry => entry.isIntersecting && entry.intersectionRatio >= .5)) {
        enqueue('product_impression', {productId: card.dataset.productId}); observer.disconnect();
      }
    }, {threshold: .5});
    observer.observe(card);
  });

  const productPage = document.querySelector('[data-product-page]');
  if (productPage) {
    const courseTitle = productPage.querySelector('h1')?.textContent || 'Course';
    enqueue('product_viewed', {productId: productPage.dataset.productId, category: productPage.dataset.category, properties: {page_visit_id: productPage.dataset.visitId}});
    optimisticSignal('Viewed course', courseTitle, 'Course detail opened and recorded.', 'course-view');
    flush().then(() => {
      refreshPersonalizationSoon();
    });
    const lifecycleTimer = setInterval(refreshPersonalization, 1000);

    let activeMs = 0; let last = Date.now(); let lastReportedMs = 0;
    const reportDwell = () => {
      if (activeMs < 15000 || activeMs - lastReportedMs < 1000) return;
      lastReportedMs = activeMs;
      const seconds = Math.round(activeMs / 1000);
      enqueue('active_dwell', {
        productId: productPage.dataset.productId, category: productPage.dataset.category,
        durationMs: activeMs, properties: {checkpoint: true}
      });
      optimisticSignal('Course dwell time', courseTitle, `${seconds} seconds of active viewing recorded.`, 'course-dwell');
      flush().then(refreshPersonalizationSoon);
    };
    const tick = () => {
      const now = Date.now();
      if (!document.hidden) activeMs += now - last;
      last = now;
      if (activeMs >= 15000 && activeMs - lastReportedMs >= 15000) reportDwell();
    };
    const timer = setInterval(tick, 1000);
    addEventListener('pagehide', () => { tick(); clearInterval(timer); clearInterval(lifecycleTimer); reportDwell(); }, {once: true});
  }

  const cartPage = document.querySelector('[data-cart-page]');
  if (cartPage) {
    document.querySelectorAll('[data-cart-item]').forEach((item) => {
      enqueue('cart_viewed', {productId: item.dataset.productId, category: item.dataset.category});
    });
    flush();
  }

  const recommendation = document.querySelector('[data-recommendation-id]');
  if (recommendation) enqueue('recommendation_impression', {recommendationId: recommendation.dataset.recommendationId});
  setInterval(flush, 1500);
  addEventListener('online', flush);
  addEventListener('smartreco:events-synced', () => {
    refreshPersonalizationSoon();
  });
  document.querySelectorAll('[data-generated-at]').forEach((time) => {
    if (time.dateTime) time.textContent = formatLocalDateTime(time.dateTime);
  });
})();
