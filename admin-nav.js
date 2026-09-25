/* ============================================================
   Menu de l'administration — rendu à partir d'une seule liste.
   Ajouter une page ici la fait apparaître partout, du même coup.
   ============================================================ */
(function () {
  const FAMILLES = [
    { cle: 'planning',  emoji: '📅', nom: 'Planning',  pages: [
      { f: 'admin-calendar.html',        emoji: '📅', nom: 'Calendrier' },
      { f: 'admin-fermetures.html',      emoji: '🚫', nom: 'Fermetures' },
    ]},
    { cle: 'ventes',    emoji: '📋', nom: 'Ventes',    pages: [
      { f: 'admin-devis-dashboard.html', emoji: '📋', nom: 'Devis' },
      { f: 'admin-documents.html',       emoji: '📄', nom: 'Documents' },
      { f: 'admin-impressions.html',     emoji: '🖨️', nom: 'Impressions' },
      { f: 'admin-exports.html',         emoji: '📤', nom: 'Exports' },
    ]},
    { cle: 'clients',   emoji: '👤', nom: 'Clients',   pages: [
      { f: 'admin-clients.html',         emoji: '👤', nom: 'Clients' },
      { f: 'admin-societes.html',        emoji: '🏢', nom: 'Sociétés' },
      { f: 'admin-prospection.html',     emoji: '🎯', nom: 'Prospection' },
    ]},
    { cle: 'offres',    emoji: '💶', nom: 'Offres',    pages: [
      { f: 'admin-tarifs.html',          emoji: '💶', nom: 'Tarifs' },
      { f: 'admin-forfaits.html',        emoji: '🎟️', nom: 'Forfaits' },
    ]},
    { cle: 'animation', emoji: '🎉', nom: 'Animation', pages: [
      { f: 'admin-evenements.html',      emoji: '🎉', nom: 'Événements' },
      { f: 'admin-communication.html',   emoji: '📣', nom: 'Communication' },
      { f: 'admin-projets.html',         emoji: '🗂️', nom: 'Projets' },
    ]},
    { cle: 'iad',       emoji: '🤝', nom: 'iad',       pages: [
      { f: 'admin-iad.html',             emoji: '🤝', nom: 'Conseillers iad' },
    ]},
    { cle: 'stats',     emoji: '📊', nom: 'Statistiques', pages: [
      { f: 'admin-stats.html',           emoji: '📊', nom: 'Statistiques' },
    ]},
  ];

  function pageCourante() {
    const p = (location.pathname.split('/').pop() || '').toLowerCase();
    if (!p) return 'admin-calendar.html';
    return p.endsWith('.html') ? p : p + '.html';
  }

  function rendre() {
    const cible = document.getElementById('acw-nav');
    if (!cible) return;
    const ici = pageCourante();
    let famille = FAMILLES.find(g => g.pages.some(x => x.f === ici)) || FAMILLES[0];

    const r1 = document.createElement('div');
    r1.className = 'acw-nav-familles';
    FAMILLES.forEach(g => {
      const a = document.createElement('a');
      a.href = g.pages[0].f;
      a.className = (g === famille) ? 'actif' : '';
      a.innerHTML = `<span class="emo">${g.emoji}</span>${g.nom}`;
      a.title = g.pages.map(x => x.nom).join(' · ');
      r1.appendChild(a);
    });

    const r2 = document.createElement('div');
    r2.className = 'acw-nav-pages';
    famille.pages.forEach(x => {
      const a = document.createElement('a');
      a.href = x.f;
      a.className = (x.f === ici) ? 'actif' : '';
      a.textContent = x.emoji + ' ' + x.nom;
      r2.appendChild(a);
    });

    cible.className = 'acw-nav';
    cible.replaceChildren(r1);
    // Une seule page dans la famille : la deuxième rangée n'apprend rien.
    if (famille.pages.length > 1) cible.appendChild(r2);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', rendre);
  } else {
    rendre();
  }
})();
