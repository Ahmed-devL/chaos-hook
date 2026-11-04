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

// Require matching evidence before publishing patch success in the dashboard.
export function isVerifiedPatchResponse(result) {
  const admission = result?.authentication_admission;
  const expectedAuthIds = [1, 2, 3, 4].flatMap((n) =>
    ['missing', 'malformed', 'wrong_key', 'modified_body', 'expired', 'valid'].map((name) => `${n}:${name}`));
  return result?.status === 'ok' && result.verified === true &&
    typeof result.handler_status?.active_code === 'string' && result.handler_status.active_code.length > 0 &&
    /^[a-f0-9]{64}$/i.test(result.verification_evidence?.candidate_sha256 ?? '') &&
    admission?.scope === 'stripe-authentication-admission-v1' && admission.passed === true &&
    admission.candidate_sha256 === result.verification_evidence.candidate_sha256 &&
    admission.configured_check_count === 24 && admission.executed_check_count === 24 &&
    admission.passed_check_count === 24 && Array.isArray(admission.checks) && admission.checks.length === 24 &&
    admission.checks.every((check, index) => check?.id === expectedAuthIds[index] && check.passed === true) &&
    result.prover_report?.passed_cells === 27 && result.prover_report?.total_cells === 27 &&
    Array.isArray(result.prover_report?.cells) && result.prover_report.cells.length === 27 &&
    result.prover_report.cells.every((cell, index) => cell?.id === index + 1 && cell.passed === true);
}
