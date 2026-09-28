/** One word of a word-level diff: kept, added, or removed. */
export type Piece = { t: 'same' | 'add' | 'del'; w: string };

/**
 * Word diff by longest common subsequence.
 *
 * Words are compared WITH their punctuation, so "video" becoming "video," shows
 * as a change — a comma is a pause to the voice, and a suggestion that only
 * adds one should be seen to.
 */
export function wordDiff(a: string, b: string): Piece[] {
  const A = a.split(/\s+/).filter(Boolean);
  const B = b.split(/\s+/).filter(Boolean);
  const n = A.length, m = B.length;
  const L = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out: Piece[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { out.push({ t: 'same', w: B[j] }); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) out.push({ t: 'del', w: A[i++] });
    else out.push({ t: 'add', w: B[j++] });
  }
  while (i < n) out.push({ t: 'del', w: A[i++] });
  while (j < m) out.push({ t: 'add', w: B[j++] });
  return out;
}
