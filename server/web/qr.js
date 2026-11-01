'use strict';
/* Минимальный QR-энкодер (ISO/IEC 18004): байтовый режим, версии 1-6, уровень коррекции M.
   Таблицы GF(256) и порождающий многочлен Рида-Соломона считаются программно, а не хранятся как
   магические числа. Портировано построчно с эталонной Python-реализации, которую независимо проверил
   декодированием через OpenCV (cv2.QRCodeDetector) на широком наборе случайных строк — см. историю
   разработки; тут описывать нечего, только код. */
const QR = (() => {
  const EXP = new Array(512).fill(0);
  const LOG = new Array(256).fill(0);
  {
    let x = 1;
    for (let i = 0; i < 255; i++) {
      EXP[i] = x; LOG[x] = i;
      x <<= 1;
      if (x & 0x100) x ^= 0x11D;
    }
    for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
  }
  const gmul = (a, b) => (a === 0 || b === 0) ? 0 : EXP[LOG[a] + LOG[b]];

  function rsGeneratorPoly(nsym) {
    let g = [1];
    for (let i = 0; i < nsym; i++) {
      const g2 = new Array(g.length + 1).fill(0);
      for (let j = 0; j < g.length; j++) {
        g2[j] ^= gmul(g[j], EXP[i]);
        g2[j + 1] ^= g[j];
      }
      g = g2;
    }
    return g.reverse();   // «от старшей степени к младшей» — в этом порядке её использует rsEncode
  }

  function rsEncode(data, nsym) {
    const gen = rsGeneratorPoly(nsym);
    const res = data.concat(new Array(nsym).fill(0));
    for (let i = 0; i < data.length; i++) {
      const coef = res[i];
      if (coef !== 0) {
        for (let j = 0; j < gen.length; j++) res[i + j] ^= gmul(gen[j], coef);
      }
    }
    return res.slice(data.length);
  }

  // версия: [всего кодовых слов, EC-слов на блок, [[число блоков, слов данных на блок], ...]]
  const VERSION_TABLE = {
    1: [26, 10, [[1, 16]]],
    2: [44, 16, [[1, 28]]],
    3: [70, 26, [[1, 44]]],
    4: [100, 18, [[2, 32]]],
    5: [134, 24, [[2, 43]]],
    6: [172, 16, [[4, 27]]],
  };
  const ALIGN_POS = { 1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34] };

  function pickVersion(nbytes) {
    for (const v of [1, 2, 3, 4, 5, 6]) {
      const [, , blocks] = VERSION_TABLE[v];
      const dataCap = blocks.reduce((s, [n, c]) => s + n * c, 0);
      if (4 + 8 + nbytes * 8 <= dataCap * 8) return v;
    }
    throw new Error('Строка слишком длинная для версий 1-6');
  }

  function encodeBits(bytes, version) {
    const [, , blocks] = VERSION_TABLE[version];
    const dataCapBytes = blocks.reduce((s, [n, c]) => s + n * c, 0);
    let bits = '0100' + bytes.length.toString(2).padStart(8, '0');
    for (const b of bytes) bits += b.toString(2).padStart(8, '0');
    const room = dataCapBytes * 8 - bits.length;
    bits += '0000'.slice(0, Math.max(0, Math.min(4, room)));
    while (bits.length % 8) bits += '0';
    const codewords = [];
    for (let i = 0; i < bits.length; i += 8) codewords.push(parseInt(bits.slice(i, i + 8), 2));
    const pad = [0xEC, 0x11];
    let pi = 0;
    while (codewords.length < dataCapBytes) codewords.push(pad[pi++ % 2]);
    return codewords;
  }

  function interleave(codewords, version) {
    const [, ecPerBlock, blocks] = VERSION_TABLE[version];
    const dataBlocks = [], ecBlocks = [];
    let pos = 0;
    for (const [n, c] of blocks) {
      for (let k = 0; k < n; k++) {
        const block = codewords.slice(pos, pos + c); pos += c;
        dataBlocks.push(block);
        ecBlocks.push(rsEncode(block, ecPerBlock));
      }
    }
    const result = [];
    const maxLen = Math.max(...dataBlocks.map((b) => b.length));
    for (let i = 0; i < maxLen; i++) for (const b of dataBlocks) if (i < b.length) result.push(b[i]);
    for (let i = 0; i < ecPerBlock; i++) for (const b of ecBlocks) result.push(b[i]);
    return result;
  }

  const FORMAT_GEN = 0x537, FORMAT_MASK = 0x5412;
  function formatBits(ecLevelBits, maskPattern) {
    const data = (ecLevelBits << 3) | maskPattern;
    let rem = data << 10;
    for (let i = 14; i >= 10; i--) {
      if (rem & (1 << i)) rem ^= FORMAT_GEN << (i - 10);
    }
    const full = ((data << 10) | rem) ^ FORMAT_MASK;
    const out = [];
    for (let i = 14; i >= 0; i--) out.push((full >> i) & 1);
    return out;
  }

  function buildMatrix(version, finalCodewords) {
    const size = 17 + version * 4;
    const m = Array.from({ length: size }, () => new Array(size).fill(null));
    const isFunc = Array.from({ length: size }, () => new Array(size).fill(false));

    function setFinder(r0, c0) {
      for (let dr = -1; dr <= 7; dr++) for (let dc = -1; dc <= 7; dc++) {
        const rr = r0 + dr, cc = c0 + dc;
        if (rr < 0 || rr >= size || cc < 0 || cc >= size) continue;
        isFunc[rr][cc] = true;
        if (dr >= 0 && dr <= 6 && dc >= 0 && dc <= 6) {
          const on = dr === 0 || dr === 6 || dc === 0 || dc === 6 || (dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4);
          m[rr][cc] = on ? 1 : 0;
        } else {
          m[rr][cc] = 0;
        }
      }
    }
    setFinder(0, 0); setFinder(0, size - 7); setFinder(size - 7, 0);

    const aps = ALIGN_POS[version];
    for (const r of aps) for (const c of aps) {
      if ((r < 9 && c < 9) || (r < 9 && c > size - 9) || (r > size - 9 && c < 9)) continue;
      for (let dr = -2; dr <= 2; dr++) for (let dc = -2; dc <= 2; dc++) {
        const on = Math.max(Math.abs(dr), Math.abs(dc)) !== 1;
        m[r + dr][c + dc] = on ? 1 : 0;
        isFunc[r + dr][c + dc] = true;
      }
    }

    for (let i = 8; i < size - 8; i++) {
      m[6][i] = 1 - (i % 2); isFunc[6][i] = true;
      m[i][6] = 1 - (i % 2); isFunc[i][6] = true;
    }
    m[size - 8][8] = 1; isFunc[size - 8][8] = true;

    for (let i = 0; i < 9; i++) {
      if (m[8][i] === null) m[8][i] = 0;
      isFunc[8][i] = true;
      if (m[i][8] === null) m[i][8] = 0;
      isFunc[i][8] = true;
    }
    for (let i = 0; i < 8; i++) {
      if (m[8][size - 1 - i] === null) m[8][size - 1 - i] = 0;
      isFunc[8][size - 1 - i] = true;
      if (m[size - 1 - i][8] === null) m[size - 1 - i][8] = 0;
      isFunc[size - 1 - i][8] = true;
    }

    const bits = [];
    for (const cw of finalCodewords) for (let i = 7; i >= 0; i--) bits.push((cw >> i) & 1);
    while (bits.length < size * size) bits.push(0);
    let bitI = 0, col = size - 1, goingUp = true;
    while (col > 0) {
      if (col === 6) col -= 1;
      const rows = goingUp ? [...Array(size).keys()].reverse() : [...Array(size).keys()];
      for (const row of rows) {
        for (const c of [col, col - 1]) {
          if (m[row][c] === null) m[row][c] = bitI < bits.length ? bits[bitI++] : 0;
        }
      }
      goingUp = !goingUp;
      col -= 2;
    }
    return { m, size, isFunc };
  }

  const MASKS = [
    (r, c) => (r + c) % 2 === 0,
    (r, c) => r % 2 === 0,
    (r, c) => c % 3 === 0,
    (r, c) => (r + c) % 3 === 0,
    (r, c) => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0,
    (r, c) => (r * c) % 2 + (r * c) % 3 === 0,
    (r, c) => ((r * c) % 2 + (r * c) % 3) % 2 === 0,
    (r, c) => ((r + c) % 2 + (r * c) % 3) % 2 === 0,
  ];

  function applyMask(base, size, isFunc, maskFn) {
    return base.map((row, r) => row.map((v, c) => (!isFunc[r][c] && maskFn(r, c)) ? v ^ 1 : v));
  }

  function penalty(m, size) {
    let score = 0;
    for (let r = 0; r < size; r++) {
      let run = 1;
      for (let c = 1; c < size; c++) {
        if (m[r][c] === m[r][c - 1]) run++; else { if (run >= 5) score += run - 2; run = 1; }
      }
      if (run >= 5) score += run - 2;
    }
    for (let c = 0; c < size; c++) {
      let run = 1;
      for (let r = 1; r < size; r++) {
        if (m[r][c] === m[r - 1][c]) run++; else { if (run >= 5) score += run - 2; run = 1; }
      }
      if (run >= 5) score += run - 2;
    }
    for (let r = 0; r < size - 1; r++) for (let c = 0; c < size - 1; c++) {
      const v = m[r][c];
      if (m[r][c + 1] === v && m[r + 1][c] === v && m[r + 1][c + 1] === v) score += 3;
    }
    let dark = 0;
    for (const row of m) for (const v of row) dark += v;
    const pct = Math.floor((dark * 100) / (size * size));
    score += Math.floor(Math.abs(pct - 50) / 5) * 10;
    return score;
  }

  /** Возвращает { matrix, size } — матрицу модулей (1 = тёмный) для текста в байтовом режиме, EC-уровень M. */
  function encode(text) {
    const bytes = Array.from(new TextEncoder().encode(text));
    const version = pickVersion(bytes.length);
    const cw = encodeBits(bytes, version);
    const final = interleave(cw, version);
    const { m: base, size, isFunc } = buildMatrix(version, final);

    let best = null;
    for (let mi = 0; mi < MASKS.length; mi++) {
      const cand = applyMask(base, size, isFunc, MASKS[mi]);
      const p = penalty(cand, size);
      if (best === null || p < best.p) best = { p, mi, cand };
    }
    const { mi: maskI, cand: finalMatrix } = best;

    const fbits = formatBits(0b00, maskI);
    const fposA = [[8, 0], [8, 1], [8, 2], [8, 3], [8, 4], [8, 5], [8, 7], [8, 8], [7, 8], [5, 8], [4, 8], [3, 8], [2, 8], [1, 8], [0, 8]];
    fposA.forEach(([r, c], i) => { finalMatrix[r][c] = fbits[i]; });
    const fposB = [[size - 1, 8], [size - 2, 8], [size - 3, 8], [size - 4, 8], [size - 5, 8], [size - 6, 8], [size - 7, 8],
      [8, size - 8], [8, size - 7], [8, size - 6], [8, size - 5], [8, size - 4], [8, size - 3], [8, size - 2], [8, size - 1]];
    fposB.forEach(([r, c], i) => { finalMatrix[r][c] = fbits[i]; });

    return { matrix: finalMatrix, size };
  }

  return { encode };
})();
if (typeof module !== 'undefined' && module.exports) module.exports = QR;
