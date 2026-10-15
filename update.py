#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
تحديث يومي لرزنامة السوق الكويتي.

يقرأ المصادر الثلاثة العامة، يعيد بناء البيانات المضمّنة داخل index.html،
ولا يكتب أي شيء ما لم تنجح كل عمليات التحقق — فلو فشل التشغيل تبقى الصفحة
على آخر نسخة سليمة.

Boursa: نقرأ صفحة الرزنامة المعروضة كما يراها الزائر (/ar/corporate-event-calendar)،
ولا نستدعي أي مسار يمنعه robots.txt.
"""
import json, os, re, sys, datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

ROOT = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(ROOT, 'index.html')
TICKERS = os.path.join(ROOT, 'tickers.json')

BOURSA = 'https://www.boursakuwait.com.kw/ar/corporate-event-calendar'
MAQASA = 'https://www.maqasa.com/ar/dividend-distribution/'
CMA_R = 'https://www.cma.gov.kw/ar/web/cma/cma-board-releases/resolutions-and-regulations'
CMA_C = 'https://www.cma.gov.kw/ar/web/cma/cma-board-releases/announcements-and-circulars'

MONTHS_AR = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
             'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']

# يُعرض هذا النوع بالإنجليزية في مصدر البورصة
TYPE_LABEL = {'Market Maker Announcement': 'إعلان صانع سوق'}

FAM_RULES = [
    (('توزيع', 'الاستحقاق', 'حيازة السهم', 'دون الاستحقاق'), 'dist'),
    (('جمعية', 'الجمعية', 'حملة وحدات'), 'agm'),
    (('مجلس الإدارة', 'مجلس الادارة', 'الهيئة الادارية'), 'board'),
    (('العطل',), 'holiday'),
]


def log(*a):
    print(*a, flush=True)


def guess_fam(title):
    for keys, fam in FAM_RULES:
        if any(k in title for k in keys):
            return fam
    return 'market'


# ---------- JavaScript المُتحقَّق منه داخل صفحة البورصة ----------
JS_SETUP = r"""
() => {
  const AR='٠١٢٣٤٥٦٧٨٩', FA='۰۱۲۳۴۵۶۷۸۹';
  const norm = s => (s||'').replace(/[٠-٩]/g, c => AR.indexOf(c)).replace(/[۰-۹]/g, c => FA.indexOf(c));
  const M=['يناير','فبراير','مارس','أبريل','مايو','يونيو','يوليو','أغسطس','سبتمبر','أكتوبر','نوفمبر','ديسمبر'];
  window.__label = () => document.querySelector('span.rbc-toolbar-label').textContent.trim();
  window.__nav = i => {
    const g = document.querySelectorAll('.rbc-toolbar .rbc-btn-group')[0];
    g.querySelectorAll('button')[i].click();   // 0 = اليوم, 1 = السابق, 2 = التالي
  };
  window.__extract = () => {
    const label = window.__label();
    const parts = label.split(/\s+/);
    const mon = M.indexOf(parts[0]) + 1;
    const yr = parseInt(norm(parts[1]), 10);
    const out = [];
    for (const row of document.querySelectorAll('.rbc-month-row')) {
      const cells = [...row.querySelectorAll('.rbc-date-cell')].map(c => {
        const r = c.getBoundingClientRect();
        return { day: parseInt(norm((c.innerText||'').trim()), 10),
                 off: c.classList.contains('rbc-off-range'), x0: r.left, x1: r.right };
      });
      for (const a of row.querySelectorAll('a[href*="news/view"]')) {
        const r = a.getBoundingClientRect(), cx = (r.left + r.right) / 2;
        const cell = cells.find(c => cx >= c.x0 && cx <= c.x1);
        if (!cell || cell.off || !cell.day) continue;
        out.push({ d: `${yr}${String(mon).padStart(2,'0')}${String(cell.day).padStart(2,'0')}`,
                   id: (a.getAttribute('href')||'').split('#')[1] || '',
                   t: (a.innerText||'').replace(/\s+/g,' ').trim() });
      }
    }
    return { label, mon, yr, rows: out };
  };
  window.__companies = () => {
    const s = document.querySelectorAll('select')[0];
    if (!s) return {};
    const m = {};
    for (const o of s.options) if (o.value !== 'ALL') m[o.value] = o.text.trim().replace(/^\d+\s*:\s*/, '');
    return m;
  };
}
"""

JS_MAQASA = r"""
() => {
  const HDR = 'توزيع النقدي نقدي ٪ منحة الاسهم أسهم ٪ ملاحظات';
  const txt = document.body.innerText.replace(/[ \t]+/g, ' ');
  const parts = txt.split(HDR);
  const out = [];
  for (let i = 1; i < parts.length; i++) {
    const prevLines = parts[i-1].trim().split('\n').filter(s => s.trim());
    const name = prevLines.length ? prevLines[prevLines.length-1].trim() : '';
    let year = '';
    for (const ln of parts[i].split('\n').map(s => s.trim()).filter(Boolean)) {
      if (/^\d{4}$/.test(ln)) { year = ln; continue; }
      const m = ln.match(/^(\d{2}\/\d{2}\/\d{4}|-)\s+([\d.]+%|-)\s+(\d{2}\/\d{2}\/\d{4}|-)\s+([\d.]+%|-)/);
      if (m) out.push([name, year, m[1], m[2], m[3], m[4]]);
      else if (/^[؀-ۿ]/.test(ln) && ln.length < 90) break;
    }
  }
  return out;
}
"""

JS_CMA = r"""
() => {
  const out = [];
  for (const box of document.querySelectorAll('.inner-cont-box')) {
    const a = box.querySelector('a[href*="detail/"]');
    if (!a) continue;
    const tx = (box.innerText||'').replace(/\s+/g,' ').trim();
    const m = tx.match(/(\d{2})\/(\d{2})\/(\d{4})/);
    if (!m) continue;
    out.push({ d: m[3]+m[2]+m[1],
               id: a.getAttribute('href').split('detail/')[1].split('?')[0],
               cat: tx.split(/\d{2}\/\d{2}\/\d{4}/)[0].trim(),
               t: (a.innerText||'').replace(/\s+/g,' ').trim() });
  }
  return out;
}
"""


def scrape(window_start, window_end):
    """window_* are (year, month). Returns (boursa_rows, companies, maqasa, cma)."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(locale='ar-KW', viewport={'width': 1400, 'height': 1000})
        page = ctx.new_page()

        # ---- Boursa ----
        log('→ بورصة الكويت')
        page.goto(BOURSA, wait_until='networkidle', timeout=90000)
        page.wait_for_selector('span.rbc-toolbar-label', timeout=60000)
        page.wait_for_timeout(3000)
        page.evaluate(JS_SETUP)
        companies = page.evaluate('() => window.__companies()')
        log('   شركات:', len(companies))

        def cur():
            lbl = page.evaluate('() => window.__label()').split()
            return MONTHS_AR.index(lbl[0]) + 1, int(re.sub(r'\D', '', lbl[1]))

        def idx(y, m):
            return y * 12 + (m - 1)

        m0, y0 = cur()
        steps = idx(window_start[0], window_start[1]) - idx(y0, m0)
        btn = 2 if steps > 0 else 1
        for _ in range(abs(steps)):
            page.evaluate(f'() => window.__nav({btn})')
            page.wait_for_timeout(450)
        page.wait_for_timeout(1500)

        total_months = idx(window_end[0], window_end[1]) - idx(window_start[0], window_start[1]) + 1
        rows, seen_labels = [], []
        for i in range(total_months):
            if i:
                page.evaluate('() => window.__nav(2)')
                page.wait_for_timeout(900)
            res = page.evaluate('() => window.__extract()')
            seen_labels.append(res['label'])
            for r in res['rows']:
                rows.append(r)
        log('   أشهر:', len(seen_labels), '| أحداث:', len(rows))
        if seen_labels[0].split()[0] != MONTHS_AR[window_start[1] - 1]:
            raise SystemExit('تنقّل الأشهر لم يصل إلى الشهر المطلوب: ' + seen_labels[0])

        # ---- Maqasa ----
        log('→ الشركة الكويتية للمقاصة')
        page.goto(MAQASA, wait_until='networkidle', timeout=90000)
        page.wait_for_timeout(2500)
        maqasa = page.evaluate(JS_MAQASA)
        log('   سجلات:', len(maqasa))

        # ---- CMA ----
        log('→ هيئة أسواق المال')
        cma = []
        for url, seg in ((CMA_R, 'resolutions-and-regulations'), (CMA_C, 'announcements-and-circulars')):
            page.goto(url, wait_until='networkidle', timeout=90000)
            page.wait_for_timeout(2500)
            for it in page.evaluate(JS_CMA):
                cma.append([it['d'], seg, it['id'], it['cat'], it['t']])
        log('   بنود:', len(cma))

        browser.close()
    return rows, companies, maqasa, cma


def fmt_pct(p):
    p = (p or '').strip().rstrip('%')
    try:
        v = float(p)
    except ValueError:
        return p
    return ('%.4f' % v).rstrip('0').rstrip('.') + '%'


def main():
    html = open(PAGE, encoding='utf-8').read()
    m = re.search(r'(<script id="seed-data"[^>]*>)(.*?)(</script>)', html, re.S)
    if not m:
        raise SystemExit('لم يُعثر على عنصر seed-data داخل index.html')
    old = json.loads(m.group(2))
    tickers = json.load(open(TICKERS, encoding='utf-8'))

    today = datetime.date.today()
    window_start = (today.year, 1)
    window_end = (today.year + 1, 12)
    raw, companies, maqasa_raw, cma = scrape(window_start, window_end)

    # ---- أنواع الأحداث: نحافظ على ترتيب القائمة القديمة لأن اللون مرتبط بالفهرس ----
    types = list(old['types'])
    fam = list(old['fam'])
    tindex = {t: i for i, t in enumerate(types)}

    def type_idx(title):
        disp = TYPE_LABEL.get(title, title)
        if disp not in tindex:
            tindex[disp] = len(types)
            types.append(disp)
            fam.append(guess_fam(disp))
        return tindex[disp]

    # ---- أحداث البورصة ----
    fresh = []
    for r in raw:
        txt = r['t']
        if ' - ' in txt:
            ticker, title = txt.split(' - ', 1)
        else:
            ticker, title = '', txt.lstrip('- ').strip()
        ticker, title = ticker.strip(), title.strip()
        if not title:
            continue
        stk = tickers.get(ticker, ['', ''])[0] if ticker else '0'
        fresh.append([r['d'], stk or '', ticker, type_idx(title), r['id'], ''])

    lo = '%04d%02d00' % window_start
    hi = '%04d%02d99' % window_end
    kept = [e for e in old['boursa'] if not (lo <= e[0] <= hi)]
    boursa = kept + fresh
    boursa.sort(key=lambda e: e[0])

    # ---- توزيعات المقاصة ----
    maqasa = []
    for name, _year, cdate, cpct, bdate, bpct in maqasa_raw:
        for dt, pct, kind in ((cdate, cpct, 'cash'), (bdate, bpct, 'bonus')):
            if not dt or dt == '-':
                continue
            dd, mm, yy = dt.split('/')
            maqasa.append([yy + mm + dd, name, kind, fmt_pct(pct)])

    # ---- أسماء الشركات ----
    comp = dict(old['companies'])
    comp.update(companies)
    comp['0'] = 'بورصة الكويت — إعلان عام للسوق'
    for code, nm in (('3001', 'صندوق بيتك كابيتال ريت'), ('614', 'شركة الرابطة'),
                     ('952', 'صندوق استثماري'), ('953', 'صندوق استثماري'), ('954', 'صندوق استثماري')):
        comp.setdefault(code, nm)

    # ---- بوابات التحقق: لا نكتب شيئًا إذا فشلت ----
    errs = []
    if len(fresh) < 1:
        errs.append('لم تُقرأ أي أحداث من البورصة')
    if len(boursa) < 1000:
        errs.append('إجمالي أحداث البورصة %d < 1000' % len(boursa))
    if any(not re.fullmatch(r'20\d{6}', e[0]) for e in boursa):
        errs.append('تاريخ غير صالح في أحداث البورصة')
    if any(len(e) != 6 for e in boursa):
        errs.append('صف بورصة بعدد حقول غير صحيح')
    if len(maqasa) < 50:
        errs.append('سجلات المقاصة %d < 50' % len(maqasa))
    if len(cma) < 5:
        errs.append('بنود الهيئة %d < 5' % len(cma))
    if len(fam) != len(types):
        errs.append('عدم تطابق عدد الأنواع مع عائلات الألوان')
    if max(e[3] for e in boursa) >= len(types):
        errs.append('فهرس نوع خارج النطاق')
    if errs:
        for e in errs:
            log('✗', e)
        raise SystemExit('فشل التحقق — لم تُعدَّل الصفحة')

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    payload = {
        'updated': now.replace(microsecond=0).isoformat(),
        'types': types, 'fam': fam, 'companies': comp,
        'boursa': boursa, 'maqasa': maqasa, 'cma': cma,
    }
    new_json = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    json.loads(new_json)  # تحقق أخير

    out = html[:m.start(2)] + new_json + html[m.end(2):]
    if '<script id="seed-data"' not in out or not out.rstrip().endswith('</html>'):
        raise SystemExit('الصفحة الناتجة غير سليمة — لم تُكتب')

    open(PAGE, 'w', encoding='utf-8').write(out)
    log('✓ تم التحديث:', payload['updated'])
    log('  بورصة %d (جديد %d) | مقاصة %d | هيئة %d | أنواع %d'
        % (len(boursa), len(fresh), len(maqasa), len(cma), len(types)))


if __name__ == '__main__':
    main()
