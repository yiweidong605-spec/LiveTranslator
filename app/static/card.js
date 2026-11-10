// Attribute card: shared by the live page and the review page
const LANG_NAME = {zh:'Chinese', fr:'French', en:'English'};
const SOURCE_NAME = {mic:'Microphone', system:'System audio', text:'Typed'};
const MODE_NAME = {auto:'Conversation', listen:'Listen', speak:'Speak', text:'Typed'};

function esc(s){ return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function pct(v){ return v == null ? '—' : Math.round(v*100) + '%'; }
function confClass(v){ return v == null ? 'na' : v >= 0.8 ? 'hi' : v >= 0.6 ? 'mid' : 'lo'; }
function confBadge(v, title){
  return `<span class="badge ${confClass(v)}" title="${esc(title||'Sentence confidence — click for the attribute card')}">${v==null?'?':pct(v)}</span>`;
}

// Underline words the recogniser was unsure about
function highlightWords(text, words){
  if (!words || !words.length) return esc(text);
  const weak = words.filter(w => w.p < 0.6);
  if (!weak.length) return esc(text);
  let html = esc(text);
  for (const w of weak){
    const t = esc(w.w.trim()); if (!t) continue;
    html = html.replace(t, `<mark class="weakw" title="Heard with ${pct(w.p)} certainty">${t}</mark>`);
  }
  return html;
}

function tokenList(list, kind){
  if (!list || !list.length) return '<span class="muted">Nothing notably uncertain</span>';
  return list.map(t => {
    const tok = esc((kind==='asr' ? t.w : t.token).trim());
    const alts = (t.alts||[]).filter(a => a.token.trim()).map(a => `${esc(a.token.trim())} ${pct(a.p)}`).join(', ');
    return `<span class="tok ${confClass(t.p)}">${tok} <b>${pct(t.p)}</b>${alts ? ` <i>alternatives: ${alts}</i>` : ''}</span>`;
  }).join(' ');
}

function attrCardHTML(c, {editable=false} = {}){
  const a = c.asr || null, m = c.mt || {};
  const e = c.edits || {};
  const rows = [];
  const row = (k, v) => rows.push(`<div class="ak">${k}</div><div class="av">${v}</div>`);
  row('Confidence', `${confBadge(c.confidence)} <span class="muted">${a ? `= heard ${pct(a.confidence)} × translated ${pct(m.confidence)}` : `= translated ${pct(m.confidence)} (typed input, no speech recognition)`}</span>`);
  if (a) row('Possibly misheard', tokenList(a.weak_words, 'asr'));
  row('Hesitant tokens', tokenList(m.weak_tokens, 'mt'));
  row('Time', `${esc(c.date||'')} ${esc(c.time||'')}`);
  row('Direction', `${LANG_NAME[c.src_lang]||c.src_lang||''} → ${LANG_NAME[c.tgt_lang]||c.tgt_lang||''} · ${c.who==='me'?'said by me':'said by them'}`);
  row('Source', `${SOURCE_NAME[c.source]||c.source||''} · ${MODE_NAME[c.mode]||c.mode||''}${c.spoken?' · spoken aloud':''}`);
  if (a) row('Recognition', `${esc(a.model)} · audio ${a.dur_s}s · took ${(a.ms/1000).toFixed(2)}s`);
  if (m.model) row('Translation', `${esc(m.model)} · first token ${(+m.first_token_s).toFixed(2)}s · total ${(+m.total_s).toFixed(2)}s · context ${m.context_turns ?? 0} turns`);
  if (c.audio) row('Audio', `<audio controls preload="none" src="/api/log/audio?path=${encodeURIComponent(c.audio)}"></audio>`);
  if (c.error) row('Error', `<span class="lo-t">${esc(c.error)}</span>`);
  if (editable){
    row('Correction', `<textarea class="edit" data-field="correction" placeholder="If the translation is wrong, write the right one (it will go into the training set)">${esc(e.correction||'')}</textarea>`);
    row('Note', `<textarea class="edit" data-field="note" placeholder="e.g. Paul's catchphrase / an in-joke meaning…">${esc(e.note||'')}</textarea>`);
  }
  return `<div class="attr"><div class="agrid">${rows.join('')}</div></div>`;
}
