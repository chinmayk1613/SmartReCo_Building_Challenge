(() => {
  const stream = document.querySelector('#activity-stream');
  if (!stream) return;
  let after = Number(stream.dataset.afterId || 0);
  const scrollToLatest = () => { stream.scrollTop = stream.scrollHeight; };
  scrollToLatest();

  const poll = async () => {
    try {
      const response = await fetch(`/api/admin/activity?after_id=${after}`, {headers: {'Accept': 'application/json'}, cache: 'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      if (data.items.length) stream.querySelector('.muted')?.remove();
      data.items.forEach(item => {
        const row = document.createElement('article'); row.className = 'stream-row new';
        const time = document.createElement('time'); time.textContent = new Intl.DateTimeFormat('en-IN', {
          hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
          timeZone: 'UTC', timeZoneName: 'short',
        }).format(new Date(item.time));
        const body = document.createElement('div');
        const label = document.createElement('strong'); label.textContent = item.event_label;
        const user = document.createElement('small'); user.textContent = `${item.user_name} (${item.user_id.slice(0, 8)})`;
        const detail = document.createElement('span'); detail.textContent = item.detail;
        body.append(label, user); row.append(time, body, detail); stream.append(row);
      });
      while (stream.children.length > 100) stream.firstElementChild?.remove();
      after = data.next_after_id;
      if (data.items.length) scrollToLatest();
    } catch { /* The next sub-second poll retries automatically. */ }
    finally { setTimeout(poll, 600); }
  };
  poll();
})();
