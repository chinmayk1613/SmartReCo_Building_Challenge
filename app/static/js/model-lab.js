(() => {
  const form = document.querySelector('#model-compare-form');
  const results = document.querySelector('#model-results');
  if (!form || !results) return;

  const message = (text, className = 'muted') => {
    results.replaceChildren();
    const paragraph = document.createElement('p'); paragraph.className = className; paragraph.textContent = text;
    results.append(paragraph);
  };

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    if (!form.querySelector('input[name="selected_models"]:checked')) {
      message('Select at least one model.', 'error'); return;
    }
    button.disabled = true; button.textContent = 'Calling selected models…';
    message('Running grounded generations. This can take up to two minutes…');
    try {
      const response = await fetch('/api/admin/model-compare', {method: 'POST', body: new FormData(form)});
      const data = await response.json();
      if (!response.ok) { message(data.detail || 'Comparison failed.', 'error'); return; }
      results.replaceChildren();
      data.results.forEach((result, index) => {
        const card = document.createElement('article'); card.className = `model-result ${result.status === 'failed' ? 'model-failed' : ''}`;
        const eyebrow = document.createElement('span'); eyebrow.className = 'eyebrow'; eyebrow.textContent = `Candidate ${String.fromCharCode(65 + index)} · ${result.status}`;
        const model = document.createElement('h2'); model.textContent = result.model;
        card.append(eyebrow, model);
        if (result.status === 'failed') {
          const error = document.createElement('p'); error.className = 'error'; error.textContent = result.error; card.append(error);
        } else {
          const headline = document.createElement('h3'); headline.textContent = result.output.headline;
          const narrative = document.createElement('p'); narrative.textContent = result.output.narrative;
          const usage = document.createElement('small'); usage.textContent = `${result.input_tokens} input · ${result.output_tokens} output tokens`;
          card.append(headline, narrative, usage);
        }
        results.append(card);
      });
    } catch {
      message('The comparison could not reach the server. Try again.', 'error');
    } finally {
      button.disabled = false; button.textContent = 'Run selected models';
    }
  });
})();
