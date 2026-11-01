'use strict';
/* Блок «Приложение для компьютера» на странице входа: ссылки на установщики с сервера (/api/downloads).
   Логика (определение ОС, порядок, форматирование) вынесена в чистые функции для тестов. */
const Downloads = (() => {
  const INFO = {
    exe: { icon: '🪟', title: 'Windows', sub: 'Установщик .exe · Windows 10/11, 64 бита' },
    deb: { icon: '🐧', title: 'Linux: Debian, Ubuntu, Mint', sub: 'Пакет .deb · 64 бита' },
    rpm: { icon: '🐧', title: 'Linux: Fedora, RHEL, openSUSE', sub: 'Пакет .rpm · 64 бита' },
    apk: { icon: '🤖', title: 'Android', sub: 'Установщик .apk · Android 8.0 и новее' },
  };

  /** 'windows' | 'linux' | 'mac' | 'mobile' | 'other' (Android содержит слово Linux, поэтому проверяется первым). */
  function detectPlatform(nav) {
    const s = `${(nav.userAgentData && nav.userAgentData.platform) || ''} ${nav.platform || ''} ${nav.userAgent || ''}`;
    if (/android|iphone|ipad|ipod|mobile/i.test(s)) return 'mobile';
    if (/mac|darwin/i.test(s)) return 'mac';
    if (/windows|win32|win64/i.test(s)) return 'windows';
    if (/linux|x11|ubuntu|debian|fedora/i.test(s)) return 'linux';
    return 'other';
  }

  const isDesktopApp = (nav) => /FreedomDesktop\//.test(nav.userAgent || '');

  function formatSize(n) {
    if (!(n >= 0)) return '';
    if (n < 1048576) return Math.max(1, Math.round(n / 1024)) + '\u00a0КБ';
    const mb = n / 1048576;
    return (mb < 10 ? mb.toFixed(1) : String(Math.round(mb))) + '\u00a0МБ';
  }

  /** Установщики нашей платформы идут первыми, порядок остальных сохраняется. */
  function orderItems(items, platform) {
    const mine = items.filter((i) => i.platform === platform);
    return mine.concat(items.filter((i) => i.platform !== platform));
  }

  const installHint = (i) => ({
    exe: 'Запустите скачанный файл и следуйте мастеру установки. Windows может показать предупреждение SmartScreen: установщик пока не подписан сертификатом издателя.',
    deb: `sudo apt install ./${i.name}`,
    rpm: `sudo dnf install ./${i.name}     # openSUSE: sudo zypper install ./${i.name}`,
    apk: 'Откройте скачанный файл на телефоне. Android попросит разрешить установку из этого источника (браузера или «Файлов») — разрешите; приложение пока не из магазина, поэтому проверьте SHA-256 ниже.',
  })[i.kind];

  function render(doc, list, items, platform) {
    const el = (tag, cls, text) => {
      const e = doc.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    };
    list.textContent = '';
    if (!items.length) {
      list.append(el('p', 'muted', 'Установочные файлы ещё не загружены на сервер.'));
      return;
    }
    for (const i of orderItems(items, platform)) {
      const info = INFO[i.kind];
      if (!info) continue;
      const item = el('div', 'dlItem' + (i.platform === platform ? ' dlMine' : ''));
      const row = el('div', 'dlRow');
      const meta = [info.sub, formatSize(i.size), i.version ? 'v' + i.version : ''].filter(Boolean).join(' · ');
      const text = el('div', 'dlInfo');
      text.append(el('b', '', info.title), el('small', 'muted', meta));
      const btn = el('a', 'dlBtn', 'Скачать');
      btn.setAttribute('href', i.url);
      btn.setAttribute('download', i.name);
      row.append(el('div', 'dlIcon', info.icon), text, btn);
      const det = el('details');
      det.append(el('summary', '', 'Как установить и проверить'));
      det.append(el('code', '', installHint(i)));
      det.append(el('div', 'muted', 'SHA-256:'), el('code', '', i.sha256));
      item.append(row, det);
      list.append(item);
    }
  }

  async function init() {
    const root = document.getElementById('dl'), list = document.getElementById('dlList');
    if (!root || !list || isDesktopApp(navigator)) return;     // в самом приложении блок не нужен
    try {
      const r = await fetch('/api/downloads', { cache: 'no-cache' });
      if (!r.ok) return;
      const p = detectPlatform(navigator);
      render(document, list, await r.json(), p === 'mobile' && /android/i.test(navigator.userAgent || '') ? 'android' : p);
      root.classList.remove('hidden');
    } catch { /* сервер старой версии или нет связи: блок остаётся скрытым */ }
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
  }
  return { detectPlatform, isDesktopApp, formatSize, orderItems, installHint, render, INFO };
})();
