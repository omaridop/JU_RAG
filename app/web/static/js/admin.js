/* University of Jordan — admin panel helpers. */
(function () {
  "use strict";

  function pad(n) { return String(n).padStart(2, '0'); }
  function toLocalDatetime(d) {
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
           'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function escapeHtml(s) {
    const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML;
  }
  const LANG = (document.documentElement.lang === 'en') ? 'en' : 'ar';
  document.querySelectorAll('.rail a.active').forEach(a => a.setAttribute('aria-current', 'page'));
  document.querySelectorAll('[data-set-lang]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.setLang === LANG)));
  // Keep editable pricing columns named for assistive technology.
  document.querySelectorAll('#priceTable tbody tr').forEach((row, index) => {
    row.querySelectorAll('input').forEach(input => {
      const labels = LANG === 'ar' ? {model: 'النموذج', input: 'سعر الإدخال', output: 'سعر الإخراج'}
        : {model: 'Model', input: 'Input price', output: 'Output price'};
      input.setAttribute('aria-label', labels[input.name] + ' · ' + (index + 1));
    });
  });

  // --- language toggle (AR / EN) -> set cookie + reload --------------------
  document.querySelectorAll('[data-set-lang]').forEach(btn => {
    btn.addEventListener('click', () => {
      const next = btn.dataset.setLang;
      document.cookie = 'ju_admin_lang=' + next + ';path=/;max-age=31536000;samesite=lax';
      location.reload();
    });
  });

  // --- default any document-date inputs to "now" ---------------------------
  document.querySelectorAll('input[name="doc_date"]').forEach(dd => {
    if (!dd.value) dd.value = toLocalDatetime(new Date());
  });

  // --- "add to knowledge base" tabs (upload file / type text) --------------
  const tabBtns = document.querySelectorAll('#addTabs .tab-btn');
  if (tabBtns.length) {
    document.getElementById('addTabs').setAttribute('role', 'tablist');
    function selectTab(btn) {
      tabBtns.forEach(b => {
        b.classList.toggle('active', b === btn);
        b.setAttribute('aria-selected', String(b === btn));
        b.tabIndex = b === btn ? 0 : -1;
      });
      document.querySelectorAll('.tab-pane').forEach(p => {
        p.style.display = (p.id === btn.dataset.tab) ? '' : 'none';
      });
    }
    tabBtns.forEach((btn, i) => {
      btn.id = 'add-tab-' + i;
      btn.setAttribute('role', 'tab');
      btn.setAttribute('aria-controls', btn.dataset.tab);
      const pane = document.getElementById(btn.dataset.tab);
      pane.setAttribute('role', 'tabpanel');
      pane.setAttribute('aria-labelledby', btn.id);
      btn.addEventListener('click', () => selectTab(btn));
      btn.addEventListener('keydown', e => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
        e.preventDefault();
        let step = e.key === 'ArrowRight' ? 1 : -1;
        if (document.documentElement.dir === 'rtl') step *= -1;
        const next = e.key === 'Home' ? 0 : e.key === 'End' ? tabBtns.length - 1 : (i + step + tabBtns.length) % tabBtns.length;
        selectTab(tabBtns[next]); tabBtns[next].focus();
      });
    });
    selectTab([...tabBtns].find(b => b.classList.contains('active')) || tabBtns[0]);
  }

  // --- confirm destructive forms -------------------------------------------
  document.querySelectorAll('form[data-confirm]').forEach(f => {
    f.addEventListener('submit', e => { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
  });

  // --- modal helpers -------------------------------------------------------
  let modalTrigger = null;
  const focusable = 'button:not([disabled]), a[href], input:not([disabled]):not([type="hidden"]), textarea:not([disabled]), select:not([disabled]), [tabindex="0"]';
  function openModal(id) {
    const m = document.getElementById(id); if (!m) return;
    modalTrigger = document.activeElement;
    const dialog = m.querySelector('.modal');
    dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.tabIndex = -1;
    const heading = dialog.querySelector('h3');
    if (heading) { heading.id = id + '-title'; dialog.setAttribute('aria-labelledby', heading.id); }
    m.classList.add('open'); document.body.classList.add('modal-open');
    const first = [...dialog.querySelectorAll(focusable)].find(el => el.getClientRects().length);
    (first || dialog).focus();
  }
  function closeModal() {
    const opened = document.querySelector('.modal-bg.open'); if (!opened) return;
    opened.classList.remove('open'); document.body.classList.remove('modal-open');
    if (modalTrigger && modalTrigger.isConnected) modalTrigger.focus();
  }
  document.querySelectorAll('[data-close-modal]').forEach(b => b.addEventListener('click', closeModal));
  document.querySelectorAll('.modal-bg').forEach(bd =>
    bd.addEventListener('click', e => { if (e.target === bd) closeModal(); }));
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeModal();
    if (e.key !== 'Tab') return;
    const modal = document.querySelector('.modal-bg.open .modal'); if (!modal) return;
    const items = [...modal.querySelectorAll(focusable)].filter(el => el.getClientRects().length);
    if (!items.length) { e.preventDefault(); modal.focus(); return; }
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === modal)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  // --- chat-log view modal -------------------------------------------------
  document.querySelectorAll('.view-log').forEach(btn => {
    btn.addEventListener('click', () => {
      document.getElementById('mQ').textContent = btn.dataset.q || '';
      document.getElementById('mA').textContent = btn.dataset.a || '';
      const pf = document.getElementById('mPromoteForm');
      pf.action = '/admin/logs/' + btn.dataset.id + '/promote';
      const served = btn.dataset.served;
      const promoted = btn.dataset.promoted === '1' || btn.dataset.promoted === 'True';
      pf.style.display = ((served === 'rag' || served === 'memory') && !promoted) ? 'inline' : 'none';
      const sd = document.getElementById('mSources'); sd.innerHTML = '';
      let sources = []; try { sources = JSON.parse(btn.dataset.sources || '[]'); } catch (e) {}
      if (!sources.length) { sd.innerHTML = '<span class="muted">—</span>'; }
      sources.forEach(s => {
        const c = document.createElement('span'); c.className = 'source-chip';
        c.textContent = (s.label || 'KB') + (s.date ? (' · ' + s.date) : '');
        sd.appendChild(c);
      });
      openModal('logModal');
    });
  });

  // --- memory edit modal ---------------------------------------------------
  document.querySelectorAll('.edit-mem').forEach(btn => {
    btn.addEventListener('click', () => {
      document.getElementById('memQ').value = btn.dataset.q || '';
      document.getElementById('memA').value = btn.dataset.a || '';
      document.getElementById('memEditForm').action = '/admin/memory/' + btn.dataset.id + '/edit';
      openModal('memModal');
    });
  });

  // --- live document status polling ----------------------------------------
  const PROCESSING = ['pending', 'processing', 'extracting', 'chunking', 'embedding', 'storing', 'loading', 'reading'];
  const STATUS_LABELS = {
    pending:    { ar: 'بالانتظار', en: 'Pending' },
    processing: { ar: 'قيد المعالجة', en: 'Processing' },
    reading:    { ar: 'قيد القراءة', en: 'Reading' },
    extracting: { ar: 'قيد الاستخراج', en: 'Extracting' },
    chunking:   { ar: 'قيد التقطيع', en: 'Chunking' },
    loading:    { ar: 'قيد التحميل', en: 'Loading' },
    embedding:  { ar: 'قيد التضمين', en: 'Embedding' },
    storing:    { ar: 'قيد التخزين', en: 'Storing' },
    done:       { ar: 'مكتمل', en: 'Done' },
    error:      { ar: 'خطأ', en: 'Error' },
    skipped:    { ar: 'تم تخطيه', en: 'Skipped' }
  };
  function pillHtml(status, error, progress) {
    var label = (STATUS_LABELS[status] && STATUS_LABELS[status][LANG]) || status;
    var h = '<span class="pill ' + status + '"><span class="dot"></span>' + label + '</span>';
    if (status === 'error' && error) {
      h += '<div class="muted" style="font-size:.74rem;margin-top:4px">' + escapeHtml(error) + '</div>';
    } else if (progress && PROCESSING.indexOf(status) >= 0) {
      h += '<div class="muted" style="font-size:.74rem;margin-top:4px" dir="ltr">' + escapeHtml(progress) + '</div>';
    }
    return h;
  }
  const table = document.getElementById('docsTable');
  const docSearch = document.getElementById('doc-search');
  if (table && docSearch) {
    const rows = [...table.querySelectorAll('tbody tr[data-id]')];
    docSearch.addEventListener('input', () => {
      const query = docSearch.value.trim().toLocaleLowerCase();
      let visible = 0;
      rows.forEach(row => {
        row.hidden = !row.textContent.toLocaleLowerCase().includes(query);
        if (!row.hidden) visible++;
      });
      document.getElementById('doc-count').textContent = LANG === 'ar'
        ? visible + ' من ' + rows.length + ' مستند'
        : visible + ' of ' + rows.length + ' documents';
    });
  }
  function anyProcessingInDom() {
    return PROCESSING.some(s => document.querySelector('#docsTable .pill.' + s));
  }
  function pollDocs() {
    fetch('/admin/documents/status').then(r => r.json()).then(list => {
      let again = false;
      list.forEach(d => {
        const row = document.querySelector('tr[data-id="' + d.id + '"]');
        if (!row) return;
        const cell = row.querySelector('.status-cell');
        const cnt = row.querySelector('.count-cell');
        if (cell) cell.innerHTML = pillHtml(d.status, d.error, d.progress);
        if (cnt) cnt.textContent = d.chunk_count;
        if (PROCESSING.indexOf(d.status) >= 0) again = true;
      });
      if (again) setTimeout(pollDocs, 3000);
    }).catch(() => {});
  }
  if (table && anyProcessingInDom()) setTimeout(pollDocs, 2500);
})();
