// Client-side mirror of uploader/api/naming.py and the allowlists in
// uploader/api/config.py. Courtesy only — the server re-checks everything
// here and its answer is the one that counts (see spec/01-api.md §3).

export const MAX_PATH_SEGMENTS = 8;
export const MAX_SEGMENT_LENGTH = 255;

const RX_CONTROL_CHARS = /[\x00-\x1f\x7f]/;
const RX_WINDOWS_DRIVE = /^[A-Za-z]:/;

const RESERVED_SEGMENTS = new Set(['', '.', '..']);

// Source of truth: uploader/api/config.py DEFAULT_ALLOWED_EXTENSIONS /
// DEFAULT_ALLOWED_CONTENT_TYPES. Kept in one module, with this comment,
// so the two lists are easy to find and reconcile when the server changes.
export const ALLOWED_EXTENSIONS = new Set([
  '.ab1',
  '.bam',
  '.bz2',
  '.csv',
  '.fa',
  '.fasta',
  '.fastq',
  '.fq',
  '.gz',
  '.json',
  '.sam',
  '.tar',
  '.tsv',
  '.txt',
  '.xlsx',
  '.zip',
]);

// Maps extension → content type for files where `file.type` is empty in
// most browsers (.fastq, .ab1, .fq, ...). Falls back to
// application/octet-stream, which is on the server allowlist.
export const EXTENSION_CONTENT_TYPES = {
  '.ab1': 'application/octet-stream',
  '.bam': 'application/octet-stream',
  '.bz2': 'application/octet-stream',
  '.csv': 'text/csv',
  '.fa': 'text/plain',
  '.fasta': 'text/plain',
  '.fastq': 'application/octet-stream',
  '.fq': 'application/octet-stream',
  '.gz': 'application/gzip',
  '.json': 'application/json',
  '.sam': 'application/octet-stream',
  '.tar': 'application/octet-stream',
  '.tsv': 'text/tab-separated-values',
  '.txt': 'text/plain',
  '.xlsx': 'application/octet-stream',
  '.zip': 'application/zip',
};

export const ACCEPT_ATTRIBUTE = Array.from(ALLOWED_EXTENSIONS).join(',');

export class ValidationError extends Error {}

export function extensionOf(filename) {
  const dot = filename.lastIndexOf('.');
  if (dot < 0) return '';
  return filename.slice(dot).toLowerCase();
}

export function contentTypeFor(filename) {
  const extension = extensionOf(filename);
  return EXTENSION_CONTENT_TYPES[extension] || 'application/octet-stream';
}

// Mirrors naming.validate_client_path(). Returns the path unchanged, or
// throws ValidationError naming the failing check.
export function validateClientPath(path) {
  if (typeof path !== 'string') {
    throw new ValidationError('Path must be a string');
  }

  const trimmed = path.trim();

  if (!trimmed) {
    throw new ValidationError('Path is empty');
  }

  if (RX_CONTROL_CHARS.test(trimmed)) {
    throw new ValidationError('Path contains a control character or NUL byte');
  }

  if (trimmed.includes('\\')) {
    throw new ValidationError('Path contains a backslash');
  }

  if (trimmed.includes('%')) {
    throw new ValidationError(
      'Path contains a percent sign; URL-encoded paths are not accepted');
  }

  if (trimmed.startsWith('/')) {
    throw new ValidationError('Path is absolute (leading \'/\')');
  }

  if (trimmed.endsWith('/')) {
    throw new ValidationError('Path names a directory, not a file');
  }

  if (RX_WINDOWS_DRIVE.test(trimmed)) {
    throw new ValidationError('Path looks absolute (drive letter)');
  }

  if (trimmed.includes(':')) {
    throw new ValidationError('Path contains a colon');
  }

  const segments = trimmed.split('/');

  if (segments.length > MAX_PATH_SEGMENTS) {
    throw new ValidationError(
      `Path has ${segments.length} segments; the maximum is `
      + `${MAX_PATH_SEGMENTS}`);
  }

  for (const segment of segments) {
    if (RESERVED_SEGMENTS.has(segment)) {
      throw new ValidationError(`Path contains a reserved segment: '${segment}'`);
    }

    if (segment.length > MAX_SEGMENT_LENGTH) {
      throw new ValidationError(
        `Path segment exceeds ${MAX_SEGMENT_LENGTH} characters`);
    }

    if (segment !== segment.trim()) {
      throw new ValidationError(
        'Path segment has leading or trailing whitespace');
    }

    if (segment.endsWith('.')) {
      throw new ValidationError('Path segment ends with a dot');
    }
  }

  return trimmed;
}

// Full pre-flight check for one File object. Throws ValidationError on the
// first failing check; returns nothing on success.
export function validateFile(file) {
  if (!file || !file.name) {
    throw new ValidationError('File has no name');
  }

  validateClientPath(file.name);

  if (!file.size || file.size <= 0) {
    throw new ValidationError('File is empty');
  }

  const extension = extensionOf(file.name);
  if (!ALLOWED_EXTENSIONS.has(extension)) {
    throw new ValidationError(
      `File extension ${extension || '(none)'} is not in the accepted list`);
  }
}
