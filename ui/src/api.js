async function request(path, method = 'GET', body) {
  const response = await fetch(`/api/${path}`, {
    method,
    ...(body === undefined ? {} : {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  });

  if (!response.ok) {
    const text = await response.text();
    let detail = text;
    try { detail = JSON.parse(text).detail ?? text; } catch { /* Keep non-JSON errors readable. */ }
    throw new Error(`${method} /api/${path} failed (${response.status}): ${detail}`);
  }

  return response.json();
}

export const getStatus = () => request('status');
export const prove = () => request('prove', 'POST');
export const diagnose = ({ diagram_image = null, ...body } = {}) => request('diagnose', 'POST', { ...body, diagram_image });
export const patch = (body) => request('patch', 'POST', body);
export const reset = () => request('reset', 'POST');
export const updateSource = (source_code) => request('update-source', 'POST', { source_code });
