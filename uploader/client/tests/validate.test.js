import { describe, expect, it } from 'vitest';
import {
  ALLOWED_EXTENSIONS,
  ValidationError,
  contentTypeFor,
  extensionOf,
  validateClientPath,
  validateFile,
} from '../src/validate.js';

// Mirrors uploader/api/tests/test_naming.py's TRAVERSAL_CORPUS /
// ACCEPTABLE_PATHS, so the client rejects (or accepts) the same inputs the
// server does.
const TRAVERSAL_CORPUS = [
  '../other@example.com/secret.txt',
  '..',
  '../..',
  'a/../../b.txt',
  'reads/../../escape.fastq',
  './reads.fastq',
  'a/./b.fastq',
  '/etc/passwd',
  '//evil.com/x.txt',
  '/reads.fastq',
  'C:/Windows/system32.txt',
  'c:reads.fastq',
  '..\\..\\escape.txt',
  'reads\\sub\\file.fastq',
  '%2e%2e/escape.txt',
  '..%2fescape.txt',
  '%2e%2e%2fescape.txt',
  '%252e%252e/escape.txt',
  'reads%2f..%2f..%2fescape.txt',
  'reads.fastq\x00.png',
  'reads\n.fastq',
  'reads\r\n.fastq',
  'reads\t.fastq',
  '\x7freads.fastq',
  '',
  '   ',
  '/',
  'reads//file.fastq',
  'reads/',
  'reads /file.fastq',
  'reads/ file.fastq',
  'reads.',
  'dir./file.fastq',
  'reads.fastq:Zone.Identifier',
];

const ACCEPTABLE_PATHS = [
  'reads.fastq',
  'reads.fastq.gz',
  'run7/reads_R1.fastq.gz',
  '2026-09-14/plate1/A01.ab1',
  'a-b_c.1.tsv',
  'Sample (1).csv',
  'naïve-sample.fasta',
];

describe('validateClientPath', () => {
  it.each(TRAVERSAL_CORPUS)('rejects %j', (candidate) => {
    expect(() => validateClientPath(candidate)).toThrow(ValidationError);
  });

  it.each(ACCEPTABLE_PATHS)('accepts %j', (candidate) => {
    expect(validateClientPath(candidate)).toBe(candidate);
  });

  it('rejects too many segments', () => {
    const deep = Array(9).fill('d').join('/') + '.fastq';
    expect(() => validateClientPath(deep)).toThrow(ValidationError);
  });

  it('accepts the maximum segment count', () => {
    const atLimit = Array(7).fill('d').concat('f.fastq').join('/');
    expect(validateClientPath(atLimit)).toBe(atLimit);
  });

  it('rejects an over-long segment', () => {
    expect(() => validateClientPath('a'.repeat(300) + '.fastq'))
      .toThrow(ValidationError);
  });
});

describe('extensionOf / contentTypeFor', () => {
  it('lowercases the extension', () => {
    expect(extensionOf('Reads.FASTQ')).toBe('.fastq');
  });

  it('returns empty string for no extension', () => {
    expect(extensionOf('README')).toBe('');
  });

  it('maps extensions with empty file.type to a content type', () => {
    expect(contentTypeFor('reads.fastq')).toBe('application/octet-stream');
    expect(contentTypeFor('data.csv')).toBe('text/csv');
    expect(contentTypeFor('archive.tar.gz')).toBe('application/gzip');
  });

  it('falls back to application/octet-stream', () => {
    expect(contentTypeFor('unknown.xyz')).toBe('application/octet-stream');
  });
});

function fakeFile(name, size = 100) {
  return { name, size };
}

describe('validateFile', () => {
  it('rejects an empty name', () => {
    expect(() => validateFile(fakeFile(''))).toThrow(ValidationError);
  });

  it('rejects a zero-byte file', () => {
    expect(() => validateFile(fakeFile('reads.fastq', 0))).toThrow(ValidationError);
  });

  it('rejects a disallowed extension', () => {
    expect(() => validateFile(fakeFile('reads.exe'))).toThrow(ValidationError);
  });

  it.each(Array.from(ALLOWED_EXTENSIONS))('accepts extension %s', (ext) => {
    expect(() => validateFile(fakeFile(`sample${ext}`))).not.toThrow();
  });
});
