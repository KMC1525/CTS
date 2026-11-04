export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function request(method, url, body) {
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, init);
  } catch (err) {
    throw new ApiError('Cannot reach CTS. Is it still running?', 0);
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join('; ') : data.detail || detail;
    } catch { /* not JSON */ }
    throw new ApiError(detail, res.status);
  }
  return res.status === 204 ? null : res.json();
}

export const api = {
  get: (url) => request('GET', url),
  post: (url, body) => request('POST', url, body ?? {}),
  put: (url, body) => request('PUT', url, body ?? {}),
  del: (url) => request('DELETE', url),
};

/** PUT a File as a raw body with byte-level progress (fetch cannot report upload progress). */
export function putFile(url, file, onBytes) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onBytes?.(e.loaded); };
    xhr.onload = () => (xhr.status < 300 ? resolve() : reject(new ApiError(`Upload failed (${xhr.status})`, xhr.status)));
    xhr.onerror = () => reject(new ApiError('Upload failed - is CTS still running?', 0));
    xhr.send(file);
  });
}
