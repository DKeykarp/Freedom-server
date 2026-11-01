// qr.js — собственный энкодер QR (байтовый режим, версии 1-6). Настоящая проверка (OpenCV, побитовое
// сравнение с независимо написанным Python-прототипом) делалась при разработке вне тестовой среды проекта;
// здесь — воспроизводимая регрессионная проверка тем же способом: полное чтение обратно (снятие маски,
// разбор формат-инфо, Рид-Соломон), без внешних зависимостей.
import assert from 'node:assert/strict';
import QR from '../../server/web/qr.js';

// ---------- та же арифметика GF(256), что и в qr.js, — для независимого чтения кодовых слов ----------
const EXP = new Array(512).fill(0), LOG = new Array(256).fill(0);
{ let x = 1; for (let i = 0; i < 255; i++) { EXP[i] = x; LOG[x] = i; x <<= 1; if (x & 0x100) x ^= 0x11D; } for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255]; }
const gmul = (a, b) => (a === 0 || b === 0) ? 0 : EXP[LOG[a] + LOG[b]];
function rsGen(nsym) {
  let g = [1];
  for (let i = 0; i < nsym; i++) {
    const g2 = new Array(g.length + 1).fill(0);
    for (let j = 0; j < g.length; j++) { g2[j] ^= gmul(g[j], EXP[i]); g2[j + 1] ^= g[j]; }
    g = g2;
  }
  return g.reverse();
}
function rsSyndromesZero(cw, nsym) {
  // если синдромы все нулевые — блок без ошибок (для теста этого достаточно, мы же не портим данные)
  for (let i = 0; i < nsym; i++) {
    let y = cw[0];
    for (let k = 1; k < cw.length; k++) y = gmul(y, EXP[i]) ^ cw[k];
    if (y !== 0) return false;
  }
  return true;
}

const VERSION_TABLE = {
  1: [26, 10, [[1, 16]]], 2: [44, 16, [[1, 28]]], 3: [70, 26, [[1, 44]]],
  4: [100, 18, [[2, 32]]], 5: [134, 24, [[2, 43]]], 6: [172, 16, [[4, 27]]],
};
const MASKS = [
  (r, c) => (r + c) % 2 === 0, (r, c) => r % 2 === 0, (r, c) => c % 3 === 0, (r, c) => (r + c) % 3 === 0,
  (r, c) => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0, (r, c) => (r * c) % 2 + (r * c) % 3 === 0,
  (r, c) => ((r * c) % 2 + (r * c) % 3) % 2 === 0, (r, c) => ((r + c) % 2 + (r * c) % 3) % 2 === 0,
];

/** Независимое от qr.js чтение: формат-инфо -> снятие маски -> зигзаг -> кодовые слова -> байтовый режим. */
function decode(matrix, size) {
  const isFunc = Array.from({ length: size }, () => new Array(size).fill(false));
  const mark = (r, c) => { isFunc[r][c] = true; };
  for (let dr = -1; dr <= 7; dr++) for (let dc = -1; dc <= 7; dc++) {
    for (const [r0, c0] of [[0, 0], [0, size - 7], [size - 7, 0]]) {
      const rr = r0 + dr, cc = c0 + dc;
      if (rr >= 0 && rr < size && cc >= 0 && cc < size) mark(rr, cc);
    }
  }
  for (let i = 8; i < size - 8; i++) { mark(6, i); mark(i, 6); }
  mark(size - 8, 8);
  for (let i = 0; i < 9; i++) { mark(8, i); mark(i, 8); }
  for (let i = 0; i < 8; i++) { mark(8, size - 1 - i); mark(size - 1 - i, 8); }
  const aps = { 21: [], 25: [6, 18], 29: [6, 22], 33: [6, 26], 37: [6, 30], 41: [6, 34] }[size] || [];
  for (const r of aps) for (const c of aps) {
    if ((r < 9 && c < 9) || (r < 9 && c > size - 9) || (r > size - 9 && c < 9)) continue;
    for (let dr = -2; dr <= 2; dr++) for (let dc = -2; dc <= 2; dc++) mark(r + dr, c + dc);
  }

  const bit = (v) => (v ? 1 : 0);
  const fposA = [[8, 0], [8, 1], [8, 2], [8, 3], [8, 4], [8, 5], [8, 7], [8, 8], [7, 8], [5, 8], [4, 8], [3, 8], [2, 8], [1, 8], [0, 8]];
  const fval = parseInt(fposA.map(([r, c]) => bit(matrix[r][c])).join(''), 2) ^ 0x5412;
  const maskI = (fval >> 10) & 0b111;
  const mfn = MASKS[maskI];

  const bits = [];
  let col = size - 1, goingUp = true;
  while (col > 0) {
    if (col === 6) col -= 1;
    const rows = goingUp ? [...Array(size).keys()].reverse() : [...Array(size).keys()];
    for (const row of rows) for (const c of [col, col - 1]) {
      if (!isFunc[row][c]) bits.push(bit(matrix[row][c]) ^ (mfn(row, c) ? 1 : 0));
    }
    goingUp = !goingUp; col -= 2;
  }
  const cws = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) cws.push(parseInt(bits.slice(i, i + 8).join(''), 2));

  const version = (size - 17) / 4;
  const [, ecPerBlock, blocks] = VERSION_TABLE[version];
  const dataCap = blocks.reduce((s, [n, c]) => s + n * c, 0);
  const dataCws = cws.slice(0, dataCap);
  const ecCws = cws.slice(dataCap, dataCap + ecPerBlock * blocks.reduce((s, [n]) => s + n, 0));
  const rsOk = rsSyndromesZero([...dataCws, ...ecCws.slice(0, ecPerBlock)], ecPerBlock);

  let bitStr = dataCws.map((b) => b.toString(2).padStart(8, '0')).join('');
  const mode = bitStr.slice(0, 4);
  const len = parseInt(bitStr.slice(4, 12), 2);
  const dataBits = bitStr.slice(12, 12 + len * 8);
  const bytes = [];
  for (let i = 0; i < dataBits.length; i += 8) bytes.push(parseInt(dataBits.slice(i, i + 8), 2));
  const text = Buffer.from(bytes).toString('utf8');
  return { text, mode, maskI, rsOk, version };
}

const cases = ['hello', 'frdm:u:testuser', 'frdm:u:' + 'x'.repeat(32), 'a', '12345', 'test_123', 'Freedom', ''];
for (const t of cases) {
  const { matrix, size } = QR.encode(t);
  assert.equal((size - 17) % 4, 0, `размер ${size} не соответствует формуле версии`);
  assert.equal(matrix.length, size);
  assert.ok(matrix.every((row) => row.length === size));
  const d = decode(matrix, size);
  assert.equal(d.mode, '0100', `режим должен быть байтовым (0100), получили ${d.mode}`);
  assert.equal(d.text, t, `не совпал текст для ${JSON.stringify(t)}: получили ${JSON.stringify(d.text)}`);
  assert.ok(d.rsOk, `синдромы Рида-Соломона не нулевые для ${JSON.stringify(t)} — данные повреждены`);
  console.log(`OK — ${JSON.stringify(t)} (версия ${d.version}, маска ${d.maskI}): читается обратно корректно`);
}

// finder-паттерны: угловые 3 позиции должны быть тёмными (это критично для распознавания камерой)
{
  const { matrix, size } = QR.encode('hello');
  assert.equal(matrix[0][0], 1);
  assert.equal(matrix[0][size - 1], 1);
  assert.equal(matrix[size - 1][0], 1);
  assert.equal(matrix[size - 1][size - 1], 0, 'в нижнем правом углу finder-паттерна быть не должно');
  console.log('OK — три finder-паттерна на своих местах, четвёртый угол пуст');
}

// граница версий 1-6: слишком длинная строка должна кидать понятную ошибку, а не портить QR молча
{
  assert.throws(() => QR.encode('x'.repeat(200)), /версий 1-6/);
  console.log('OK — слишком длинный текст даёт понятную ошибку, а не битый QR');
}

console.log('\nВсе проверки qr.js прошли');
