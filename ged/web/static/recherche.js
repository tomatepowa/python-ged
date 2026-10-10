// Recherche instantanée : chaque frappe lance immédiatement la recherche et les suggestions.
// La requête précédente encore en cours est annulée, et les réponses déjà reçues sont gardées
// en mémoire (retour arrière instantané). Sans JavaScript, le formulaire fonctionne normalement.
(() => {
  const formulaire = document.getElementById('recherche');
  const zone = document.getElementById('resultats');
  const champ = formulaire.elements.q;
  const liste = document.getElementById('suggestions');

  // --- Cache borné des réponses --------------------------------------------------------------

  function cache(taille) {
    const m = new Map();
    return {
      lire: (cle) => m.get(cle),
      ecrire: (cle, valeur) => {
        m.set(cle, valeur);
        if (m.size > taille) m.delete(m.keys().next().value);
      },
    };
  }
  const cacheResultats = cache(150);
  const cacheSuggestions = cache(300);

  // --- Résultats -------------------------------------------------------------------------------

  let requeteResultats = null;

  function parametres() {
    const p = new URLSearchParams(new FormData(formulaire));
    for (const [cle, valeur] of [...p.entries()]) if (!valeur) p.delete(cle);
    return p;
  }

  function afficherResultats(html, p, defiler) {
    zone.innerHTML = html;
    zone.classList.remove('chargement');
    const qs = p.toString();
    history.replaceState(null, '', qs ? '/?' + qs : '/');
    document.title = (p.get('q') ? p.get('q') + ' – ' : '') + 'GED DSI';
    if (defiler) zone.scrollIntoView({ behavior: 'smooth' });
  }

  async function chercher(p = parametres(), defiler = false) {
    const cle = p.toString();
    if (requeteResultats) requeteResultats.abort();
    const dejaVu = cacheResultats.lire(cle);
    if (dejaVu !== undefined) return afficherResultats(dejaVu, p, defiler);

    requeteResultats = new AbortController();
    zone.classList.add('chargement');
    try {
      const reponse = await fetch('/resultats?' + cle, { signal: requeteResultats.signal });
      if (!reponse.ok) throw new Error(String(reponse.status));
      const html = await reponse.text();
      cacheResultats.ecrire(cle, html);
      afficherResultats(html, p, defiler);
    } catch (e) {
      if (e.name !== 'AbortError') formulaire.submit();
    }
  }

  // --- Suggestions -----------------------------------------------------------------------------

  let requeteSuggestions = null;
  let elements = [];
  let actif = -1;

  const sansAccents = (t) => t.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

  function element(balise, classe, texte) {
    const e = document.createElement(balise);
    if (classe) e.className = classe;
    if (texte !== undefined) e.textContent = texte;
    return e;
  }

  function fermerSuggestions() {
    liste.hidden = true;
    champ.setAttribute('aria-expanded', 'false');
    actif = -1;
  }

  function selectionner(index) {
    actif = index;
    elements.forEach((el, i) => el.noeud.setAttribute('aria-selected', String(i === actif)));
    if (actif >= 0) elements[actif].noeud.scrollIntoView({ block: 'nearest' });
  }

  function choisir(choix) {
    fermerSuggestions();
    if (choix.type === 'document') {
      window.location = '/document/' + choix.id;
      return;
    }
    champ.value = choix.texte + ' ';
    champ.focus();
    chercher();
  }

  function afficherSuggestions(q, donnees) {
    liste.replaceChildren();
    elements = [];

    // Complétions : la partie déjà tapée en normal, le complément en gras (comme Google)
    const tape = sansAccents(q.trim());
    for (const texte of donnees.completions) {
      const noeud = element('div', 'suggestion completion');
      const longueur = texte.startsWith(tape) ? tape.length : 0;
      noeud.append(element('span', 'icone', '⌕'), element('span', 'texte'));
      noeud.lastChild.append(texte.slice(0, longueur), element('strong', '', texte.slice(longueur)));
      elements.push({ type: 'completion', texte, noeud });
    }

    if (donnees.documents.length) {
      elements.push(null); // séparateur, retiré plus bas
      for (const d of donnees.documents) {
        const noeud = element('div', 'suggestion document');
        noeud.append(element('span', 'badge', d.famille), element('span', 'nom', d.nom), element('span', 'chemin', d.chemin_relatif));
        elements.push({ type: 'document', id: d.id, noeud });
      }
    }

    for (const e of elements) {
      if (e === null) {
        liste.append(element('div', 'suggestions-titre', 'Documents'));
        continue;
      }
      e.noeud.setAttribute('role', 'option');
      // mousedown plutôt que click : se déclenche avant que le champ perde le focus
      e.noeud.addEventListener('mousedown', (ev) => { ev.preventDefault(); choisir(e); });
      liste.append(e.noeud);
    }
    elements = elements.filter((e) => e !== null);

    actif = -1;
    liste.hidden = elements.length === 0;
    champ.setAttribute('aria-expanded', String(!liste.hidden));
  }

  async function suggerer() {
    const q = champ.value;
    if (requeteSuggestions) requeteSuggestions.abort();
    if (!q.trim()) return fermerSuggestions();

    const dejaVu = cacheSuggestions.lire(q);
    if (dejaVu) return afficherSuggestions(q, dejaVu);

    requeteSuggestions = new AbortController();
    try {
      const reponse = await fetch('/suggestions?q=' + encodeURIComponent(q), { signal: requeteSuggestions.signal });
      const donnees = await reponse.json();
      cacheSuggestions.ecrire(q, donnees);
      if (champ.value === q && document.activeElement === champ) afficherSuggestions(q, donnees);
    } catch (e) {
      // requête annulée par une frappe plus récente
    }
  }

  // --- Événements ------------------------------------------------------------------------------

  champ.addEventListener('input', () => {
    suggerer();
    // Une seule lettre : seulement les suggestions (une recherche sur « p » ne sert à rien
    // et compte parmi les plus coûteuses : elle correspond à presque tous les documents)
    if (champ.value.trim().length !== 1) chercher();
  });
  champ.addEventListener('focus', () => { if (champ.value.trim()) suggerer(); });
  champ.addEventListener('blur', fermerSuggestions);
  champ.addEventListener('keydown', (e) => {
    if (liste.hidden || !elements.length) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      selectionner((actif + 1) % elements.length);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      selectionner(actif <= 0 ? elements.length - 1 : actif - 1);
    } else if (e.key === 'Enter' && actif >= 0) {
      e.preventDefault();
      choisir(elements[actif]);
    } else if (e.key === 'Escape') {
      fermerSuggestions();
    }
  });

  formulaire.addEventListener('change', (e) => { if (e.target !== champ) chercher(); });
  formulaire.addEventListener('submit', (e) => {
    e.preventDefault();
    fermerSuggestions();
    chercher();
  });

  zone.addEventListener('click', (e) => {
    const lien = e.target.closest('a[data-page]');
    if (!lien) return;
    e.preventDefault();
    chercher(new URLSearchParams(lien.search), true);
  });

  document.getElementById('effacer-filtres').addEventListener('click', () => {
    formulaire.querySelectorAll('select').forEach((s) => { s.value = ''; });
    formulaire.querySelectorAll('input[type=checkbox]').forEach((c) => { c.checked = false; });
    chercher();
  });

  // Curseur en fin de texte quand on revient sur une recherche existante
  champ.setSelectionRange(champ.value.length, champ.value.length);
})();
