import { describe, expect, it } from 'vitest';
import { isChecksumMismatch, readBlock } from '../src/checksum.js';

function chunkOf(text) {
  const bytes = new TextEncoder().encode(text);
  return { arrayBuffer: async () => bytes.buffer };
}

function hex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

describe('readBlock', () => {
  it('returns the raw 16-byte MD5 of the bytes (RFC 1321 vectors)', async () => {
    const empty = await readBlock(chunkOf(''));
    expect(empty.md5).toBeInstanceOf(Uint8Array);
    expect(hex(empty.md5)).toBe('d41d8cd98f00b204e9800998ecf8427e');

    const abc = await readBlock(chunkOf('abc'));
    expect(hex(abc.md5)).toBe('900150983cd24fb0d6963f7d28e17f72');
  });

  it('returns the same bytes it hashed', async () => {
    const { data } = await readBlock(chunkOf('abc'));
    expect(data.byteLength).toBe(3);
  });
});

describe('isChecksumMismatch', () => {
  it('recognises only the Md5Mismatch code', () => {
    expect(isChecksumMismatch({ code: 'Md5Mismatch' })).toBe(true);
    expect(isChecksumMismatch({ code: 'InvalidMd5' })).toBe(false);
    expect(isChecksumMismatch({ code: 'AuthorizationFailure' })).toBe(false);
    expect(isChecksumMismatch(null)).toBe(false);
  });
});
