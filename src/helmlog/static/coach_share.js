// "Share with coach" dialog (#849): mint an expiring, no-account coach link
// for the race (or compare set) on screen. Admin-only — the server enforces
// that; the button is only rendered for admins.
//
// openCoachShareDialog({sessionIds, label, landingPath})

function _csEsc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function _csDefaultExpiry() {
  const d = new Date(Date.now() + 14 * 86400 * 1000);
  return d.toISOString().slice(0, 10);
}

function openCoachShareDialog(opts) {
  const sessionIds = [...new Set(opts.sessionIds || [])];
  let dlg = document.getElementById('coach-share-dialog');
  if (dlg) dlg.remove();
  dlg = document.createElement('dialog');
  dlg.id = 'coach-share-dialog';
  dlg.style.cssText = 'background:var(--bg-secondary);color:var(--text-primary);border:1px solid var(--border);'
    + 'border-radius:12px;padding:20px;max-width:440px;width:calc(100% - 32px)';
  const races = sessionIds.length === 1 ? 'this race' : sessionIds.length + ' races';
  dlg.innerHTML =
    '<div style="font-weight:700;margin-bottom:6px">Share with coach</div>'
    + '<div style="font-size:.82rem;color:var(--text-secondary);margin-bottom:12px">Creates a link that gives read-only access to '
    + races + ' — no account needed. Crew, audio, notes and sails are never shared.</div>'
    + '<label style="display:block;font-size:.8rem;color:var(--text-secondary);margin-bottom:4px">Label</label>'
    + '<input id="cs-label" type="text" value="' + _csEsc(opts.label || 'Coach link') + '" style="width:100%;box-sizing:border-box;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;padding:7px 9px;color:var(--text-primary);margin-bottom:10px">'
    + '<label style="display:flex;gap:8px;align-items:center;font-size:.88rem;margin-bottom:4px"><input id="cs-video" type="checkbox"> Include race video</label>'
    + '<div style="font-size:.74rem;color:var(--text-secondary);margin-bottom:10px">Video shows crew faces — only share it if your crew is OK with that.</div>'
    + '<label style="display:block;font-size:.8rem;color:var(--text-secondary);margin-bottom:4px">Link stops working after</label>'
    + '<input id="cs-expires" type="date" value="' + _csDefaultExpiry() + '" style="background:var(--bg-input);border:1px solid var(--border);border-radius:6px;padding:7px 9px;color:var(--text-primary);margin-bottom:12px">'
    + '<div id="cs-err" style="color:var(--danger);font-size:.82rem;margin-bottom:8px"></div>'
    + '<div id="cs-result" style="display:none;margin-bottom:10px"><input id="cs-url" type="text" readonly style="width:100%;box-sizing:border-box;font-family:monospace;font-size:.78rem;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;padding:7px 9px;color:var(--text-primary)">'
    + '<div style="font-size:.74rem;color:var(--text-secondary);margin-top:4px">Copy it now — it isn’t shown again. Revoke it any time under Admin → Coaches.</div></div>'
    + '<div style="display:flex;gap:8px;justify-content:flex-end">'
    + '<button id="cs-close" type="button" style="padding:7px 14px;border:1px solid var(--border);border-radius:6px;background:var(--bg-input);color:var(--text-primary);cursor:pointer">Close</button>'
    + '<button id="cs-create" type="button" style="padding:7px 14px;border:none;border-radius:6px;background:var(--accent-strong);color:var(--bg-primary);font-weight:700;cursor:pointer">Create link</button>'
    + '</div>';
  document.body.appendChild(dlg);
  dlg.querySelector('#cs-close').onclick = () => dlg.close();
  dlg.querySelector('#cs-create').onclick = async () => {
    const err = dlg.querySelector('#cs-err');
    err.textContent = '';
    const date = dlg.querySelector('#cs-expires').value;
    if (!date) { err.textContent = 'Choose when the link stops working.'; return; }
    const body = {
      kind: 'link',
      label: dlg.querySelector('#cs-label').value,
      session_ids: sessionIds,
      allow_video: dlg.querySelector('#cs-video').checked,
      landing_path: opts.landingPath || null,
      expires_at: new Date(date + 'T23:59:59').toISOString(),
    };
    const r = await fetch('/api/admin/coach-grants', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      err.textContent = typeof d.detail === 'string' ? d.detail : 'Could not create link (HTTP ' + r.status + ').';
      return;
    }
    const d = await r.json();
    const btn = dlg.querySelector('#cs-create');
    dlg.querySelector('#cs-result').style.display = '';
    dlg.querySelector('#cs-url').value = d.share_url;
    dlg.querySelector('#cs-url').select();
    btn.textContent = 'Copy link';
    btn.onclick = async () => {
      try { await navigator.clipboard.writeText(d.share_url); btn.textContent = 'Copied'; } catch (e) { dlg.querySelector('#cs-url').select(); }
    };
  };
  dlg.showModal();
}
