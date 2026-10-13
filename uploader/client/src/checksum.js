// Per-block integrity: each block is read into memory once, hashed, and the
// same bytes are sent with a Content-MD5 header. Azure recomputes the hash
// on receipt and rejects the request (400 Md5Mismatch) if the body was
// altered in transit, so the caller can re-send just that block.
//
// MD5 rather than CRC64 because it is a plain, well-specified algorithm
// (Azure's CRC64 is a non-standard variant) and WebCrypto does not offer it.
// This is a transport check, not a security control.

import SparkMD5 from 'spark-md5';

// Reads a Blob slice and returns its bytes with the raw 16-byte MD5 digest
// (the form the SDK's `transactionalContentMD5` option takes).
export async function readBlock(chunk) {
  const data = await chunk.arrayBuffer();
  const binary = SparkMD5.ArrayBuffer.hash(data, true);
  const md5 = Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
  return { data, md5 };
}

// True when Azure rejected a block because its body did not match the hash
// we sent. The SDK surfaces the x-ms-error-code header as `err.code`.
export function isChecksumMismatch(err) {
  return Boolean(err) && err.code === 'Md5Mismatch';
}
