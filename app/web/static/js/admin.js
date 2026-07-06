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
    tabBtns.forEach(btn => btn.addEventListener('click', () => {
      tabBtns.forEach(b => b.classList.toggle('active', b === btn));
      document.querySelectorAll('.tab-pane').forEach(p => {
        p.style.display = (p.id === btn.dataset.tab) ? '' : 'none';
      });
    }));
  }

  // --- confirm destructive forms -------------------------------------------
  document.querySelectorAll('form[data-confirm]').forEach(f => {
    f.addEventListener('submit', e => { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
  });

  // --- modal helpers -------------------------------------------------------
  function openModal(id) { const m = document.getElementById(id); if (m) m.classList.add('open'); }
  function closeModal() { document.querySelectorAll('.modal-bg.open').forEach(m => m.classList.remove('open')); }
  document.querySelectorAll('[data-close-modal]').forEach(b => b.addEventListener('click', closeModal));
  document.querySelectorAll('.modal-bg').forEach(bd =>
    bd.addEventListener('click', e => { if (e.target === bd) closeModal(); }));
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

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
