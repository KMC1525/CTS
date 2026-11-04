// Drag & drop / picker handling: turns dropped folders and ZIPs into uploads.
import { api, putFile } from './api.js';

const SUPPORTED = /\.(jpe?g|png|webp|gif|bmp|tiff?|zip|cbz)$/i;
const ARCHIVE = /\.(zip|cbz)$/i;

// ---- reading dropped items (File System Entries API) -------------------------
function readAllEntries(reader) {
  return new Promise((resolve, reject) => {
    const all = [];
    const next = () => reader.readEntries((batch) => {
      if (!batch.length) return resolve(all);
      all.push(...batch);
      next();
    }, reject);
    next();
  });
}

async function walk(entry, prefix) {
  if (entry.isFile) {
    const file = await new Promise((res, rej) => entry.file(res, rej));
    return [{ file, path: `${prefix}${entry.name}` }];
  }
  const children = await readAllEntries(entry.createReader());
  const nested = await Promise.all(children.map((child) => walk(child, `${prefix}${entry.name}/`)));
  return nested.flat();
}

/** Returns groups [{label, files:[{file, path}]}] - one group becomes one upload (one or more volumes). */
export async function groupsFromDrop(dataTransfer) {
  const items = [...(dataTransfer.items || [])].filter((i) => i.kind === 'file');
  const entries = items.map((i) => i.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length) return groupsFromFiles([...dataTransfer.files]);

  const groups = [];
  const loose = [];
  for (const entry of entries) {
    if (entry.isDirectory) {
      groups.push({ label: entry.name, files: await walk(entry, '') });
    } else {
      const [item] = await walk(entry, '');
      if (ARCHIVE.test(item.path)) groups.push({ label: item.file.name, files: [item] });
      else loose.push(item);
    }
  }
  if (loose.length) groups.push({ label: 'Loose pages', files: loose });
  return groups;
}

/** From a plain FileList: ZIPs one by one, folder picks grouped by top-level folder. */
export function groupsFromFiles(files) {
  const groups = [];
  const folders = new Map();
  const loose = [];
  for (const file of files) {
    const rel = file.webkitRelativePath;
    if (rel) {
      const top = rel.split('/')[0];
      if (!folders.has(top)) folders.set(top, []);
      folders.get(top).push({ file, path: rel });
    } else if (ARCHIVE.test(file.name)) {
      groups.push({ label: file.name, files: [{ file, path: file.name }] });
    } else {
      loose.push({ file, path: file.name });
    }
  }
  for (const [label, list] of folders) groups.push({ label, files: list });
  if (loose.length) groups.push({ label: 'Loose pages', files: loose });
  return groups;
}

// ---- uploading ---------------------------------------------------------------
/**
 * Uploads one group and returns the volumes the server found in it.
 * onProgress(fraction, text) is called as bytes go up.
 */
export async function uploadGroup(group, onProgress) {
  const files = group.files.filter((f) => SUPPORTED.test(f.path));
  if (!files.length) throw new Error(`No comic pages (jpg, png, webp...) or ZIP files found in "${group.label}".`);
  const total = files.reduce((sum, f) => sum + f.file.size, 0) || 1;
  const { id } = await api.post('/api/volumes/attachments', { label: group.label });

  let finished = 0;
  const inFlight = new Map();
  const report = () => {
    const sent = finished + [...inFlight.values()].reduce((a, b) => a + b, 0);
    onProgress?.(Math.min(1, sent / total), `Uploading ${Math.round((sent / total) * 100)}%`);
  };
  const queue = [...files];
  const worker = async () => {
    while (queue.length) {
      const item = queue.shift();
      const key = item;
      inFlight.set(key, 0);
      await putFile(`/api/volumes/attachments/${id}/file?path=${encodeURIComponent(item.path)}`, item.file, (bytes) => {
        inFlight.set(key, bytes);
        report();
      });
      inFlight.delete(key);
      finished += item.file.size;
      report();
    }
  };
  await Promise.all(Array.from({ length: Math.min(3, files.length) }, worker));
  onProgress?.(1, 'Reading pages...');
  const result = await api.post(`/api/volumes/attachments/${id}/finalize`);
  return result.volumes;
}
