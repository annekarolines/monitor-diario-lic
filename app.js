/* Licitações de Comunicação — Frontend */

const DATA_URL = "data/licitacoes.json";

const CATEGORY_LABELS = {
  "Publicidade & Propaganda":    "Publicidade",
  "Marketing Digital":           "Marketing Digital",
  "Conteúdo & Redes Sociais":   "Conteúdo",
  "Identidade Visual & Criação": "Identidade Visual",
  "Comunicação Institucional":   "Institucional",
};

let allLicitacoes = [];
let activeFilter  = "all";
let activeDays    = 0;   // 0 = hoje, 7 = últimos 7 dias, 30 = todos
let searchQuery   = "";
let currentPage   = 1;
const PAGE_SIZE   = 12;

// --- Data loading ---

async function loadData() {
  try {
    const res = await fetch(`${DATA_URL}?_=${Date.now()}`);
    if (!res.ok) throw new Error("Arquivo não encontrado");
    const data = await res.json();
    // Garante ordenação mais nova → mais velha
    allLicitacoes = (data.licitacoes || []).sort((a, b) => {
      const dateDiff = (b.data_publicacao || "").localeCompare(a.data_publicacao || "");
      if (dateDiff !== 0) return dateDiff;
      return (b.relevance_score || 0) - (a.relevance_score || 0);
    });
    updateLastUpdated(data.last_updated);
    renderAll();
  } catch (err) {
    document.getElementById("emptyState").style.display = "flex";
    document.getElementById("statsBar").textContent = "";
  }
}

function updateLastUpdated(iso) {
  const el = document.getElementById("lastUpdated");
  if (!iso) return;
  const d = new Date(iso);
  const opts = { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" };
  el.textContent = `Atualizado em ${d.toLocaleDateString("pt-BR", opts)}`;
}

// --- Filtering & search ---

function dateThreshold() {
  if (activeDays === 30) return null;  // sem limite
  const d = new Date();
  if (activeDays === 0) {
    // Apenas a data mais recente disponível nos dados
    return allLicitacoes[0]?.data_publicacao || null;
  }
  d.setDate(d.getDate() - activeDays);
  return d.toISOString().slice(0, 10);
}

function getFiltered() {
  const threshold = dateThreshold();
  return allLicitacoes.filter(l => {
    const matchCat  = activeFilter === "all" || l.categoria === activeFilter;
    const matchDate = !threshold
      ? true
      : activeDays === 0
        ? l.data_publicacao === threshold
        : l.data_publicacao >= threshold;
    const q = searchQuery.toLowerCase();
    const matchSearch = !q ||
      l.orgao?.toLowerCase().includes(q) ||
      l.objeto?.toLowerCase().includes(q) ||
      l.ambito?.toLowerCase().includes(q) ||
      l.categoria?.toLowerCase().includes(q) ||
      l.justificativa?.toLowerCase().includes(q) ||
      l.modalidade?.toLowerCase().includes(q);
    return matchCat && matchDate && matchSearch;
  });
}

// --- Rendering ---

function renderAll() {
  const filtered  = getFiltered();
  const grid      = document.getElementById("licitacoesGrid");
  const emptyState = document.getElementById("emptyState");
  const statsBar  = document.getElementById("statsBar");

  grid.innerHTML = "";

  if (!allLicitacoes.length) {
    emptyState.style.display = "flex";
    statsBar.textContent = "";
    renderPagination(0, 0);
    return;
  }

  emptyState.style.display = "none";

  const total      = allLicitacoes.length;
  const showing    = filtered.length;
  const catLabel   = activeFilter === "all" ? "todas as categorias" : (CATEGORY_LABELS[activeFilter] || activeFilter);
  const totalPages = Math.ceil(showing / PAGE_SIZE);
  if (currentPage > totalPages) currentPage = 1;

  const dateLabel = activeDays === 0 ? "hoje" : activeDays === 7 ? "últimos 7 dias" : "últimos 30 dias";
  statsBar.textContent = showing === total
    ? `${total} licitações · ${dateLabel}`
    : `${showing} de ${total} licitações · ${catLabel} · ${dateLabel}`;

  if (!filtered.length) {
    const msg = document.createElement("div");
    msg.className = "empty-state";
    msg.style.cssText = "display:flex;padding:40px 0";
    msg.innerHTML = `<p style="color:var(--text-muted);font-size:.875rem">Nenhuma licitação encontrada para "<strong>${escapeHtml(searchQuery)}</strong>"</p>`;
    grid.appendChild(msg);
    renderPagination(0, 0);
    return;
  }

  const start     = (currentPage - 1) * PAGE_SIZE;
  const paginated = filtered.slice(start, start + PAGE_SIZE);
  const template  = document.getElementById("cardTemplate");
  paginated.forEach(l => grid.appendChild(buildCard(template, l)));
  renderPagination(currentPage, totalPages);
}

function renderPagination(page, total) {
  const bar = document.getElementById("paginationBar");
  bar.innerHTML = "";
  if (total <= 1) return;

  const prev = document.createElement("button");
  prev.className = "page-btn" + (page <= 1 ? " disabled" : "");
  prev.disabled  = page <= 1;
  prev.innerHTML = "← anterior";
  prev.addEventListener("click", () => {
    currentPage--;
    renderAll();
    window.scrollTo({ top: document.getElementById("licitacoesSection").offsetTop - 120, behavior: "smooth" });
  });

  const info = document.createElement("span");
  info.className   = "page-info";
  info.textContent = `${page} / ${total}`;

  const next = document.createElement("button");
  next.className = "page-btn" + (page >= total ? " disabled" : "");
  next.disabled  = page >= total;
  next.innerHTML = "próximo →";
  next.addEventListener("click", () => {
    currentPage++;
    renderAll();
    window.scrollTo({ top: document.getElementById("licitacoesSection").offsetTop - 120, behavior: "smooth" });
  });

  bar.appendChild(prev);
  bar.appendChild(info);
  bar.appendChild(next);
}

function buildCard(template, l) {
  const clone = template.content.cloneNode(true);
  const card  = clone.querySelector(".card");

  card.dataset.cat = l.categoria || "";

  // Top row
  card.querySelector(".card-badge").textContent = CATEGORY_LABELS[l.categoria] || l.categoria || "—";
  card.querySelector(".card-date").textContent  = formatDate(l.data_publicacao);

  const scoreEl = card.querySelector(".card-score");
  const score   = l.relevance_score || 0;
  scoreEl.textContent = `${score}/10`;
  if (score >= 8)      scoreEl.classList.add("high");
  else if (score >= 6) scoreEl.classList.add("med");

  // Título e objeto
  card.querySelector(".card-title").textContent   = l.orgao   || "Órgão não informado";
  card.querySelector(".card-summary").textContent = l.objeto  || "";

  // Meta: âmbito + modalidade
  card.querySelector(".card-ambito").textContent    = l.ambito    || "—";
  card.querySelector(".card-modalidade").textContent = l.modalidade || "—";

  // Financeiro
  card.querySelector(".card-valor").textContent = formatValor(l.valor_estimado);
  card.querySelector(".card-prazo").textContent = l.prazo_proposta
    ? `Prazo: ${formatDate(l.prazo_proposta)}`
    : "Prazo: a confirmar";

  // Justificativa (accordion)
  card.querySelector(".card-justificativa").textContent = l.justificativa || "";
  const toggle = card.querySelector(".insights-toggle");
  const body   = card.querySelector(".insights-body");
  toggle.addEventListener("click", () => {
    const expanded = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", String(!expanded));
    body.hidden = expanded;
  });

  // Link
  card.querySelector(".card-link").href = l.fonte_url || "#";

  return clone;
}

// --- Event listeners ---

document.getElementById("filterBar").addEventListener("click", e => {
  const pill = e.target.closest(".filter-pill");
  if (!pill) return;
  document.querySelectorAll("#filterBar .filter-pill").forEach(p => p.classList.remove("active"));
  pill.classList.add("active");
  activeFilter = pill.dataset.cat;
  currentPage  = 1;
  renderAll();
});

document.getElementById("dateFilterBar").addEventListener("click", e => {
  const pill = e.target.closest(".date-pill");
  if (!pill) return;
  document.querySelectorAll(".date-pill").forEach(p => p.classList.remove("active"));
  pill.classList.add("active");
  activeDays  = parseInt(pill.dataset.days, 10);
  currentPage = 1;
  renderAll();
});

let searchTimeout;
document.getElementById("searchInput").addEventListener("input", e => {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(() => {
    searchQuery = e.target.value.trim();
    currentPage = 1;
    renderAll();
  }, 250);
});

// --- Utils ---

function formatDate(dateStr) {
  if (!dateStr) return "—";
  try {
    const d = new Date(dateStr + "T12:00:00");
    return d.toLocaleDateString("pt-BR", { day: "2-digit", month: "short", year: "numeric" });
  } catch {
    return dateStr;
  }
}

function formatValor(valor) {
  if (valor === null || valor === undefined) return "Valor não informado";
  try {
    const n = parseFloat(valor);
    return "R$ " + n.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  } catch {
    return "Valor não informado";
  }
}

function escapeHtml(str) {
  return str.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --- Init ---
loadData();
